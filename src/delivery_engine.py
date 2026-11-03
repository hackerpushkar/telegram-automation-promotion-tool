import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UserPrivacyRestrictedError,
    UserDeactivatedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    PeerFloodError,
)

from src.account_manager import AccountManager
from src.db import Database

logger = logging.getLogger("telegram_promotion.delivery_engine")


class DeliveryEngine:
    """
    Manages promotional message dispatching with account rotation,
    anti-flood safety, delivery report logging, and live progress tracking.
    """

    def __init__(self):
        self.is_running: bool = False
        self.current_campaign: Dict[str, Any] = {
            "is_running": False,
            "campaign_id": None,
            "title": "",
            "total_targeted": 0,
            "sent_count": 0,
            "failed_count": 0,
            "floodwait_count": 0,
            "current_recipient": None,
            "current_account": None,
            "started_at": None,
            "progress_percent": 0,
            "logs": []
        }
        self._stop_event = asyncio.Event()

    def get_campaign_status(self) -> Dict[str, Any]:
        """Returns the real-time operational status of the dispatcher."""
        st = dict(self.current_campaign)
        st["recent_logs"] = st.get("logs", [])[-30:] if st.get("logs") else []
        st["total_targets"] = st.get("total_targeted", 0)
        st["current_lead"] = st.get("current_recipient")
        st["post_title"] = st.get("title", "")
        if "status" not in st or not st["status"]:
            st["status"] = "running" if st.get("is_running") else "idle"
        return st

    def stop_campaign(self) -> bool:
        """Signals the active delivery campaign to halt gracefully."""
        if not self.is_running:
            return False
        self._stop_event.set()
        self.is_running = False
        self.current_campaign["is_running"] = False
        self.log_event("🛑 Manual sending campaign stopped by admin.")
        return True

    def log_event(self, message: str):
        """Appends a timestamped log entry to the active campaign monitor."""
        ts = datetime.utcnow().strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        self.current_campaign["logs"].insert(0, entry)
        if len(self.current_campaign["logs"]) > 100:
            self.current_campaign["logs"].pop()
        logger.info("[DeliveryEngine] %s", message)

    async def start_campaign(
        self,
        message_text: str,
        title: str = "Promotional Dispatch",
        image_url: Optional[str] = None,
        button_text: Optional[str] = None,
        button_url: Optional[str] = None,
        target_type: str = "all_userbots",  # all_userbots, all_usernames, uncontacted, custom
        custom_recipients: Optional[List[str]] = None,
        delay_seconds: float = 10.0,
        max_per_account: int = 40
    ) -> Dict[str, Any]:
        """
        Executes a delivery campaign rotating across all healthy connected Telegram accounts.
        """
        if self.is_running:
            raise RuntimeError("A promotional sending campaign is already in progress. Please wait or stop it first.")

        if not message_text or not message_text.strip():
            raise ValueError("Message text cannot be empty.")

        # 1. Fetch available sending accounts
        accounts = await Database.get_all_telegram_accounts()
        active_accounts = [
            acc for acc in accounts
            if acc.get("status") == "active" and acc.get("session_string")
        ]

        if not active_accounts:
            raise RuntimeError("No active Telegram sending accounts found. Please log in to at least one account in 'Login Accounts'.")

        # 2. Resolve target leads
        leads_col = Database.get_leads_collection()
        recipients: List[Dict[str, Any]] = []

        if target_type == "custom" and custom_recipients:
            for handle in custom_recipients:
                clean_handle = handle.strip().replace("@", "")
                if clean_handle:
                    lead_type = "bot" if clean_handle.lower().endswith("bot") else "user"
                    recipients.append({"username": clean_handle, "lead_type": lead_type})

        elif target_type == "all_userbots":
            if leads_col is not None:
                cursor = leads_col.find({"lead_type": "bot", "status": {"$ne": "ignored"}}, {"_id": 0})
                recipients = await cursor.to_list(length=1000)
            else:
                recipients = [
                    lead for lead in Database._fallback_leads.values()
                    if lead.get("lead_type") == "bot" and lead.get("status") != "ignored"
                ]

        elif target_type == "all_usernames":
            if leads_col is not None:
                cursor = leads_col.find({"lead_type": "user", "status": {"$ne": "ignored"}}, {"_id": 0})
                recipients = await cursor.to_list(length=1000)
            else:
                recipients = [
                    lead for lead in Database._fallback_leads.values()
                    if lead.get("lead_type") == "user" and lead.get("status") != "ignored"
                ]

        elif target_type == "uncontacted":
            if leads_col is not None:
                cursor = leads_col.find({"status": "new"}, {"_id": 0})
                recipients = await cursor.to_list(length=1000)
            else:
                recipients = [
                    lead for lead in Database._fallback_leads.values()
                    if lead.get("status") == "new"
                ]
        else:
            # Default to all leads
            if leads_col is not None:
                cursor = leads_col.find({"status": {"$ne": "ignored"}}, {"_id": 0})
                recipients = await cursor.to_list(length=1000)
            else:
                recipients = list(Database._fallback_leads.values())

        if not recipients:
            raise ValueError(f"No leads found matching target criteria '{target_type}'. Add or discover leads first.")

        # Initialize campaign state
        import uuid
        campaign_id = str(uuid.uuid4())
        self.is_running = True
        self._stop_event.clear()

        self.current_campaign = {
            "is_running": True,
            "campaign_id": campaign_id,
            "title": title or "Manual Promotional Post",
            "total_targeted": len(recipients),
            "sent_count": 0,
            "failed_count": 0,
            "floodwait_count": 0,
            "current_recipient": None,
            "current_account": None,
            "started_at": datetime.utcnow().isoformat(),
            "progress_percent": 0,
            "logs": []
        }

        self.log_event(f"🚀 Started delivery campaign: '{title}' targeting {len(recipients)} leads with {len(active_accounts)} accounts.")

        # Launch dispatch loop as background task so API returns immediately
        asyncio.create_task(
            self._dispatch_loop(
                recipients=recipients,
                accounts=active_accounts,
                message_text=message_text,
                title=title,
                image_url=image_url,
                button_text=button_text,
                button_url=button_url,
                delay_seconds=delay_seconds
            )
        )

        return self.current_campaign

    async def _dispatch_loop(
        self,
        recipients: List[Dict[str, Any]],
        accounts: List[Dict[str, Any]],
        message_text: str,
        title: str,
        image_url: Optional[str],
        button_text: Optional[str],
        button_url: Optional[str],
        delay_seconds: float
    ):
        """Rotates accounts and delivers messages sequentially."""
        total = len(recipients)
        account_pool: List[Dict[str, Any]] = list(accounts)
        account_clients: Dict[str, TelegramClient] = {}
        account_idx = 0

        # Build full message with button link if provided
        final_message = message_text.strip()
        if button_text and button_url:
            clean_btn_url = button_url.strip()
            clean_btn_text = button_text.strip()
            final_message += f"\n\n👉 **[{clean_btn_text}]({clean_btn_url})**"

        try:
            for idx, lead in enumerate(recipients, start=1):
                if self._stop_event.is_set():
                    self.log_event("Delivery loop interrupted by stop signal.")
                    break

                if not account_pool:
                    self.log_event("❌ All sending accounts hit FloodWait or restrictions. Halting campaign.")
                    break

                # Pick next account via Round-Robin rotation
                current_acc = account_pool[account_idx % len(account_pool)]
                account_idx += 1

                phone = current_acc.get("phone", "Unknown")
                sender_name = f"{current_acc.get('first_name', '')} {current_acc.get('last_name', '')}".strip() or phone
                recipient_handle = (lead.get("username") or lead.get("username_clean") or "").replace("@", "").strip()
                lead_type = lead.get("lead_type") or ("bot" if recipient_handle.lower().endswith("bot") else "user")
                
                # Normalize lead type for reporting: 'userbot' vs 'username'
                report_type = "userbot" if lead_type == "bot" else "username"

                if not recipient_handle:
                    continue

                self.current_campaign["current_recipient"] = f"@{recipient_handle}"
                self.current_campaign["current_account"] = phone
                self.current_campaign["progress_percent"] = int((idx / total) * 100)

                # Get or create connected Telethon client for this account
                if phone not in account_clients:
                    try:
                        client = AccountManager.get_client(current_acc["session_string"])
                        await client.connect()
                        account_clients[phone] = client
                    except Exception as conn_err:
                        self.log_event(f"⚠️ Failed connecting account {phone}: {conn_err}")
                        account_pool = [a for a in account_pool if a.get("phone") != phone]
                        continue

                client = account_clients[phone]

                # Dispatch message
                sent_ok = False
                error_msg = None

                try:
                    target_peer = f"@{recipient_handle}"

                    # If recipient is a bot, execute handshake / language bypass first
                    if report_type == "bot":
                        try:
                            from src.bot_interactor import prepare_bot_for_promo
                            bot_ent = await client.get_entity(target_peer)
                            await prepare_bot_for_promo(client, bot_ent, bot_handle=target_peer)
                        except Exception as hs_err:
                            logger.debug("Handshake notice on %s: %s", target_peer, hs_err)

                    if image_url and os.path.exists(image_url):
                        await client.send_file(target_peer, image_url, caption=final_message)
                    else:
                        await client.send_message(target_peer, final_message, link_preview=False)

                    sent_ok = True
                    self.current_campaign["sent_count"] += 1
                    self.log_event(f"✅ [{idx}/{total}] Sent to @{recipient_handle} ({report_type}) via {phone}")

                    # Mark lead as contacted in database
                    await Database.update_lead_status(recipient_handle, "contacted")
                    # Increment send count for account
                    await Database.increment_account_send_count(phone)

                except FloodWaitError as fw:
                    error_msg = f"Telegram FloodWait: wait {fw.seconds}s"
                    self.current_campaign["floodwait_count"] += 1
                    self.current_campaign["failed_count"] += 1
                    self.log_event(f"⏳ FloodWait on account {phone} for {fw.seconds}s. Cooling down account.")
                    
                    # Remove from active pool temporarily
                    account_pool = [a for a in account_pool if a.get("phone") != phone]

                except (UsernameInvalidError, UsernameNotOccupiedError) as un_err:
                    error_msg = f"Username does not exist: {un_err}"
                    self.current_campaign["failed_count"] += 1
                    self.log_event(f"❌ Failed: @{recipient_handle} is not a valid Telegram user.")

                except UserPrivacyRestrictedError:
                    error_msg = "User privacy settings disallow direct messages."
                    self.current_campaign["failed_count"] += 1
                    self.log_event(f"🔒 Privacy restriction on @{recipient_handle}.")

                except PeerFloodError:
                    error_msg = "Account received PeerFlood (spam limit)."
                    self.current_campaign["failed_count"] += 1
                    self.log_event(f"⚠️ Account {phone} received PeerFlood restriction. Removing from active pool.")
                    account_pool = [a for a in account_pool if a.get("phone") != phone]

                except Exception as send_err:
                    error_msg = str(send_err)
                    self.current_campaign["failed_count"] += 1
                    self.log_event(f"❌ Error sending to @{recipient_handle} via {phone}: {send_err}")

                # Save delivery audit report
                status_str = "sent" if sent_ok else ("floodwait" if "floodwait" in (error_msg or "").lower() else "failed")
                snippet = message_text[:80] + ("..." if len(message_text) > 80 else "")

                report_entry = {
                    "recipient_username": recipient_handle,
                    "recipient_type": report_type,
                    "sender_account_phone": phone,
                    "sender_account_name": sender_name,
                    "post_title": title,
                    "message_snippet": snippet,
                    "status": status_str,
                    "error_message": error_msg,
                    "sent_at": datetime.utcnow()
                }
                await Database.save_sending_report(report_entry)

                # Human-like delay between messages
                if idx < total and not self._stop_event.is_set():
                    await asyncio.sleep(delay_seconds)

        finally:
            # Clean up open Telethon clients
            for cl in account_clients.values():
                try:
                    await cl.disconnect()
                except Exception:
                    pass

            self.is_running = False
            self.current_campaign["is_running"] = False
            self.current_campaign["progress_percent"] = 100
            self.current_campaign["completed_at"] = datetime.utcnow().isoformat()
            self.log_event(
                f"🏁 Campaign finished! Delivered: {self.current_campaign['sent_count']}, "
                f"Failed: {self.current_campaign['failed_count']}, "
                f"FloodWait: {self.current_campaign['floodwait_count']}."
            )


# Global singleton instance
delivery_engine = DeliveryEngine()
