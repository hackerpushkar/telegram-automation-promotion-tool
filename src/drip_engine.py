import asyncio
import logging
import os
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

    _is_running: bool = False

    @classmethod
    async def execute_drip_stage(
        cls,
        stage: int,
        batch_size: int = 20,
        delay_seconds: float = 12.0
    ) -> Dict[str, Any]:
        """
        Executes a single week/stage batch:
          1. Connects the account assigned to Slot #stage (or rotates active accounts).
          2. Finds eligible contact bots for that stage.
          3. Forwards Post #stage from the official channel (or sends high-res banner + text).
          4. Advances bot to next stage (sets 7-day cooldown) and logs sending report.
        """
        if stage < 1 or stage > 5:
            raise ValueError(f"Invalid stage {stage}. Must be between 1 and 5.")

        # Resolve sending account: match assigned_slot, or rotate active accounts
        account = await Database.get_telegram_account_by_slot(stage)
        if not account or account.get("status") != "active":
            all_accounts = await Database.get_all_telegram_accounts()
            active_accounts = [
                a for a in all_accounts
                if a.get("status") not in ["banned", "logged_out", "revoked", "invalid_session"]
                and a.get("session_string")
            ]
            if not active_accounts:
                raise ValueError(f"No active Telegram sending account available to execute Stage #{stage}!")
            # Rotate account based on stage
            account = active_accounts[(stage - 1) % len(active_accounts)]

        # Resolve promo content preview and channel post
        preview = None
        try:
            from src.channel_publisher import get_promo_post_preview
            preview = get_promo_post_preview(stage)
        except Exception as e:
            logger.debug("Could not load promo post preview for stage %d: %s", stage, e)

        post = await Database.get_published_post(stage)
        channel_username = post.get("channel_username", settings.promo_channel_username) if post else settings.promo_channel_username
        message_id = post.get("message_id") if post else None
        message_link = post.get("message_link", f"https://t.me/{channel_username.lstrip('@')}/{message_id}") if (post and message_id) else None

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
            "🚀 Starting Stage #%d promotional batch using Account %s. Target: %d bots. Post #%d.",
            stage, phone, len(eligible_bots), stage
        )

        results = []
        successful_count = 0
        failed_count = 0

        try:
            await client.connect()
            if not await client.is_user_authorized():
                await Database.update_telegram_account_status(phone, "logged_out")
                raise RuntimeError(f"Account {phone} session is no longer authorized.")

            # Resolve channel peer if message_id exists
            channel_entity = None
            if message_id:
                try:
                    channel_entity = await client.get_entity(channel_username)
                except Exception as e:
                    logger.warning("Could not resolve channel %s: %s. Will send directly.", channel_username, e)

            for idx, bot_lead in enumerate(eligible_bots, 1):
                raw_handle = bot_lead.get("username", "").strip()
                clean_handle = bot_lead.get("username_clean", "").strip() or raw_handle.lstrip("@").lower()

                if not clean_handle:
                    continue

                lead_target = f"@{clean_handle}"
                logger.info("[%d/%d] Stage #%d: Sending Post #%d to %s...", idx, len(eligible_bots), stage, lead_target)

                try:
                    # Resolve bot entity
                    bot_entity = await client.get_entity(lead_target)

                    sent_success = False

                    # 1. Attempt official channel post forward
                    if channel_entity and message_id:
                        try:
                            await client.forward_messages(
                                entity=bot_entity,
                                messages=message_id,
                                from_peer=channel_entity
                            )
                            sent_success = True
                        except Exception as fwd_err:
                            logger.warning("Channel forward to %s failed (%s). Falling back to direct promo post...", lead_target, fwd_err)

                    # 2. Fallback: send banner image and text directly
                    if not sent_success:
                        caption = (preview.get("caption") if preview else "") or (
                            "🚀 STILL RUNNING YOUR TELEGRAM DEAL CHANNEL MANUALLY?\n\n"
                            "Stop wasting hours on manual posting. Let ProAffiliaters automate your workflow!\n\n"
                            "⚡ Everything You Need:\n"
                            "- Quick Post - Create deal posts from product links in seconds\n"
                            "- AutoPilot - Automate deal posting 24/7\n"
                            "- Custom Templates - Create eye-catching deal posts\n\n"
                            "🌐 Start Now: https://proaffiliaters.com\n"
                            "🤖 Support: @ProAffiliatersHelpBot\n"
                            "📢 Community: @pro_affiliaters"
                        )
                        img_path = preview.get("image_path") if preview else None
                        if img_path and os.path.exists(img_path):
                            await client.send_file(
                                entity=bot_entity,
                                file=img_path,
                                caption=caption
                            )
                        else:
                            await client.send_message(
                                entity=bot_entity,
                                message=caption
                            )
                        sent_success = True

                    # Advance lead to new stage (sets 7-day cooldown)
                    new_stage = stage
                    await Database.advance_lead_promo_stage(
                        username_clean=clean_handle,
                        new_stage=new_stage,
                        account_phone=phone,
                        post_number=stage,
                        message_link=message_link or "direct_promo_send"
                    )

                    # Log to Sending Reports collection
                    snippet = (preview.get("caption", "") if preview else "")[:100] or f"Promotional Post #{stage}"
                    await Database.save_sending_report({
                        "recipient_username": clean_handle,
                        "recipient_type": "bot",
                        "sender_account_phone": phone,
                        "sender_account_name": f"{account.get('first_name', '')} {account.get('last_name', '')}".strip() or phone,
                        "post_title": (preview.get("title") if preview else f"Promotional Post #{stage}"),
                        "message_snippet": snippet,
                        "status": "sent",
                        "sent_at": datetime.utcnow()
                    })

                    successful_count += 1
                    results.append({"username": clean_handle, "status": "sent", "new_stage": new_stage})
                    logger.info("  ✅ Successfully dispatched Promo #%d to %s! (Advanced to Stage #%d)", stage, lead_target, new_stage)

                    # Delay between sends to avoid Telegram rate limits
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
                    logger.error("Error sending promo to %s: %s", lead_target, e)
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

    @classmethod
    async def run_autonomous_drip_cycle(cls, batch_per_stage: int = 10) -> Dict[str, Any]:
        """
        Master Autonomous Autopilot Runner:
          1. Identifies newly discovered userbots (Stage 0 / None) and dispatches Promo #1 using Account #1.
          2. Identifies userbots whose 7-day cooldown expired (Stages 2 to 5) and dispatches the next promo using rotating accounts.
        """
        if cls._is_running:
            logger.info("⏸️ [AutonomousDrip] An automated outreach cycle is already running. Skipping concurrent trigger.")
            return {"status": "already_running", "total_sent": 0}

        cls._is_running = True
        logger.info("🤖 [AutonomousDrip] Starting autonomous promotional outreach cycle across 5 stages...")

        try:
            all_accs = await Database.get_all_telegram_accounts()
            active_accs = [
                a for a in all_accs
                if a.get("status") not in ["banned", "logged_out", "revoked", "invalid_session"]
                and a.get("session_string")
            ]
            if not active_accs:
                logger.info("⏸️ [AutonomousDrip] No active sending accounts found. Skipping cycle until an account is connected.")
                return {"status": "no_accounts", "stages_run": 0, "total_sent": 0}

            total_sent = 0
            stages_summary = []

            # Check all 5 stages in sequence
            for st in range(1, 6):
                try:
                    eligible = await Database.get_eligible_bots_for_stage(st, min_cooldown_days=7, limit=batch_per_stage)
                    if not eligible:
                        continue

                    logger.info("🎯 [AutonomousDrip] Found %d bot(s) eligible for Stage #%d.", len(eligible), st)
                    res = await cls.execute_drip_stage(st, batch_size=batch_per_stage, delay_seconds=12.0)
                    sent = res.get("successful", 0)
                    total_sent += sent
                    stages_summary.append({
                        "stage": st,
                        "targeted": len(eligible),
                        "sent": sent,
                        "account": res.get("account_phone")
                    })
                    # Pause between stages to maintain MTProto session health
                    if sent > 0 and st < 5:
                        await asyncio.sleep(5.0)
                except Exception as e:
                    logger.error("❌ [AutonomousDrip] Error executing Stage #%d: %s", st, e)

            msg = f"Autonomous Drip complete: Dispatched {total_sent} promotional message(s) across {len(stages_summary)} stage(s)."
            logger.info("🏁 [AutonomousDrip] %s", msg)

            if total_sent > 0:
                try:
                    from src.web import log_activity
                    log_activity(msg, "lead")
                except Exception:
                    pass

            return {
                "status": "completed",
                "total_sent": total_sent,
                "stages": stages_summary
            }

        finally:
            cls._is_running = False
