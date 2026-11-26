import base64
import logging
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter, TimedOut

from account_health_monitor import AccountMonitorPanel, MonitorClient, MonitorUnavailable


KEY = base64.urlsafe_b64encode(b"test key for account monitor!!!!").decode()


def update(user_id=10, chat_id=10, chat_type="private", data="am:home"):
    query = SimpleNamespace(data=data, edit_message_text=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        callback_query=query, effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )


class AccountMonitorPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(call=AsyncMock(), close=AsyncMock())
        self.ack = AsyncMock()
        self.panel = AccountMonitorPanel(10, self.client, nullcontext, self.ack, lambda bot: {"rate_limit_args": {}})

    async def test_owner_authorization_and_ack(self):
        for item in (update(11, 11), update(chat_id=-10, chat_type="supergroup"), update(chat_id=12)):
            await self.panel.callback(item, None)
            self.client.call.assert_not_awaited()
            item.callback_query.edit_message_text.assert_not_awaited()
        item = update()

        async def summary(action):
            self.ack.assert_awaited_with(item.callback_query)
            self.assertEqual(action, "summary")
            return {"total": 1, "counts": {"normal": 1}, "latest": None}
        self.client.call.side_effect = summary
        await self.panel.callback(item, None)
        edit = item.callback_query.edit_message_text.call_args
        self.assertIn("Total akun <b>1</b>", edit.args[0])
        self.assertIn("admpanel_back", [b.callback_data for row in edit.kwargs["reply_markup"].inline_keyboard for b in row])

    async def test_owner_can_return_when_service_is_offline(self):
        self.client.call.side_effect = MonitorUnavailable("Account Monitor sedang tidak tersedia.")
        item = update()
        await self.panel.callback(item, None)
        edit = item.callback_query.edit_message_text.call_args
        self.assertIn("tidak tersedia", edit.args[0])
        self.assertIn("admpanel_back", [b.callback_data for row in edit.kwargs["reply_markup"].inline_keyboard for b in row])

    async def test_buyer_command_has_no_side_effect(self):
        item = update(11, 11)
        item.effective_message.text = "/monitor_input alias 1 Akun"
        await self.panel.message(item, None)
        self.client.call.assert_not_awaited()
        item.effective_message.reply_text.assert_not_awaited()

    async def test_notification_retries_ack_without_resending(self):
        phases = [
            {"notice": {"id": 12, "lease_token": "fake-token-123", "text": "Frozen"}},
            {"ok": True}, MonitorUnavailable(), {"ok": True},
        ]
        self.client.call.side_effect = phases
        bot = SimpleNamespace(send_message=AsyncMock())
        with self.assertRaises(MonitorUnavailable):
            await self.panel._send_notice(bot)
        await self.panel._send_notice(bot)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.call_args.args, (10, "Frozen"))
        self.assertTrue(bot.send_message.call_args.kwargs["protect_content"])
        self.assertEqual(self.client.call.await_args.kwargs["outcome"], "sent")

    async def test_retry_after_and_timeout_have_distinct_outcomes(self):
        for effect, outcome in ((RetryAfter(4), "retry"), (TimedOut(), "uncertain")):
            self.client.call.side_effect = [
                {"notice": {"id": 12, "lease_token": "fake-token-123", "text": "Frozen"}},
                {"ok": True}, {"ok": True},
            ]
            bot = SimpleNamespace(send_message=AsyncMock(side_effect=effect))
            await self.panel._send_notice(bot)
            self.assertEqual(self.client.call.await_args.kwargs["outcome"], outcome)

    async def test_offline_notice_at_most_once_until_recovery(self):
        self.client.call.side_effect = [
            {"notify_system": True, "scheduler": "running"},
            *[MonitorUnavailable()] * 4,
            {"notify_system": True, "scheduler": "running"},
        ]
        bot = SimpleNamespace(send_message=AsyncMock())
        for _ in range(6):
            await self.panel._system_check(bot)
        self.assertEqual(bot.send_message.await_count, 2)


class MonitorClientTests(unittest.IsolatedAsyncioTestCase):
    def test_config_fails_closed(self):
        with self.assertRaises(ValueError):
            MonitorClient("http://example.com", KEY)
        with self.assertRaises(ValueError):
            MonitorClient("https://example.com/path", KEY)
        with patch.dict("os.environ", {"ACCOUNT_MONITOR_ENABLED": "0"}):
            self.assertIsNone(AccountMonitorPanel.from_env(1, None, None, None))

    async def test_circuit_breaker_limits_failed_requests(self):
        client = MonitorClient("https://example.com", KEY)
        from unittest.mock import Mock
        client.session = SimpleNamespace(post=Mock(side_effect=TimeoutError()))
        for _ in range(5):
            with self.assertRaises(MonitorUnavailable):
                await client.call("system")
        self.assertEqual(client.session.post.call_count, 3)

    def test_incomplete_configuration_logs_no_credentials(self):
        with patch.dict("os.environ", {
            "ACCOUNT_MONITOR_ENABLED": "1", "ACCOUNT_MONITOR_URL": "https://example.com",
            "ACCOUNT_MONITOR_API_KEY": "credential-fixture",
        }):
            with self.assertLogs("account_health_monitor", logging.WARNING) as logs:
                self.assertIsNone(AccountMonitorPanel.from_env(1, None, None, None))
            self.assertNotIn("credential-fixture", "\n".join(logs.output))
