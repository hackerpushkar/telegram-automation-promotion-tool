"""
Test suite for ProAffiliaters Official Info Bot (@ProaffiliatersInfoBot) Service.

Verifies:
1. Inline button markup parser ([Button Title | URL] syntax)
2. Welcome payload and callback menu structure
3. Telegram Bot API connectivity (getMe)
4. MongoDB upsert, query, and broadcast storage
5. Status reporting
"""

import os
import sys
import asyncio
from datetime import datetime

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.stdout.reconfigure(encoding="utf-8")

from src.config import settings
from src.db import Database
from src.info_bot import parse_inline_buttons_markup, escape_html, info_bot


async def run_tests():
    print("==================================================================")
    print("  TEST: ProAffiliaters Info Bot (@ProaffiliatersInfoBot) Verification")
    print("==================================================================\n")

    # 1. Test HTML escaping
    raw_sample = "<script>alert('xss & deals')</script>"
    escaped = escape_html(raw_sample)
    assert "&lt;script&gt;" in escaped and "&amp;" in escaped, "HTML escape failed"
    print("✅ HTML Escaping Verified:", escaped)

    # 2. Test Inline Button Markup Parser
    markup = "[🔥 View Deals | https://app.proaffiliaters.com/deals] [📢 Official Channel | https://t.me/proaffiliaters] [💬 Help Bot | t.me/ProAffiliatersHelpBot]"
    parsed = parse_inline_buttons_markup(markup)
    print(f"✅ Parsed {len(parsed)} button row(s):")
    for r_idx, row in enumerate(parsed):
        print(f"   Row {r_idx + 1}: {[b['text'] + ' -> ' + b['url'] for b in row]}")

    assert len(parsed) == 2, f"Expected 2 rows (2 buttons max per row), got {len(parsed)}"
    assert parsed[0][0]["text"] == "🔥 View Deals"
    assert parsed[0][0]["url"] == "https://app.proaffiliaters.com/deals"
    assert parsed[1][0]["url"].startswith("https://"), "URL without protocol was not prefixed with https://"
    print("✅ Inline Button Syntax Parser Passed.")

    # 3. Test Welcome Payload & Admin Recognition
    admin_id = settings.telegram_info_bot_admin_id
    payload_user = info_bot.get_main_welcome_payload("RegularUser", "123456789")
    payload_admin = info_bot.get_main_welcome_payload("SuperAdmin", admin_id)

    user_has_admin_btn = any(
        any(b.get("callback_data") == "admin_menu" for b in row)
        for row in payload_user["reply_markup"]["inline_keyboard"]
    )
    admin_has_admin_btn = any(
        any(b.get("callback_data") == "admin_menu" for b in row)
        for row in payload_admin["reply_markup"]["inline_keyboard"]
    )

    assert not user_has_admin_btn, "Regular user should NOT see admin button"
    assert admin_has_admin_btn, "Configured admin MUST see admin console button"
    print(f"✅ Menu Authorization Verified (Admin ID {admin_id} detected).")

    # 4. Test Telegram Bot API Connection via getMe
    token = settings.telegram_info_bot_token
    print(f"\n[Test] Checking Telegram Bot API Connection with token: {token[:12]}...")
    try:
        me = await info_bot.call_api("getMe")
        assert me.get("is_bot") is True, "getMe response is not a bot"
        bot_user = me.get("username")
        print(f"✅ Telegram Bot API Connected Successfully: @{bot_user} (ID: {me.get('id')})")
    except Exception as e:
        print(f"❌ Telegram API Error: {e}")
        raise

    # 5. Test Database Connection and Persistence
    print("\n[Test] Testing Database Operations...")
    await Database.connect()

    test_chat_id = "test_lead_7573032265_verification"
    test_user_data = {
        "telegram_chat_id": test_chat_id,
        "username": "TestLeadVerifyUser",
        "first_name": "Antigravity",
        "last_name": "Tester",
        "start_payload": "ads_deal_campaign_q4_2026",
    }

    # Upsert user
    is_new, doc = await Database.upsert_info_bot_user(test_user_data)
    print(f"✅ Upserted Info Bot User (is_new={is_new}): @{doc.get('username')}, Tag: {doc.get('start_payload')}")

    # Query user
    queried = await Database.get_info_bot_users(search="TestLeadVerifyUser")
    assert len(queried) > 0, "Failed to query back newly saved info bot user"
    assert queried[0]["telegram_chat_id"] == test_chat_id
    print("✅ Queried Info Bot User Verified in Database.")

    # Test Block flag
    await Database.set_info_bot_user_blocked(test_chat_id, True)
    blocked_check = await Database.get_info_bot_users(search="TestLeadVerifyUser")
    assert blocked_check[0]["is_blocked"] is True, "Failed to set is_blocked flag"
    print("✅ User Block Tracking Verified.")

    # Test Broadcast Logging
    test_broadcast = {
        "admin_chat_id": admin_id,
        "message_text": "<b>Test Announcement</b>\nWelcome to ProAffiliaters!",
        "media_url": None,
        "media_type": None,
        "inline_buttons": [{"text": "Test Button", "url": "https://proaffiliaters.com"}],
        "total_targeted": 1,
        "successful_sent": 1,
        "failed_count": 0,
        "status": "completed",
    }
    b_id = await Database.create_info_bot_broadcast(test_broadcast)
    print(f"✅ Created Broadcast Record (ID: {b_id}).")

    history = await Database.get_info_bot_broadcasts(limit=5)
    assert len(history) > 0, "Failed to retrieve broadcast history"
    print(f"✅ Retrieved Broadcast History ({len(history)} entry/entries).")

    # Clean up test user
    users_col = Database.get_info_bot_users_collection()
    if users_col is not None:
        await users_col.delete_one({"telegram_chat_id": test_chat_id})
    Database._fallback_info_users.pop(test_chat_id, None)

    # 6. Test Status Method
    status = await info_bot.get_status()
    print("\n✅ info_bot.get_status() Result:", status)
    assert status["success"] is True

    print("\n==================================================================")
    print("  ALL TESTS PASSED: @ProaffiliatersInfoBot is fully operational!")
    print("==================================================================")

    await Database.close()


if __name__ == "__main__":
    asyncio.run(run_tests())
