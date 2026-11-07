import asyncio
import ast
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from support import ROOT, update, load_bot, close_bot

@pytest.mark.asyncio
@pytest.mark.parametrize('variant,overlaps', [('baseline', False), ('candidate', True)])
async def test_navigation_ack_and_render_order(variant, overlaps):
    bot = load_bot(variant)
    ack_started, ack_release, rendered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    u = update()
    async def ack(**kwargs):
        ack_started.set()
        await ack_release.wait()
    u.callback_query.answer = ack
    async def handler():
        await bot._fast_callback_ack(u.callback_query)
        rendered.set()
    p = bot.PerChatUpdateProcessor(2)
    await p.initialize()
    task = asyncio.create_task(p.process_update(u, handler()))
    try:
        await ack_started.wait()
        await asyncio.sleep(0.02)
        assert rendered.is_set() is overlaps
    finally:
        ack_release.set()
        await task
        await asyncio.gather(*list(bot._background_tasks))
        await p.shutdown()
        close_bot(bot)

@pytest.mark.asyncio
async def test_latest_navigation_wins_without_dropping_transactions(bot):
    p = bot.PerChatUpdateProcessor(2)
    await p.initialize()
    gate, entered = asyncio.Event(), asyncio.Event()
    seen = []
    async def first():
        entered.set()
        await gate.wait()
    first_task = asyncio.create_task(p.process_update(update('confirm_buy_a', query_id='first'), first()))
    await entered.wait()
    async def record(value):
        seen.append(value)
    tasks = []
    for i in range(12):
        tasks.append(asyncio.create_task(p.process_update(update('buy', query_id=f'n{i}'), record(i))))
        await asyncio.sleep(0)
    tasks.append(asyncio.create_task(p.process_update(update('cancel_order', query_id='transaction'), record('transaction'))))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first_task, *tasks)
    assert seen == [11, 'transaction']
    assert not p._chat_locks
    await asyncio.gather(*list(bot._background_tasks))

@pytest.mark.asyncio
async def test_busy_chat_does_not_consume_other_chat_slots(bot):
    p = bot.PerChatUpdateProcessor(2)
    await p.initialize()
    gate, entered, other = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def blocking():
        entered.set()
        await gate.wait()
    tasks = [asyncio.create_task(p.process_update(update('confirm_buy_a', query_id='first'), blocking()))]
    await entered.wait()
    for i in range(10):
        tasks.append(asyncio.create_task(p.process_update(update('cancel_order', query_id=f'q{i}'), asyncio.sleep(0))))
    async def independent():
        other.set()
    tasks.append(asyncio.create_task(p.process_update(update('buy', uid=99, query_id='other'), independent())))
    try:
        await asyncio.wait_for(other.wait(), 0.5)
    finally:
        gate.set()
        await asyncio.gather(*tasks)
        await asyncio.gather(*list(bot._background_tasks))

@pytest.mark.asyncio
async def test_slow_transactions_cannot_fill_all_navigation_slots(bot):
    p=bot.PerChatUpdateProcessor(24)
    await p.initialize()
    release,menu_done=asyncio.Event(),asyncio.Event()
    entered=0
    async def heavy():
        nonlocal entered
        entered+=1
        await release.wait()
    transactions=[asyncio.create_task(p.process_update(update('confirm_buy_p',uid=i+100,query_id=f'h{i}'),heavy())) for i in range(24)]
    await asyncio.sleep(.02)
    assert entered==18
    async def menu(): menu_done.set()
    task=asyncio.create_task(p.process_update(update('buy',uid=999,query_id='menu'),menu()))
    try:
        await asyncio.wait_for(menu_done.wait(),.5)
    finally:
        release.set()
        await asyncio.gather(task,*transactions)
        await asyncio.gather(*list(bot._background_tasks))
    assert entered==24

@pytest.mark.asyncio
async def test_cancelled_db_awaiter_keeps_admission_until_thread_finishes(bot):
    executor = ThreadPoolExecutor(1)
    bot._executor_limits[executor] = 1
    entered, release = threading.Event(), threading.Event()
    submitted = 0
    original = executor.submit
    def submit(*a, **kw):
        nonlocal submitted
        submitted += 1
        return original(*a, **kw)
    executor.submit = submit
    def work():
        entered.set()
        release.wait(2)
        return 1
    first = asyncio.create_task(bot._run_executor(executor, work))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(bot._run_executor(executor, lambda: 2))
    try:
        await asyncio.sleep(0.02)
        assert submitted == 1
        assert not second.done()
    finally:
        release.set()
        assert await second == 2
        executor.shutdown()

