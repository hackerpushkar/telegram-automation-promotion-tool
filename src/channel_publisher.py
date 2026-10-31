"""
Channel Post Publisher for ProAffiliaters Campaign Kit.

Publishes promotional deal posts (HD Banner + Smart Ad Text + Deep-link Inline Buttons)
to the official channel (@pro_affiliaters) via Telegram Bot API.
The inline buttons include a direct deep-link that auto-launches @ProaffiliatersInfoBot,
registering any clickers as broadcastable leads in MongoDB.
"""

import os
import json
import logging
from typing import Optional, Dict, Any, List
import httpx

from src.config import settings
from src.db import Database

logger = logging.getLogger("ChannelPublisher")

BASE_PROMO_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "promotion images and text")
)

POST_ORDINAL_MAP = {
    1: ("1st Image.png", "1st Text.txt", "Automate Your Telegram Deal Channels"),
    2: ("2nd Image.png", "2nd Text.txt", "24/7 Hands-Free AutoPilot"),
    3: ("3rd Image.png", "3rd Text.txt", "Custom Templates & Visual Branding"),
    4: ("4th Image.png", "4th Text.txt", "Multi-Template Engine & Rotation"),
    5: ("5th Image.png", "5th Text.txt", "1-Click Quick Post & Auto-Delete"),
}


def get_promo_post_preview(post_number: int) -> Dict[str, Any]:
    """Reads banner file paths, caption text, and deep link for a post number (1 to 5)."""
    if post_number not in POST_ORDINAL_MAP:
        raise ValueError(f"Invalid post_number {post_number}. Must be between 1 and 5.")

    img_name, txt_name, title = POST_ORDINAL_MAP[post_number]
    img_path = os.path.join(BASE_PROMO_DIR, img_name)
    txt_path = os.path.join(BASE_PROMO_DIR, txt_name)

    caption = ""
    if os.path.exists(txt_path):
        with open(txt_path, "r", encoding="utf-8") as f:
            caption = f.read().strip()

    info_bot_username = settings.telegram_info_bot_username or "ProaffiliatersInfoBot"
    deep_link = f"https://t.me/{info_bot_username}?start=promo_post{post_number}"

    return {
        "post_number": post_number,
        "title": title,
        "image_name": img_name,
        "image_path": img_path,
        "image_exists": os.path.exists(img_path),
        "text_file": txt_name,
        "caption": caption,
        "deep_link": deep_link,
        "inline_buttons": [
            [{"text": "🤖 Start Bot / Learn More", "url": deep_link}]
        ]
    }


async def publish_promo_post(
    post_number: int,
    channel_username: Optional[str] = None
) -> Dict[str, Any]:
    """
    Publishes a single promotional post to the specified Telegram channel.
    Uses Telegram Bot API sendPhoto with inline buttons.
    """
    token = settings.telegram_info_bot_token
    if not token:
        raise ValueError("TELEGRAM_INFO_BOT_TOKEN is not configured in .env!")

    target_channel = (channel_username or settings.promo_channel_username).strip()
    if not target_channel.startswith("@") and not target_channel.startswith("-100"):
        target_channel = f"@{target_channel}"

    preview = get_promo_post_preview(post_number)
    img_path = preview["image_path"]

    if not os.path.exists(img_path):
        raise FileNotFoundError(f"Promotional banner not found: {img_path}")

    # Build inline keyboard JSON
    reply_markup = {
        "inline_keyboard": preview["inline_buttons"]
    }

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    raw_caption = preview["caption"]

    # Convert markdown **bold** and [link](url) to HTML for robust rendering without parse errors
    import re
    html_caption = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', raw_caption)
    html_caption = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'<a href="\2">\1</a>', html_caption)

    logger.info("Publishing Post #%d to channel '%s'...", post_number, target_channel)

    async with httpx.AsyncClient(timeout=45.0) as client:
        with open(img_path, "rb") as photo_file:
            files = {"photo": (preview["image_name"], photo_file, "image/png")}
            data = {
                "chat_id": target_channel,
                "caption": html_caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup)
            }

            resp = await client.post(url, data=data, files=files)
            res_json = resp.json()

            # Fallback to plain text if HTML parse error occurred
            if not res_json.get("ok"):
                logger.warning("Telegram parse error (%s). Retrying in plain text...", res_json.get("description"))
                photo_file.seek(0)
                data["caption"] = raw_caption
                data.pop("parse_mode", None)
                resp = await client.post(url, data=data, files=files)
                res_json = resp.json()

    if not res_json.get("ok"):
        error_msg = res_json.get("description", "Failed to send photo to Telegram channel")
        logger.error("Failed to publish Post #%d to %s: %s", post_number, target_channel, error_msg)
        raise RuntimeError(f"Telegram API Error: {error_msg}")

    result = res_json["result"]
    message_id = result["message_id"]

    clean_channel = target_channel.lstrip("@")
    message_link = f"https://t.me/{clean_channel}/{message_id}"

    record = {
        "post_number": post_number,
        "title": preview["title"],
        "channel_username": target_channel,
        "message_id": message_id,
        "message_link": message_link,
        "image_name": preview["image_name"],
        "caption": raw_caption,
        "deep_link": preview["deep_link"],
        "inline_buttons": [
            {"text": b["text"], "url": b["url"]}
            for row in preview["inline_buttons"]
            for b in row
        ]
    }

    # Save to MongoDB
    await Database.save_published_post(record)
    logger.info("✅ Post #%d successfully published to %s! Message Link: %s", post_number, target_channel, message_link)

    return record


