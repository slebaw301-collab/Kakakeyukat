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

from premium_emoji import PremiumEmoji, PremiumEmojiBot, SETTING, input_custom_emoji
from premium_emoji_catalog import EMOJI_CATALOG
from premium_emoji_manager import STATE
from premium_emoji_slots import LOCATIONS, button_key, emoji_scope, glyph_key, surface

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
                 'height': 100, 'is_animated': True, 'is_video': False}
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
                                    for i in emoji_ids if self.behavior != ('strip_id', i)]
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

    async def test_catalog_buttons_use_premium_badges_and_preserve_normal_fallback(self):
        labels = ('🎉 Elite', '👠 STW VIP', '📺 OME TV', '✨ Hijabs VIP',
                  '🍭 Paket', '👑 VVIP', '♠️ Exclusive', '🦊 Produk Baru')
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=f'pilih_{index}',
                                  api_kwargs={'style': 'primary'})]
            for index, label in enumerate(labels)
        ])
        self.engine.set_enabled(True)
        await self.bot.send_message(100, '📌 Detail · ♾️ Akses · 🔗 Link · ➕ Tambah',
                                    reply_markup=keyboard)
        payload = self.request.calls[-1][1]
        self.assertEqual(payload['text'].count('<tg-emoji'), 4)
        rows = payload['reply_markup']['inline_keyboard']
        for index, (row, label) in enumerate(zip(rows, labels, strict=True)):
            button = row[0]
            source, name = label.split(' ', 1)
            self.assertEqual(button, {
                'text': name, 'callback_data': f'pilih_{index}', 'style': 'primary',
                'icon_custom_emoji_id': self.engine.ids.get(source.rstrip('️'), self.engine.ids['📦']),
            })
        self.engine.set_enabled(False)
        await self.bot.send_message(100, '📦 Produk', reply_markup=keyboard)
        self.assertEqual(self.request.calls[-1][1]['reply_markup'], keyboard.to_dict())

    async def test_screen_message_button_and_product_overrides_are_independent(self):
        self.engine.set_enabled(True)
        self.engine.set_override(glyph_key('claim_intro', 'text', '📢'), ('777', '⭐'))
        self.engine.set_override(glyph_key('claim_intro', 'button', '📢'), ('888', '⭐'))
        self.engine.set_override(button_key('claim_intro', 'join_review'), ('999', '⭐'))
        self.engine.set_override('product/first', ('111', '📦'))
        self.engine.set_override('product/second', ('222', '📦'))
        plain = InlineKeyboardMarkup([
            [InlineKeyboardButton('📢 Gabung Testimoni', url='https://example.test/')],
            [InlineKeyboardButton('📢 Syarat', callback_data='requirements')],
        ])
        token = surface.set('claim_intro')
        try:
            await self.bot.send_message(100, '📢 Claim', reply_markup=plain)
            payload = self.request.calls[-1][1]
            self.assertIn('emoji-id="777"', payload['text'])
            keyboard = payload['reply_markup']['inline_keyboard']
            self.assertEqual([row[0]['icon_custom_emoji_id'] for row in keyboard], ['999', '888'])
            self.engine.set_override(button_key('claim_intro', 'join_review'), None)
            await self.bot.send_message(100, '📢 Claim', reply_markup=plain)
            keyboard = self.request.calls[-1][1]['reply_markup']['inline_keyboard']
            self.assertEqual([row[0]['icon_custom_emoji_id'] for row in keyboard], ['888', '888'])
        finally:
            surface.reset(token)
        first, second = (self.engine.mark_product(key, '📦') for key in ('first', 'second'))
        await self.bot.send_message(100, '📢 Notifikasi')
        self.assertNotIn('emoji-id="777"', self.request.calls[-1][1]['text'])
        products = InlineKeyboardMarkup([
            [InlineKeyboardButton(f'{first} Satu', callback_data='pilih_first', api_kwargs={'style': 'success'})],
            [InlineKeyboardButton(f'{second} Dua', callback_data='pilih_second')],
        ])
        await self.bot.send_photo(100, 'file', caption=f'{first} Satu · {second} Dua',
                                  reply_markup=products)
        payload = self.request.calls[-1][1]
        self.assertIn('emoji-id="111"', payload['caption'])
        self.assertIn('emoji-id="222"', payload['caption'])
        keyboard = payload['reply_markup']['inline_keyboard']
        self.assertEqual([row[0]['icon_custom_emoji_id'] for row in keyboard], ['111', '222'])
        self.assertEqual(keyboard[0][0]['style'], 'success')

        self.request.behavior = 'reject'
        await self.bot.send_message(100, f'{first} Satu', reply_markup=products)
        payload = self.request.calls[-1][1]
        self.assertEqual(payload['text'], '📦 Satu')
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['text'], '📦 Satu')
        self.engine.set_enabled(False)
        await self.bot.send_message(100, f'{second} Dua', reply_markup=products)
        self.assertEqual(self.request.calls[-1][1]['text'], '📦 Dua')

    async def test_manager_reads_telegram_entity_and_returns_to_slot_list(self):
        from premium_emoji_manager import EmojiManager

        emoji_id, alt = EMOJI_CATALOG['💎']
        message = Message.de_json({
            'message_id': 1, 'date': 1, 'chat': {'id': 100, 'type': 'private'},
            'text': alt, 'entities': [{
                'type': 'custom_emoji', 'offset': 0, 'length': len(alt.encode('utf-16-le')) // 2,
                'custom_emoji_id': emoji_id,
            }],
        }, self.bot)
        user = SimpleNamespace(id=100)
        query = SimpleNamespace(data='cem|e|welcome|0', answer=AsyncMock(), edit_message_text=AsyncMock())
        outgoing = SimpleNamespace(entities=message.entities, text=message.text,
                                   parse_entity=message.parse_entity, reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=user, effective_message=outgoing,
                                 effective_chat=message.chat, callback_query=query)
        context = SimpleNamespace(bot=self.bot, user_data={})
        async def write(key, choice, clear=(), *, revision=None):
            self.engine.set_override(key, choice)
            for location_key in clear:
                self.engine.set_override(location_key, None)

        save = AsyncMock(side_effect=write)
        manager = EmojiManager(lambda: self.engine, save, AsyncMock(return_value=True),
                               lambda ctx: ctx.user_data.pop(STATE, None), InlineKeyboardButton)
        await manager.callback(update, context)
        self.assertTrue(await manager.text_input(update, context))
        save.assert_awaited_once_with('role/welcome', (emoji_id, alt), (), revision=None)
        self.assertIn('Emoji disimpan', outgoing.reply_text.call_args.args[0])
        self.assertNotIn(STATE, context.user_data)
        query.data = 'cem|r|welcome|0'
        await manager.callback(update, context)
        self.assertNotIn('role/welcome', self.engine.overrides)
        self.assertEqual(await input_custom_emoji(self.bot, SimpleNamespace(entities=[])), None)

    async def test_manager_shows_exact_preview_and_requires_confirmation_to_clear(self):
        from premium_emoji_manager import EmojiManager

        emoji_id, alt = EMOJI_CATALOG['💎']
        old_id, old_alt = EMOJI_CATALOG['⭐']
        self.engine.set_override(button_key('welcome', 'contact'), (old_id, old_alt))
        product_key = 'product/vip'
        self.engine.set_override(product_key, ('111', '💎'))
        message = Message.de_json({
            'message_id': 1, 'date': 1, 'chat': {'id': 100, 'type': 'private'}, 'text': alt,
            'entities': [{'type': 'custom_emoji', 'offset': 0,
                          'length': len(alt.encode('utf-16-le')) // 2, 'custom_emoji_id': emoji_id}],
        }, self.bot)
        async def edit(*args, **kwargs):
            return await self.bot.edit_message_text(*args, chat_id=100, message_id=1, **kwargs)

        query = SimpleNamespace(data='cem|e|contact|0', answer=AsyncMock(), edit_message_text=edit)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=100), effective_message=message,
                                 effective_chat=message.chat, callback_query=query)
        context = SimpleNamespace(bot=self.bot, user_data={})
        async def write(key, choice, clear=(), *, revision=None):
            self.engine.set_override(key, choice)
            for location_key in clear:
                self.engine.set_override(location_key, None)

        save = AsyncMock(side_effect=write)
        manager = EmojiManager(lambda: self.engine, save, AsyncMock(return_value=True),
                               lambda ctx: ctx.user_data.pop(STATE, None), InlineKeyboardButton)
        await manager.callback(update, context)
        self.assertTrue(await manager.text_input(update, context))
        save.assert_not_called()
        preview = self.request.calls[-1][1]
        self.assertIn(f'<tg-emoji emoji-id="{emoji_id}">', preview['text'])
        buttons = preview['reply_markup']['inline_keyboard']
        cancel = buttons[-1][0]['callback_data']
        query.data = cancel
        await manager.callback(update, context)
        save.assert_not_called()
        self.assertNotIn(STATE, context.user_data)

        for mode in ('keep', 'clear'):
            query.data = 'cem|e|contact|0'
            await manager.callback(update, context)
            await manager.text_input(update, context)
            state = context.user_data[STATE]
            query.data = f"cem|a|{mode}|{state['nonce']}"
            await manager.callback(update, context)
            self.assertNotIn(STATE, context.user_data)
            self.assertIn('Emoji disimpan', self.request.calls[-1][1]['text'])
            self.assertIn(f'<tg-emoji emoji-id="{emoji_id}">', self.request.calls[-1][1]['text'])
            location = next(loc for loc in LOCATIONS['contact']
                            if loc.screen == 'welcome' and loc.field == 'button')
            current = self.engine.choice_at(location, *location.legacy_keys)
            self.assertEqual(current[0], old_id if mode == 'keep' else emoji_id)
            self.assertEqual(self.engine.overrides[product_key][0], '111')

    async def test_existing_product_edit_handler_accepts_premium_and_standard(self):
        emoji_id, alt = EMOJI_CATALOG['💎']
        message = Message.de_json({
            'message_id': 1, 'date': 1, 'chat': {'id': 100, 'type': 'private'},
            'text': alt, 'entities': [{
                'type': 'custom_emoji', 'offset': 0, 'length': len(alt.encode('utf-16-le')) // 2,
                'custom_emoji_id': emoji_id,
            }],
        }, self.bot)
        outgoing = SimpleNamespace(
            from_user=SimpleNamespace(id=store.ADMIN_ID), entities=message.entities,
            text=message.text, chat=message.chat, parse_entity=message.parse_entity, reply_text=AsyncMock(),
        )
        update = SimpleNamespace(message=outgoing)
        context = SimpleNamespace(bot=self.bot, user_data={})
        with patch.object(store._claim_free, 'text_input', AsyncMock(return_value=False)), \
             patch.object(store, '_set_product_emoji', AsyncMock(return_value=True)) as save, \
             patch.object(store, 'get_product', AsyncMock(return_value=None)):
            context.user_data['editing_product'] = {'paket_id': 'basic', 'field': 'emoji'}
            await store.message_handler(update, context)
            save.assert_awaited_once_with('basic', alt, (emoji_id, alt))
            self.assertNotIn('editing_product', context.user_data)

            context.user_data['editing_product'] = {'paket_id': 'basic', 'field': 'emoji'}
            outgoing.text, outgoing.entities = '⭐', ()
            await store.message_handler(update, context)
            save.assert_awaited_with('basic', '⭐', None)

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

    async def test_validation_batched_cached_and_rejects_static_repainted_or_wrong_alt(self):
        engine = PremiumEmoji()

        def sticker(symbol, *, animated=True, video=False, repaint=False, alt=None):
            emoji_id, original_alt = EMOJI_CATALOG[symbol]
            return SimpleNamespace(custom_emoji_id=emoji_id, emoji=alt or original_alt,
                                   is_animated=animated, is_video=video, needs_repainting=repaint)

        package = sticker('📦')
        video = sticker('✅', animated=False, video=True)
        bot = SimpleNamespace(get_custom_emoji_stickers=AsyncMock(return_value=[
            package,
            video,
            sticker('💎', alt='🚫'),
            sticker('👋', repaint=True),
            sticker('⭐', animated=False),
        ]))
        await asyncio.gather(engine.validate(bot), engine.validate(bot))
        bot.get_custom_emoji_stickers.assert_awaited_once()
        self.assertEqual(engine.ids['📦'], package.custom_emoji_id)
        self.assertEqual(engine.ids['✅'], video.custom_emoji_id)
        self.assertEqual(set(engine.ids.values()), {package.custom_emoji_id, video.custom_emoji_id})
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


    async def test_semantic_contact_uses_one_choice_in_welcome_help_and_caption(self):
        self.engine.set_enabled(True)
        emoji_id, alt = EMOJI_CATALOG['⭐']
        self.engine.set_override('role/contact', (emoji_id, alt))
        self.engine.set_override('role/reviews', ('1234', '✨'))
        settings = {
            'link_testimoni': 'https://example.test/testi', 'link_admin': 'https://example.test/admin',
            'main_group_button_name': '', 'main_group_button_link': '',
        }
        text, markup = await store.build_main_menu_parts('Buyer', settings)
        with emoji_scope('welcome'):
            await self.bot.send_photo(100, 'file-id', caption=text, reply_markup=InlineKeyboardMarkup(markup), parse_mode='HTML')
        payload = self.request.calls[-1][1]
        self.assertIn(f'emoji-id="{emoji_id}"', payload['caption'])
        rows = payload['reply_markup']['inline_keyboard']
        self.assertEqual(rows[2][1]['icon_custom_emoji_id'], emoji_id)
        self.assertEqual(rows[1][1]['icon_custom_emoji_id'], '1234')
        message = Message.de_json({'message_id': 1, 'chat': {'id': 100, 'type': 'private'}, 'date': 1}, self.bot)
        with patch.object(store, 'get_setting', AsyncMock(return_value=settings['link_admin'])):
            await store.cmd_help(SimpleNamespace(message=message), None)
        payload = self.request.calls[-1][1]
        self.assertIn(f'emoji-id="{emoji_id}"', payload['text'])
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['icon_custom_emoji_id'], emoji_id)

    async def test_same_symbol_has_different_functions_and_badges_keep_priority(self):
        self.engine.set_enabled(True)
        for role, value in {'success': '111', 'verify': '222', 'price': '333',
                            'access': '444', 'package': '555'}.items():
            self.engine.set_override(f'role/{role}', (value, '⭐'))
        self.engine.set_override('product/vip', ('999', '💎'))
        with emoji_scope('invoice'):
            await self.bot.send_photo(100, 'file', caption=(
                '✅ Nominal sudah termasuk fee\n✅ Pembayaran otomatis terverifikasi\n'
                '✅ Link produk dikirim otomatis setelah bayar'
            ))
        self.assertEqual(re.findall(r'emoji-id="(\d+)"', self.request.calls[-1][1]['caption']),
                         ['333', '222', '444'])
        with emoji_scope('product'):
            vip = self.engine.mark_product('vip', '📦')
            basic = self.engine.mark_product('basic', '🦊')
            await self.bot.send_message(100, f'{vip} VIP\n{basic} Basic')
        self.assertEqual(re.findall(r'emoji-id="(\d+)"', self.request.calls[-1][1]['text']), ['999', '555'])

    async def test_excluded_payload_and_exact_product_preview_with_global_off(self):
        self.engine.set_enabled(True)
        self.engine.set_override('role/contact', ('777', '✨'))
        self.engine.set_override('product/vip', ('999', '💎'))
        self.engine.set_override(glyph_key('global', 'text', '✅'), ('888', '⭐'))
        text = '<b>✅ Pesanan masuk</b>'
        markup = InlineKeyboardMarkup([[InlineKeyboardButton('💬 Hubungi Admin', url='https://example.test')]])
        for area in ('admin_main', 'admin_settings', 'admin_products', 'order_notification', 'review_post'):
            with emoji_scope(area):
                await self.bot.send_message(100, text, parse_mode='HTML', reply_markup=markup)
            self.assertEqual(self.request.calls[-1][1]['text'], text)
            self.assertEqual(self.request.calls[-1][1]['reply_markup'], markup.to_dict())
        self.engine.set_enabled(False)
        chat = SimpleNamespace(type='private', id=100)
        async def send(*args, **kwargs):
            return await self.bot.send_message(100, *args, **kwargs)
        with patch.object(store, '_premium_emoji', self.engine):
            await store._show_product_emoji(send, chat, 'vip', '📦', text=text, reply_markup=markup)
        rendered = self.request.calls[-1][1]
        self.assertIn(text, rendered['text'])
        self.assertIn('<tg-emoji emoji-id="999">💎</tg-emoji>', rendered['text'])
        self.assertEqual(rendered['reply_markup'], markup.to_dict())
        self.assertIsNone(self.engine.preview_target.get())

    async def test_one_rejected_emoji_is_repaired_without_disabling_other_ids(self):
        self.engine.set_enabled(True)
        failed_id = self.engine.ids['📦']
        self.request.behavior = ('strip_id', failed_id)
        await self.bot.send_message(100, '📦 Paket ✅ Berhasil')
        self.assertEqual([call[0] for call in self.request.calls], ['sendMessage', 'editMessageText'])
        self.assertIn('📦 Paket', self.request.calls[-1][1]['text'])
        success = self.engine.ids['✅']
        self.assertIn(f'emoji-id="{success}"', self.request.calls[-1][1]['text'])
        self.assertFalse(self.engine._block('private', 'text'))
        await self.bot.send_message(100, '✅ Berhasil')
        self.assertIn(f'emoji-id="{success}"', self.request.calls[-1][1]['text'])

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
    async def test_manual_selections_and_product_standard_reset_persist(self):
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

        engine = PremiumEmoji()
        first, second = glyph_key('claim_intro', 'text', '✅'), glyph_key('access', 'text', '✅')
        with session() as conn, conn.cursor() as cur:
            cur.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
            cur.execute('CREATE TABLE IF NOT EXISTS products (paket_id TEXT PRIMARY KEY, emoji TEXT)')
            cur.execute("INSERT INTO products VALUES ('test_emoji_123', '📦') ON CONFLICT DO NOTHING")
        try:
            with patch.object(store, '_premium_emoji', engine), patch.object(store, 'db_session_safe', session):
                await store._save_emoji_slot(first, ('123', '✅'))
                await store._save_emoji_slot(second, ('456', '✅'))
                self.assertTrue(await store._set_product_emoji('test_emoji_123', '📦', ('789', '📦')))
                await store._save_emoji_slot(first, None)
                restarted = PremiumEmoji()
                restarted.load_overrides(store._migrate_emoji_settings_sync()[1])
                self.assertNotIn(first, restarted.overrides)
                self.assertEqual(restarted.overrides[second][:2], ('456', '✅'))
                self.assertEqual(restarted.overrides['product/test_emoji_123'][:2], ('789', '📦'))
                self.assertTrue(await store._set_product_emoji('test_emoji_123', '⭐', None))
                self.assertNotIn('product/test_emoji_123', store._migrate_emoji_settings_sync()[1])
                with session() as conn, conn.cursor() as cur:
                    cur.execute("SELECT emoji FROM products WHERE paket_id='test_emoji_123'")
                    self.assertEqual(cur.fetchone()['emoji'], '⭐')
        finally:
            with session() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM products WHERE paket_id='test_emoji_123'")
                cur.execute("DELETE FROM settings WHERE key IN (%s,%s,%s)",
                            tuple(store.EMOJI_SETTINGS_PREFIX + key for key in (first, second, 'product/test_emoji_123')))

    async def test_migration_preserves_raw_choices_and_confirmed_clear_survives_restart(self):
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

        welcome_contact = button_key('welcome', 'contact')
        help_contact = glyph_key('help', 'button', '💬')
        product = 'product/vip-semantic-test'
        originals = {welcome_contact: ('111', '⭐'), help_contact: ('222', '✨'), product: ('333', '💎')}
        original_json = {key: json.dumps(value, ensure_ascii=False) for key, value in originals.items()}
        tracked = [loc.key for loc in LOCATIONS['contact']]
        keys = [store.EMOJI_BACKUP_SETTING, SETTING, *(
            store.EMOJI_SETTINGS_PREFIX + key for key in [*originals, *tracked, 'role/contact']
        )]
        with session() as conn, conn.cursor() as cur:
            cur.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
            cur.execute('CREATE TABLE IF NOT EXISTS products (paket_id TEXT PRIMARY KEY, emoji TEXT)')
            cur.execute('DELETE FROM settings WHERE key=ANY(%s)', (keys,))
            cur.execute('INSERT INTO settings (key, value) VALUES (%s, %s)', (SETTING, '1'))
            cur.executemany('INSERT INTO settings (key, value) VALUES (%s, %s)', [
                (store.EMOJI_SETTINGS_PREFIX + key, value) for key, value in original_json.items()
            ])
        try:
            engine = PremiumEmoji()
            with patch.object(store, '_premium_emoji', engine), patch.object(store, 'db_session_safe', session):
                enabled, migrated, glyphs = store._migrate_emoji_settings_sync()
                self.assertTrue(enabled)
                self.assertEqual(store._migrate_emoji_settings_sync(), (enabled, migrated, glyphs))
                self.assertEqual({key: migrated[key] for key in originals}, original_json)
                engine.load_overrides(migrated)
                locations = {loc.screen: loc for loc in LOCATIONS['contact'] if loc.field == 'button'}
                self.assertEqual(engine.choice_at(locations['welcome'])[0], '111')
                self.assertEqual(engine.choice_at(locations['help'])[0], '222')
                await store._save_emoji_slot('role/contact', ('444', '📞'))
                self.assertEqual(engine.choice_at(locations['welcome'])[0], '111')
                await store._save_emoji_slot('role/contact', ('555', '📚'), tuple(tracked), revision=engine.revision)
                restarted = PremiumEmoji()
                restarted.load_overrides(store._migrate_emoji_settings_sync()[1])
                self.assertEqual(restarted.choice_at(locations['welcome'], welcome_contact)[0], '555')
                self.assertEqual(restarted.choice_at(locations['help'], help_contact)[0], '555')
                self.assertEqual(restarted.overrides[product][0], '333')
                with session() as conn, conn.cursor() as cur:
                    cur.execute('SELECT value FROM settings WHERE key=%s', (store.EMOJI_BACKUP_SETTING,))
                    stored = json.loads(cur.fetchone()['value'])['settings']
                    self.assertEqual({key: stored[key] for key in originals}, original_json)
                    self.assertNotIn('role/contact', stored)
        finally:
            with session() as conn, conn.cursor() as cur:
                cur.execute('DELETE FROM settings WHERE key=ANY(%s)', (keys,))

    async def test_admin_toggle_persists_and_boot_loads_same_setting(self):
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

        request = TelegramRequest()
        engine = PremiumEmoji()
        engine.record_preview(True)
        bot = make_bot(engine, request)
        query = SimpleNamespace(data='admpanel_emoji|on', answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=store.ADMIN_ID),
                                 effective_chat=SimpleNamespace(type='private'), callback_query=query)
        with session() as conn, conn.cursor() as cursor:
            cursor.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
            cursor.execute('CREATE TABLE IF NOT EXISTS products (paket_id TEXT PRIMARY KEY, emoji TEXT)')
            cursor.execute('DELETE FROM settings WHERE key=%s', (SETTING,))
        with patch.object(store, '_premium_emoji', engine), patch.object(store, 'db_session_safe', session):
            await store.admpanel_emoji(update, SimpleNamespace(bot=bot, user_data={}))
            self.assertTrue(engine.enabled)
            self.assertEqual(store._get_setting_sync(SETTING), '1')
            restarted = PremiumEmoji()
            with patch.object(store, '_premium_emoji', restarted):
                await store._load_premium_emoji(bot)
                self.assertTrue(restarted.enabled)
                query.data = 'admpanel_emoji|off'
                await store.admpanel_emoji(update, SimpleNamespace(bot=bot, user_data={}))
                self.assertFalse(restarted.enabled)
                self.assertEqual(store._get_setting_sync(SETTING), '0')


if __name__ == '__main__':
    unittest.main()
