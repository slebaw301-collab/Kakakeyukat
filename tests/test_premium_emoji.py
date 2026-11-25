import asyncio
from contextlib import contextmanager
from io import BytesIO
import json
import os
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity
from telegram.error import NetworkError, TimedOut
from telegram.request import BaseRequest

from premium_emoji import PremiumEmoji, PremiumEmojiBot, SETTING
from premium_emoji_catalog import EMOJI_CATALOG

with patch.dict(os.environ, {
    'BOT_TOKEN': '123:offline', 'ADMIN_ID': '456',
    'DATABASE_URL': 'postgresql://unused', 'AUTOGOPAY_API_KEY': 'offline',
}):
    import bot as store


class TelegramRequest(BaseRequest):
    """Exercise real PTB serialization without making Telegram requests."""

    def __init__(self):
        self.calls = []
        self.behavior = 'valid'
        self.message_id = 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **_):
        endpoint = url.rsplit('/', 1)[1]
        data = request_data.parameters
        self.calls.append((endpoint, data, request_data.multipart_data))
        if endpoint == 'getCustomEmojiStickers':
            items = [
                {'custom_emoji_id': emoji_id, 'emoji': alt, 'type': 'custom_emoji',
                 'file_id': emoji_id, 'file_unique_id': emoji_id, 'width': 100,
                 'height': 100, 'is_animated': False, 'is_video': False}
                for emoji_id, alt in dict(EMOJI_CATALOG.values()).items()
                if emoji_id in data['custom_emoji_ids']
            ]
            return 200, json.dumps({'ok': True, 'result': items}).encode()
        if endpoint == 'answerCallbackQuery':
            return 200, b'{"ok":true,"result":true}'
        chat_id = int(data['chat_id'])
        chat_type = 'private' if chat_id > 0 else 'channel'
        result = {
            'message_id': data.get('message_id', self.message_id),
            'chat': {'id': chat_id, 'type': chat_type}, 'date': 1,
        }
        if endpoint.startswith('send'):
            self.message_id += 1
        if 'photo' in data or endpoint == 'sendPhoto':
            result['photo'] = [
                {'file_id': 'catalog-photo', 'file_unique_id': 'photo-identity', 'width': 128, 'height': 128}
            ]
        field = 'caption' if 'caption' in data else 'text'
        text = data.get(field, '')
        emoji_ids = re.findall(r'<tg-emoji emoji-id="(\d+)">', text)
        result[field] = re.sub(r'<[^>]+>', '', text)
        keyboard = data.get('reply_markup')
        if keyboard:
            result['reply_markup'] = json.loads(json.dumps(keyboard))
        buttons = [b for row in (keyboard or {}).get('inline_keyboard', []) for b in row]
        enhanced = bool(emoji_ids or any(b.get('icon_custom_emoji_id') for b in buttons))
        entity_field = 'caption_entities' if field == 'caption' else 'entities'
        if enhanced and self.behavior == 'reject':
            return 400, b'{"ok":false,"description":"Bad Request: custom emoji not allowed"}'
        if enhanced and self.behavior == 'network':
            raise TimedOut
        if self.behavior != 'strip_text':
            result[entity_field] = [dict(type='custom_emoji', offset=0, length=2, custom_emoji_id=i)
                                    for i in emoji_ids]
        if self.behavior == 'strip_buttons' and keyboard:
            for row in result['reply_markup']['inline_keyboard']:
                for button in row:
                    button.pop('icon_custom_emoji_id', None)
        return 200, json.dumps({'ok': True, 'result': result}).encode()


def make_bot(engine, request):
    return PremiumEmojiBot(premium_emoji=engine, token='123:offline',
                           request=request, get_updates_request=request)


class PremiumEmojiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.request = TelegramRequest()
        self.engine = PremiumEmoji()
        self.bot = make_bot(self.engine, self.request)
        await self.engine.validate(self.bot)
        self.request.calls.clear()
        self.keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ Bayar sekarang', callback_data='pay', api_kwargs={'style': 'success'}),
            InlineKeyboardButton('Kembali ⬅️', url='https://example.test/', api_kwargs={'style': 'danger'}),
        ]])

    async def test_off_retains_exact_payload(self):
        text = '<b>⭐ Barang</b>\n\n🔗 Akses'
        await self.bot.send_message(100, text, parse_mode='HTML',
                                    reply_markup=self.keyboard, protect_content=True)
        self.assertEqual(len(self.request.calls), 1)
        data = self.request.calls[0][1]
        self.assertEqual(data['text'], text)
        self.assertEqual(data['reply_markup'], self.keyboard.to_dict())
        self.assertTrue(data['protect_content'])

    async def test_html_markup_and_caption_with_file_id(self):
        self.engine.set_enabled(True)
        text = ('<blockquote><b>📦 Paket</b> <tg-spoiler>🔒 Rahasia</tg-spoiler></blockquote>\n'
                '<a href="https://example.test/💎">🔗 Link</a> <code>✅</code>\n'
                '👍🏽 👋🏽 👥 https://example.test/💎\n'
                '<tg-emoji emoji-id="123">✅</tg-emoji>')
        result = await self.bot.send_photo(100, 'saved-photo', caption=text, parse_mode='HTML',
                                           reply_markup=self.keyboard, protect_content=True)
        self.assertEqual(len(self.request.calls), 1)
        endpoint, data, files = self.request.calls[0]
        self.assertEqual(endpoint, 'sendPhoto')
        self.assertEqual(data['photo'], 'saved-photo')
        self.assertEqual(files, {})
        self.assertEqual(result.photo[0].file_id, 'catalog-photo')
        self.assertTrue(data['protect_content'])
        self.assertIn('<blockquote><b><tg-emoji', data['caption'])
        self.assertIn('<tg-spoiler><tg-emoji', data['caption'])
        self.assertIn('<a href="https://example.test/💎">🔗 Link</a> <code>✅</code>', data['caption'])
        self.assertIn('👍🏽 👋🏽', data['caption'])
        self.assertIn('https://example.test/💎\n<tg-emoji emoji-id="123">✅</tg-emoji>', data['caption'])
        first, second = data['reply_markup']['inline_keyboard'][0]
        self.assertEqual(first, {'text': 'Bayar sekarang', 'callback_data': 'pay',
                                 'style': 'success', 'icon_custom_emoji_id': self.engine.ids['✅']})
        self.assertEqual(second['text'], 'Kembali')
        self.assertEqual(second['url'], 'https://example.test/')
        self.assertEqual(second['style'], 'danger')

    async def test_render_plain_and_preserve_explicit_entities(self):
        self.engine.set_enabled(True)
        await self.bot.send_message(100, '🔒 <literal> & name')
        data = self.request.calls[-1][1]
        self.assertEqual(data['parse_mode'], 'HTML')
        self.assertIn('&lt;literal&gt; &amp; name', data['text'])
        await self.bot.send_message(100, '🔒 Name', entities=[MessageEntity('bold', 0, 2)])
        self.assertEqual(self.request.calls[-1][1]['text'], '🔒 Name')
        self.assertNotIn('parse_mode', self.request.calls[-1][1])

    async def test_group_supergroup_and_channel(self):
        self.engine.set_enabled(True)
        for chat_id, chat_type in ((-1, 'group'), (-2, 'supergroup'), (-1003, 'channel')):
            self.engine.remember_chat(chat_id, chat_type)
            await self.bot.send_message(chat_id, '📦 Katalog', reply_markup=self.keyboard)
            data = self.request.calls[-1][1]
            self.assertEqual('<tg-emoji' in data['text'], chat_type != 'channel')
            self.assertEqual('icon_custom_emoji_id' in data['reply_markup']['inline_keyboard'][0][0],
                             chat_type != 'channel')
        await self.bot.send_message(-1004, '📦 Testimoni')
        self.assertEqual(self.request.calls[-1][1]['text'], '📦 Testimoni')

    async def test_rejected_upload_fallback_preserves_payload_and_cooldown(self):
        self.engine.set_enabled(True)
        self.request.behavior = 'reject'
        raw_photo = BytesIO(b'jpeg-test-bytes')
        await self.bot.send_photo(100, raw_photo, caption='📦 QRIS', parse_mode='HTML',
                                  reply_markup=self.keyboard, protect_content=True)
        self.assertEqual([c[0] for c in self.request.calls], ['sendPhoto', 'sendPhoto'])
        attempted, fallback = (c[1] for c in self.request.calls)
        self.assertIn('<tg-emoji', attempted['caption'])
        self.assertEqual(fallback['caption'], '📦 QRIS')
        self.assertEqual(fallback['reply_markup'], self.keyboard.to_dict())
        self.assertTrue(fallback['protect_content'])
        for _, _, files in self.request.calls:
            self.assertEqual(files['photo'][1], b'jpeg-test-bytes')
        await self.bot.send_photo(100, 'file-id', caption='📦 QRIS', reply_markup=self.keyboard)
        self.assertEqual(len(self.request.calls), 3)
        self.assertEqual(self.request.calls[-1][1]['caption'], '📦 QRIS')

    async def test_uncertain_network_outcome_is_never_retried(self):
        self.engine.set_enabled(True)
        self.request.behavior = 'network'
        with self.assertRaises(NetworkError):
            await self.bot.send_message(100, '✅ Pembayaran telah diterima')
        self.assertEqual(len(self.request.calls), 1)

    async def test_missing_button_icon_repairs_same_message_and_keeps_text(self):
        self.engine.set_enabled(True)
        self.request.behavior = 'strip_buttons'
        await self.bot.send_message(100, '📦 Paket', reply_markup=self.keyboard)
        self.assertEqual([c[0] for c in self.request.calls], ['sendMessage', 'editMessageReplyMarkup'])
        self.assertEqual(self.request.calls[-1][1]['message_id'], 1)
        self.assertEqual(self.request.calls[-1][1]['reply_markup'], self.keyboard.to_dict())
        await self.bot.send_message(100, '📦 Paket', reply_markup=self.keyboard)
        self.assertIn('<tg-emoji', self.request.calls[-1][1]['text'])
        self.assertEqual(self.request.calls[-1][1]['reply_markup'], self.keyboard.to_dict())

    async def test_silently_removed_text_restores_original_format_and_alias(self):
        self.engine.set_enabled(True)
        self.request.behavior = 'strip_text'
        text = '<b>📜 Riwayat</b>\n🔒 <i>Privat</i>'
        await self.bot.send_message(100, text, parse_mode='HTML')
        self.assertEqual([c[0] for c in self.request.calls], ['sendMessage', 'editMessageText'])
        self.assertEqual(self.request.calls[-1][1]['text'], text)
        self.assertEqual(self.request.calls[-1][1]['parse_mode'], 'HTML')
        self.assertEqual(self.request.calls[-1][1]['message_id'], 1)

    async def test_validation_batched_cached_and_rejects_wrong_metadata(self):
        engine = PremiumEmoji()
        package_id = EMOJI_CATALOG['📦'][0]
        diamond_id = EMOJI_CATALOG['💎'][0]
        bot = SimpleNamespace(get_custom_emoji_stickers=AsyncMock(return_value=[
            SimpleNamespace(custom_emoji_id=package_id, emoji='📦'),
            SimpleNamespace(custom_emoji_id=diamond_id, emoji='🚫'),
        ]))
        await asyncio.gather(engine.validate(bot), engine.validate(bot))
        bot.get_custom_emoji_stickers.assert_awaited_once()
        self.assertEqual(engine.ids, {'📦': package_id})
        self.assertLessEqual(len(bot.get_custom_emoji_stickers.call_args.args[0]), 200)

    async def test_preview_verifies_text_caption_and_button_while_off(self):
        message = Message.de_json(
            {'message_id': 3, 'chat': {'id': 100, 'type': 'private'}, 'date': 1}, self.bot,
        )
        with patch.object(store, '_premium_emoji', self.engine), patch.object(store, '_MENU_PHOTO_FILE_ID', 'cached'):
            self.assertTrue(await store._preview_premium_emoji(message, self.bot))
        self.assertFalse(self.engine.enabled)
        self.assertIsNone(self.engine.preview_target.get())
        self.assertEqual([c[0] for c in self.request.calls], ['sendMessage', 'sendPhoto'])

    async def test_product_delivery_and_public_testimonial(self):
        self.engine.set_enabled(True)
        paket = {'nama': 'GB BASIC', 'emoji': '📦', 'harga': 5000, 'link': 'https://example.test/akses'}
        with patch.object(store, 'generate_group_link', AsyncMock(return_value=None)), \
             patch.object(store, 'set_buyer_msg_id', AsyncMock()) as save:
            link, group = await store._send_buyer_product_link(self.bot, 100, 'order', paket, 5000, 'Dibayar')
        self.assertFalse(group)
        self.assertEqual(link, paket['link'])
        save.assert_awaited_once_with('order', 1)
        payload = self.request.calls[-1][1]
        self.assertTrue(payload['protect_content'])
        self.assertIn('<tg-emoji', payload['text'])
        self.assertIn(link, [b.get('url') for row in payload['reply_markup']['inline_keyboard'] for b in row])

        order = {'order_id': 'order', 'status': 'completed', 'paket_id': 'basic', 'user_id': 100}
        settings = {'testimoni_channel_id': '-100123', 'link_admin': 'https://example.test/admin'}
        self.engine.remember_chat(-100123, 'channel')
        with patch.object(PremiumEmojiBot, 'username', 'offline_bot'), \
             patch.object(store, 'get_setting', AsyncMock(side_effect=lambda key, *_: settings.get(key))), \
             patch.object(store, 'get_order_by_id', AsyncMock(return_value=order)), \
             patch.object(store, 'get_product', AsyncMock(return_value=paket)), \
             patch.object(store, 'run_db', AsyncMock(return_value=True)) as db:
            self.assertEqual(await store._publish_public_testimonial(self.bot, 'order'), 'sent')
        self.assertEqual(db.await_count, 2)
        payload = self.request.calls[-1][1]
        self.assertEqual(payload['text'], store._format_public_transaction(order, paket))
        self.assertTrue(payload['disable_web_page_preview'])
        self.assertEqual(len(self.request.calls), 2)


    async def test_qris_invoice_created_once_when_premium_caption_is_rejected(self):
        self.engine.set_enabled(True)
        trans_data = {'qr_string': 'qr-test', 'fee': 250, 'amount': 5000,
                      'checkout_url': 'https://example.test/checkout'}
        paket = {'paket_id': 'basic', 'nama': 'GB BASIC', 'emoji': '📦', 'harga': 5000}
        context = SimpleNamespace(bot=self.bot, user_data={}, bot_data={})
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=100))

        async def qr_image(*args):
            self.request.behavior = 'reject'
            return BytesIO(b'qr-test-image')

        def skip_background(coroutine, **kwargs):
            coroutine.close()

        with patch.object(store, '_reject_new_invoice_during_maintenance', AsyncMock(return_value=False)), \
             patch.object(store, 'create_transaction_qris', AsyncMock(return_value=trans_data)) as create, \
             patch.object(store, 'save_order', AsyncMock()) as save, \
             patch.object(store, 'run_cpu', AsyncMock(side_effect=qr_image)), \
             patch.object(store, 'set_buyer_msg_id', AsyncMock()) as track, \
             patch.object(store, 'spawn_background', side_effect=skip_background), \
             patch.object(store, '_start_payment_task') as monitor:
            await store._buat_order_baru(update, context, None, 100, 'Test Buyer', paket)

        create.assert_awaited_once()
        save.assert_awaited_once()
        track.assert_awaited_once()
        monitor.assert_called_once()
        self.assertEqual(save.call_args.kwargs['harga_dibayar'], 5250)
        self.assertEqual([c[0] for c in self.request.calls], ['sendMessage', 'sendPhoto', 'sendPhoto'])
        _, fallback, files = self.request.calls[-1]
        self.assertIn('📱 <b>Scan QRIS di atas untuk membayar</b>', fallback['caption'])
        self.assertIn('https://example.test/checkout',
                      [b.get('url') for row in fallback['reply_markup']['inline_keyboard'] for b in row])
        self.assertEqual(files['photo'][1], b'qr-test-image')

    async def test_resend_protection_and_button_link(self):
        self.engine.set_enabled(True)
        order = {'order_id': 'order', 'status': 'completed', 'paket_id': 'basic', 'delivery_status': 'sent'}
        product = {'nama': 'GB BASIC', 'emoji': '📦', 'harga': 5000, 'link': 'https://example.test/akses'}
        query = SimpleNamespace(data='resend_select|1', from_user=SimpleNamespace(id=100))
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(bot=self.bot, user_data={})
        with patch.object(store, '_resend_access_allowed', AsyncMock(return_value=True)), \
             patch.object(store, '_schedule_navigation_ack'), \
             patch.object(store, 'get_resend_order', AsyncMock(return_value=order)), \
             patch.object(store, 'get_resend_product', AsyncMock(return_value=product)), \
             patch.object(store, 'set_sent_link', AsyncMock()) as save:
            await store.resend_group_link(update, context)
        save.assert_awaited_once_with('order', product['link'])
        self.assertEqual(len(self.request.calls), 1)
        payload = self.request.calls[-1][1]
        self.assertTrue(payload['protect_content'])
        self.assertIn('<tg-emoji', payload['text'])
        self.assertIn(product['link'], [b.get('url') for row in payload['reply_markup']['inline_keyboard'] for b in row])


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL to an isolated PostgreSQL database')
class PremiumEmojiSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_toggle_persists_and_boot_loads_same_setting(self):
        import psycopg2
        from psycopg2.extras import RealDictCursor

        @contextmanager
        def session():
            conn = psycopg2.connect(os.environ['TEST_DATABASE_URL'], cursor_factory=RealDictCursor)
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

        request = TelegramRequest()
        engine = PremiumEmoji()
        engine.record_preview(True)
        bot = make_bot(engine, request)
        query = SimpleNamespace(data='admpanel_emoji|on', answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=store.ADMIN_ID),
                                 effective_chat=SimpleNamespace(type='private'), callback_query=query)
        with session() as conn, conn.cursor() as cursor:
            cursor.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
            cursor.execute('DELETE FROM settings WHERE key=%s', (SETTING,))
        with patch.object(store, '_premium_emoji', engine), patch.object(store, 'db_session_safe', session):
            await store.admpanel_emoji(update, SimpleNamespace(bot=bot))
            self.assertTrue(engine.enabled)
            self.assertEqual(store._get_setting_sync(SETTING), '1')
            restarted = PremiumEmoji()
            with patch.object(store, '_premium_emoji', restarted):
                await store._load_premium_emoji(bot)
                self.assertTrue(restarted.enabled)
                query.data = 'admpanel_emoji|off'
                await store.admpanel_emoji(update, SimpleNamespace(bot=bot))
                self.assertFalse(restarted.enabled)
                self.assertEqual(store._get_setting_sync(SETTING), '0')


if __name__ == '__main__':
    unittest.main()
