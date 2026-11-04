"""
Bot Handshake & Anti-Spam / Language Selection Interactor.

Before sending promotional deal posts to target deal userbots:
1. Sends an initial greeting ("Hi").
2. Waits 2.5 - 3.0 seconds for the bot's automated reply.
3. Detects if the bot sent language options (e.g. "English", "🇺🇸 English") or anti-spam buttons ("Continue", "Start", "Confirm").
4. Automatically clicks the English / confirmation button to unlock the bot session.
5. If no language selection or button is received, immediately proceeds with the direct promotional post.
"""

import asyncio
import logging
import re
from typing import Any, Optional
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UserIsBlockedError,
)

logger = logging.getLogger("telegram_promotion.bot_interactor")

# Regex patterns for preferred English language buttons
LANGUAGE_PATTERNS = [
    re.compile(r'\benglish\b', re.IGNORECASE),
    re.compile(r'🇺🇸|🇬🇧|\ben\b', re.IGNORECASE),
    re.compile(r'\beng\b', re.IGNORECASE),
]

# Regex patterns for anti-spam confirmation / continue buttons
CONFIRMATION_PATTERNS = [
    re.compile(r'\bcontinue\b', re.IGNORECASE),
    re.compile(r'\bstart\b', re.IGNORECASE),
    re.compile(r'\baccept\b', re.IGNORECASE),
    re.compile(r'\bagree\b', re.IGNORECASE),
    re.compile(r'\bconfirm\b', re.IGNORECASE),
    re.compile(r'\bproceed\b', re.IGNORECASE),
    re.compile(r'\byes\b', re.IGNORECASE),
    re.compile(r'\bi agree\b', re.IGNORECASE),
    re.compile(r'\bdone\b', re.IGNORECASE),
]

# Text cues in message body indicating a language prompt
LANGUAGE_BODY_CUES = [
    re.compile(r'choose.*language', re.IGNORECASE),
    re.compile(r'select.*language', re.IGNORECASE),
    re.compile(r'please.*language', re.IGNORECASE),
    re.compile(r'language:', re.IGNORECASE),
    re.compile(r'язык', re.IGNORECASE),   # Russian: language
    re.compile(r'idioma', re.IGNORECASE), # Spanish/Portuguese: language
]


async def prepare_bot_for_promo(
    client: TelegramClient,
    bot_entity: Any,
    bot_handle: str = "",
    greeting: str = "Hi",
    wait_seconds: float = 2.5
) -> bool:
    """
    Executes pre-promo handshake with a deal userbot:
    - Sends greeting ("Hi").
    - If bot presents language selection or anti-spam barrier, auto-clicks English/confirmation.
    - If no language option is received, proceeds directly without delay.
    """
    display_name = bot_handle or getattr(bot_entity, "username", "") or "target bot"
    if not str(display_name).startswith("@"):
        display_name = f"@{display_name}"

    try:
        logger.info("👋 [BotHandshake] Sending initial greeting '%s' to %s...", greeting, display_name)
        await client.send_message(bot_entity, greeting)

        # Wait briefly for bot's automated reply
        await asyncio.sleep(wait_seconds)

        # Fetch recent incoming messages from the bot
        messages = await client.get_messages(bot_entity, limit=2)
        if not messages:
            logger.info("ℹ️ [BotHandshake] No reply received from %s after '%s'. Proceeding with direct promo.", display_name, greeting)
            return True

        latest_msg = messages[0]
        # Ensure message is from the bot (out=False)
        if getattr(latest_msg, "out", False):
            logger.info("ℹ️ [BotHandshake] No bot reply message detected. Proceeding directly.")
            return True

        msg_text = (getattr(latest_msg, "text", "") or "").strip()
        buttons = getattr(latest_msg, "buttons", None)

        if not buttons:
            logger.info("ℹ️ [BotHandshake] %s replied without buttons: '%s'. Proceeding with direct promo.", display_name, msg_text[:60])
            return True

        # Check for language or confirmation buttons
        target_coords = None
        target_btn_text = ""

        # Priority 1: Direct English language button
        for i, row in enumerate(buttons):
            for j, btn in enumerate(row):
                btn_txt = (getattr(btn, "text", "") or "").strip()
                if not btn_txt:
                    continue
                # Don't click external URL links
                if getattr(btn, "url", None):
                    continue

                for pat in LANGUAGE_PATTERNS:
                    if pat.search(btn_txt):
                        target_coords = (i, j)
                        target_btn_text = btn_txt
                        break
                if target_coords:
                    break
            if target_coords:
                break

        # Priority 2: Confirmation / Continue button
        if not target_coords:
            for i, row in enumerate(buttons):
                for j, btn in enumerate(row):
                    btn_txt = (getattr(btn, "text", "") or "").strip()
                    if not btn_txt or getattr(btn, "url", None):
                        continue
                    for pat in CONFIRMATION_PATTERNS:
                        if pat.search(btn_txt):
                            target_coords = (i, j)
                            target_btn_text = btn_txt
                            break
                    if target_coords:
                        break
                if target_coords:
                    break

        # Priority 3: If message body asks to choose language, pick first non-URL button
        if not target_coords:
            is_language_prompt = any(pat.search(msg_text) for pat in LANGUAGE_BODY_CUES)
            if is_language_prompt and buttons and len(buttons) > 0 and len(buttons[0]) > 0:
                first_btn = buttons[0][0]
                if not getattr(first_btn, "url", None):
                    target_coords = (0, 0)
                    target_btn_text = getattr(first_btn, "text", "") or "Default Option"

        if target_coords:
            row_idx, col_idx = target_coords
            logger.info("🔘 [BotHandshake] Found interactive button '%s' on %s. Auto-clicking...", target_btn_text, display_name)
            try:
                await latest_msg.click(row_idx, col_idx)
                logger.info("✅ [BotHandshake] Successfully clicked '%s' on %s. Language/barrier cleared!", target_btn_text, display_name)
                # Brief pause for bot to register choice before promotional post
                await asyncio.sleep(1.5)
            except Exception as click_err:
                logger.warning("⚠️ [BotHandshake] Could not click button '%s' on %s: %s. Continuing with promo.", target_btn_text, display_name, click_err)
        else:
            logger.info("ℹ️ [BotHandshake] No language/anti-spam option matched on %s. Proceeding with direct promo.", display_name)

        return True

    except FloodWaitError as e:
        logger.warning("⏳ [BotHandshake] FloodWait of %ds on %s.", e.seconds, display_name)
        await asyncio.sleep(min(e.seconds, 10))
        return False
    except Exception as e:
        logger.warning("⚠️ [BotHandshake] Handshake error with %s: %s. Will attempt direct promo.", display_name, e)
        return True

