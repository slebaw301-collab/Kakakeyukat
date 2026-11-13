import ast
import asyncio
import os
from pathlib import Path
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from claim_free import ClaimFree, member_ok, parse_chat, parse_link, INTRO, FAILED, SUCCESS, UNAVAILABLE
from telegram.error import TelegramError


def config(enabled=True):
    return dict(id=1, enabled=enabled, testi_chat=-10011, testi_link='https://t.me/testigroup',
                free_chat=-10022, version=1)


def update(uid=123, data='cf_verify'):
    message = NS(reply_text=AsyncMock(), text='')
    return NS(effective_user=NS(id=uid), effective_chat=NS(type='private'),
              effective_message=message, callback_query=NS(data=data, answer=AsyncMock()))


def context():
    bot = NS(id=99, get_chat_member=AsyncMock(return_value=NS(status='administrator')),
             create_chat_invite_link=AsyncMock(return_value=NS(invite_link='https://t.me/+private')),
             revoke_chat_invite_link=AsyncMock(), get_chat=AsyncMock())
    return NS(bot=bot, user_data={}, args=[])


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = ClaimFree(Mock(), AsyncMock(), AsyncMock(return_value=False), AsyncMock(return_value=False))
        self.u, self.ctx = update(), context()
        self.cfg = config()
        self.f.config = AsyncMock(side_effect=lambda: dict(self.cfg))
        async def call(action, *args):
            return {'reserve': True, 'finish': True, 'latest': None}.get(action)
        self.f.call = AsyncMock(side_effect=call)

    def text(self):
        return self.u.effective_message.reply_text.call_args.args[0]

    async def test_disabled_deep_link(self):
        self.cfg['enabled'] = False
        await self.f.show(self.u, self.ctx)
        self.assertEqual(self.text(), UNAVAILABLE)

    async def test_enabled_deep_link(self):
        await self.f.show(self.u, self.ctx)
        self.assertEqual(self.text(), INTRO)

    async def test_success(self):
        await self.f.verify(self.u, self.ctx)
        self.assertEqual(self.text(), SUCCESS)
        self.assertTrue(self.ctx.bot.create_chat_invite_link.call_args.kwargs['creates_join_request'])
        self.assertIn('expire_date', self.ctx.bot.create_chat_invite_link.call_args.kwargs)
        self.f.call.assert_any_await('release', 123, self.f.call.call_args.args[-1])

    async def test_not_member(self):
        self.ctx.bot.get_chat_member.side_effect = lambda chat, uid: NS(status='administrator' if uid == 99 else 'left')
        await self.f.verify(self.u, self.ctx)
        self.assertEqual(self.text(), FAILED)
        self.ctx.bot.create_chat_invite_link.assert_not_awaited()

    async def test_api_error_fails_closed(self):
        self.ctx.bot.get_chat_member.side_effect = TelegramError('offline')
        await self.f.verify(self.u, self.ctx)
        self.ctx.bot.create_chat_invite_link.assert_not_awaited()

    async def test_bot_demoted_fails_closed(self):
        self.ctx.bot.get_chat_member.return_value = NS(status='member')
        await self.f.verify(self.u, self.ctx)
        self.ctx.bot.create_chat_invite_link.assert_not_awaited()

    async def test_banned(self):
        self.f.is_banned.return_value = True
        await self.f.verify(self.u, self.ctx)
        self.ctx.bot.create_chat_invite_link.assert_not_awaited()

    async def test_disabled_callback(self):
        self.cfg['enabled'] = False
        await self.f.verify(self.u, self.ctx)
        self.assertEqual(self.text(), UNAVAILABLE)

    async def test_db_lease_busy(self):
        self.f.call.side_effect = lambda action, *args: False
        await self.f.verify(self.u, self.ctx)
        self.ctx.bot.create_chat_invite_link.assert_not_awaited()

    async def test_recovery_reuses_link(self):
        self.f.call.side_effect = lambda action, *args: {'reserve': True, 'latest': 'https://t.me/+original'}.get(action)
        await self.f.verify(self.u, self.ctx)
        self.assertEqual(self.text(), SUCCESS)
        self.ctx.bot.create_chat_invite_link.assert_not_awaited()
        self.assertEqual(self.u.effective_message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url, 'https://t.me/+original')

    async def test_invite_error_does_not_finish(self):
        self.ctx.bot.create_chat_invite_link.side_effect = TelegramError('forbidden')
        await self.f.verify(self.u, self.ctx)
        self.assertNotIn('finish', [c.args[0] for c in self.f.call.await_args_list])

    async def test_changed_configuration_revokes_unissued_link(self):
        self.f.call.side_effect = lambda action, *args: action == 'reserve'
        await self.f.verify(self.u, self.ctx)
        self.ctx.bot.revoke_chat_invite_link.assert_awaited_once()
        self.assertNotEqual(self.text(), SUCCESS)

    async def test_database_failure_revokes_link(self):
        async def fail(action, *args):
            if action == 'finish':
                raise RuntimeError('db unavailable')
            return action == 'reserve'
        self.f.call.side_effect = fail
        await self.f.verify(self.u, self.ctx)
        self.ctx.bot.revoke_chat_invite_link.assert_awaited_once()

    async def test_repeated_concurrent_click(self):
        await asyncio.gather(self.f.verify(self.u, self.ctx), self.f.verify(self.u, self.ctx))
        self.ctx.bot.create_chat_invite_link.assert_awaited_once()

    async def test_admin_callback_denied(self):
        for data in ('cf_admin', 'cf_testi', 'cf_free', 'cf_stats', 'cf_toggle', 'cf_cancel'):
            self.u.callback_query.data = data
            await self.f.callback(self.u, self.ctx)
        self.f.config.assert_not_awaited()
        self.f.call.assert_not_awaited()

    async def test_admin_text_denied(self):
        self.ctx.user_data['awaiting_claim_free_edit'] = 'cf_free'
        self.assertTrue(await self.f.text_input(self.u, self.ctx))
        self.f.call.assert_not_awaited()

    async def test_incomplete_config_cannot_activate(self):
        self.f.is_admin.return_value = True
        self.cfg['enabled'], self.cfg['free_chat'] = False, None
        self.u.callback_query.data = 'cf_toggle'
        await self.f.callback(self.u, self.ctx)
        self.f.call.assert_not_awaited()

    async def test_invalid_text_not_saved(self):
        self.f.is_admin.return_value = True
        self.ctx.user_data['awaiting_claim_free_edit'] = 'cf_testi'
        self.u.effective_message.text = '-10011\nhttps://evil.test/steal'
        await self.f.text_input(self.u, self.ctx)
        self.f.call.assert_not_awaited()

    async def test_save_disables_feature(self):
        self.f.is_admin.return_value = True
        self.f.validate_chat = AsyncMock()
        self.f.call.side_effect = None
        self.f.call.return_value = True
        self.ctx.user_data['awaiting_claim_free_edit'] = 'cf_free'
        self.u.effective_message.text = '-10033'
        await self.f.text_input(self.u, self.ctx)
        self.f.call.assert_awaited_once_with('save', -10011, 'https://t.me/testigroup', -10033, 1)
        self.assertNotIn('awaiting_claim_free_edit', self.ctx.user_data)

    async def test_public_free_group_rejected(self):
        self.ctx.bot.get_chat.return_value = NS(type='supergroup', username='publicgroup')
        self.ctx.bot.get_chat_member.return_value = NS(status='administrator', can_invite_users=True)
        with self.assertRaises(ValueError):
            await self.f.validate_chat(self.ctx.bot, -10022, free=True)

    async def test_missing_invite_permission_rejected(self):
        self.ctx.bot.get_chat.return_value = NS(type='supergroup', username=None)
        self.ctx.bot.get_chat_member.return_value = NS(status='administrator', can_invite_users=False)
        with self.assertRaises(ValueError):
            await self.f.validate_chat(self.ctx.bot, -10022, free=True)

    async def test_mismatched_testimonial_link_rejected(self):
        self.ctx.bot.get_chat.return_value = NS(type='supergroup', username='realgroup', invite_link=None)
        with self.assertRaises(ValueError):
            await self.f.validate_chat(self.ctx.bot, -10011, 'https://t.me/wronggroup')

    async def join(self, owner=123, **changes):
        row = dict(user_id=owner, valid=True, chat_id=-10022, config_version=1)
        row.update(changes)
        self.f.call.side_effect = lambda action, *args: row if action == 'invite' else None
        req = NS(invite_link=NS(name='hf-free-test', invite_link='https://t.me/+private'),
                 from_user=NS(id=123), chat=NS(id=-10022), approve=AsyncMock(), decline=AsyncMock())
        self.u.chat_join_request = req
        self.assertTrue(await self.f.join_request(self.u, self.ctx))
        return req

    async def test_join_owner_approved(self):
        req = await self.join()
        req.approve.assert_awaited_once()
        req.decline.assert_not_awaited()

    async def test_shared_link_other_user_declined(self):
        req = await self.join(owner=456)
        req.decline.assert_awaited_once()
        req.approve.assert_not_awaited()
        self.ctx.bot.revoke_chat_invite_link.assert_not_awaited()

    async def test_expired_link_declined(self):
        req = await self.join(valid=False)
        req.approve.assert_not_awaited()

    async def test_changed_group_declined(self):
        req = await self.join(config_version=0)
        req.approve.assert_not_awaited()

    async def test_disabled_join_declined(self):
        self.cfg['enabled'] = False
        req = await self.join()
        req.approve.assert_not_awaited()

    async def test_left_testimonial_before_join(self):
        self.ctx.bot.get_chat_member.side_effect = lambda chat, uid: NS(status='administrator' if uid == 99 else 'left')
        req = await self.join()
        req.approve.assert_not_awaited()

    async def test_join_api_failure(self):
        self.ctx.bot.get_chat_member.side_effect = TelegramError('offline')
        req = await self.join()
        req.approve.assert_not_awaited()
        req.decline.assert_awaited_once()

    async def test_paid_invite_not_intercepted(self):
        self.u.chat_join_request = NS(invite_link=NS(name='Paid product'))
        self.assertFalse(await self.f.join_request(self.u, self.ctx))
        self.f.call.assert_not_awaited()