async def publish_all_promo_posts(channel_username: Optional[str] = None) -> List[Dict[str, Any]]:
    """Publishes all 5 promotional posts in sequence to the channel."""
    results = []
    for num in range(1, 6):
        try:
            res = await publish_promo_post(num, channel_username)
            results.append(res)
            await asyncio.sleep(2.0)
        except Exception as e:
            logger.error("Failed to publish Post #%d: %s", num, e)
            results.append({"post_number": num, "error": str(e)})
    return results


async def edit_promo_post_caption(
    post_number: int,
    channel_username: Optional[str] = None
) -> Dict[str, Any]:
    """
    Edits the caption and buttons of an already published promotional post on Telegram.
    Reads the latest text from the promotional text file and syncs it live.
    """
    token = settings.telegram_info_bot_token
    if not token:
        raise ValueError("TELEGRAM_INFO_BOT_TOKEN is not configured in .env!")

    target_channel = (channel_username or settings.promo_channel_username).strip()
    if not target_channel.startswith("@") and not target_channel.startswith("-100"):
        target_channel = f"@{target_channel}"

    preview = get_promo_post_preview(post_number)
    saved = await Database.get_published_post(post_number)
    if not saved or not saved.get("message_id"):
        raise ValueError(f"No published message found for Post #{post_number} in database.")

    message_id = saved["message_id"]
    reply_markup = {"inline_keyboard": preview["inline_buttons"]}
    url = f"https://api.telegram.org/bot{token}/editMessageCaption"

    raw_caption = preview["caption"]
    import re
    html_caption = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', raw_caption)
    html_caption = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'<a href="\2">\1</a>', html_caption)

    logger.info("Editing Post #%d (Message ID %d) in channel '%s'...", post_number, message_id, target_channel)

    async with httpx.AsyncClient(timeout=30.0) as client:
        payload = {
            "chat_id": target_channel,
            "message_id": message_id,
            "caption": html_caption,
            "parse_mode": "HTML",
            "reply_markup": reply_markup
        }
        resp = await client.post(url, json=payload)
        res_json = resp.json()

        # Fallback to plain text if HTML error
        if not res_json.get("ok"):
            logger.warning("Telegram parse error on edit (%s). Retrying in plain text...", res_json.get("description"))
            payload["caption"] = raw_caption
            payload.pop("parse_mode", None)
            resp = await client.post(url, json=payload)
            res_json = resp.json()

    if not res_json.get("ok"):
        error_msg = res_json.get("description", "Failed to edit caption")
        logger.error("Failed to edit Post #%d: %s", post_number, error_msg)
        raise RuntimeError(f"Telegram API Edit Error: {error_msg}")

    # Update database record
    saved["caption"] = raw_caption
    saved["inline_buttons"] = [
        {"text": b["text"], "url": b["url"]}
        for row in preview["inline_buttons"]
        for b in row
    ]
    await Database.save_published_post(saved)
    logger.info("✅ Post #%d caption successfully updated on Telegram & database!", post_number)
    return saved

