"""
Admin Bot Forwarder Engine for ProAffiliaters Leads.

Uses Telegram MTProto user client (Telethon) to forward promotional posts
from the official channel (@pro_affiliaters) directly to target deal channel admin bots.
Retains all banner graphics, formatted caption, and clickable inline buttons.
"""

import asyncio
import logging
import random
from datetime import datetime
from typing import Optional, Dict, Any, List
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UserIsBlockedError,
    BotMethodInvalidError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
    ChannelPrivateError,
    ChatWriteForbiddenError
)

from src.config import settings
from src.db import Database
from src.channel_publisher import get_promo_post_preview

logger = logging.getLogger("BotForwarder")


class BotForwarderEngine:
    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self.is_running: bool = False
        self.is_paused: bool = False
        self.active_campaign: Optional[Dict[str, Any]] = None
        self._worker_task: Optional[asyncio.Task] = None

    def set_telethon_client(self, client: TelegramClient):
        """Sets or shares the active Telethon MTProto client."""
        self.client = client

    def get_status(self) -> Dict[str, Any]:
        """Returns current engine and active campaign status."""
        is_client_connected = bool(self.client and self.client.is_connected())
        
        status_data = {
            "engine_state": "running" if self.is_running else ("paused" if self.is_paused else "idle"),
            "telethon_connected": is_client_connected,
            "session_name": settings.telegram_session_name,
            "has_active_campaign": self.active_campaign is not None,
            "campaign": self.active_campaign or {}
        }
        return status_data

    async def forward_to_single_bot(
        self,
        bot_username: str,
        channel_message_id: int,
        channel_username: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Forwards a single promotional channel post to a specific bot username.
        Used for dry-runs and single targeted outreach.
        """
        clean_bot = bot_username.strip().lstrip("@")
        clean_channel = (channel_username or settings.promo_channel_username).strip()

        if not self.client or not self.client.is_connected():
            return {
                "success": False,
                "bot": clean_bot,
                "error": "Telethon client not connected or unauthorized. Complete MTProto authorization first."
            }

        try:
            target_entity = await self.client.get_entity(clean_bot)
            channel_entity = await self.client.get_entity(clean_channel)

            logger.info("Forwarding msg #%d from %s to @%s...", channel_message_id, clean_channel, clean_bot)

            forwarded = await self.client.forward_messages(
                entity=target_entity,
                messages=channel_message_id,
                from_peer=channel_entity
            )

            # Mark in DB
            await Database.mark_lead_contacted(clean_bot, contact_method="channel_forward")

            return {
                "success": True,
                "bot": clean_bot,
                "message_id": getattr(forwarded, "id", None) or channel_message_id,
                "timestamp": datetime.utcnow().isoformat()
            }

        except FloodWaitError as e:
            logger.warning("Telegram FloodWait error on @%s: wait %ds", clean_bot, e.seconds)
            return {"success": False, "bot": clean_bot, "error": f"FloodWaitError: wait {e.seconds}s", "flood_wait": e.seconds}
        except (UsernameNotOccupiedError, UsernameInvalidError):
            logger.warning("Invalid bot username: @%s", clean_bot)
            return {"success": False, "bot": clean_bot, "error": "Bot username does not exist"}
        except UserIsBlockedError:
            logger.warning("User is blocked by bot: @%s", clean_bot)
            return {"success": False, "bot": clean_bot, "error": "Bot has blocked user"}
        except ChatWriteForbiddenError:
            logger.warning("Cannot write to bot: @%s", clean_bot)
            return {"success": False, "bot": clean_bot, "error": "Write forbidden"}
        except Exception as e:
            logger.exception("Error forwarding to @%s: %s", clean_bot, e)
            return {"success": False, "bot": clean_bot, "error": str(e)}

    async def start_campaign(
        self,
        post_number: int,
        channel_message_id: int,
        channel_username: Optional[str] = None,
        delay_seconds: int = 8,
        target_limit: int = 100
    ) -> Dict[str, Any]:
        """
        Initiates a bulk forward campaign to pending bot leads from MongoDB.
        """
        if self.is_running:
            raise RuntimeError("A broadcast campaign is already running! Pause or cancel it first.")

        clean_channel = (channel_username or settings.promo_channel_username).strip()

        # Fetch eligible bot leads
        uncontacted_bots = await Database.get_uncontacted_bots(limit=target_limit)
        if not uncontacted_bots:
            raise ValueError("No uncontacted bot leads found in database to broadcast to!")

        # Initialize campaign metadata
        now = datetime.utcnow()
        campaign_data = {
            "post_number": post_number,
            "channel_username": clean_channel,
            "channel_message_id": channel_message_id,
            "delay_seconds": delay_seconds,
            "total_targeted": len(uncontacted_bots),
            "successful_sent": 0,
            "failed_count": 0,
            "status": "in_progress",
            "current_index": 0,
            "current_bot": "",
            "logs": [f"[{now.strftime('%H:%M:%S')}] Campaign initialized for {len(uncontacted_bots)} bots."],
            "created_at": now
        }

        cid = await Database.create_bot_campaign(campaign_data)
        campaign_data["id"] = cid

        self.active_campaign = campaign_data
        self.is_running = True
        self.is_paused = False

        # Launch background dispatch worker
        self._worker_task = asyncio.create_task(
            self._campaign_dispatch_worker(uncontacted_bots, channel_message_id, clean_channel, delay_seconds)
        )

        return self.active_campaign

    async def _campaign_dispatch_worker(
        self,
        bot_leads: List[Dict[str, Any]],
        channel_message_id: int,
        channel_username: str,
        delay_seconds: int
    ):
        """Background asynchronous dispatcher loop."""
        cid = self.active_campaign.get("id")
        total = len(bot_leads)

        try:
            for idx, lead in enumerate(bot_leads, 1):
                # Check for cancellation or stop
                if not self.is_running:
                    self._add_log("Campaign was manually stopped.")
                    break

                # Handle pause
                while self.is_paused:
                    await asyncio.sleep(1.0)
                    if not self.is_running:
                        break

                bot_username = lead.get("username", "")
                self.active_campaign["current_index"] = idx
                self.active_campaign["current_bot"] = bot_username

                logger.info("[BotCampaign %s] [%d/%d] Contacting @%s...", cid, idx, total, bot_username)
                self._add_log(f"[{idx}/{total}] Forwarding to @{bot_username}...")

                res = await self.forward_to_single_bot(
                    bot_username=bot_username,
                    channel_message_id=channel_message_id,
                    channel_username=channel_username
                )

                if res.get("success"):
                    self.active_campaign["successful_sent"] += 1
                    self._add_log(f"  ✅ Sent successfully to @{bot_username}")
                else:
                    self.active_campaign["failed_count"] += 1
                    err = res.get("error", "Unknown error")
                    self._add_log(f"  ❌ Failed @{bot_username}: {err}")

                    # If flood wait, handle automatic sleep
                    if "flood_wait" in res:
                        wait_sec = res["flood_wait"] + 3
                        self._add_log(f"  ⏳ FloodWait hit! Pausing for {wait_sec}s...")
                        await asyncio.sleep(wait_sec)

                # Update campaign in DB periodically
                if idx % 5 == 0 or idx == total:
                    await Database.update_bot_campaign(cid, {
                        "successful_sent": self.active_campaign["successful_sent"],
                        "failed_count": self.active_campaign["failed_count"],
                        "logs": self.active_campaign["logs"][-30:],
                    })

                # Anti-flood pacing delay
                if idx < total:
                    actual_delay = max(4.0, random.uniform(delay_seconds - 1, delay_seconds + 3))
                    await asyncio.sleep(actual_delay)

            # Finalize campaign
            final_status = "completed" if self.is_running else "stopped"
            self.active_campaign["status"] = final_status
            self._add_log(f"Campaign finished with status: {final_status}.")
            await Database.update_bot_campaign(cid, {
                "status": final_status,
                "successful_sent": self.active_campaign["successful_sent"],
                "failed_count": self.active_campaign["failed_count"],
                "logs": self.active_campaign["logs"][-30:],
            })

        except Exception as exc:
            logger.exception("Error in campaign dispatch worker: %s", exc)
            self._add_log(f"Worker crashed: {str(exc)}")
            if cid:
                await Database.update_bot_campaign(cid, {"status": "error", "error": str(exc)})
        finally:
            self.is_running = False
            self.is_paused = False

    def pause_campaign(self) -> bool:
        if self.is_running and not self.is_paused:
            self.is_paused = True
            self._add_log("Campaign paused by user.")
            return True
        return False

    def resume_campaign(self) -> bool:
        if self.is_running and self.is_paused:
            self.is_paused = False
            self._add_log("Campaign resumed by user.")
            return True
        return False

    def stop_campaign(self) -> bool:
        if self.is_running:
            self.is_running = False
            self.is_paused = False
            self._add_log("Campaign stop signal received.")
            return True
        return False

    def _add_log(self, text: str):
        now_str = datetime.utcnow().strftime("%H:%M:%S")
        entry = f"[{now_str}] {text}"
        if self.active_campaign:
            self.active_campaign["logs"].append(entry)
            if len(self.active_campaign["logs"]) > 100:
                self.active_campaign["logs"] = self.active_campaign["logs"][-100:]


# Global singleton instance
bot_forwarder = BotForwarderEngine()
