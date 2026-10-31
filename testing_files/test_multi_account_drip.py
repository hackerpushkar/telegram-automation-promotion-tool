import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.db import Database
from src.drip_engine import DripEngine
from src.models import Lead, LeadSourceInfo, LeadType, TelegramAccount


class TestMultiAccountAndDripEngine(unittest.IsolatedAsyncioTestCase):
    """Test suite for Multi-Account Login Management and 5-Week Drip Forwarding."""

    async def asyncSetUp(self):
        """Initialize database connection for test loop."""
        Database.client = None
        await Database.connect()

    async def test_01_telegram_account_model(self):
        """Verify TelegramAccount model validation."""
        acc = TelegramAccount(
            account_index=1,
            phone="+919876543210",
            user_id=12345678,
            first_name="TestAccount1",
            username="test_acc_1",
            session_string="1BVtsOHQBu...",
            assigned_slot=1
        )
        self.assertEqual(acc.assigned_slot, 1)
        self.assertEqual(acc.phone, "+919876543210")
        self.assertEqual(acc.status, "active")
        print("  [PASS] TelegramAccount model validated successfully.")

    async def test_02_database_account_crud(self):
        """Verify account saving, retrieval by slot, and status update."""
        # Save 5 sample accounts for slots 1 to 5
        for slot in range(1, 6):
            data = {
                "account_index": slot,
                "phone": f"+199900000{slot}",
                "user_id": 900000 + slot,
                "first_name": f"Manager_Slot{slot}",
                "username": f"slot_{slot}_bot",
                "session_string": f"mock_session_string_slot_{slot}",
                "status": "active",
                "assigned_slot": slot,
                "daily_send_count": 0
            }
            saved = await Database.save_telegram_account(data)
            self.assertTrue(saved)

        # Retrieve by slot 2
        acc2 = await Database.get_telegram_account_by_slot(2)
        self.assertIsNotNone(acc2)
        self.assertEqual(acc2.get("assigned_slot"), 2)
        self.assertEqual(acc2.get("phone"), "+1999000002")

        # Retrieve all accounts
        all_accs = await Database.get_all_telegram_accounts()
        self.assertTrue(len(all_accs) >= 5)

        # Update status
        up = await Database.update_telegram_account_status("+1999000001", "flood_wait_60s")
        self.assertTrue(up)
        acc1 = await Database.get_telegram_account_by_phone("+1999000001")
        self.assertEqual(acc1.get("status"), "flood_wait_60s")

        # Reset back to active
        await Database.update_telegram_account_status("+1999000001", "active")
        print("  [PASS] Database Telegram accounts CRUD and slot retrieval verified.")

    async def test_03_drip_stage_progression_and_cooldown(self):
        """Verify 5-week stage progression, cooldown enforcement, and stage 5 completion."""
        now = datetime.utcnow()
        # Clean up prior test runs
        leads_col = Database.get_leads_collection()
        if leads_col is not None:
            await leads_col.delete_one({"username_clean": "driptestbot"})
        Database._fallback_leads.pop("driptestbot", None)

        test_bot = Lead(
            username="DripTestBot",
            username_clean="driptestbot",
            lead_type=LeadType.BOT,
            extracted_from=LeadSourceInfo(source_name="Test Source", platform="telegram"),
            bio_context="Deal bot for testing progressive drip campaigns."
        )
        await Database.upsert_lead(test_bot)

        # Check eligibility for Stage 1 (promo_stage = 0)
        eligible_s1 = await Database.get_eligible_bots_for_stage(1, min_cooldown_days=7)
        clean_names_s1 = [b.get("username_clean") for b in eligible_s1]
        self.assertIn("driptestbot", clean_names_s1)

        # Advance to Stage 1 (Week 1 / Post 1)
        adv1 = await Database.advance_lead_promo_stage(
            username_clean="driptestbot",
            new_stage=1,
            account_phone="+1999000001",
            post_number=1,
            message_link="https://t.me/pro_affiliaters/13"
        )
        self.assertTrue(adv1)

        # Immediately check eligibility for Stage 2 (should FAIL because 7 days have not passed)
        eligible_s2_immediate = await Database.get_eligible_bots_for_stage(2, min_cooldown_days=7)
        clean_names_s2_imm = [b.get("username_clean") for b in eligible_s2_immediate]
        self.assertNotIn("driptestbot", clean_names_s2_imm)

        # Simulate 8 days passed by backdating last_promo_sent_at
        backdated_time = now - timedelta(days=8)
        await Database.backdate_lead_last_promo("driptestbot", backdated_time)

        # Now check eligibility for Stage 2 (should PASS because > 7 days passed)
        eligible_s2_after_cooldown = await Database.get_eligible_bots_for_stage(2, min_cooldown_days=7)
        clean_names_s2_after = [b.get("username_clean") for b in eligible_s2_after_cooldown]
        self.assertIn("driptestbot", clean_names_s2_after)

        # Fast forward through Stage 2, 3, 4, 5
        for st in range(2, 6):
            await Database.advance_lead_promo_stage(
                username_clean="driptestbot",
                new_stage=st,
                account_phone=f"+199900000{st}",
                post_number=st,
                message_link=f"https://t.me/pro_affiliaters/{12 + st}"
            )

        # Verify that at Stage 5, bot is completed and NOT eligible for any further stage
        eligible_future = await Database.get_eligible_bots_for_stage(5, min_cooldown_days=7)
        self.assertNotIn("driptestbot", [b.get("username_clean") for b in eligible_future])
        print("  [PASS] 5-Week stage progression and 7-day cooldown verified successfully.")

    async def test_04_drip_overview_and_funnel_stats(self):
        """Verify DripEngine overview aggregation across all 5 stages."""
        overview = await DripEngine.get_drip_overview()
        self.assertIn("funnel", overview)
        self.assertIn("stages", overview)
        self.assertEqual(len(overview["stages"]), 5)

        # Verify all 5 stages have mapped posts
        for st in overview["stages"]:
            self.assertIn("stage", st)
            self.assertIn("post_number", st)
            self.assertIn("has_active_account", st)
        print("  [PASS] DripEngine overview and funnel analytics verified.")


if __name__ == "__main__":
    print("==================================================================")
    print("[TEST] Running Automated Test Suite: Multi-Account & 5-Week Drip")
    print("==================================================================")
    unittest.main()
