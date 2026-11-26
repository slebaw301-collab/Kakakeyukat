import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from account_health_monitor import AccountMonitorPanel, MonitorClient


class EnrollmentPanelTest(unittest.IsolatedAsyncioTestCase):
    async def test_phone_uses_secure_link_and_qr_has_a_chat_action(self):
        client = MonitorClient("https://monitor.example", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        client.call = AsyncMock(return_value={"token": "a" * 43})
        panel = AccountMonitorPanel(42, client, None, None, None)
        _, methods = await panel.render("enroll", ["12"])
        self.assertEqual(methods.inline_keyboard[1][0].callback_data, "am:qr_generate:12")
        client.call.assert_not_called()
        _, phone = await panel.render("enroll_phone", ["12"])
        client.call.assert_awaited_once_with("enroll_ticket", id=12)
        self.assertEqual(phone.inline_keyboard[0][0].url, "https://monitor.example/auth#ticket=" + "a" * 43)

    async def test_qr_photo_is_protected_refreshed_and_removed_after_scan(self):
        client = AsyncMock()
        client.url = "https://monitor.example/v1/admin"
        image = base64.b64encode(b"fixture").decode()
        client.call.side_effect = [
            {"phase": "qr", "image": image, "qr_revision": 1, "qr_seconds": 25},
            {"phase": "qr", "image": image, "qr_revision": 2, "qr_seconds": 29},
            {"phase": "password", "password_ticket": "x" * 43},
            {"phase": "cancelled", "message": "Dibatalkan"},
        ]
        panel = AccountMonitorPanel(42, client, None, None, lambda _: {})
        bot = AsyncMock()
        bot.send_photo.return_value = SimpleNamespace(message_id=1)
        bot.send_message.return_value = SimpleNamespace(message_id=2)
        with patch("account_health_monitor.asyncio.sleep", new_callable=AsyncMock):
            await panel.watch_enrollment(bot, "a" * 43)
        bot.send_photo.assert_awaited_once()
        self.assertTrue(bot.send_photo.await_args.kwargs["protect_content"])
        bot.edit_message_media.assert_awaited_once()
        self.assertEqual(bot.edit_message_media.await_args.kwargs["message_id"], 1)
        self.assertEqual([call.args[1] for call in bot.delete_message.await_args_list], [1, 2])
        ticket = bot.send_message.await_args_list[0].kwargs["reply_markup"].inline_keyboard[0][0].url
        self.assertTrue(ticket.endswith("x" * 43))
