"""
Official ProAffiliaters Info & Ad Landing Telegram Bot (@ProaffiliatersInfoBot) Service.

Ported from Web Application to Telegram Promotion Tool engine.
Captures user leads from ad campaigns (/start <campaign_name>),
presents rich interactive feature tours via Telegram inline keyboards,
and provides an Admin Command Console for lead analytics and WYSIWYG broadcasting.
"""

import asyncio
import html
import logging
import re
from datetime import datetime
from typing import Optional, Dict, Any, List

import httpx

from src.config import settings
from src.db import Database

logger = logging.getLogger("TelegramInfoBot")


def escape_html(text: str) -> str:
    """Escapes HTML characters for Telegram HTML parse_mode."""
    if not text:
        return ""
    return html.escape(str(text))


def parse_inline_buttons_markup(raw_text: str) -> List[List[Dict[str, str]]]:
    """
    Parses custom inline button markup: e.g. [Button Title | https://example.com] [Channel | t.me/proaffiliaters]
    Returns a 2D array formatted for Telegram's inline_keyboard payload.
    """
    if not raw_text or not isinstance(raw_text, str):
        return []

    regex = r"\[([^\|\]]+)\|([^\]]+)\]"
    matches = re.findall(regex, raw_text)
    buttons: List[Dict[str, str]] = []

    for title, url_raw in matches:
        title = title.strip()
        url = url_raw.strip()
        if title and url:
            if not url.startswith(("http://", "https://", "tg://")):
                url = f"https://{url}"
            buttons.append({"text": title, "url": url})

    # Group into rows of 2 buttons for optimal mobile viewport
    rows: List[List[Dict[str, str]]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    return rows


class TelegramInfoBot:
    def __init__(self):
        self.token: Optional[str] = settings.telegram_info_bot_token
        self.admin_id: str = str(settings.telegram_info_bot_admin_id).strip()
        self.username: str = settings.telegram_info_bot_username
        self.webapp_url: str = settings.webapp_url
        self.official_channel_url: str = settings.official_channel_url
        self.support_bot_url: str = settings.support_bot_url

        self.is_polling: bool = False
        self.last_update_id: int = 0
        self._polling_task: Optional[asyncio.Task] = None
        self._http_client: Optional[httpx.AsyncClient] = None

        # Admin broadcast state wizard keyed by chat_id
        self._admin_broadcast_states: Dict[str, Dict[str, Any]] = {}

    def is_admin(self, chat_id: Any) -> bool:
        """Verifies if the specified chat ID matches the configured admin."""
        if not chat_id:
            return False
        return str(chat_id).strip() == self.admin_id

    async def _get_client(self) -> httpx.AsyncClient:
        """Returns or initializes singleton AsyncClient."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=35.0)
        return self._http_client

    async def call_api(self, method: str, body: Optional[Dict[str, Any]] = None) -> Any:
        """Dispatches an HTTP POST request to Telegram Bot API."""
        if not self.token:
            raise ValueError("TELEGRAM_INFO_BOT_TOKEN is not configured")

        client = await self._get_client()
        url = f"https://api.telegram.org/bot{self.token}/{method}"

        response = await client.post(url, json=body or {})
        data = response.json()

        if not data.get("ok"):
            error_desc = data.get("description", f"Telegram API error on {method}")
            error_code = data.get("error_code")
            logger.warning("[TelegramInfoBot] API Error on %s: %s (code: %s)", method, error_desc, error_code)
            exc = RuntimeError(error_desc)
            setattr(exc, "error_code", error_code)
            raise exc

        return data.get("result")

    def get_main_welcome_payload(self, user_name: str, chat_id: str) -> Dict[str, Any]:
        """Generates the rich ProAffiliaters landing menu with interactive buttons."""
        admin_flag = self.is_admin(chat_id)
        name_clean = escape_html(user_name or "Friend")

        text = (
            f"✨ <b>Welcome to ProAffiliaters, {name_clean}!</b> 🚀\n\n"
            f"<i>The Ultimate All-in-One Automation Ecosystem for E-Commerce & Deal Curators.</i>\n\n"
            f"⚡ <b>What is ProAffiliaters?</b>\n"
            f"ProAffiliaters fully automates your entire affiliate business — from discovering top deals, "
            f"replacing competitor affiliate tags, scraping live prices, to broadcasting across Telegram, "
            f"WhatsApp, and WordPress 24/7 without lifting a finger.\n\n"
            f"🔥 <b>Core Superpowers:</b>\n"
            f"• <b>Telegram & WhatsApp Forwarder</b> — Auto-copy deals with instant tag swapping.\n"
            f"• <b>24/7 AutoPilot</b> — Scheduled automated deal distribution while you sleep.\n"
            f"• <b>Trending Deals Engine</b> — Live verified 50%–90% price drops across Amazon & Flipkart.\n"
            f"• <b>WordPress Auto-Publisher</b> — Instant WooCommerce products & deal blog articles.\n"
            f"• <b>Smart Link Cloaker</b> — In-house shortener & multi-token rotation.\n\n"
            f"👇 <b>Explore features or launch your automated deals channel below:</b>"
        )

        inline_keyboard = [
            [
                {"text": "⚡ Auto Forwarder", "callback_data": "info_forwarder"},
                {"text": "🚀 24/7 AutoPilot", "callback_data": "info_autopilot"},
            ],
            [
                {"text": "🔥 Trending Deals", "callback_data": "info_deals"},
                {"text": "🌐 WordPress Engine", "callback_data": "info_wordpress"},
            ],
            [
                {"text": "💎 Plans & Pricing", "callback_data": "info_pricing"},
                {"text": "🎥 Step-by-Step Blueprint", "callback_data": "info_blueprint"},
            ],
            [
                {"text": "🌐 Launch Web Dashboard", "url": self.webapp_url},
            ],
            [
                {"text": "📢 Official Channel", "url": self.official_channel_url},
                {"text": "💬 1-on-1 Human Support", "url": self.support_bot_url},
            ],
        ]

        if admin_flag:
            inline_keyboard.append([
                {"text": "👑 Admin Command Console", "callback_data": "admin_menu"}
            ])

        return {"text": text, "reply_markup": {"inline_keyboard": inline_keyboard}}

    async def handle_callback_query(self, callback_query: Dict[str, Any]):
        """Processes inline button clicks with interactive message navigation."""
        query_id = callback_query.get("id")
        message = callback_query.get("message")
        chat_id = str(message.get("chat", {}).get("id")) if message else ""
        data = callback_query.get("data", "")
        message_id = message.get("message_id") if message else None

        # Acknowledge callback immediately to eliminate Telegram spinner
        try:
            await self.call_api("answerCallbackQuery", {"callback_query_id": query_id})
        except Exception:
            pass

        if not chat_id or not message_id:
            return

        back_button = [{"text": "🔙 Back to Main Menu", "callback_data": "info_main"}]
        response_text = ""
        keyboard: List[List[Dict[str, Any]]] = []

        if data == "info_main":
            first_name = callback_query.get("from", {}).get("first_name", "Friend")
            payload = self.get_main_welcome_payload(first_name, chat_id)
            try:
                await self.call_api("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": payload["text"],
                    "parse_mode": "HTML",
                    "reply_markup": payload["reply_markup"],
                    "disable_web_page_preview": True,
                })
            except Exception as e:
                logger.debug("Failed to edit main menu: %s", e)
            return

        elif data == "info_forwarder":
            response_text = (
                "⚡ <b>Telegram & WhatsApp Auto Forwarder</b>\n\n"
                "Never copy-paste deals manually again! Our forwarder engine monitors your selected deal channels 24/7:\n\n"
                "✅ <b>Instant Tag Replacement</b>: Automatically swaps other creators' affiliate tags with your personal Amazon & Flipkart tags.\n"
                "✅ <b>Cross-Platform Delivery</b>: Simultaneously forward deals from Telegram source channels into both your private Telegram channels and WhatsApp groups.\n"
                "✅ <b>Custom Templates & Branding</b>: Tailor post layouts with placeholders like <code>{title}</code>, <code>{price}</code>, <code>{discount}</code>, and <code>{link}</code>.\n"
                "✅ <b>High-Res Media Attachment</b>: Clean retailer image preservation without watermark clutter.\n"
                "✅ <b>Out-of-Stock Protection</b>: Skips dead deals and auto-deletes expired posts."
            )
            keyboard = [
                [{"text": "🚀 Open Web Dashboard", "url": f"{self.webapp_url}/bots"}],
                back_button,
            ]

        elif data == "info_autopilot":
            response_text = (
                "🚀 <b>24/7 Hands-Free AutoPilot Engine</b>\n\n"
                "Run your affiliate business 100% on autopilot without monitoring or forwarding from source channels!\n\n"
                "🤖 <b>How It Works:</b>\n"
                "• <b>Automated Discovery</b>: Sifts through thousands of deals daily across 15+ shopping categories.\n"
                "• <b>Configurable Schedules</b>: Set posting intervals (e.g. every 15, 30, or 60 minutes) and active time windows.\n"
                "• <b>Multi-Platform Broadcasting</b>: Delivers high-converting deals directly to Telegram channels, WhatsApp groups, and WordPress blogs simultaneously.\n"
                "• <b>Quality & Rating Filters</b>: Only posts products meeting your minimum star rating (e.g. 4.0+) and discount thresholds."
            )
            keyboard = [
                [{"text": "⚙️ Configure AutoPilot", "url": f"{self.webapp_url}/autopilot"}],
                back_button,
            ]

        elif data == "info_deals":
            response_text = (
                "🔥 <b>Trending Deals & Live Price Verifier</b>\n\n"
                "Find real discounts, not fake inflated MRPs!\n\n"
                "📊 <b>Data-Driven Relative Benchmarking:</b>\n"
                "• Evaluates products over a rolling 14-day observation window.\n"
                "• Detects statistical discount outliers (Fire Deals ≥ 10% above category average).\n"
                "• <b>30-Minute Live Verifier</b>: Continuously re-checks Amazon & Flipkart to guarantee that all listed deals hold genuine active discounts and live stock.\n"
                "• <b>1-Click Sharing</b>: Curate top deals directly to your community with a single tap."
            )
            keyboard = [
                [{"text": "🔥 View Live Trending Deals", "url": f"{self.webapp_url}/trending-deals"}],
                back_button,
            ]

        elif data == "info_wordpress":
            response_text = (
                "🌐 <b>WordPress & WooCommerce Auto-Publishing</b>\n\n"
                "Turn your WordPress website into an automated affiliate earning machine!\n\n"
                "📝 <b>Features:</b>\n"
                "• <b>Dual Post Types</b>: Publish deals as WooCommerce Products or standard formatted Blog Articles.\n"
                "• <b>Automated Taxonomy</b>: Categorizes deals automatically into Electronics, Fashion, Home, etc.\n"
                "• <b>Clean Affiliate Redirects</b>: Integrates with your site's buy buttons pre-tagged with your associate credentials.\n"
                "• <b>Live Stock Sync</b>: Keeps pricing and availability updated."
            )
            keyboard = [
                [{"text": "🌐 Connect WordPress Site", "url": f"{self.webapp_url}/wordpress"}],
                back_button,
            ]

        elif data == "info_pricing":
            response_text = (
                "💎 <b>Transparent Plans & Pricing</b>\n\n"
                "Scale your affiliate business with plans built for creators at every stage:\n\n"
                "🌱 <b>Startup Plan (₹499/mo)</b>\n"
                "• 1 Telegram Forwarder + 1 WhatsApp Forwarder\n"
                "• Post Customization & Tag Replacement\n"
                "• Basic Trending Deals Access\n\n"
                "⭐ <b>Pro Plan (₹999/mo)</b>\n"
                "• 3 Telegram + 3 WhatsApp Forwarders\n"
                "• 24/7 Telegram AutoPilot Engine\n"
                "• WordPress Auto-Publishing\n\n"
                "🚀 <b>Pro Plus Plan (₹1,999/mo)</b>\n"
                "• 10 Telegram + 10 WhatsApp Forwarders\n"
                "• Full WhatsApp + WordPress 24/7 AutoPilot\n"
                "• Multi-Template Rotation & Priority Scraping\n\n"
                "👑 <b>Enterprise Plan (Custom)</b>\n"
                "• Unlimited forwarders, channels, and dedicated server resources."
            )
            keyboard = [
                [{"text": "💳 Choose Your Plan & Activate", "url": f"{self.webapp_url}/payment"}],
                back_button,
            ]

        elif data == "info_blueprint":
            response_text = (
                "🎥 <b>Interactive Step-by-Step Blueprint</b>\n\n"
                "Set up your complete automated deal channel in under 3 minutes!\n\n"
                "1️⃣ <b>Step 1</b>: Add your Amazon & Flipkart affiliate tags in the dashboard.\n"
                "2️⃣ <b>Step 2</b>: Connect your Telegram userbot or bot token.\n"
                "3️⃣ <b>Step 3</b>: Select your deal source channels and your destination channel.\n"
                "4️⃣ <b>Step 4</b>: Choose a custom post template and turn on Forwarding!\n\n"
                "💡 <i>Our interactive workflow dashboard guides you step-by-step with real-time status checkmarks.</i>"
            )
            keyboard = [
                [{"text": "🧭 Open Setup Blueprint", "url": f"{self.webapp_url}/workflow"}],
                back_button,
            ]

        # --- Admin Console Callbacks ---
        elif data == "admin_menu":
            if not self.is_admin(chat_id):
                return
            response_text = (
                f"👑 <b>ProAffiliaters Info Bot Admin Console</b>\n\n"
                f"Admin ID: <code>{chat_id}</code>\n\n"
                f"Select an administrative action below:"
            )
            keyboard = [
                [
                    {"text": "📊 Real-Time Analytics", "callback_data": "admin_stats"},
                    {"text": "👥 Recent Users", "callback_data": "admin_users"},
                ],
                [
                    {"text": "📢 Send Broadcast Message", "callback_data": "admin_start_broadcast"},
                    {"text": "📜 Broadcast History", "callback_data": "admin_history"},
                ],
                back_button,
            ]

        elif data == "admin_stats":
            if not self.is_admin(chat_id):
                return
            stats = await Database.get_info_bot_stats()
            response_text = (
                f"📊 <b>Info Bot Real-Time Analytics</b>\n\n"
                f"• <b>Total Registered Leads:</b> <code>{stats.get('total_leads', 0)}</code>\n"
                f"• <b>Active in Last 24 Hours:</b> <code>{stats.get('active_24h', 0)}</code>\n"
                f"• <b>Blocked / Inactive:</b> <code>{stats.get('blocked_leads', 0)}</code>\n"
                f"• <b>Total Broadcasts Dispatched:</b> <code>{stats.get('broadcasts_count', 0)}</code>\n\n"
                f"<i>Data synchronized live from MongoDB.</i>"
            )
            keyboard = [
                [{"text": "🔄 Refresh Analytics", "callback_data": "admin_stats"}],
                [{"text": "👑 Back to Admin Menu", "callback_data": "admin_menu"}],
                back_button,
            ]

        elif data == "admin_users":
            if not self.is_admin(chat_id):
                return
            recent_users = await Database.get_info_bot_users(limit=10)
            user_lines = []
            for i, u in enumerate(recent_users, 1):
                name = " ".join(filter(None, [u.get("first_name"), u.get("last_name")])) or "Unnamed"
                handle = f"@{u['username']}" if u.get("username") else "No handle"
                ref = f"(Ad: <code>{escape_html(u.get('start_payload'))}</code>)" if u.get("start_payload") else "(Organic)"
                user_lines.append(f"<b>{i}.</b> {escape_html(name)} — {handle}\n   ID: <code>{u.get('telegram_chat_id')}</code> {ref}")

            lines_str = "\n\n".join(user_lines) if user_lines else "No users registered yet."
            response_text = f"👥 <b>Latest 10 Users / Leads:</b>\n\n{lines_str}"
            keyboard = [
                [{"text": "🔄 Refresh Users", "callback_data": "admin_users"}],
                [{"text": "👑 Back to Admin Menu", "callback_data": "admin_menu"}],
                back_button,
            ]

        elif data == "admin_start_broadcast":
            if not self.is_admin(chat_id):
                return
            self._admin_broadcast_states[chat_id] = {"step": "awaiting_message"}
            response_text = (
                "📢 <b>Create New Broadcast</b>\n\n"
                "Please send the <b>message text</b> (or a photo with a caption) that you want to broadcast to all Info Bot leads.\n\n"
                "💡 <i>You can use HTML formatting like &lt;b&gt;bold&lt;/b&gt; and &lt;i&gt;italic&lt;/i&gt;.</i>\n\n"
                "Send /cancel anytime to abort."
            )
            keyboard = [[{"text": "❌ Cancel Broadcast", "callback_data": "admin_cancel_broadcast"}]]

        elif data == "admin_confirm_broadcast":
            if not self.is_admin(chat_id):
                return
            state = self._admin_broadcast_states.get(chat_id)
            if not state or not state.get("text"):
                response_text = "⚠️ Broadcast session expired or no message found. Please restart."
                keyboard = [[{"text": "👑 Admin Menu", "callback_data": "admin_menu"}]]
            else:
                asyncio.create_task(self.execute_broadcast(chat_id, state))
                self._admin_broadcast_states.pop(chat_id, None)
                response_text = (
                    "🚀 <b>Broadcast Dispatched!</b>\n\n"
                    "Sending messages in the background with rate-limiting. "
                    "A detailed delivery report will be sent here upon completion."
                )
                keyboard = [[{"text": "👑 Admin Menu", "callback_data": "admin_menu"}]]

        elif data == "admin_cancel_broadcast":
            if not self.is_admin(chat_id):
                return
            self._admin_broadcast_states.pop(chat_id, None)
            response_text = "❌ Broadcast wizard cancelled."
            keyboard = [[{"text": "👑 Admin Menu", "callback_data": "admin_menu"}]]

        elif data == "admin_history":
            if not self.is_admin(chat_id):
                return
            history = await Database.get_info_bot_broadcasts(limit=5)
            history_lines = []
            for i, b in enumerate(history, 1):
                created_dt = b.get("created_at")
                date_str = created_dt.strftime("%Y-%m-%d %H:%M") if isinstance(created_dt, datetime) else str(created_dt)[:16]
                preview = (b.get("message_text", "")[:40]).replace("\n", " ")
                btns_count = len(b.get("inline_buttons", []))
                history_lines.append(
                    f"<b>{i}. [{date_str}]</b>\n"
                    f"   \"{escape_html(preview)}...\"\n"
                    f"   ✅ Sent: <b>{b.get('successful_sent', 0)}</b> | ❌ Failed: <b>{b.get('failed_count', 0)}</b> | 🔘 Buttons: <b>{btns_count}</b>"
                )

            lines_str = "\n\n".join(history_lines) if history_lines else "No previous broadcasts found."
            response_text = f"📜 <b>Recent Broadcast History:</b>\n\n{lines_str}"
            keyboard = [
                [{"text": "👑 Back to Admin Menu", "callback_data": "admin_menu"}],
                back_button,
            ]
        else:
            return

        try:
            await self.call_api("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": response_text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": keyboard},
                "disable_web_page_preview": True,
            })
        except Exception:
            # Fallback to sending a new message if editing fails
            await self.call_api("sendMessage", {
                "chat_id": chat_id,
                "text": response_text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": keyboard},
                "disable_web_page_preview": True,
            })

    async def handle_message(self, message: Dict[str, Any]):
        """Processes incoming user and admin chat messages."""
        chat_id = str(message.get("chat", {}).get("id"))
        if not chat_id:
            return

        text = (message.get("text") or message.get("caption") or "").strip()
        photos = message.get("photo") or []
        photo_id = photos[-1].get("file_id") if photos else None
        admin_flag = self.is_admin(chat_id)

        # 1. Handle /start [payload]
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            start_payload = parts[1].strip() if len(parts) > 1 else None

            # Upsert user lead into MongoDB
            from_user = message.get("from", {})
            user_data = {
                "telegram_chat_id": chat_id,
                "username": from_user.get("username"),
                "first_name": from_user.get("first_name", ""),
                "last_name": from_user.get("last_name", ""),
                "start_payload": start_payload,
            }
            try:
                is_new, doc = await Database.upsert_info_bot_user(user_data)
                tag_info = f" [Ad: {start_payload}]" if start_payload else " [Organic]"
                if is_new:
                    logger.info("✨ New Info Bot Lead captured: @%s (ID: %s)%s", user_data["username"], chat_id, tag_info)
                else:
                    logger.info("🔁 Info Bot Lead returned: @%s (ID: %s)%s", user_data["username"], chat_id, tag_info)
            except Exception as e:
                logger.error("Error saving info bot user: %s", e)

            welcome = self.get_main_welcome_payload(from_user.get("first_name", "Friend"), chat_id)
            await self.call_api("sendMessage", {
                "chat_id": chat_id,
                "text": welcome["text"],
                "parse_mode": "HTML",
                "reply_markup": welcome["reply_markup"],
                "disable_web_page_preview": True,
            })
            return

        # 2. Handle /admin
        if text == "/admin":
            if not admin_flag:
                await self.call_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": "⛔ <i>You are not authorized to access the admin command console.</i>",
                    "parse_mode": "HTML",
                })
                return

            admin_menu = {
                "text": (
                    f"👑 <b>ProAffiliaters Info Bot Admin Console</b>\n\n"
                    f"Admin ID: <code>{chat_id}</code>\n\n"
                    f"Select an administrative action below:"
                ),
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "📊 Real-Time Analytics", "callback_data": "admin_stats"},
                            {"text": "👥 Recent Users", "callback_data": "admin_users"},
                        ],
                        [
                            {"text": "📢 Send Broadcast Message", "callback_data": "admin_start_broadcast"},
                            {"text": "📜 Broadcast History", "callback_data": "admin_history"},
                        ],
                        [{"text": "🔙 Back to Main Menu", "callback_data": "info_main"}],
                    ]
                }
            }
            await self.call_api("sendMessage", {
                "chat_id": chat_id,
                "text": admin_menu["text"],
                "parse_mode": "HTML",
                "reply_markup": admin_menu["reply_markup"],
            })
            return

        # 3. Handle /cancel
        if text == "/cancel" and admin_flag:
            self._admin_broadcast_states.pop(chat_id, None)
            welcome = self.get_main_welcome_payload("Admin", chat_id)
            await self.call_api("sendMessage", {
                "chat_id": chat_id,
                "text": "❌ Action cancelled. Returning to main menu.",
                "reply_markup": welcome["reply_markup"],
            })
            return

        # 4. Handle Admin Broadcast State Machine
        if admin_flag and chat_id in self._admin_broadcast_states:
            state = self._admin_broadcast_states[chat_id]

            # Step A: Awaiting Message Content
            if state.get("step") == "awaiting_message":
                state["text"] = text
                state["photo_file_id"] = photo_id
                state["step"] = "awaiting_buttons"
                self._admin_broadcast_states[chat_id] = state

                prompt = (
                    "✅ <b>Message Content Received!</b>\n\n"
                    "🔘 <b>Now, do you want to attach inline URL buttons?</b>\n\n"
                    "Send button markup in this format:\n"
                    "<code>[Button Title | https://example.com] [Button 2 | https://example2.com]</code>\n\n"
                    "<i>Or send <code>/skip</code> if you do not want to add buttons.</i>"
                )
                await self.call_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": prompt,
                    "parse_mode": "HTML",
                })
                return

            # Step B: Awaiting Buttons
            if state.get("step") == "awaiting_buttons":
                buttons: List[List[Dict[str, str]]] = []
                if text != "/skip":
                    buttons = parse_inline_buttons_markup(text)
                state["buttons"] = buttons
                state["step"] = "awaiting_confirmation"
                self._admin_broadcast_states[chat_id] = state

                # Render WYSIWYG Preview
                preview_text = f"👁️ <b>PREVIEW OF YOUR BROADCAST:</b>\n\n---\n{state['text']}\n---"
                preview_keyboard = [row for row in buttons]
                preview_keyboard.append([
                    {"text": "🚀 Confirm & Broadcast Now", "callback_data": "admin_confirm_broadcast"},
                    {"text": "❌ Cancel", "callback_data": "admin_cancel_broadcast"},
                ])

                if state.get("photo_file_id"):
                    await self.call_api("sendPhoto", {
                        "chat_id": chat_id,
                        "photo": state["photo_file_id"],
                        "caption": preview_text,
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": preview_keyboard},
                    })
                else:
                    await self.call_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": preview_text,
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": preview_keyboard},
                        "disable_web_page_preview": True,
                    })
                return

        # 5. Default Fallback
        first_name = message.get("from", {}).get("first_name", "Friend")
        welcome = self.get_main_welcome_payload(first_name, chat_id)
        await self.call_api("sendMessage", {
            "chat_id": chat_id,
            "text": welcome["text"],
            "parse_mode": "HTML",
            "reply_markup": welcome["reply_markup"],
            "disable_web_page_preview": True,
        })

    async def execute_broadcast(self, admin_chat_id: str, broadcast_data: Dict[str, Any]):
        """Executes rate-limited broadcast across all registered, active Info Bot leads."""
        try:
            active_users = await Database.get_all_active_info_bot_users()
            total = len(active_users)
            successful_sent = 0
            failed_count = 0

            logger.info("[TelegramInfoBot] Starting broadcast to %d user(s)...", total)

            # Flatten buttons for storage
            flat_buttons = []
            buttons = broadcast_data.get("buttons") or []
            for row in buttons:
                for btn in row:
                    flat_buttons.append({"text": btn["text"], "url": btn["url"]})

            reply_markup = {"inline_keyboard": buttons} if buttons else None
            msg_text = broadcast_data.get("text", "")
            photo_file_id = broadcast_data.get("photo_file_id")

            for user in active_users:
                target_chat_id = user["telegram_chat_id"]
                try:
                    if photo_file_id:
                        await self.call_api("sendPhoto", {
                            "chat_id": target_chat_id,
                            "photo": photo_file_id,
                            "caption": msg_text,
                            "parse_mode": "HTML",
                            "reply_markup": reply_markup,
                        })
                    else:
                        await self.call_api("sendMessage", {
                            "chat_id": target_chat_id,
                            "text": msg_text,
                            "parse_mode": "HTML",
                            "reply_markup": reply_markup,
                            "disable_web_page_preview": False,
                        })
                    successful_sent += 1
                except Exception as err:
                    failed_count += 1
                    err_code = getattr(err, "error_code", None)
                    err_str = str(err).lower()
                    if err_code == 403 or "blocked" in err_str:
                        await Database.set_info_bot_user_blocked(target_chat_id, True)

                # Delay 35ms between sends (~28 msgs/sec, under Telegram's 30/s limit)
                await asyncio.sleep(0.035)

            # Record broadcast in MongoDB
            await Database.create_info_bot_broadcast({
                "admin_chat_id": admin_chat_id,
                "message_text": msg_text,
                "media_url": photo_file_id,
                "media_type": "photo" if photo_file_id else None,
                "inline_buttons": flat_buttons,
                "total_targeted": total,
                "successful_sent": successful_sent,
                "failed_count": failed_count,
                "status": "completed",
            })

            # Send delivery report to admin if possible
            report = (
                f"🎉 <b>Broadcast Completed!</b>\n\n"
                f"👥 <b>Total Targeted:</b> <code>{total}</code>\n"
                f"✅ <b>Delivered Successfully:</b> <code>{successful_sent}</code>\n"
                f"❌ <b>Failed / Blocked:</b> <code>{failed_count}</code>\n"
                f"🔘 <b>Buttons Attached:</b> <code>{len(flat_buttons)}</code>\n\n"
                f"<i>All active leads received your update.</i>"
            )
            if admin_chat_id:
                try:
                    await self.call_api("sendMessage", {
                        "chat_id": admin_chat_id,
                        "text": report,
                        "parse_mode": "HTML",
                    })
                except Exception as notify_err:
                    logger.debug("[TelegramInfoBot] Could not deliver report to admin %s: %s", admin_chat_id, notify_err)

        except Exception as e:
            logger.error("[TelegramInfoBot] Broadcast execution error: %s", e)
            if admin_chat_id:
                try:
                    await self.call_api("sendMessage", {
                        "chat_id": admin_chat_id,
                        "text": f"⚠️ Broadcast failed: {escape_html(str(e))}",
                        "parse_mode": "HTML",
                    })
                except Exception:
                    pass

    async def poll_updates(self):
        """Asynchronous long-polling worker for Telegram Bot updates."""
        logger.info("[TelegramInfoBot] Starting polling daemon loop...")
        while self.is_polling:
            try:
                updates = await self.call_api("getUpdates", {
                    "offset": self.last_update_id + 1,
                    "timeout": 25,
                    "allowed_updates": ["message", "callback_query"],
                })

                if isinstance(updates, list):
                    for update in updates:
                        self.last_update_id = max(self.last_update_id, update.get("update_id", 0))

                        if "callback_query" in update:
                            await self.handle_callback_query(update["callback_query"])
                        elif "message" in update:
                            await self.handle_message(update["message"])

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self.is_polling:
                    break
                logger.warning("[TelegramInfoBot] Polling loop backoff: %s", e)
                await asyncio.sleep(4.0)

    async def start(self):
        """Validates credentials and starts the polling daemon."""
        if not self.token:
            logger.warning("[TelegramInfoBot] No TELEGRAM_INFO_BOT_TOKEN found. Bot disabled.")
            return

        if self.is_polling:
            logger.info("[TelegramInfoBot] Already running.")
            return

        try:
            me = await self.call_api("getMe")
            self.username = me.get("username", self.username)
            logger.info("==================================================================")
            logger.info("🤖 ProAffiliaters Info Bot connected as @%s (ID: %s)", self.username, me.get("id"))
            logger.info("👑 Admin ID: %s | WebApp: %s", self.admin_id, self.webapp_url)
            logger.info("==================================================================")

            self.is_polling = True
            self._polling_task = asyncio.create_task(self.poll_updates())
        except Exception as e:
            logger.error("[TelegramInfoBot] Failed to connect to Telegram Bot API: %s", e)

    async def stop(self):
        """Stops the polling daemon and closes HTTP client."""
        self.is_polling = False
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None

        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("[TelegramInfoBot] Info Bot daemon stopped.")

    async def get_status(self) -> Dict[str, Any]:
        """Provides status summary for Web UI and REST API."""
        stats = await Database.get_info_bot_stats()
        return {
            "success": True,
            "running": self.is_polling,
            "bot_username": self.username,
            "deep_link": f"https://t.me/{self.username}",
            "admin_id": self.admin_id,
            "total_leads": stats.get("total_leads", 0),
            "active_24h": stats.get("active_24h", 0),
            "blocked_leads": stats.get("blocked_leads", 0),
            "broadcasts_count": stats.get("broadcasts_count", 0),
        }

    async def broadcast_from_api(
        self,
        message_text: str,
        buttons_markup: Optional[str] = None,
        photo_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Allows initiating a broadcast from the FastAPI Web UI."""
        buttons = parse_inline_buttons_markup(buttons_markup) if buttons_markup else []
        broadcast_data = {
            "text": message_text,
            "buttons": buttons,
            "photo_file_id": photo_url,
        }
        asyncio.create_task(self.execute_broadcast(self.admin_id, broadcast_data))
        active_users = await Database.get_all_active_info_bot_users()
        return {
            "success": True,
            "message": f"Broadcast dispatched in background to {len(active_users)} active leads.",
            "total_targeted": len(active_users),
            "buttons_attached": sum(len(r) for r in buttons),
        }


# Global singleton instance
info_bot = TelegramInfoBot()
