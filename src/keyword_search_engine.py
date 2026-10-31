import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel, Chat
from telethon.errors import (
    FloodWaitError,
    ChannelPrivateError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from src.config import settings
from src.db import Database
from src.extractor import extract_leads_from_text
from src.account_manager import AccountManager

logger = logging.getLogger("telegram_promotion.keyword_search")


class KeywordSearchEngine:
    """
    Autonomous Telegram Keyword Search System:
    - Searches Telegram's global public directory for target affiliate/deal keywords.
    - Discovers deal, loot, and coupon channels.
    - Resolves full channel details, descriptions, and bio context.
    - Extracts direct human owner handles (@username) and deal submission bots (@...bot).
    - Stores deduplicated leads directly into MongoDB collection 'leads'.
    - Maintains search history and channel registry in collection 'discovered_channels'.
    """

    _running_autonomous: bool = False
    _background_task: Optional[asyncio.Task] = None

    @classmethod
    async def get_authorized_client(cls) -> Optional[TelegramClient]:
        """
        Retrieves an authorized Telethon client:
        First checks connected user accounts (Slots 1-5 in MongoDB),
        then falls back to the default scanner session string.
        """
        # Strategy 1: Check active accounts in MongoDB
        accounts = await Database.get_all_telegram_accounts()
        active_accounts = [a for a in accounts if a.get("status") == "active" and a.get("session_string")]
        
        for acc in active_accounts:
            try:
                client = AccountManager.get_client(acc["session_string"])
                await client.connect()
                if await client.is_user_authorized():
                    return client
                else:
                    await client.disconnect()
            except Exception as e:
                logger.warning("Could not connect with account %s: %s", acc.get("phone"), e)

        # Strategy 2: Check scanner session if configured
        if settings.telegram_api_id and settings.telegram_api_hash:
            try:
                client = TelegramClient(settings.telegram_session_name, settings.telegram_api_id, settings.telegram_api_hash)
                await client.connect()
                if await client.is_user_authorized():
                    return client
                else:
                    await client.disconnect()
            except Exception as e:
                logger.warning("Could not connect with scanner session: %s", e)

        return None

    @classmethod
    async def search_keyword(
        cls,
        keyword: str,
        limit: int = 15,
        delay_seconds: float = 2.5
    ) -> Dict[str, Any]:
        """
        Executes a targeted search for a keyword:
        1. Searches Telegram global directory via contacts.SearchRequest.
        2. Filters public channels and supergroups.
        3. Retrieves full bio/about and subscriber count.
        4. Extracts direct owner handles and bots.
        5. Persists leads to MongoDB 'leads' and channel to 'discovered_channels'.
        """
        clean_kw = keyword.strip().lower()
        if not clean_kw:
            raise ValueError("Keyword cannot be empty.")

        client = await cls.get_authorized_client()
        if not client:
            raise RuntimeError(
                "No authorized Telegram account is available to search. "
                "Please connect an account in the 'Telegram Accounts' tab first."
            )

        channels_discovered = 0
        leads_extracted_total = 0
        discovered_list = []

        try:
            logger.info("🔍 [KeywordSearch] Executing global search for '%s' (limit: %d)...", clean_kw, limit)
            found = await client(SearchRequest(q=clean_kw, limit=limit))
            raw_chats = found.chats or []
            logger.info("🔍 [KeywordSearch] Found %d total chat entities for '%s'.", len(raw_chats), clean_kw)

            for chat in raw_chats:
                # We target public broadcast channels and mega-groups
                if not isinstance(chat, (Channel, Chat)):
                    continue

                username = getattr(chat, "username", None)
                title = getattr(chat, "title", "Untitled Channel")
                channel_id = getattr(chat, "id", None)
                if not channel_id:
                    continue

                # Deduplication check: skip if scanned in the last 7 days
                recently_scanned = await Database.is_channel_recently_scanned(channel_id, cooldown_days=7)
                if recently_scanned:
                    logger.debug("Skipping recently scanned channel: %s (ID: %d)", title, channel_id)
                    continue

                bio_text = ""
                subscribers_count = getattr(chat, "participants_count", None)

                try:
                    # Fetch full channel info for description / about
                    full = await client(GetFullChannelRequest(chat))
                    if hasattr(full, "full_chat") and full.full_chat:
                        bio_text = full.full_chat.about or ""
                        subscribers_count = getattr(full.full_chat, "participants_count", subscribers_count) or 0
                except (ChannelPrivateError, UsernameInvalidError, UsernameNotOccupiedError):
                    bio_text = ""
                except FloodWaitError as fe:
                    logger.warning("FloodWait encountered on channel inspection: %d seconds. Sleeping...", fe.seconds)
                    await asyncio.sleep(fe.seconds + 2)
                    continue
                except Exception as ex:
                    logger.debug("Could not get full channel for %s: %s", title, ex)

                # Extract leads from bio text
                extracted_leads = []
                invite_link = f"https://t.me/{username}" if username else None
                if bio_text:
                    extracted_leads = extract_leads_from_text(
                        bio_text=bio_text,
                        source_name=title,
                        source_invite_link=invite_link or f"tg://resolve?domain={channel_id}",
                        source_id=f"keyword_search:{clean_kw}"
                    )

                # Save each extracted lead to MongoDB
                saved_leads_for_channel = 0
                for lead in extracted_leads:
                    saved, _ = await Database.upsert_lead(lead)
                    if saved:
                        saved_leads_for_channel += 1
                        leads_extracted_total += 1

                # Save channel to discovered_channels
                channel_doc = {
                    "channel_id": channel_id,
                    "title": title,
                    "username": username,
                    "invite_link": invite_link,
                    "subscribers_count": subscribers_count or 0,
                    "bio": bio_text[:500] if bio_text else None,
                    "matching_keyword": clean_kw,
                    "extracted_leads_count": len(extracted_leads),
                    "status": "scanned"
                }
                await Database.save_discovered_channel(channel_doc)
                channels_discovered += 1

                discovered_list.append({
                    "title": title,
                    "username": username,
                    "subscribers": subscribers_count or 0,
                    "leads_found": len(extracted_leads),
                    "leads": [l.username for l in extracted_leads]
                })

                logger.info(
                    "✅ [KeywordSearch] Channel: '%s' (@%s) | Subs: %s | Extracted %d lead(s)",
                    title, username or "none", subscribers_count or 0, len(extracted_leads)
                )

                if len(extracted_leads) > 0:
                    try:
                        from src.web import log_activity
                        lead_samples = ", ".join("@" + l.username for l in extracted_leads[:3])
                        log_activity(f"🎯 Discovered '{title}' (@{username or 'private'}) | Extracted {len(extracted_leads)} lead(s): {lead_samples}", "sync")
                    except Exception:
                        pass

                # Safe pacing delay between channel inspections
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)

            # Update stats on keyword record
            await Database.update_keyword_stats(clean_kw, channels_discovered, leads_extracted_total)

            return {
                "status": "success",
                "keyword": clean_kw,
                "channels_found": channels_discovered,
                "leads_extracted": leads_extracted_total,
                "discovered": discovered_list
            }

        except FloodWaitError as e:
            logger.warning("FloodWait on keyword search '%s': %d seconds", clean_kw, e.seconds)
            return {
                "status": "flood_wait",
                "keyword": clean_kw,
                "message": f"Telegram rate limit: please wait {e.seconds} seconds.",
                "wait_seconds": e.seconds
            }
        except Exception as e:
            logger.error("Error executing keyword search for '%s': %s", clean_kw, e, exc_info=True)
            return {
                "status": "error",
                "keyword": clean_kw,
                "message": str(e)
            }
        finally:
            if client and client.is_connected():
                await client.disconnect()

    @classmethod
    async def run_autonomous_cycle(cls, max_keywords: int = 2) -> Dict[str, Any]:
        """
        Executes one autonomous search cycle:
        Selects oldest searched enabled keywords and executes discovery safely.
        """
        if not settings.keyword_search_enabled:
            logger.info("Autonomous keyword search is currently disabled in settings.")
            return {"status": "disabled", "processed": 0}

        all_keywords = await Database.get_search_keywords(enabled_only=True)
        if not all_keywords:
            logger.info("No active search keywords configured. Skipping autonomous cycle.")
            return {"status": "no_keywords", "processed": 0}

        # Sort: Never searched first (None), then oldest searched
        def sort_key(k):
            last = k.get("last_searched_at")
            if last is None:
                return datetime.min
            return last

        sorted_keywords = sorted(all_keywords, key=sort_key)
        target_batch = sorted_keywords[:max_keywords]

        logger.info(
            "🚀 [AutonomousKeywordSearch] Starting cycle for %d keyword(s): %s",
            len(target_batch), [k["keyword"] for k in target_batch]
        )

        try:
            from src.web import log_activity
            kw_names = ", ".join(f'"{k["keyword"]}"' for k in target_batch)
            log_activity(f"🚀 Autonomous search cycle started for keywords: {kw_names}", "sync")
        except Exception:
            pass

        cycle_results = []
        total_channels = 0
        total_leads = 0

        for kw_doc in target_batch:
            kw = kw_doc["keyword"]
            res = await cls.search_keyword(
                keyword=kw,
                limit=settings.keyword_search_limit_per_keyword,
                delay_seconds=3.0
            )
            cycle_results.append(res)
            total_channels += res.get("channels_found", 0)
            total_leads += res.get("leads_extracted", 0)
            # Inter-keyword rest to maintain healthy MTProto session
            await asyncio.sleep(5.0)

        logger.info(
            "🏁 [AutonomousKeywordSearch] Cycle complete! Discovered %d channel(s) and extracted %d lead(s).",
            total_channels, total_leads
        )

        try:
            from src.web import log_activity
            log_activity(f"🏁 Autonomous search cycle complete: {total_channels} channels discovered, {total_leads} leads saved to MongoDB.", "sync")
        except Exception:
            pass

        return {
            "status": "completed",
            "keywords_searched": len(target_batch),
            "channels_found": total_channels,
            "leads_extracted": total_leads,
            "details": cycle_results
        }

