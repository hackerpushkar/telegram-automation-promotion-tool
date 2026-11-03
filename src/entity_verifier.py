"""
Telegram Entity Verifier.

Resolves Telegram handles (@usernames) using MTProto and distinguishes
between Human User Accounts, Bots, Broadcast Channels, and Groups.
Ensures only genuine user accounts and bots are added to the Leads Directory.
"""

import asyncio
import logging
from typing import Any, Dict, Optional
from telethon import TelegramClient
from telethon.tl.types import User, Channel, Chat
from telethon.errors import (
    UsernameNotOccupiedError,
    UsernameInvalidError,
    FloodWaitError,
    ChannelPrivateError,
)

logger = logging.getLogger("telegram_promotion.entity_verifier")

# In-memory LRU cache to avoid repeated Telegram API lookups
_VERIFIED_ENTITY_CACHE: Dict[str, Dict[str, Any]] = {}


async def verify_telegram_entity(
    client: TelegramClient,
    raw_username: str
) -> Optional[Dict[str, Any]]:
    """
    Resolves a Telegram username via MTProto and verifies its exact entity type:
    
    Returns a dictionary:
      {
        "username": clean_handle,
        "entity_type": "user" | "bot" | "channel" | "group",
        "is_lead": True | False,
        "title": str,
        "id": int
      }
      
    Returns None if username is invalid or does not exist on Telegram.
    """
    clean_handle = raw_username.strip().lstrip("@").lower()
    if not clean_handle or len(clean_handle) < 3:
        return None

    # Check cache first
    if clean_handle in _VERIFIED_ENTITY_CACHE:
        return _VERIFIED_ENTITY_CACHE[clean_handle]

    if not client or not client.is_connected():
        logger.warning("[EntityVerifier] Telethon client not connected. Skipping entity verification for @%s.", clean_handle)
        return None

    try:
        entity = await client.get_entity(clean_handle)

        if isinstance(entity, User):
            if entity.bot:
                entity_type = "bot"
            else:
                entity_type = "user"
            
            first = getattr(entity, "first_name", "") or ""
            last = getattr(entity, "last_name", "") or ""
            title = f"{first} {last}".strip() or clean_handle

            result = {
                "username": clean_handle,
                "entity_type": entity_type,
                "is_lead": True,  # User and Bot are valid leads
                "title": title,
                "id": entity.id
            }

        elif isinstance(entity, Channel):
            if getattr(entity, "broadcast", False):
                entity_type = "channel"
            else:
                entity_type = "group"  # Supergroup / megagroup

            result = {
                "username": clean_handle,
                "entity_type": entity_type,
                "is_lead": False,  # Channels and Groups are NOT leads!
                "title": getattr(entity, "title", clean_handle),
                "id": entity.id
            }

        elif isinstance(entity, Chat):
            result = {
                "username": clean_handle,
                "entity_type": "group",
                "is_lead": False,
                "title": getattr(entity, "title", clean_handle),
                "id": entity.id
            }

        else:
            result = {
                "username": clean_handle,
                "entity_type": "unknown",
                "is_lead": False,
                "title": clean_handle,
                "id": getattr(entity, "id", None)
            }

        # Cache result
        if len(_VERIFIED_ENTITY_CACHE) > 2000:
            _VERIFIED_ENTITY_CACHE.clear()
        _VERIFIED_ENTITY_CACHE[clean_handle] = result

        logger.info(
            "🔍 [EntityVerifier] @%s verified as '%s' (is_lead=%s, title='%s')",
            clean_handle, result["entity_type"], result["is_lead"], result["title"]
        )
        return result

    except (UsernameNotOccupiedError, UsernameInvalidError):
        logger.info("❌ [EntityVerifier] @%s does not exist on Telegram.", clean_handle)
        _VERIFIED_ENTITY_CACHE[clean_handle] = {
            "username": clean_handle,
            "entity_type": "invalid",
            "is_lead": False,
            "title": "",
            "id": None
        }
        return None

    except FloodWaitError as e:
        logger.warning("⏳ [EntityVerifier] Telegram FloodWait hit (%ds) while resolving @%s.", e.seconds, clean_handle)
        await asyncio.sleep(min(e.seconds, 10))
        return None

    except ChannelPrivateError:
        logger.warning("🔒 [EntityVerifier] @%s is a private channel/chat.", clean_handle)
        return {
            "username": clean_handle,
            "entity_type": "channel",
            "is_lead": False,
            "title": clean_handle,
            "id": None
        }

    except Exception as e:
        logger.warning("⚠️ [EntityVerifier] Error resolving entity @%s: %s", clean_handle, e)
        return None


