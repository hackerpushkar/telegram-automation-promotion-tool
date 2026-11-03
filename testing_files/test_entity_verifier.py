import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock
from telethon.tl.types import User, Channel, Chat

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.extractor import extract_leads_from_text, HANDLE_REGEX, IGNORED_SYSTEM_NAMES
from src.entity_verifier import verify_telegram_entity, scrub_non_user_leads
from src.db import Database
from src.models import Lead, LeadType, LeadSourceInfo


class TestEntityVerifier(unittest.IsolatedAsyncioTestCase):
    """Test suite for verifying Telegram entity types and filtering out channels/groups."""

    async def asyncSetUp(self):
        Database.client = None
        await Database.connect()

    def test_01_regex_ignores_emails_and_system_names(self):
        """Verify regex does not capture email domains like user@gmail.com or hello@yahoo.com."""
        text = "Contact support@gmail.com or visit @valid_admin. Also deals@yahoo.com and t.me/my_deal_bot!"
        leads = extract_leads_from_text(text, source_name="Test")
        usernames = [l.username.lower().lstrip("@") for l in leads]

        self.assertNotIn("gmail", usernames)
        self.assertNotIn("yahoo", usernames)
        self.assertIn("valid_admin", usernames)
        self.assertIn("my_deal_bot", usernames)
        print("  [PASS] 1. Email domains (@gmail, @yahoo) successfully ignored by regex.")

    async def test_02_entity_resolution_types(self):
        """Verify distinction between User, Bot, Channel, and Group entities."""
        mock_client = MagicMock()

        # Mock User (human)
        human_user = MagicMock(spec=User)
        human_user.id = 111
        human_user.first_name = "John"
        human_user.last_name = "Admin"
        human_user.bot = False

        # Mock Bot
        bot_user = MagicMock(spec=User)
        bot_user.id = 222
        bot_user.first_name = "LootBot"
        bot_user.last_name = ""
        bot_user.bot = True

        # Mock Broadcast Channel
        channel_ent = MagicMock(spec=Channel)
        channel_ent.id = 333
        channel_ent.title = "Best Deals Channel"
        channel_ent.broadcast = True
        channel_ent.megagroup = False

        # Mock Supergroup
        group_ent = MagicMock(spec=Channel)
        group_ent.id = 444
        group_ent.title = "Deals Discussion"
        group_ent.broadcast = False
        group_ent.megagroup = True

        async def mock_get_entity(handle):
            if handle == "human_admin":
                return human_user
            elif handle == "loot_bot":
                return bot_user
            elif handle == "deals_channel":
                return channel_ent
            elif handle == "deals_group":
                return group_ent
            raise ValueError("Unknown")

        mock_client.get_entity = AsyncMock(side_effect=mock_get_entity)
        mock_client.is_connected = MagicMock(return_value=True)

        # 1. Test Human User
        res_user = await verify_telegram_entity(mock_client, "human_admin")
        self.assertEqual(res_user["entity_type"], "user")
        self.assertTrue(res_user["is_lead"])

        # 2. Test Bot
        res_bot = await verify_telegram_entity(mock_client, "loot_bot")
        self.assertEqual(res_bot["entity_type"], "bot")
        self.assertTrue(res_bot["is_lead"])

        # 3. Test Channel
        res_ch = await verify_telegram_entity(mock_client, "deals_channel")
        self.assertEqual(res_ch["entity_type"], "channel")
        self.assertFalse(res_ch["is_lead"])

        # 4. Test Group
        res_gr = await verify_telegram_entity(mock_client, "deals_group")
        self.assertEqual(res_gr["entity_type"], "group")
        self.assertFalse(res_gr["is_lead"])

        print("  [PASS] 2. MTProto entity resolution accurately distinguishes User, Bot, Channel, and Group.")

    async def test_03_scrubber_removes_channels_and_groups_from_leads(self):
        """Verify scrubber removes channel/group handles from leads collection."""
        # Insert a dummy channel handle as a lead
        channel_lead = Lead(
            username="TestBroadcastChannel",
            username_clean="testbroadcastchannel",
            lead_type=LeadType.USER,
            extracted_from=LeadSourceInfo(source_name="Test", platform="telegram")
        )
        await Database.upsert_lead(channel_lead)

        # Mock client resolving this as a broadcast channel
        mock_client = MagicMock()
        mock_client.is_connected = MagicMock(return_value=True)
        ch_entity = MagicMock(spec=Channel)
        ch_entity.id = 555
        ch_entity.title = "Test Channel"
        ch_entity.broadcast = True
        ch_entity.megagroup = False
        mock_client.get_entity = AsyncMock(return_value=ch_entity)

        # Run scrubber
        res = await scrub_non_user_leads(mock_client)
        self.assertIn("TestBroadcastChannel", res["removed_channels"])

        # Verify it was removed from leads
        leads = await Database.get_leads(search="testbroadcastchannel")
        self.assertEqual(len(leads), 0)
        print("  [PASS] 3. Scrubber successfully identified and removed broadcast channel from leads.")


if __name__ == "__main__":
    print("==================================================================")
    print("[TEST] Running Automated Test Suite: Entity Verification & Scrubbing")
    print("==================================================================")
    unittest.main()