class ValidationTests(unittest.TestCase):
    def test_membership_statuses(self):
        for status in ('creator', 'administrator', 'member'):
            self.assertTrue(member_ok(NS(status=status)))
        for status in ('left', 'kicked'):
            self.assertFalse(member_ok(NS(status=status)))
        self.assertFalse(member_ok(NS(status='restricted', is_member=False)))
        self.assertTrue(member_ok(NS(status='restricted', is_member=True)))

    def test_chat_and_link_validation(self):
        self.assertEqual(parse_chat('-10012345'), -10012345)
        self.assertEqual(parse_link('https://t.me/+abc_123'), 'https://t.me/+abc_123')
        for value in ('123', '@somegroup', '-9223372036854775809', '-1; DROP TABLE orders'):
            with self.assertRaises(ValueError):
                parse_chat(value)
        for value in ('http://t.me/hello', 'https://t.me.evil/hello', 'https://t.me/hello?start=x', 'https://user@t.me/hello'):
            with self.assertRaises(ValueError):
                parse_link(value)

    def test_start_dispatch_and_normal_menu(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot.py').read_text())
        start = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'start')
        host = dict(PerfTrace=Mock(return_value=NS(finish=Mock())), _start_handler=AsyncMock(),
                    _claim_free=NS(show=AsyncMock()), Update=object, ContextTypes=NS(DEFAULT_TYPE=object))
        exec(compile(ast.Module(body=[start], type_ignores=[]), '<start>', 'exec'), host)
        ctx = context()
        asyncio.run(host['start'](update(), ctx))
        host['_start_handler'].assert_awaited_once()
        ctx.args = ['claim_free']
        asyncio.run(host['start'](update(), ctx))
        host['_claim_free'].show.assert_awaited_once()
        host['_start_handler'].assert_awaited_once()
        # Claim callbacks are only on admin keyboard, never the buyer catalog.
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in ('build_main_menu_parts', '_start_handler'):
                self.assertNotIn('cf_', ast.unparse(n))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL to an isolated PostgreSQL database')
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        from psycopg2.extras import RealDictCursor
        @contextmanager
        def session():
            conn = psycopg2.connect(os.environ['TEST_DATABASE_URL'], cursor_factory=RealDictCursor)
            conn.autocommit = True
            try:
                yield conn
            finally:
                conn.close()
        cls.f = ClaimFree(session, None, None, None)
        cls.f.migrate()

    def setUp(self):
        with self.f.session() as conn, conn.cursor() as c:
            c.execute('TRUNCATE claim_free_invites,claim_free_claims')
            c.execute('UPDATE claim_free_config SET enabled=TRUE,testi_chat=-10011,testi_link=%s,free_chat=-10022,version=1', ('https://t.me/testigroup',))

    def test_concurrent_lease_and_unique_right(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.f.db('reserve', 123, str(i)), range(8)))
        self.assertEqual(sum(results), 1)
        winner = str(results.index(True))
        exp = datetime.now(timezone.utc) + timedelta(minutes=15)
        self.assertTrue(self.f.db('finish', 123, winner, 'https://t.me/+one', -10022, 1, exp))
        self.assertFalse(self.f.db('finish', 123, winner, 'https://t.me/+duplicate', -10022, 1, exp))
        self.assertTrue(self.f.db('reserve', 123, 'recovery'))
        self.assertTrue(self.f.db('finish', 123, 'recovery', 'https://t.me/+two', -10022, 1, exp))
        self.assertEqual(self.f.db('stats'), dict(total=1, today=1, unique_users=1))

    def test_failed_issuance_not_counted_and_stale_finish_rejected(self):
        self.f.db('reserve', 123, 'pending')
        self.assertEqual(self.f.db('stats')['total'], 0)
        self.f.db('toggle', False, 1)
        self.assertFalse(self.f.db('finish', 123, 'pending', 'https://t.me/+bad', -10022, 1, datetime.now(timezone.utc)))
        self.assertEqual(self.f.db('stats')['total'], 0)

    def test_additive_idempotent_migration(self):
        self.f.db('reserve', 123, 'pending')
        self.f.migrate()
        self.assertFalse(self.f.db('reserve', 123, 'other'))
        self.assertTrue(self.f.db('config')['enabled'])

    def test_recovery_link_consumption_and_config_disable(self):
        self.f.db('reserve', 123, 'pending')
        self.f.db('finish', 123, 'pending', 'https://t.me/+ok', -10022, 1, datetime.now(timezone.utc)+timedelta(minutes=15))
        self.assertEqual(self.f.db('latest', 123, 1), 'https://t.me/+ok')
        self.f.db('consume', 'https://t.me/+ok')
        self.assertIsNone(self.f.db('latest', 123, 1))
        self.assertFalse(self.f.db('invite', 'https://t.me/+ok')['valid'])
        self.assertTrue(self.f.db('save', -10011, 'https://t.me/testigroup', -10033, 1))
        self.assertFalse(self.f.db('config')['enabled'])
        self.assertEqual(self.f.db('stats')['total'], 1)


if __name__ == '__main__':
    unittest.main()
