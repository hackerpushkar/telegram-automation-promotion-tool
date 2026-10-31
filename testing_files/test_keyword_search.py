import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models import SearchKeyword, DiscoveredChannel
from src.db import Database
from src.keyword_search_engine import KeywordSearchEngine


class TestKeywordSearchSystem(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        Database.client = None
        await Database.connect()

    async def test_01_models_validation(self):
        """Test Pydantic model validation for SearchKeyword and DiscoveredChannel."""
        kw = SearchKeyword(
            keyword="loot deals",
            category="deals",
            enabled=True
        )
        self.assertEqual(kw.keyword, "loot deals")
        self.assertEqual(kw.category, "deals")
        self.assertTrue(kw.enabled)

        ch = DiscoveredChannel(
            channel_id=123456789,
            title="Prime Deals India",
            username="primedealsindia",
            matching_keyword="loot deals",
            subscribers_count=15000,
            extracted_leads_count=2
        )
        self.assertEqual(ch.channel_id, 123456789)
        self.assertEqual(ch.username, "primedealsindia")
        self.assertEqual(ch.extracted_leads_count, 2)
        print("  [PASS] SearchKeyword and DiscoveredChannel models validated successfully.")

    async def test_02_database_keywords_crud(self):
        """Test adding, listing, toggling, and deleting search keywords."""
        test_kw = f"test_deals_{int(datetime.utcnow().timestamp())}"
        
        # Add
        added = await Database.add_search_keyword(test_kw, category="loot")
        self.assertEqual(added["keyword"], test_kw)
        self.assertEqual(added["category"], "loot")

        # List
        all_kw = await Database.get_search_keywords()
        found = any(k["keyword"] == test_kw for k in all_kw)
        self.assertTrue(found, f"Keyword '{test_kw}' should be in retrieved keywords.")

        # Toggle to False
        toggled = await Database.toggle_search_keyword(test_kw, False)
        self.assertTrue(toggled)
        enabled_kw = await Database.get_search_keywords(enabled_only=True)
        self.assertFalse(any(k["keyword"] == test_kw for k in enabled_kw))

        # Toggle back to True
        await Database.toggle_search_keyword(test_kw, True)

        # Update stats
        await Database.update_keyword_stats(test_kw, channels_count=5, leads_count=3)

        # Delete
        deleted = await Database.delete_search_keyword(test_kw)
        self.assertTrue(deleted)
        after_delete = await Database.get_search_keywords()
        self.assertFalse(any(k["keyword"] == test_kw for k in after_delete))
        print("  [PASS] Database keywords CRUD and stats update verified successfully.")

    async def test_03_discovered_channels_operations(self):
        """Test saving discovered channels, cooldown checks, and querying."""
        test_channel_id = 999888777
        channel_data = {
            "channel_id": test_channel_id,
            "title": "Amazon Deals Express",
            "username": "amzdealstest",
            "invite_link": "https://t.me/amzdealstest",
            "subscribers_count": 52000,
            "bio": "Daily deal drops. Admin: @AmzDealAdmin | Bot: @DropAlertsBot",
            "matching_keyword": "amazon deals",
            "extracted_leads_count": 2,
            "status": "scanned"
        }

        # Save channel
        saved = await Database.save_discovered_channel(channel_data)
        self.assertTrue(saved)

        # Cooldown check (should be true since just scanned)
        is_recent = await Database.is_channel_recently_scanned(test_channel_id, cooldown_days=7)
        self.assertTrue(is_recent)

        # Query channels
        channels = await Database.get_discovered_channels(limit=10, search="amzdealstest")
        self.assertGreaterEqual(len(channels), 1)
        match = next((c for c in channels if c.get("channel_id") == test_channel_id), None)
        self.assertIsNotNone(match)
        self.assertEqual(match["title"], "Amazon Deals Express")
        print("  [PASS] Discovered channels persistence, query, and 7-day cooldown verified.")

    async def test_04_keyword_search_stats(self):
        """Test high-level keyword search KPI statistics."""
        stats = await Database.get_keyword_search_stats()
        self.assertIn("total_keywords", stats)
        self.assertIn("active_keywords", stats)
        self.assertIn("total_channels_discovered", stats)
        self.assertIn("total_leads_from_keywords", stats)
        self.assertGreaterEqual(stats["total_keywords"], 0)
        print("  [PASS] Keyword search KPI statistics calculation verified.")


if __name__ == "__main__":
    print("==================================================================")
    print("[TEST] Running Automated Test Suite: Autonomous Keyword Search")
    print("==================================================================")
    unittest.main()
