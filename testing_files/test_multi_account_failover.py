import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.models import Lead, LeadType, LeadStatus, LeadSourceInfo
from src.db import Database
from src.drip_engine import DripEngine
from src.delivery_engine import DeliveryEngine
from telethon.errors import PeerFloodError


class TestMultiAccountFailover(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Database.is_connected = False
        Database._fallback_leads.clear()
        Database._fallback_telegram_accounts.clear()

        # Add 2 test accounts
        self.acc1 = {
            "account_index": 1,
            "phone": "+916000598852",
            "first_name": "Nicole",
            "last_name": "Brown",
            "session_string": "mock_session_1",
            "status": "active",
            "assigned_slot": 1,
            "daily_send_count": 0
        }
        self.acc2 = {
            "account_index": 2,
            "phone": "+916000698847",
            "first_name": "Anthony",
            "last_name": "Pearson",
            "session_string": "mock_session_2",
            "status": "active",
            "assigned_slot": 2,
            "daily_send_count": 0
        }
        await Database.save_telegram_account(self.acc1)
        await Database.save_telegram_account(self.acc2)

        # Add 2 bot leads
        lead_b1 = Lead(
            username="@bot_alpha",
            username_clean="bot_alpha",
            lead_type=LeadType.BOT,
            status=LeadStatus.NEW,
            extracted_from=LeadSourceInfo(source_name="Test Source")
        )
        lead_b2 = Lead(
            username="@bot_beta",
            username_clean="bot_beta",
            lead_type=LeadType.BOT,
            status=LeadStatus.NEW,
            extracted_from=LeadSourceInfo(source_name="Test Source")
        )
        await Database.upsert_lead(lead_b1)
        await Database.upsert_lead(lead_b2)

        # Add 2 human user leads
        lead_u1 = Lead(
            username="@user_alpha",
            username_clean="user_alpha",
            lead_type=LeadType.USER,
            status=LeadStatus.NEW,
            extracted_from=LeadSourceInfo(source_name="Test Source")
        )
        lead_u2 = Lead(
            username="@user_beta",
            username_clean="user_beta",
            lead_type=LeadType.USER,
            status=LeadStatus.NEW,
            extracted_from=LeadSourceInfo(source_name="Test Source")
        )
        await Database.upsert_lead(lead_u1)
        await Database.upsert_lead(lead_u2)

    @patch("src.account_manager.AccountManager.get_client")
    async def test_bot_drip_failover_on_peerflood(self, mock_get_client):
        """When Account 1 hits PeerFlood, Account 2 takes over and finishes the batch."""
        # Account 1 client raises PeerFlood on forward/send
        mock_client1 = MagicMock()
        mock_client1.connect = AsyncMock()
        mock_client1.is_user_authorized = AsyncMock(return_value=True)
        mock_client1.disconnect = AsyncMock()
        mock_client1.get_entity = AsyncMock(return_value=MagicMock())
        mock_client1.get_messages = AsyncMock(return_value=[])
        mock_client1.forward_messages = AsyncMock(side_effect=PeerFloodError(request=None))
        mock_client1.send_message = AsyncMock(side_effect=PeerFloodError(request=None))
        mock_client1.send_file = AsyncMock(side_effect=PeerFloodError(request=None))

        # Account 2 client succeeds
        mock_client2 = MagicMock()
        mock_client2.connect = AsyncMock()
        mock_client2.is_user_authorized = AsyncMock(return_value=True)
        mock_client2.disconnect = AsyncMock()
        mock_client2.get_entity = AsyncMock(return_value=MagicMock())
        mock_client2.get_messages = AsyncMock(return_value=[])
        mock_client2.forward_messages = AsyncMock()
        mock_client2.send_message = AsyncMock()
        mock_client2.send_file = AsyncMock()

        def side_effect_client(session_str):
            if "mock_session_1" in session_str:
                return mock_client1
            return mock_client2

        mock_get_client.side_effect = side_effect_client

        # Execute Stage 1
        res = await DripEngine.execute_drip_stage(stage=1, batch_size=5, delay_seconds=0.01)

        # Account 1 hits PeerFlood, failover to Account 2 succeeds for both bots!
        self.assertEqual(res["status"], "completed")
        self.assertGreaterEqual(res["successful"], 1)

        # Account 1 is marked with peer_flood_lock
        acc1_after = await Database.get_telegram_account_by_phone("+916000598852")
        self.assertEqual(acc1_after.get("status"), "peer_flood_lock")

    @patch("src.account_manager.AccountManager.get_client")
    async def test_user_funnel_failover_on_peerflood(self, mock_get_client):
        """When Account 1 hits PeerFlood on user greeting, Account 2 takes over and succeeds."""
        mock_client1 = MagicMock()
        mock_client1.connect = AsyncMock()
        mock_client1.is_user_authorized = AsyncMock(return_value=True)
        mock_client1.disconnect = AsyncMock()
        mock_client1.get_entity = AsyncMock(return_value=MagicMock())
        mock_client1.send_message = AsyncMock(side_effect=PeerFloodError(request=None))

        mock_client2 = MagicMock()
        mock_client2.connect = AsyncMock()
        mock_client2.is_user_authorized = AsyncMock(return_value=True)
        mock_client2.disconnect = AsyncMock()
        mock_client2.get_entity = AsyncMock(return_value=MagicMock())
        mock_client2.send_message = AsyncMock()

        def side_effect_client(session_str):
            if "mock_session_1" in session_str:
                return mock_client1
            return mock_client2

        mock_get_client.side_effect = side_effect_client

        res = await DripEngine.execute_user_funnel_stage(stage=1, batch_size=5, delay_seconds=0.01)

        self.assertEqual(res["status"], "completed")
        self.assertGreaterEqual(res["successful"], 1)

        acc1_after = await Database.get_telegram_account_by_phone("+916000598852")
        self.assertEqual(acc1_after.get("status"), "peer_flood_lock")


if __name__ == "__main__":
    unittest.main()
