import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.bot_interactor import prepare_bot_for_promo


class TestBotInteractor(unittest.IsolatedAsyncioTestCase):
    """Test suite for Bot Handshake, Language Selection, and Anti-Spam Bypass."""

    async def test_01_language_selection_button_clicked(self):
        """Verify bot that asks for language with [ English ] button gets auto-clicked."""
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()

        # Mock incoming message with language prompt and [ English ] button
        mock_msg = MagicMock()
        mock_msg.out = False
        mock_msg.text = "Please choose your language:"

        # Mock Button
        btn_english = MagicMock()
        btn_english.text = "English"
        btn_english.url = None

        btn_hindi = MagicMock()
        btn_hindi.text = "Hindi"
        btn_hindi.url = None

        mock_msg.buttons = [[btn_english, btn_hindi]]
        mock_msg.click = AsyncMock()

        mock_client.get_messages = AsyncMock(return_value=[mock_msg])

        mock_bot = MagicMock()
        mock_bot.username = "Offerboardbot"

        ready = await prepare_bot_for_promo(mock_client, mock_bot, greeting="Hi", wait_seconds=0.01)
        self.assertTrue(ready)

        # Verify greeting was sent
        mock_client.send_message.assert_called_once_with(mock_bot, "Hi")

        # Verify English button was clicked (row 0, col 0)
        mock_msg.click.assert_called_once_with(0, 0)
        print("  [PASS] 1. Language prompt detected and [ English ] button clicked successfully.")

    async def test_02_no_language_option_proceeds_directly(self):
        """Verify if no language option is received, promo post proceeds directly without clicking."""
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()

        # Mock incoming message without buttons (regular reply)
        mock_msg = MagicMock()
        mock_msg.out = False
        mock_msg.text = "Hello! Send your deals here."
        mock_msg.buttons = None
        mock_msg.click = AsyncMock()

        mock_client.get_messages = AsyncMock(return_value=[mock_msg])

        mock_bot = MagicMock()
        mock_bot.username = "NormalDealBot"

        ready = await prepare_bot_for_promo(mock_client, mock_bot, greeting="Hi", wait_seconds=0.01)
        self.assertTrue(ready)

        # Verify greeting was sent
        mock_client.send_message.assert_called_once_with(mock_bot, "Hi")

        # Verify click was NEVER called
        mock_msg.click.assert_not_called()
        print("  [PASS] 2. No language barrier detected; proceeds to promo without clicking.")

    async def test_03_antispam_continue_button_clicked(self):
        """Verify anti-spam verification with [ Continue ] button gets auto-clicked."""
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()

        # Mock incoming message with anti-spam check
        mock_msg = MagicMock()
        mock_msg.out = False
        mock_msg.text = "Spam protection: Click Continue to start sending messages."

        btn_continue = MagicMock()
        btn_continue.text = "Continue"
        btn_continue.url = None

        mock_msg.buttons = [[btn_continue]]
        mock_msg.click = AsyncMock()

        mock_client.get_messages = AsyncMock(return_value=[mock_msg])

        mock_bot = MagicMock()
        mock_bot.username = "ProtectedDealBot"

        ready = await prepare_bot_for_promo(mock_client, mock_bot, greeting="Hi", wait_seconds=0.01)
        self.assertTrue(ready)

        # Verify Continue button was clicked (row 0, col 0)
        mock_msg.click.assert_called_once_with(0, 0)
        print("  [PASS] 3. Anti-spam barrier detected and [ Continue ] button clicked successfully.")


if __name__ == "__main__":
    print("==================================================================")
    print("[TEST] Running Automated Test Suite: Bot Handshake & Language Bypass")
    print("==================================================================")
    unittest.main()

