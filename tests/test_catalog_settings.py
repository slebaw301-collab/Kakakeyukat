from contextlib import closing, contextmanager
import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from telegram.error import BadRequest

with patch.dict(os.environ, {
    'BOT_TOKEN': '123:offline', 'ADMIN_ID': '456',
    'DATABASE_URL': 'postgresql://unused', 'AUTOGOPAY_API_KEY': 'offline',
}):
    import bot as store


def product(index, *, active=True, emoji='📦'):
    return dict(paket_id=f'paket_{index}', nama=f'Paket {index}', emoji=emoji,
                harga=index * 1000, deskripsi='Isi paket', aktif=active)


def callback(data, *, photo=False, message_id=123):
    query = NS(
        data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(), delete_message=AsyncMock(),
        message=NS(message_id=message_id, chat_id=456, photo=['existing'] if photo else []),
    )
    return NS(callback_query=query, effective_user=NS(id=456),
              effective_chat=NS(id=456, type='private'))


def button_data(markup):
    return [[button.callback_data for button in row] for row in markup.inline_keyboard]


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = dict(store._CATALOG_SETTING_DEFAULTS)
        self.products = [product(1), product(2, active=False), product(3), product(4)]
        self.context = NS(user_data={}, bot=NS(send_photo=AsyncMock(), edit_message_text=AsyncMock()))
        self.engine = store.PremiumEmoji()
        patches = dict(
            _premium_emoji=self.engine, _products_cache=None, _buyer_products_view=None,
            _menu_render_cache=store.OrderedDict(),
            get_all_products=AsyncMock(side_effect=lambda: self.products),
            get_settings=AsyncMock(side_effect=lambda defaults: {**defaults, **self.settings}),
        )
        patcher = patch.multiple(store, **patches)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def buyer(self, data='buy', *, photo=False):
        update = callback(data, photo=photo)
        await store.buy_callback(update, self.context)
        query = update.callback_query
        editor = query.edit_message_caption if photo else query.edit_message_text
        return editor.call_args.kwargs

    async def test_defaults_and_two_columns_keep_product_order_and_button_style(self):
        result = await self.buyer('buy|4')
        self.assertNotIn('Halaman', result['text'])
        self.assertEqual(button_data(result['reply_markup']), [
            ['pilih_paket_1|0'], ['pilih_paket_3|0'], ['pilih_paket_4|0'], ['back_to_menu'],
        ])
        self.settings['catalog_columns'] = '2'
        paired = await self.buyer()
        self.assertEqual(button_data(paired['reply_markup']), [
            ['pilih_paket_1|0', 'pilih_paket_3|0'], ['pilih_paket_4|0'], ['back_to_menu'],
        ])
        original_buttons = [b.to_dict() for row in result['reply_markup'].inline_keyboard for b in row]
        paired_buttons = [b.to_dict() for row in paired['reply_markup'].inline_keyboard for b in row]
        self.assertEqual(original_buttons, paired_buttons)
        self.products.clear()
        empty = await self.buyer()
        self.assertIn('Belum ada paket', empty['text'])
        self.assertEqual(button_data(empty['reply_markup']), [['back_to_menu']])

    async def test_pagination_old_callbacks_and_detail_back(self):
        self.settings.update(catalog_mode='pagination', catalog_page_size='2', catalog_columns='2')
        first = await self.buyer()
        self.assertIn('Halaman 1/2', first['text'])
        self.assertEqual(button_data(first['reply_markup']), [
            ['pilih_paket_1|0', 'pilih_paket_3|0'], ['buy|1'], ['back_to_menu'],
        ])
        second = await self.buyer('buy|8', photo=True)
        self.assertIn('Halaman 2/2', second['caption'])
        self.assertEqual(button_data(second['reply_markup']), [['pilih_paket_4|1'], ['buy|0']])

        with patch.object(store, 'get_product', AsyncMock(return_value=product(4))) as get_product:
            for message_id, data in enumerate(('pilih_paket_4', 'pilih_paket_4|1'), 123):
                detail = callback(data, photo=True, message_id=message_id)
                await store.pilih_paket(detail, self.context)
                get_product.assert_awaited_with('paket_4')
                args = detail.callback_query.edit_message_caption.call_args.kwargs
                self.assertIn('DETAIL PAKET', args['caption'])
                self.assertEqual(button_data(args['reply_markup']), [['confirm_buy_paket_4'], ['buy|1']])

        self.settings['catalog_mode'] = 'all'
        result = await self.buyer('buy|1', photo=True)
        self.assertNotIn('Halaman', result['caption'])
        self.assertEqual(button_data(result['reply_markup'])[-1], ['back_to_menu'])

    async def test_large_catalog_fallback_and_keyboard_limit(self):
        self.products = [product(index) for index in range(99)]
        _, text, markup = await store._buyer_catalog_parts(0)
        self.assertNotIn('Halaman', text)
        self.assertEqual(sum(map(len, markup.inline_keyboard)), 100)
        self.products.extend(product(index) for index in range(99, 210))
        self.settings['catalog_page_size'] = '3'
        page, text, markup = await store._buyer_catalog_parts(0)
        self.assertIn('Halaman 1/27', text)
        self.assertEqual(len(markup.inline_keyboard[:-2]), 8)
        self.assertEqual(button_data(markup)[-2:], [['buy|1'], ['back_to_menu']])
        self.settings.update(catalog_mode='pagination', catalog_page_size='97', catalog_columns='2')
        page, text, markup = await store._buyer_catalog_parts(1, preview=True)
        self.assertEqual(page, 1)
        self.assertIn('Halaman 2/3', text)
        self.assertEqual(sum(map(len, markup.inline_keyboard)), 100)
        self.assertEqual(button_data(markup)[-3:], [
            ['admpanel_catalog|preview|2'], ['admpanel_catalog|preview|0'], ['admpanel_catalog|close'],
        ])

    async def test_preview_shares_buyer_photo_text_premium_emoji_and_colors(self):
        self.products[0]['emoji'] = '🦊'
        self.engine.ids['📦'] = '123'
        self.engine.set_override('product/paket_3', ('789', '⭐'))
        self.engine.set_enabled(True)
        self.settings.update(catalog_mode='pagination', catalog_page_size='2', catalog_columns='2')
        buyer = await self.buyer(photo=True)
        preview = callback('admpanel_catalog|preview')
        with patch.object(store, '_MENU_PHOTO_FILE_ID', 'existing-file-id'), \
                patch.object(store, '_catalog_photo_messages', {}) as photo_cache:
            await store.admpanel_catalog_preview(preview, self.context)
        self.assertEqual(photo_cache, {})
        self.assertEqual(self.context.user_data, {'_buyer_package_page': 0})
        shown = self.context.bot.send_photo.call_args.kwargs
        self.assertEqual(shown['photo'], 'existing-file-id')
        self.assertEqual(shown['caption'], buyer['caption'])
        for markup in (shown['reply_markup'], buyer['reply_markup']):
            rendered, _ = self.engine.render_markup(markup, screen='catalog')
            cells = rendered['inline_keyboard'][0]
            self.assertEqual([cell['text'] for cell in cells], ['Paket 1', 'Paket 3'])
            self.assertEqual([cell['icon_custom_emoji_id'] for cell in cells], ['123', '789'])
            self.assertEqual([cell['style'] for cell in cells], ['primary', 'primary'])
            self.assertEqual(rendered['inline_keyboard'][-2][0]['style'], 'success')
            self.assertEqual(rendered['inline_keyboard'][-1][0]['style'], 'danger')

        next_page = callback('admpanel_catalog|preview|1', photo=True)
        await store.admpanel_catalog_preview(next_page, self.context)
        self.assertIn('Halaman 2/2', next_page.callback_query.edit_message_caption.call_args.kwargs['caption'])
        select = callback('pilih_|preview', photo=True)
        await store.admpanel_catalog_preview(select, self.context)
        select.callback_query.answer.assert_awaited_once_with('Ini pratinjau katalog.')
        select.callback_query.edit_message_caption.assert_not_awaited()
        close = callback('admpanel_catalog|close', photo=True)
        await store.admpanel_catalog_preview(close, self.context)
        close.callback_query.delete_message.assert_awaited_once()

    async def test_preview_stale_file_id_fallback_does_not_write_buyer_photo_cache(self):
        self.context.bot.send_photo.side_effect = [BadRequest('Wrong file identifier'), None]
        preview = callback('admpanel_catalog|preview')
        with patch.object(store, '_MENU_PHOTO_FILE_ID', 'old-id'), \
                patch.object(store, 'set_setting', AsyncMock()) as save:
            await store.admpanel_catalog_preview(preview, self.context)
            self.assertEqual(store._MENU_PHOTO_FILE_ID, 'old-id')
        save.assert_not_awaited()
        first, second = self.context.bot.send_photo.await_args_list
        self.assertEqual(first.kwargs['photo'], 'old-id')
        self.assertEqual(second.kwargs['photo'].read(2), b'\xff\xd8')

    async def test_settings_navigation_validation_and_admin_guard(self):
        panel = callback('admpanel_catalog')
        await store.admpanel_catalog(panel, self.context)
        call = panel.callback_query.edit_message_text.call_args
        self.assertIn('Semua Produk', call.args[0])
        self.assertIn('Total produk aktif: <b>3</b>', call.args[0])
        self.assertEqual(button_data(call.kwargs['reply_markup'])[-1], ['admpanel_setting'])

        size_picker = callback('admpanel_catalog|size')
        await store.admpanel_catalog(size_picker, self.context)
        self.assertTrue(size_picker.callback_query.answer.call_args.kwargs['show_alert'])
        self.assertEqual(self.context.user_data, {})

        async def save(key, value):
            self.settings[key] = str(value)

        with patch.object(store, 'set_setting', AsyncMock(side_effect=save)) as setting, \
                patch.object(store._claim_free, 'text_input', AsyncMock(return_value=False)), \
                patch.object(store._emoji_manager, 'text_input', AsyncMock(return_value=False)):
            await store.admpanel_catalog(callback('admpanel_catalog|mode|pagination'), self.context)
            await store.admpanel_catalog(callback('admpanel_catalog|layout|2'), self.context)
            await store.admpanel_catalog(size_picker, self.context)
            message = NS(from_user=NS(id=456), text='', reply_text=AsyncMock())
            update = NS(message=message, effective_chat=NS(id=456))
            for value in ('0', '98', '2.5', '-1', 'dua'):
                message.text = value
                await store.message_handler(update, self.context)
                self.assertEqual(self.context.user_data['awaiting_catalog_page_size'], 123)
            self.assertEqual(setting.await_count, 2)
            message.text = '97'
            await store.message_handler(update, self.context)
            setting.assert_awaited_with('catalog_page_size', 97)
            self.context.bot.edit_message_text.assert_awaited_once()
            self.assertEqual(self.context.user_data, {})
            await store.admpanel_catalog(callback('admpanel_catalog|mode|all'), self.context)
            self.assertEqual(await store._get_catalog_settings(), (False, 97, 2))

            denied = callback('admpanel_catalog|mode|pagination')
            with patch.object(store, 'is_admin', AsyncMock(return_value=False)):
                await store.admpanel_catalog(denied, self.context)
                await store.admpanel_catalog_preview(callback('admpanel_catalog|preview'), self.context)
            denied.callback_query.answer.assert_awaited_once_with('⛔ Akses ditolak.', show_alert=True)
            setting.assert_awaited_with('catalog_mode', 'all')
            self.context.bot.send_photo.assert_not_awaited()


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL to an isolated PostgreSQL database')
class CatalogDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_survive_cache_reset_and_product_mutations_update_catalog(self):
        import psycopg2
        from psycopg2.extras import RealDictCursor

        url = os.environ['TEST_DATABASE_URL']
        schema = f'catalog_test_{uuid4().hex}'

        @contextmanager
        def session():
            with closing(psycopg2.connect(url, cursor_factory=RealDictCursor)) as conn:
                with conn, conn.cursor() as cursor:
                    cursor.execute(f'SET search_path TO {schema}')
                    yield conn

        with closing(psycopg2.connect(url)) as conn:
            with conn, conn.cursor() as cursor:
                cursor.execute(f'CREATE SCHEMA {schema}')
        try:
            with patch.multiple(
                store, db_session_safe=session, _settings_cache={}, _settings_cache_versions={},
                _settings_cache_locks={}, _products_cache=None, _products_by_id_cache=None,
                _products_cache_lock=None, _products_cache_generation=0, _buyer_products_view=None,
                _premium_emoji=store.PremiumEmoji(), _premium_emoji_config_lock=store.asyncio.Lock(),
            ):
                await store.run_db_admin(store.init_db)
                await store.set_setting('catalog_regression_sentinel', 'preserved')
                self.assertEqual(await store._get_catalog_settings(), (False, 8, 1))
                await store.set_setting('catalog_mode', 'pagination')
                await store.set_setting('catalog_page_size', 1)
                await store.set_setting('catalog_columns', 2)
                self.assertEqual(await store._get_catalog_settings(), (True, 1, 2))
                await store.set_setting('catalog_mode', 'all')
                self.assertEqual(await store._get_catalog_settings(), (False, 1, 2))
                with patch.multiple(store, _settings_cache={}, _settings_cache_versions={}, _settings_cache_locks={}):
                    self.assertEqual(await store._get_catalog_settings(), (False, 1, 2))
                    await store.set_setting('catalog_mode', 'pagination')
                    self.assertEqual(await store._get_catalog_settings(), (True, 1, 2))
                    self.assertEqual(await store.get_setting('catalog_regression_sentinel'), 'preserved')

                await store.set_setting('catalog_mode', 'all')
                await store.add_product('late', 'Late', '📦', '', 8000)
                await store.add_product('first', 'First', '📦', '', 1000)
                self.assertEqual([p['paket_id'] for p in await store._get_active_buyer_products()],
                                 ['first', 'gb_biasa', 'late', 'gb_vip'])
                await store.add_product('middle', 'Middle', '📦', '', 3000)
                self.assertEqual([p['paket_id'] for p in await store._get_active_buyer_products()],
                                 ['first', 'middle', 'gb_biasa', 'late', 'gb_vip'])
                await store.update_product_field('first', 'aktif', False)
                await store.delete_product('middle')
                _, text, markup = await store._buyer_catalog_parts(0)
                self.assertNotIn('Halaman', text)
                self.assertEqual(button_data(markup), [
                    ['pilih_gb_biasa|0', 'pilih_late|0'], ['pilih_gb_vip|0'], ['back_to_menu'],
                ])
        finally:
            with closing(psycopg2.connect(url)) as conn:
                with conn, conn.cursor() as cursor:
                    cursor.execute(f'DROP SCHEMA {schema} CASCADE')
