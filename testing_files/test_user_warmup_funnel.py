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
from src.models import Lead, LeadType, LeadSourceInfo


class TestHumanUserWarmupFunnel(unittest.IsolatedAsyncioTestCase):
    """Test suite for human channel owner 2-step anti-ban warmup funnel."""

    async def asyncSetUp(self):
        Database.client = None
        await Database.connect()

    async def test_01_user_warmup_cooldown_and_promo_progression(self):
        """Verify human user enters Stage 1 (Hi), gets 7-day cooldown, then enters Stage 2 (Promo)."""
        now = datetime.utcnow()
        user_handle = "testchannelowner"

        leads_col = Database.get_leads_collection()
        if leads_col is not None:
            await leads_col.delete_one({"username_clean": user_handle})
        Database._fallback_leads.pop(user_handle, None)

        # 1. Insert new human lead (lead_type: USER, promo_stage: 0)
        human_lead = Lead(
            username="TestChannelOwner",
            username_clean=user_handle,
            lead_type=LeadType.USER,
            extracted_from=LeadSourceInfo(source_name="Deals Channel Bio", platform="telegram")
        )
        is_new, _ = await Database.upsert_lead(human_lead)
        self.assertTrue(is_new)

        # 2. Verify eligibility for Stage 1 (Warmup Greeting)
        eligible_s1 = await Database.get_eligible_users_for_warmup(1, min_cooldown_days=7)
        s1_handles = [u.get("username_clean") for u in eligible_s1]
        self.assertIn(user_handle, s1_handles)
        print("  [PASS] 1. New human channel owner enters Stage 1 (Warmup Greeting) immediately.")

        # 3. Simulate Stage 1 Greeting sent by Account 1
        await Database.advance_lead_promo_stage(
            username_clean=user_handle,
            new_stage=1,
            account_phone="+1999000001",
            post_number=1,
            message_link="user_warmup_greeting"
        )

        # 4. Must NOT be eligible for Stage 1 again
        eligible_s1_post = await Database.get_eligible_users_for_warmup(1, min_cooldown_days=7)
        self.assertNotIn(user_handle, [u.get("username_clean") for u in eligible_s1_post])

        # 5. Must NOT be eligible for Stage 2 yet (7-day cooldown active)
        eligible_s2_imm = await Database.get_eligible_users_for_warmup(2, min_cooldown_days=7)
        self.assertNotIn(user_handle, [u.get("username_clean") for u in eligible_s2_imm])
        print("  [PASS] 2. After greeting, human user is locked in 7-day anti-ban cooldown.")

        # 6. Simulate 7 days passed (backdate last_promo_sent_at by 8 days)
        backdate = now - timedelta(days=8)
        await Database.backdate_lead_last_promo(user_handle, backdate)

        # 7. Now user MUST be eligible for Stage 2 (Promotional Post)
        eligible_s2_due = await Database.get_eligible_users_for_warmup(2, min_cooldown_days=7)
        self.assertIn(user_handle, [u.get("username_clean") for u in eligible_s2_due])
        print("  [PASS] 3. Exactly after 7 days, user becomes eligible for Stage 2 (Promotional Post).")

        # 8. Simulate Stage 2 Promo Post dispatched
        await Database.advance_lead_promo_stage(
            username_clean=user_handle,
            new_stage=2,
            account_phone="+1999000002",
            post_number=1,
            message_link="user_promo_post"
        )

        # 9. At Stage 2, user is complete/converted and excluded permanently
        eligible_s2_final = await Database.get_eligible_users_for_warmup(2, min_cooldown_days=7)
        self.assertNotIn(user_handle, [u.get("username_clean") for u in eligible_s2_final])
        print("  [PASS] 4. User funnel marks completion after Stage 2 promo. Excluded from further outreach.")

        # Cleanup
        if leads_col is not None:
            await leads_col.delete_one({"username_clean": user_handle})
        Database._fallback_leads.pop(user_handle, None)


if __name__ == "__main__":
    print("==================================================================")
    print("[TEST] Running Automated Test Suite: Human User Warmup Funnel")
    print("==================================================================")
    unittest.main()

