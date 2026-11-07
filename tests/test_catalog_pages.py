import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from support import update, load_bot, close_bot


def seed(bot, n=30):
    products = [dict(paket_id=f'p{i}', nama=f'Product {i}', emoji='📦', aktif=i % 7 != 0,
                     harga=1000+i, deskripsi='description') for i in range(n)]
    bot._products_cache = (products, bot._time.monotonic() + 60)
    return products


@pytest.mark.asyncio
async def test_warm_page_avoids_full_copy_and_scan(bot):
    products = seed(bot, 1000)
    bot.get_all_products = AsyncMock(side_effect=AssertionError('Warm page should not copy all products'))
    page, pages, shown = await bot._get_buyer_products_page(1)
    view = bot._buyer_products_view
    assert page == 1 and len(shown) == 8 and pages > 1
    await bot._get_buyer_products_page(2)
    assert bot._buyer_products_view is view
    shown[0]['nama'] = 'caller change'
    assert all(p['nama'] != 'caller change' for p in products)


@pytest.mark.asyncio
async def test_page_refresh_and_admin_invalidation(bot):
    products = seed(bot)
    await bot._get_buyer_products_page(0)
    bot._invalidate_products_cache()
    assert bot._buyer_products_view is None
    fresh = [dict(paket_id='new', nama='Updated', aktif=True)]
    async def load():
        bot._products_cache = (fresh, bot._time.monotonic() + 60)
        return fresh
    bot.get_all_products = AsyncMock(side_effect=load)
    page, pages, rows = await bot._get_buyer_products_page(999)
    assert (page, pages) == (0, 1) and rows == fresh
    assert bot.get_all_products.await_count == 1
    bot._products_cache = (fresh, 0)
    await bot._get_buyer_products_page(0)
    assert bot.get_all_products.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('size,page', [(0,0), (1,0), (8,0), (9,1), (40,2), (40,999)])
async def test_catalog_output_matches_phase1(bot, size, page):
    before = load_bot('phase1')
    try:
        outputs=[]
        for module in (before, bot):
            seed(module, size)
            u = update(f'buy|{page}', query_id='catalog')
            u.callback_query.message.photo=('photo',)
            context=NS(user_data={}, bot_data={})
            await module.buy_callback(u,context)
            outputs.append((u.callback_query.edit_message_caption.call_args, context.user_data))
        assert outputs[0] == outputs[1]
    finally:
        close_bot(before)


@pytest.mark.asyncio
async def test_navigation_build_metric_and_context_cleanup(bot):
    seed(bot)
    processor=bot.PerChatUpdateProcessor(24)
    await processor.initialize()
    u=update('buy',query_id='metric')
    await processor.process_update(u,bot.buy_callback(u,NS(user_data={},bot_data={})))
    await asyncio.gather(*list(bot._background_tasks))
    assert len(bot._latency_samples['navigation-build']) == 1
    assert bot._navigation_timing.get() is None
    await processor.shutdown()
