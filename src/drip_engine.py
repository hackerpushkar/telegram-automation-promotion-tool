import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UserIsBlockedError,
    UserPrivacyRestrictedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from src.account_manager import AccountManager
from src.config import settings
from src.db import Database

logger = logging.getLogger("telegram_promotion.drip_engine")


class DripEngine:
    """
    Orchestrates the 5-week progressive promotion schedule for deal channel contact bots:
      - Week 1: Account 1 forwards Post #1 (Stage 0 -> 1)
      - Week 2: Account 2 forwards Post #2 (Stage 1 -> 2, min 7-day cooldown)
      - Week 3: Account 3 forwards Post #3 (Stage 2 -> 3, min 7-day cooldown)
      - Week 4: Account 4 forwards Post #4 (Stage 3 -> 4, min 7-day cooldown)
      - Week 5: Account 5 forwards Post #5 (Stage 4 -> 5, min 7-day cooldown)
      - Stage 5: Progression reaches completion and STOPS permanently.
    """

    @classmethod
    async def get_stage_info(cls, stage: int) -> Dict[str, Any]:
        """Returns metadata for a specific stage (assigned account, published post, eligible bot count)."""
        if stage < 1 or stage > 5:
            raise ValueError(f"Invalid stage {stage}. Must be between 1 and 5.")

        account = await Database.get_telegram_account_by_slot(stage)
        post = await Database.get_published_post(stage)
        eligible_bots = await Database.get_eligible_bots_for_stage(stage, min_cooldown_days=7, limit=100)

        return {
            "stage": stage,
            "week_label": f"Week {stage}",
            "post_number": stage,
            "post_title": post.get("title", f"Promotional Post #{stage}") if post else f"Post #{stage} (Not Published)",
            "post_link": post.get("message_link") if post else None,
            "post_id": post.get("message_id") if post else None,
            "account_slot": stage,
            "account_phone": account.get("phone") if account else None,
            "account_name": f"{account.get('first_name', '')} {account.get('last_name', '')}".strip() if account else None,
            "account_status": account.get("status") if account else "unassigned",
            "eligible_bots_count": len(eligible_bots)
        }

    @classmethod
    async def get_drip_overview(cls) -> Dict[str, Any]:
        """Provides full 5-week funnel stats, stage mappings, and accounts health."""
        funnel = await Database.get_drip_stage_funnel()
        accounts = await Database.get_all_telegram_accounts()
        accounts_by_slot = {acc.get("assigned_slot"): acc for acc in accounts}

        stages = []
        for s in range(1, 6):
            acc = accounts_by_slot.get(s)
            post = await Database.get_published_post(s)
            eligible = await Database.get_eligible_bots_for_stage(s, min_cooldown_days=7, limit=1)

            stages.append({
                "stage": s,
                "week": f"Week {s}",
                "post_number": s,
                "post_title": post.get("title", f"Post #{s}") if post else f"Post #{s} (Not Published)",
                "post_link": post.get("message_link") if post else None,
                "has_post": bool(post and post.get("message_id")),
                "account_phone": acc.get("phone") if acc else None,
                "account_name": f"{acc.get('first_name', '')} {acc.get('last_name', '')}".strip() if acc else None,
                "account_status": acc.get("status") if acc else "unassigned",
                "has_active_account": bool(acc and acc.get("status") == "active"),
                "leads_in_stage": funnel.get(f"stage_{s}_sent", 0) if s < 5 else funnel.get("stage_5_completed", 0),
                "ready_to_send": funnel.get(f"stage_{s-1}_eligible_for_stage_{s}", 0) if s > 1 else funnel.get("stage_0_pending", 0)
            })

        return {
            "funnel": funnel,
            "stages": stages,
            "total_accounts": len(accounts),
            "timestamp": datetime.utcnow().isoformat()
        }

    @classmethod
    async def execute_drip_stage(
        cls,
        stage: int,
        batch_size: int = 20,
        delay_seconds: float = 12.0
    ) -> Dict[str, Any]:
        """
        Executes a single week/stage batch:
          1. Connects the account assigned to Slot #stage.
          2. Finds eligible contact bots for that stage.
          3. Forwards Post #stage from the official channel.
          4. Advances bot to next stage and records history.
        """
        if stage < 1 or stage > 5:
            raise ValueError(f"Invalid stage {stage}. Must be between 1 and 5.")

        account = await Database.get_telegram_account_by_slot(stage)
        if not account or account.get("status") != "active":
            raise ValueError(f"No active Telegram account assigned to Slot #{stage} (Week {stage})!")

        post = await Database.get_published_post(stage)
        if not post or not post.get("message_id"):
            raise ValueError(f"Promotional Post #{stage} has not been published to the channel yet!")

        channel_username = post.get("channel_username", settings.promo_channel_username)
        message_id = post["message_id"]
        message_link = post.get("message_link", f"https://t.me/{channel_username.lstrip('@')}/{message_id}")

        eligible_bots = await Database.get_eligible_bots_for_stage(stage, min_cooldown_days=7, limit=batch_size)
        if not eligible_bots:
            logger.info("No eligible contact bots found for Stage #%d.", stage)
            return {
                "stage": stage,
                "status": "completed",
                "message": f"No contact bots currently eligible for Stage #{stage}.",
                "targeted": 0,
                "successful": 0,
                "failed": 0,
                "results": []
            }

        session_str = account["session_string"]
        phone = account["phone"]
        client = AccountManager.get_client(session_str)

        logger.info(
            "🚀 Starting Stage #%d forwarding batch using Account %s (Slot #%d). Target: %d bots. Post #%d.",
            stage, phone, stage, len(eligible_bots), stage
        )

        results = []
        successful_count = 0
        failed_count = 0

        try:
            await client.connect()
            if not await client.is_user_authorized():
                await Database.update_telegram_account_status(phone, "logged_out")
                raise RuntimeError(f"Account {phone} session is no longer authorized.")

            # Resolve channel peer
            try:
                channel_entity = await client.get_entity(channel_username)
            except Exception as e:
                raise RuntimeError(f"Could not resolve channel {channel_username}: {e}")

            for idx, bot_lead in enumerate(eligible_bots, 1):
                raw_handle = bot_lead.get("username", "").strip()
                clean_handle = bot_lead.get("username_clean", "").strip() or raw_handle.lstrip("@").lower()

                if not clean_handle:
                    continue

                lead_target = f"@{clean_handle}"
                logger.info("[%d/%d] Stage #%d: Forwarding Post #%d to %s...", idx, len(eligible_bots), stage, lead_target)

                try:
                    # Resolve bot entity
                    bot_entity = await client.get_entity(lead_target)

                    # Forward post from channel to contact bot
                    await client.forward_messages(
                        entity=bot_entity,
                        messages=message_id,
                        from_peer=channel_entity
                    )

                    # Advance lead to new stage
                    new_stage = stage
                    await Database.advance_lead_promo_stage(
                        username_clean=clean_handle,
                        new_stage=new_stage,
                        account_phone=phone,
                        post_number=stage,
                        message_link=message_link
                    )

                    successful_count += 1
                    results.append({"username": clean_handle, "status": "sent", "new_stage": new_stage})
                    logger.info("  ✅ Successfully forwarded to %s! (Advanced to Stage #%d)", lead_target, new_stage)

                    # Delay between sends to avoid Telegram spam trigger
                    if idx < len(eligible_bots):
                        await asyncio.sleep(delay_seconds)

                except FloodWaitError as e:
                    logger.warning("Telegram FloodWait of %d seconds on Account %s.", e.seconds, phone)
                    results.append({"username": clean_handle, "status": "flood_wait", "error": f"FloodWait {e.seconds}s"})
                    failed_count += 1
                    if e.seconds > 300:
                        await Database.update_telegram_account_status(phone, f"flood_wait_{e.seconds}s")
                        logger.error("FloodWait is too high (%ds). Halting batch for Account %s.", e.seconds, phone)
                        break
                    else:
                        await asyncio.sleep(e.seconds + 2)

                except (UsernameInvalidError, UsernameNotOccupiedError):
                    logger.warning("Invalid/non-existent bot username: %s. Marking ignored.", lead_target)
                    await Database.update_lead_status(clean_handle, "ignored")
                    results.append({"username": clean_handle, "status": "invalid_username", "error": "Username not found"})
                    failed_count += 1

                except (UserPrivacyRestrictedError, UserIsBlockedError) as e:
                    logger.warning("Privacy restriction or bot blocked by %s: %s", lead_target, e)
                    results.append({"username": clean_handle, "status": "restricted", "error": str(e)})
                    failed_count += 1

                except Exception as e:
                    logger.error("Error forwarding to %s: %s", lead_target, e)
                    results.append({"username": clean_handle, "status": "error", "error": str(e)})
                    failed_count += 1

        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        logger.info(
            "🏁 Stage #%d batch finished. Success: %d | Failed: %d | Total: %d",
            stage, successful_count, failed_count, len(eligible_bots)
        )

        return {
            "stage": stage,
            "status": "completed",
            "account_phone": phone,
            "post_number": stage,
            "targeted": len(eligible_bots),
            "successful": successful_count,
            "failed": failed_count,
            "results": results
        }