async def scrub_non_user_leads(client: Optional[TelegramClient] = None) -> Dict[str, Any]:
    """
    Inspects all existing leads in the database and cleans up invalid entries:
    - Deletes handles matching email domains or system keywords (e.g., 'gmail').
    - If Telegram MTProto client is available, verifies whether each lead is a User, Bot, Channel, or Group.
    - Automatically removes Channels and Groups from the 'leads' collection.
    - Automatically converts any channel into 'discovered_channels' so it is not lost.
    """
    from src.db import Database
    from src.extractor import IGNORED_SYSTEM_NAMES

    leads = await Database.get_leads(limit=1000)
    removed_system = []
    removed_channels = []
    removed_groups = []
    reclassified_bots = []
    reclassified_users = []
    valid_count = 0

    logger.info("🧹 [Scrubber] Starting database leads audit (Total leads: %d)...", len(leads))

    for lead in leads:
        handle = lead.get("username_clean", "").lower()
        raw_handle = lead.get("username", handle)

        # 1. Filter system / email domain names
        if handle in IGNORED_SYSTEM_NAMES or len(handle) < 4:
            await Database.delete_lead(handle)
            removed_system.append(raw_handle)
            logger.info("🗑️ [Scrubber] Deleted system/email handle @%s from leads.", raw_handle)
            continue

        if not client:
            valid_count += 1
            continue

        # 2. MTProto entity resolution
        try:
            ver = await verify_telegram_entity(client, raw_handle)
            if not ver:
                valid_count += 1
                continue

            entity_type = ver.get("entity_type")
            is_lead = ver.get("is_lead", False)

            if not is_lead:
                # Handle is actually a Channel or Group! Remove from leads.
                await Database.delete_lead(handle)

                if entity_type == "channel":
                    removed_channels.append(raw_handle)
                    logger.info("🗑️ [Scrubber] Removed broadcast channel @%s from leads directory.", raw_handle)
                    # Preserve in discovered_channels
                    await Database.save_discovered_channel({
                        "channel_id": ver.get("id"),
                        "title": ver.get("title", raw_handle),
                        "username": handle,
                        "invite_link": f"https://t.me/{handle}",
                        "subscribers_count": 0,
                        "status": "pending_scan"
                    })
                else:
                    removed_groups.append(raw_handle)
                    logger.info("🗑️ [Scrubber] Removed group @%s from leads directory.", raw_handle)

            else:
                # Valid user or bot - verify classification
                current_type = lead.get("lead_type")
                verified_type = entity_type  # 'user' or 'bot'
                if current_type != verified_type:
                    leads_col = Database.get_leads_collection()
                    if leads_col is not None:
                        await leads_col.update_one(
                            {"username_clean": handle},
                            {"$set": {"lead_type": verified_type}}
                        )
                    if verified_type == "bot":
                        reclassified_bots.append(raw_handle)
                    else:
                        reclassified_users.append(raw_handle)

                valid_count += 1

        except Exception as e:
            logger.debug("[Scrubber] Error checking @%s: %s", raw_handle, e)
            valid_count += 1

    summary = {
        "total_checked": len(leads),
        "removed_system_or_emails": removed_system,
        "removed_channels": removed_channels,
        "removed_groups": removed_groups,
        "reclassified_as_bots": reclassified_bots,
        "reclassified_as_users": reclassified_users,
        "valid_leads_remaining": valid_count
    }
    logger.info(
        "🏁 [Scrubber] Audit finished! Removed %d system/email, %d channels, %d groups. Valid leads remaining: %d.",
        len(removed_system), len(removed_channels), len(removed_groups), valid_count
    )
    return summary

