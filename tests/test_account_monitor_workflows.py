import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from account_health_monitor import AccountMonitorPanel


def make_panel():
    return AccountMonitorPanel(42, SimpleNamespace(call=AsyncMock()), lambda _: nullcontext(),
                               AsyncMock(), lambda _: {})


class AccountMonitorWorkflows(unittest.IsolatedAsyncioTestCase):
    async def test_channel_selection_persists_across_pages_filters_and_search(self):
        panel = make_panel()
        calls = panel.client.call
        async def api(action, **fields):
            if action == "save_peers":
                self.assertEqual(fields["ids"], [-102, -101])
                return {"added": 2}
            offset = fields.get("page", 0)
            return {"rows": [{"id": -101-offset, "title": "Grup", "role":"member", "monitored": False}],
                    "next": offset == 0, "saved": 0, "limit": 20, "available": True, "pending": False}
        calls.side_effect = api
        path = ["p", "123", "add"]
        await panel.render("pick", [*path, "-101"])
        await panel.render("picks", [*path, "1"])
        await panel.render("pick", [*path, "-102"])
        await panel.render("pickfilter", [*path, "role", "member"])
        state_view = panel.view_state(("p", 123, "add"))
        state_view["query"] = "abc"
        text, markup = await panel.render("picks", path)
        self.assertIn("2 dipilih", text)
        self.assertIn("Simpan 2 pilihan", [btn.text for row in markup.inline_keyboard for btn in row])
        await panel.render("picksave", path)
        self.assertEqual(state_view["selected"], set())

    async def test_check_all_replaces_message_with_progress_and_final(self):
        panel = make_panel()
        pending = {"success": 1, "failure": 0, "pending": 1, "deferred": 1, "total": 2, "rows": [],
                   "kind": "account", "next": False}
        final = {**pending, "pending": 0, "deferred": 0, "failure": 1}
        panel.client.call.side_effect = [{"run": 9}, pending, final]
        query = SimpleNamespace(data="am:checkall", edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=42),
                                 effective_chat=SimpleNamespace(id=42, type="private"))
        with patch("account_health_monitor.asyncio.sleep", new_callable=AsyncMock):
            await panel.callback(update, None)
            await panel.refresh_task
        data = [call.args[0] for call in query.edit_message_text.await_args_list]
        self.assertIn("Tertunda 1", data[0])
        self.assertIn("Gagal 1", data[1])
        self.assertIn("Pemeriksaan selesai", data[1])
