import unittest
from unittest.mock import AsyncMock

from account_health_monitor import AccountMonitorPanel, MonitorClient


class EnrollmentPanelTest(unittest.IsolatedAsyncioTestCase):
    async def test_reauth_requests_a_scoped_single_use_link(self):
        client = MonitorClient("https://monitor.example", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        client.call = AsyncMock(return_value={"token": "a" * 43})
        panel = AccountMonitorPanel(42, client, None, None, None)
        message, markup = await panel.render("enroll", ["12"])
        client.call.assert_awaited_once_with("enroll_ticket", id=12)
        self.assertIn("--replace 12", message)
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://monitor.example/auth#ticket=" + "a" * 43)
