import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from pg_support import PgBridge, Pool
from support import load_bot,close_bot,ROOT
from types import SimpleNamespace as NS
import hashlib,hmac

@pytest.fixture(scope='module')
def bridge():
    b=PgBridge()
    yield b
    b.close()

@pytest.fixture
def sql_bot(bot,bridge):
    bot._pool=Pool(bridge)
    bot.init_db()
    bridge.query('TRUNCATE orders, testimonials, cooldowns RESTART IDENTITY')
    return bot

def row(bridge, status='waiting', delivery='not_sent', link=None):
    bridge.query("INSERT INTO orders (user_id,paket_id,order_id,status,harga_dibayar,delivery_status,sent_link) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 (42,'gb_biasa','test-order',status,10000,delivery,link))

@pytest.mark.asyncio
async def test_exact_schema_and_concurrent_completion(sql_bot,bridge):
    row(bridge)
    results=await asyncio.gather(*(sql_bot.mark_order_completed('test-order') for _ in range(40)))
    assert sum(results)==1
    assert bridge.query("SELECT status FROM orders WHERE order_id='test-order'")['rows'][0]['status']=='completed'

@pytest.mark.asyncio
@pytest.mark.parametrize('new_status',['cancelled','expired','rejected'])
async def test_terminal_status_is_never_overwritten(sql_bot,bridge,new_status):
    row(bridge,'completed')
    assert await sql_bot.update_order_status('test-order',new_status) is None
    assert bridge.query("SELECT status FROM orders WHERE order_id='test-order'")['rows'][0]['status']=='completed'

@pytest.mark.asyncio
async def test_cancel_and_settlement_have_one_winner(sql_bot,bridge):
    row(bridge)
    completed,cancelled=await asyncio.gather(sql_bot.mark_order_completed('test-order'),
                                           sql_bot.update_order_status('test-order','cancelled'))
    assert int(completed)+int(cancelled is not None)==1

@pytest.mark.asyncio
async def test_pending_manual_confirmation_is_supported(sql_bot,bridge):
    row(bridge,'pending')
    assert not await sql_bot.mark_order_completed('test-order')
    assert await sql_bot.mark_order_completed('test-order',expected_status='pending')

@pytest.mark.asyncio
@pytest.mark.parametrize('status,delivery,link,eligible',[
    ('waiting','not_sent',None,False),('cancelled','not_sent',None,False),
    ('completed','pending','pending',False),('completed','sent','https://example.com',False),
    ('completed','failed','https://example.com',False),('completed','held',None,True),
    ('completed','not_sent',None,True),('completed','failed',None,True)])
async def test_delivery_claim_sql(sql_bot,bridge,status,delivery,link,eligible):
    row(bridge,status,delivery,link)
    results=await asyncio.gather(*(sql_bot.atomic_claim_for_delivery('test-order') for _ in range(24)))
    assert sum(results)==int(eligible)

@pytest.mark.asyncio
async def test_repeated_provider_status_does_not_write_again(sql_bot,bridge):
    row(bridge)
    await sql_bot.update_order_provider_status('test-order','settlement')
    first=bridge.query("SELECT xmin::text FROM orders WHERE order_id='test-order'")['rows'][0]['xmin']
    await sql_bot.update_order_provider_status('test-order','settlement')
    second=bridge.query("SELECT xmin::text FROM orders WHERE order_id='test-order'")['rows'][0]['xmin']
    assert first==second

@pytest.mark.asyncio
async def test_baseline_reproductions(bridge):
    old=load_bot('baseline')
    old._pool=Pool(bridge)
    try:
        old.init_db()
        bridge.query('TRUNCATE orders RESTART IDENTITY')
        row(bridge,'completed')
        assert await old.update_order_status('test-order','cancelled') == 42
        assert bridge.query("SELECT status FROM orders WHERE order_id='test-order'")['rows'][0]['status']=='cancelled'
        bridge.query("UPDATE orders SET status='pending'")
        assert not await old.mark_order_completed('test-order')
        bridge.query("UPDATE orders SET status='waiting'")
        assert await old.atomic_claim_for_delivery('test-order')
    finally: close_bot(old)

def test_product_and_order_queries_compile_on_postgres(sql_bot,bridge):
    # Real PostgreSQL engine, synthetic fixtures; no connection to production.
    row(bridge,'completed')
    assert sql_bot._get_active_order_sync(42) is None
    snapshot=sql_bot._get_start_snapshot_sync(42)
    assert snapshot['banned'] is False
    assert len(sql_bot._load_all_products_sync())==2
    assert sql_bot._get_admin_order_page_sync('waiting',8,0)==[]

@pytest.mark.asyncio
async def test_duplicate_signed_webhooks_deliver_once(sql_bot,bridge):
    row(bridge)
    bridge.query("UPDATE orders SET provider_transaction_id='tx',payment_provider='gopay'")
    sql_bot._current_bot=object()
    sql_bot._process_completed_order_delivery=AsyncMock()
    body=json.dumps({'event':'transaction.received','transaction':
                     {'payment_method':'QRIS','transaction_id':'tx','amount':10000,'status':'settlement'}}).encode()
    signature=hmac.new(sql_bot.AUTOGOPAY_API_KEY.encode(),body,hashlib.sha256).hexdigest()
    async def call():
        return await sql_bot.autogopay_webhook_handler(NS(read=AsyncMock(return_value=body),headers={'X-Signature':signature}))
    responses=await asyncio.gather(*(call() for _ in range(8)))
    assert all(r.status==200 for r in responses)
    await asyncio.gather(*list(sql_bot._background_tasks))
    sql_bot._process_completed_order_delivery.assert_awaited_once()

@pytest.mark.asyncio
async def test_signed_wrong_amount_cannot_complete_or_mark_settled(sql_bot,bridge):
    row(bridge)
    bridge.query("UPDATE orders SET provider_transaction_id='tx',payment_provider='gopay'")
    sql_bot._current_bot=object()
    body=json.dumps({'event':'transaction.received','transaction':
                     {'payment_method':'QRIS','transaction_id':'tx','amount':1,'status':'settlement'}}).encode()
    signature=hmac.new(sql_bot.AUTOGOPAY_API_KEY.encode(),body,hashlib.sha256).hexdigest()
    response=await sql_bot.autogopay_webhook_handler(NS(read=AsyncMock(return_value=body),headers={'X-Signature':signature}))
    assert response.status==400
    order=bridge.query("SELECT status,provider_status FROM orders WHERE order_id='test-order'")['rows'][0]
    assert order=={'status':'waiting','provider_status':None}

@pytest.mark.asyncio
async def test_resend_query_enforces_ownership(sql_bot,bridge):
    row(bridge,'completed')
    assert await sql_bot.get_resend_order(99,1) is None
    assert (await sql_bot.get_resend_order(42,1))['user_id']==42
