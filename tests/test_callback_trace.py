import asyncio
import json
import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from support import update


def records(caplog):
    return [json.loads(r.message.split('[CBTRACE] ',1)[1]) for r in caplog.records if '[CBTRACE] ' in r.message]


@pytest.mark.asyncio
async def test_ingress_ack_edit_transport_trace_is_correlated_and_private(bot,monkeypatch,caplog):
    real_client=httpx.AsyncClient
    async def transport(request):
        trace=request.extensions['trace']
        for op in ('connect_tcp','start_tls'):
            await trace('connection.'+op+'.started',{'sensitive':'DO_NOT_LOG'})
            await asyncio.sleep(.003)
            await trace('connection.'+op+'.complete',{})
        for op in ('send_request_headers','send_request_body'):
            await trace('http11.'+op+'.started',{})
            await trace('http11.'+op+'.complete',{})
        await trace('http11.receive_response_headers.started',{})
        await asyncio.sleep(.025 if request.url.path.endswith('editMessageCaption') else .005)
        await trace('http11.receive_response_headers.complete',{})
        await trace('http11.receive_response_body.started',{})
        await trace('http11.receive_response_body.complete',{})
        return httpx.Response(200,json={'ok':True,'result':True})
    def client(**kwargs):
        kwargs.update(transport=httpx.MockTransport(transport),trust_env=False)
        return real_client(**kwargs)
    monkeypatch.setattr(bot.httpx,'AsyncClient',client)
    request=bot.TelegramHTTPXRequest()
    limiter=bot.BuyerPriorityRateLimiter()
    await limiter.initialize()
    u=update('buy',query_id='DO_NOT_LOG')
    u.callback_query.message.photo=('photo',)
    async def api(endpoint):
        async def send():return await request.do_request('https://api.telegram.org/botDO_NOT_LOG/'+endpoint,'POST')
        return await limiter.process_request(send,(),{},endpoint,{'chat_id':42} if endpoint!='answerCallbackQuery' else {},None)
    u.callback_query.answer=lambda **kw:api('answerCallbackQuery')
    u.callback_query.edit_message_caption=lambda **kw:api('editMessageCaption')
    bot._products_cache=([dict(paket_id='secret',nama='DO_NOT_LOG',aktif=True)],bot._time.monotonic()+60)
    queue=bot.CallbackTimingQueue()
    processor=bot.PerChatUpdateProcessor(24)
    await processor.initialize()
    try:
        with caplog.at_level(logging.INFO):
            bot._last_poll_read_ns=bot._time.perf_counter_ns()
            await queue.put(u)
            await asyncio.sleep(.005)
            value=await queue.get()
            await processor.process_update(value,bot.buy_callback(value,NS(user_data={},bot_data={})))
            await asyncio.gather(*list(bot._background_tasks))
        captured=records(caplog)
        assert len(captured)==1
        r=captured[0]
        assert r['route']=='buyer:list' and r['source']=='queue'
        assert r['stages']['queue_ms']>=4 and r['stages']['poll_decode_enqueue_ms'] is not None
        edit=next(s for s in r['api'] if s['method']=='editMessageCaption')
        assert edit['wait_headers_ms']>=20 and edit['tcp_ms']>=2 and edit['tls_ms']>=2
        assert edit['http_ms']>=30 and edit['attempt']==1 and edit['outcome']=='ok'
        assert len(r['api'])==2 and r['stages']['ack_task_done_ms'] is not None
        assert 'DO_NOT_LOG' not in caplog.text
        assert not bot._callback_ingress and bot._callback_trace_state.get() is None
    finally:
        await request.shutdown()
        await limiter.shutdown()
        await processor.shutdown()


@pytest.mark.asyncio
async def test_background_work_does_not_hold_navigation_trace(bot,caplog):
    entered=asyncio.Event()
    release=asyncio.Event()
    seen=[]
    async def background():
        seen.append(bot._callback_trace_state.get())
        entered.set()
        await release.wait()
    async def handler():
        bot.spawn_background(background(),name='synthetic-cleanup')
        await entered.wait()
    processor=bot.PerChatUpdateProcessor(24)
    await processor.initialize()
    with caplog.at_level(logging.INFO):
        await processor.process_update(update(),handler())
        await asyncio.sleep(0)
    assert len(records(caplog))==1 and seen==[None]
    release.set()
    await asyncio.gather(*list(bot._background_tasks))
    await processor.shutdown()


@pytest.mark.asyncio
async def test_cancelled_callback_releases_trace_and_chat(bot,caplog):
    entered=asyncio.Event()
    async def handler():
        entered.set()
        await asyncio.Event().wait()
    processor=bot.PerChatUpdateProcessor(24)
    await processor.initialize()
    with caplog.at_level(logging.INFO):
        task=asyncio.create_task(processor.process_update(update('cancel_order'),handler()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    assert records(caplog)[0]['outcome']=='CancelledError'
    assert not processor._chat_locks and bot._callback_trace_state.get() is None
    await processor.shutdown()


@pytest.mark.asyncio
async def test_bounded_queue_keeps_original_delivery_and_trace_limit(bot,monkeypatch):
    monkeypatch.setattr(bot,'CALLBACK_TRACE_MAX',1)
    queue=bot.CallbackTimingQueue(maxsize=1)
    a,b=update(query_id='a'),update(query_id='b')
    queue.put_nowait(a)
    with pytest.raises(asyncio.QueueFull):queue.put_nowait(b)
    assert bot._callback_trace_created==1
    assert await queue.get() is a
    queue.task_done()
    await queue.put(b)
    assert await queue.get() is b
    queue.task_done()
    await queue.join()
    assert bot._callback_trace_created==1


@pytest.mark.asyncio
async def test_concurrent_callback_traces_are_isolated(bot,caplog):
    limiter=bot.BuyerPriorityRateLimiter(overall_max_rate=100000,background_max_rate=100000)
    await limiter.initialize()
    processor=bot.PerChatUpdateProcessor(24)
    await processor.initialize()
    bot._products_cache=([dict(paket_id='p',nama='Synthetic',aktif=True)],bot._time.monotonic()+60)
    queue=bot.CallbackTimingQueue()
    for i in range(100):await queue.put(update('buy',uid=1000+i,query_id=f'PRIVATE_QUERY_{i}'))
    async def worker():
        while not queue.empty():
            u=queue.get_nowait()
            async def api(endpoint):
                async def complete():await asyncio.sleep(.003)
                return await limiter.process_request(complete,(),{},endpoint,{},None)
            u.callback_query.answer=lambda **kwargs:api('answerCallbackQuery')
            u.callback_query.edit_message_text=lambda **kwargs:api('editMessageText')
            await processor.process_update(u,bot.buy_callback(u,NS(user_data={},bot_data={})))
            queue.task_done()
    with caplog.at_level(logging.INFO):
        await asyncio.gather(*(worker() for _ in range(24)))
        await queue.join()
        await asyncio.gather(*list(bot._background_tasks))
    captured=records(caplog)
    assert len(captured)==100 and len({r['id'] for r in captured})==100
    assert all(sorted(s['method'] for s in r['api'])==['answerCallbackQuery','editMessageText'] for r in captured)
    assert all(r['route']=='buyer:list' and r['stages']['queue_ms'] is not None for r in captured)
    assert 'PRIVATE_QUERY_' not in caplog.text
    assert not bot._callback_ingress and not processor._chat_locks
    await limiter.shutdown()
    await processor.shutdown()
