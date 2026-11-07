import asyncio
import logging

import httpx
import pytest
import telegram.error


def install_transport(monkeypatch, bot, handler):
    real_client = httpx.AsyncClient
    def client(**kwargs):
        kwargs.update(transport=httpx.MockTransport(handler), trust_env=False)
        return real_client(**kwargs)
    monkeypatch.setattr(bot.httpx, 'AsyncClient', client)
    return bot.TelegramHTTPXRequest(connection_pool_size=64, pool_timeout=15)


@pytest.mark.asyncio
async def test_transport_preserves_timeouts_limits_payload_and_safe_metrics(bot, monkeypatch, caplog):
    calls = []
    async def handler(request):
        calls.append(request)
        trace = request.extensions['trace']
        for op in ('connect_tcp', 'start_tls'):
            await trace('connection.' + op + '.started', {'secret': 'NEVER_LOG'})
            await asyncio.sleep(0)
            await trace('connection.' + op + '.complete', {})
        return httpx.Response(200, json={'ok': True, 'result': True})
    request = install_transport(monkeypatch, bot, handler)
    try:
        limits = request._client_kwargs['limits']
        assert (limits.max_connections, limits.max_keepalive_connections) == (64, 64)
        assert limits.keepalive_expiry == bot.TELEGRAM_KEEPALIVE_SECONDS
        assert request._client.timeout == httpx.Timeout(connect=5, read=5, write=5, pool=15)
        with caplog.at_level(logging.INFO):
            token = bot._telegram_transport_probe.set(True)
            try:
                status, body = await request.do_request('https://api.telegram.org/botNEVER_LOG/getMe', 'POST')
            finally:
                bot._telegram_transport_probe.reset(token)
        assert status == 200 and b'"ok"' in body
        assert len(calls) == 1 and calls[0].method == 'POST'
        assert bot._telegram_transport_state.get() is None
        assert 'NEVER_LOG' not in caplog.text
        assert 'connection=cold' in caplog.text
        assert bot._latency_samples['telegram-probe-cold']
    finally:
        await request.shutdown()


@pytest.mark.asyncio
async def test_transport_timeout_is_not_retried_and_context_resets(bot, monkeypatch):
    count = 0
    async def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout('synthetic', request=request)
    request = install_transport(monkeypatch, bot, handler)
    try:
        with pytest.raises(telegram.error.TimedOut):
            await request.do_request('https://api.telegram.org/botTEST/sendMessage', 'POST')
        assert count == 1
        assert bot._telegram_transport_state.get() is None
        assert bot._latency_samples['telegram-http-failed']
    finally:
        await request.shutdown()


@pytest.mark.asyncio
async def test_transport_cancellation_does_not_leak_context(bot, monkeypatch):
    entered = asyncio.Event()
    async def handler(request):
        entered.set()
        await asyncio.Event().wait()
    request = install_transport(monkeypatch, bot, handler)
    try:
        task = asyncio.create_task(request.do_request('https://api.telegram.org/botTEST/getMe', 'POST'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert bot._telegram_transport_state.get() is None
    finally:
        await request.shutdown()


@pytest.mark.asyncio
async def test_real_application_builder_accepts_custom_request(bot,monkeypatch):
    real_client=httpx.AsyncClient
    def client(**kwargs):
        kwargs.update(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'ok':True})),trust_env=False)
        return real_client(**kwargs)
    monkeypatch.setattr(bot.httpx,'AsyncClient',client)
    monkeypatch.setattr(bot,'init_pool',lambda:None)
    applications=[]
    monkeypatch.setattr(bot.Application,'run_polling',lambda self,**kwargs:applications.append(self))
    bot.main()
    app=applications[0]
    assert isinstance(app.bot.request,bot.TelegramHTTPXRequest)
    assert app.bot.request._client_kwargs['limits'].keepalive_expiry==bot.TELEGRAM_KEEPALIVE_SECONDS
    assert len(app.handlers[0])>50
    for request in app.bot._request:
        await request.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('expiry,close_response,expected',[(.02,False,2),(.2,False,1),(.2,True,2)])
async def test_httpcore_pool_reuse_and_server_close(bot,monkeypatch,expiry,close_response,expected):
    # Real HTTPX/httpcore pool and HTTP/1.1 state machine, emulated network bytes.
    # This executor disallows creating local Unix/TCP servers.
    import httpcore
    from httpcore._backends.mock import AsyncMockBackend
    body=b'{"ok":true}'
    close_header=b'Connection: close\r\n' if close_response else b''
    response=b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n'+close_header+b'\r\n'+body
    class Backend(AsyncMockBackend):
        count=0
        async def connect_tcp(self,*args,**kwargs):
            self.count+=1
            return await super().connect_tcp(*args,**kwargs)
    backend=Backend([response,response])
    real_client=httpx.AsyncClient
    def client(**kwargs):
        transport=httpx.AsyncHTTPTransport(limits=kwargs['limits'])
        transport._pool=httpcore.AsyncConnectionPool(network_backend=backend,keepalive_expiry=expiry)
        kwargs['transport']=transport
        kwargs['trust_env']=False
        return real_client(**kwargs)
    monkeypatch.setattr(bot.httpx,'AsyncClient',client)
    monkeypatch.setattr(bot,'TELEGRAM_KEEPALIVE_SECONDS',expiry)
    request=bot.TelegramHTTPXRequest()
    try:
        for i in range(2):
            if i:await asyncio.sleep(.06)
            status,body=await request.do_request('http://localhost/getMe','POST')
            assert status==200 and b'"ok"' in body
        assert backend.count==expected
    finally:
        await request.shutdown()
