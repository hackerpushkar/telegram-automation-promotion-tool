import re
import asyncio
import logging
from typing import List, Optional
from telethon import TelegramClient, functions, types
from telethon.errors import (
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelPrivateError,
    UsernameInvalidError
)
from src.config import settings
from src.models import SourceChannel, Lead
from src.extractor import extract_leads_from_text

logger = logging.getLogger(__name__)

# Patterns for extracting username or invite hash
INVITE_HASH_REGEX = re.compile(r'(?:t\.me/\+|t\.me/joinchat/)([a-zA-Z0-9_\-]+)', re.IGNORECASE)
PUBLIC_USERNAME_REGEX = re.compile(r'(?:t\.me/|@)([a-zA-Z0-9_]{4,32})', re.IGNORECASE)


class TelegramBioScanner:
    def __init__(self):
        self.api_id = settings.telegram_api_id
        self.api_hash = settings.telegram_api_hash
        self.session_name = settings.telegram_session_name
        self.client: Optional[TelegramClient] = None

    async def start(self):
        """Initializes and connects the Telethon MTProto client."""
        if not self.api_id or not self.api_hash:
            logger.warning(
                "[Scanner] TELEGRAM_API_ID or TELEGRAM_API_HASH not configured in .env! "
                "Telegram bio scanning will be skipped until credentials are provided."
            )
            return

        logger.info("[Scanner] Initializing Telegram MTProto client (Session: %s)...", self.session_name)
        self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
        await self.client.connect()

        if not await self.client.is_user_authorized():
            logger.warning(
                "[Scanner] Telegram client is not authorized. "
                "Run 'python src/main.py' interactively once to complete the SMS/code authorization."
            )
        else:
            me = await self.client.get_me()
            logger.info("[Scanner] Telegram MTProto authorized as: @%s (ID: %s)", me.username, me.id)

    async def stop(self):
        """Disconnects the Telethon client."""
        if self.client and self.client.is_connected():
            await self.client.disconnect()
            logger.info("[Scanner] Telegram client disconnected.")

    async def scan_source_for_leads(self, source: SourceChannel) -> List[Lead]:
        """
        Inspects a source channel's bio/description using Telegram MTProto and extracts leads.
        """
        client = self.client
        disconnect_custom = False

        if not client or not client.is_connected():
            from src.keyword_search_engine import KeywordSearchEngine
            client = await KeywordSearchEngine.get_authorized_client()
            disconnect_custom = True

        if not client:
            logger.warning("[Scanner] No authorized Telegram account available to scan source: %s", source.name)
            return []

        invite_link = source.inviteLink or ""
        bio_text: str = ""

        try:
            # Case 1: Private invite link with hash (e.g., https://t.me/+Hash or /joinchat/Hash)
            hash_match = INVITE_HASH_REGEX.search(invite_link)
            if hash_match:
                invite_hash = hash_match.group(1)
                logger.info("[Scanner] Inspecting private invite link hash '%s' for '%s'...", invite_hash, source.name)
                try:
                    invite_info = await client(functions.messages.CheckChatInviteRequest(invite_hash))
                    if isinstance(invite_info, types.ChatInvite):
                        bio_text = getattr(invite_info, "about", "") or ""
                    elif isinstance(invite_info, types.ChatInviteAlready):
                        # Already joined, fetch full entity
                        entity = invite_info.chat
                        full = await client(functions.channels.GetFullChannelRequest(entity))
                        bio_text = full.full_chat.about or ""
                except (InviteHashExpiredError, InviteHashInvalidError) as e:
                    logger.warning("[Scanner] Invalid or expired invite link for '%s': %s", source.name, e)
                    return []

            # Case 2: Public channel username or link (e.g., https://t.me/deals_channel or @deals_channel)
            else:
                user_match = PUBLIC_USERNAME_REGEX.search(invite_link)
                target = user_match.group(1) if user_match else (source.chatId or "")
                if target:
                    logger.info("[Scanner] Resolving public entity '%s' for '%s'...", target, source.name)
                    entity = await client.get_entity(target)
                    full = await client(functions.channels.GetFullChannelRequest(entity))
                    bio_text = full.full_chat.about or ""

            if not bio_text:
                logger.info("[Scanner] No bio/about found in channel: %s", source.name)
                return []

            logger.info("[Scanner] Fetched bio for '%s' (%d chars). Parsing leads...", source.name, len(bio_text))

            # Extract leads (users & bots) from bio text
            leads = extract_leads_from_text(
                bio_text=bio_text,
                source_name=source.name or "Unknown Channel",
                source_invite_link=invite_link,
                source_id=source.id or ""
            )

            logger.info("[Scanner] Extracted %d lead(s) from '%s'", len(leads), source.name)
            return leads

        except FloodWaitError as e:
            logger.warning("[Scanner] Telegram FloodWait hit! Need to wait %d seconds.", e.seconds)
            await asyncio.sleep(e.seconds + 2)
            return []
        except (ChannelPrivateError, UsernameInvalidError) as e:
            logger.warning("[Scanner] Could not access channel '%s': %s", source.name, e)
            return []
        except Exception as e:
            logger.exception("[Scanner] Error scanning channel '%s': %s", source.name, e)
            return []
        finally:
            if disconnect_custom and client and client.is_connected():
                await client.disconnect()
