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
from src.models import Lead, LeadSourceInfo, LeadType


class TestAutonomousDripWorkflow(unittest.IsolatedAsyncioTestCase):
    """Test suite for Autonomous Drip Promotion Workflow."""

    async def asyncSetUp(self):
        Database.client = None
        await Database.connect()

    async def test_01_autonomous_drip_eligibility_and_cooldown(self):
        """Verify automatic Stage 1 entry for new leads and strict 7-day cooldown enforcement."""
        now = datetime.utcnow()
        bot_handle = "autotestdealbot"

        # Cleanup
        leads_col = Database.get_leads_collection()
        if leads_col is not None:
            await leads_col.delete_one({"username_clean": bot_handle})
        Database._fallback_leads.pop(bot_handle, None)

        # 1. Insert newly discovered userbot (Stage 0 / None)
        lead = Lead(
            username="AutoTestDealBot",
            username_clean=bot_handle,
            lead_type=LeadType.BOT,
            extracted_from=LeadSourceInfo(source_name="Global Search", platform="telegram"),
            bio_context="Deal channel bot discovered via autonomous search engine."
        )
        is_new, _ = await Database.upsert_lead(lead)
        self.assertTrue(is_new)

        # 2. Stage 1 Check: Must be eligible immediately
        eligible_s1 = await Database.get_eligible_bots_for_stage(1, min_cooldown_days=7)
        s1_handles = [b.get("username_clean") for b in eligible_s1]
        self.assertIn(bot_handle, s1_handles)
        print("  [PASS] 1. Newly discovered userbot enters Stage 1 immediately.")

        # 3. Simulate Stage 1 Dispatch by Account #1 (Slot 1)
        await Database.advance_lead_promo_stage(
            username_clean=bot_handle,
            new_stage=1,
            account_phone="+1999000001",
            post_number=1,
            message_link="https://t.me/pro_affiliaters/13"
        )
        await Database.save_sending_report({
            "recipient_username": bot_handle,
            "recipient_type": "bot",
            "sender_account_phone": "+1999000001",
            "sender_account_name": "Account 1",
            "post_title": "Promotional Post #1",
            "message_snippet": "STILL RUNNING YOUR DEAL CHANNEL MANUALLY?",
            "status": "sent",
            "sent_at": now
        })

        # 4. Bot must NOT be eligible for Stage 1 again
        eligible_s1_post = await Database.get_eligible_bots_for_stage(1, min_cooldown_days=7)
        self.assertNotIn(bot_handle, [b.get("username_clean") for b in eligible_s1_post])

        # 5. Bot must NOT be eligible for Stage 2 yet (7-day cooldown active)
        eligible_s2_imm = await Database.get_eligible_bots_for_stage(2, min_cooldown_days=7)
        self.assertNotIn(bot_handle, [b.get("username_clean") for b in eligible_s2_imm])
        print("  [PASS] 2. After Stage 1 promo sent, bot is locked in 7-day cooldown.")

        # 6. Simulate 7 days passing (backdate last_promo_sent_at by 8 days)
        backdate = now - timedelta(days=8)
        await Database.backdate_lead_last_promo(bot_handle, backdate)

        # 7. Now bot MUST be eligible for Stage 2 (Post #2 by Account #2)
        eligible_s2_due = await Database.get_eligible_bots_for_stage(2, min_cooldown_days=7)
        self.assertIn(bot_handle, [b.get("username_clean") for b in eligible_s2_due])
        print("  [PASS] 3. Exactly after 7 days, bot automatically becomes eligible for Stage 2 (Account #2).")

        # 8. Clean up
        if leads_col is not None:
            await leads_col.delete_one({"username_clean": bot_handle})
        Database._fallback_leads.pop(bot_handle, None)


if __name__ == "__main__":
    print("==================================================================")
    print("[TEST] Running Automated Test Suite: Autonomous Drip Promotion")
    print("==================================================================")
    unittest.main()