@pytest.mark.asyncio
async def test_menu_text_and_keyboard_compatibility(bot):
    baseline = load_bot('baseline')
    product = dict(paket_id='example', nama='Example', emoji='📦', harga=10000, aktif=True, deskripsi='Description')
    try:
        for name, data in [('buy_callback', 'buy'), ('pilih_paket', 'pilih_example|0'),
                           ('buyer_how', 'buyer_how'), ('back_to_menu', 'back_to_menu')]:
            outputs = []
            for m in (baseline, bot):
                m.get_all_products = AsyncMock(return_value=[product])
                m.get_product = AsyncMock(return_value=product)
                m.get_settings = AsyncMock(return_value=m._menu_setting_defaults())
                u = update(data, query_id=name)
                u.callback_query.message.photo = ('photo',)
                context = NS(user_data={}, bot_data={})
                await getattr(m, name)(u, context)
                outputs.append(u.callback_query.edit_message_caption.call_args)
            assert outputs[0] == outputs[1], name
    finally:
        close_bot(baseline)

@pytest.mark.asyncio
async def test_duplicate_renders_do_not_send_repeated_edits(bot):
    u=update()
    q=u.callback_query
    markup=bot.InlineKeyboardMarkup([])
    await bot._render_buyer_menu(q,'Menu A',markup)
    await bot._render_buyer_menu(q,'Menu A',markup)
    assert q.edit_message_text.await_count==1
    bot._forget_menu_render(q)
    await bot._render_buyer_menu(q,'Menu A',markup)
    assert q.edit_message_text.await_count==2

@pytest.mark.asyncio
async def test_stale_callback_snapshot_cannot_hide_new_render(bot):
    q=update().callback_query
    markup=bot.InlineKeyboardMarkup([])
    q.message.text_html='Menu A'
    q.message.reply_markup=markup
    await bot._render_buyer_menu(q,'Menu B',markup)
    await bot._render_buyer_menu(q,'Menu A',markup)
    assert q.edit_message_text.await_count==2

@pytest.mark.asyncio
async def test_failed_edit_is_never_cached(bot):
    import telegram.error
    q=update().callback_query
    q.edit_message_text.side_effect=[telegram.error.TimedOut(),None]
    with pytest.raises(telegram.error.TimedOut):
        await bot._render_buyer_menu(q,'Menu',None)
    await bot._render_buyer_menu(q,'Menu',None)
    assert q.edit_message_text.await_count==2

def test_all_features_assets_handlers_and_schema_preserved():
    before = ast.parse((ROOT/'baseline/bot.py').read_text())
    after = ast.parse((ROOT/'bot.py').read_text(), feature_version=(3,11))
    old = {n.name:n for n in before.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))}
    new = {n.name:n for n in after.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))}
    assert old.keys() <= new.keys()
    # Transport construction changed; all command/callback registrations must match.
    registrations = lambda node: [ast.dump(n) for n in ast.walk(node) if isinstance(n,ast.Call)
                                  and isinstance(n.func,ast.Attribute)
                                  and n.func.attr in {'add_handler','add_error_handler','run_polling'}]
    assert registrations(old['main']) == registrations(new['main'])
    assert ast.dump(old['init_db']) == ast.dump(new['init_db'])
    def asset(tree):
        return next(n.value.value for n in tree.body if isinstance(n,ast.Assign)
                    and any(isinstance(t,ast.Name) and t.id=='_MENU_PHOTO_BASE64' for t in n.targets))
    assert asset(before) == asset(after)
    from collections import Counter
    defs=lambda tree: Counter(n.name for n in ast.walk(tree)
                             if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)))
    assert not (defs(before)-defs(after))

@pytest.mark.asyncio
async def test_new_callback_caches_remain_bounded_under_5000_users(bot):
    processor=bot.PerChatUpdateProcessor(24)
    await processor.initialize()
    async def noop(**kw): await asyncio.sleep(0)
    markup=bot.InlineKeyboardMarkup([])
    for batch in range(50):
        tasks=[]
        for index in range(100):
            uid=batch*100+index+100
            user=NS(id=uid)
            message=NS(message_id=1,chat_id=uid,photo=(),text_html='',reply_markup=None)
            query=NS(id=str(uid),data='buy',from_user=user,message=message,
                     answer=noop,edit_message_text=noop)
            u=NS(effective_chat=NS(id=uid),effective_user=user,callback_query=query,update_id=uid)
            async def handler(q=query):
                await bot._fast_callback_ack(q)
                await bot._render_buyer_menu(q,'Menu',markup)
            tasks.append(processor.process_update(u,handler()))
        await asyncio.gather(*tasks)
    await asyncio.gather(*list(bot._background_tasks))
    await asyncio.sleep(0)
    assert len(bot._menu_render_cache)<=4096
    assert len(bot._callback_acks)<=4096
    assert not bot._callback_ack_tasks
    assert not bot._background_tasks
    assert not processor._chat_locks
