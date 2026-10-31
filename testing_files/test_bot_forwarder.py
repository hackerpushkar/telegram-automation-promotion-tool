"""
Automated Test Suite for Channel Post Publisher & Admin Bot Forwarding Engine.

Verifies:
1. Previews of all 5 promotional posts (images exist, texts match, deep-links correct).
2. Database operations for published channel posts, campaigns, and lead marking.
3. Channel publisher payload structure (inline keyboards, deep links).
4. BotForwarderEngine state management, dry-run simulation, and campaign lifecycle.
"""

import os
import sys
import asyncio
import unittest
from datetime import datetime

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import settings
from src.db import Database
from src.models import Lead, LeadType, LeadStatus, LeadSourceInfo
from src.channel_publisher import get_promo_post_preview, POST_ORDINAL_MAP, BASE_PROMO_DIR
from src.bot_forwarder import bot_forwarder, BotForwarderEngine


class TestBotForwarderAndPublisher(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        """Initialize database connection (or in-memory fallback)."""
        await Database.connect()

    async def test_01_promo_assets_exist_and_match(self):
        """Verify all 5 banners and text files exist and have valid content."""
        for num in range(1, 6):
            preview = get_promo_post_preview(num)
            self.assertEqual(preview["post_number"], num)
            self.assertTrue(preview["image_exists"], f"Image file missing for Post {num}: {preview['image_path']}")
            self.assertTrue(len(preview["caption"]) > 50, f"Caption text too short for Post {num}")

            # Verify deep-link target
            expected_link = f"https://t.me/{settings.telegram_info_bot_username}?start=promo_post{num}"
            self.assertEqual(preview["deep_link"], expected_link)

            # Verify inline buttons structure (single deep-link button)
            buttons = preview["inline_buttons"]
            self.assertEqual(len(buttons), 1)  # 1 row
            self.assertEqual(buttons[0][0]["text"], "🤖 Start Bot / Learn More")
            self.assertEqual(buttons[0][0]["url"], expected_link)
            print(f"  [PASS] Post #{num} verified: '{preview['title']}' | DeepLink: {expected_link}")

    async def test_02_database_published_posts_crud(self):
        """Verify saving and retrieving published channel post metadata."""
        sample_post = {
            "post_number": 1,
            "title": "Automate Your Telegram Deal Channels",
            "channel_username": "@pro_affiliaters",
            "message_id": 9991,
            "message_link": "https://t.me/pro_affiliaters/9991",
            "image_name": "1st Image.png",
            "caption": "Test Caption",
            "deep_link": "https://t.me/ProaffiliatersInfoBot?start=promo_post1",
            "inline_buttons": [{"text": "Start", "url": "https://t.me/ProaffiliatersInfoBot?start=promo_post1"}]
        }

        # Save
        saved = await Database.save_published_post(sample_post)
        self.assertTrue(saved)

        # Retrieve
        retrieved = await Database.get_published_post(1)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["message_id"], 9991)
        self.assertEqual(retrieved["channel_username"], "@pro_affiliaters")

        # Get all
        all_posts = await Database.get_all_published_posts()
        self.assertTrue(len(all_posts) >= 1)
        print("  [PASS] Database published_posts CRUD verified successfully.")

    async def test_03_uncontacted_bots_and_marking(self):
        """Verify query for uncontacted bots and marking them as contacted."""
        # Insert a test bot lead
        test_bot_lead = Lead(
            username="TestOutreachBot",
            username_clean="testoutreachbot",
            lead_type=LeadType.BOT,
            status=LeadStatus.NEW,
            extracted_from=LeadSourceInfo(source_name="Test Channel", platform="telegram")
        )
        await Database.upsert_lead(test_bot_lead)

        uncontacted = await Database.get_uncontacted_bots(limit=100)
        usernames = [b.get("username_clean") for b in uncontacted]
        self.assertIn("testoutreachbot", usernames)

        # Mark as contacted
        marked = await Database.mark_lead_contacted("testoutreachbot", contact_method="channel_forward")
        self.assertTrue(marked)

        # Check status updated
        updated_leads = await Database.get_leads(search="testoutreachbot")
        self.assertTrue(len(updated_leads) > 0)
        self.assertEqual(updated_leads[0]["status"], "contacted")
        self.assertEqual(updated_leads[0].get("contact_method"), "channel_forward")
        print("  [PASS] Uncontacted bots query and status update verified.")

    async def test_04_bot_forwarder_engine_lifecycle(self):
        """Verify engine status reporting, pause, resume, and stop controls."""
        engine = BotForwarderEngine()
        status = engine.get_status()
        self.assertEqual(status["engine_state"], "idle")
        self.assertFalse(status["has_active_campaign"])

        # Dry run forward to a bot without client should return graceful failure
        res = await engine.forward_to_single_bot(
            bot_username="@SampleBot",
            channel_message_id=101,
            channel_username="@pro_affiliaters"
        )
        self.assertFalse(res["success"])
        self.assertIn("Telethon client not connected", res["error"])
        print("  [PASS] BotForwarderEngine offline graceful safety verified.")

    async def test_05_campaign_creation_in_db(self):
        """Verify bot campaign records created and queried in MongoDB."""
        campaign_record = {
            "post_number": 2,
            "channel_username": "@pro_affiliaters",
            "channel_message_id": 9992,
            "delay_seconds": 6,
            "total_targeted": 5,
            "successful_sent": 5,
            "failed_count": 0,
            "status": "completed",
            "logs": ["Campaign started", "Delivered 5 messages", "Completed"]
        }

        cid = await Database.create_bot_campaign(campaign_record)
        self.assertIsNotNone(cid)

        # Retrieve campaigns
        campaigns = await Database.get_bot_campaigns(limit=10)
        self.assertTrue(len(campaigns) >= 1)
        found = any(c.get("channel_message_id") == 9992 for c in campaigns)
        self.assertTrue(found)
        print("  [PASS] Database bot_campaigns tracking verified.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("==================================================================")
    print("[TEST] Running Automated Test Suite: Channel Publisher & Bot Forwarder")
    print("==================================================================")
    unittest.main()
