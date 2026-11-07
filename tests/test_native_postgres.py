"""Native multi-connection checks. Only a disposable localhost CI database is allowed."""
import asyncio
import os
from types import SimpleNamespace as NS
from urllib.parse import urlparse
from unittest.mock import AsyncMock
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
import pytest
import telegram.error
from support import load_bot,close_bot

DSN=os.environ.get('NATIVE_TEST_DSN','')
pytestmark=pytest.mark.skipif(not DSN,reason='Native PostgreSQL CI service not available')

@pytest.fixture
def native_bot():
    parsed=urlparse(DSN)
    assert parsed.hostname in {'127.0.0.1','localhost'}
    assert parsed.path=='/public_testi_ci', 'Never use production data for this test'
    b=load_bot()
    b._pool=ThreadedConnectionPool(2,20,DSN,cursor_factory=RealDictCursor)
    b.init_db()
    with b.db_session_safe() as conn:
        with conn.cursor() as c:c.execute('TRUNCATE orders, testimonials, cooldowns RESTART IDENTITY')
    yield b
    b._pool.closeall();close_bot(b)

def query(b,sql,params=()):
    with b.db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(sql,params)
            return c.fetchall() if c.description else []

def order(b,status='completed',delivery='sent',link='https://example.com/synthetic'):
    query(b,"INSERT INTO orders (user_id,paket_id,order_id,status,harga_dibayar,delivery_status,sent_link) VALUES (42,'p','native-order',%s,10000,%s,%s)",(status,delivery,link))

def sender(b,error=None):
    b.get_setting=AsyncMock(side_effect=lambda k,default=None:{
        'testimoni_channel_id':'-10042','notif_channel_id':'-10043',
        'link_admin':'https://t.me/ExistingAdminFromBot',
    }.get(k,default))
    b.get_product=AsyncMock(return_value={'nama':'Synthetic Product','harga':99999})
    return NS(username='HyperDemoBot',send_message=AsyncMock(
        return_value=NS(message_id=91),side_effect=error))

@pytest.mark.asyncio
async def test_native_unique_claim_with_real_connections(native_bot):
    b=native_bot;order(b)
    result=await asyncio.gather(*(b.run_db(b._claim_public_testimonial_sync,'native-order') for _ in range(40)))
    assert sum(result)==1
    assert query(b,'SELECT status FROM testimonials')==[{'status':'publishing'}]
    await b.run_db(b._finish_public_testimonial_sync,'native-order','approved')
    assert not await b.run_db(b._claim_public_testimonial_sync,'native-order',True)
    assert query(b,'SELECT status,delivery_status,harga_dibayar FROM orders')==[
        {'status':'completed','delivery_status':'sent','harga_dibayar':10000}]

@pytest.mark.asyncio
async def test_native_publisher_concurrency_does_not_duplicate(native_bot):
    b=native_bot;order(b);s=sender(b)
    result=await asyncio.gather(*(b._publish_public_testimonial(s,'native-order') for _ in range(30)))
    assert result.count('sent')==1 and result.count('already')==29
    s.send_message.assert_awaited_once()
    assert query(b,'SELECT status FROM testimonials')==[{'status':'approved'}]
    text=s.send_message.call_args.kwargs['text']
    assert 'Rp 10.000' in text and '99.999' not in text

@pytest.mark.asyncio
@pytest.mark.parametrize('error,state',[
    (telegram.error.TimedOut(),'publish_uncertain'),
    (telegram.error.BadRequest('rejected'),'publish_failed'),
])
async def test_native_failure_does_not_change_payment_or_delivery(native_bot,error,state):
    b=native_bot;order(b);s=sender(b,error)
    await b._publish_public_testimonial(s,'native-order')
    await b._publish_public_testimonial(s,'native-order')
    s.send_message.assert_awaited_once()
    assert query(b,'SELECT status FROM testimonials')==[{'status':state}]
    assert query(b,'SELECT status,delivery_status,sent_link FROM orders')==[
        {'status':'completed','delivery_status':'sent','sent_link':'https://example.com/synthetic'}]

@pytest.mark.asyncio
async def test_native_legacy_data_is_preserved(native_bot):
    b=native_bot;order(b)
    query(b,"INSERT INTO testimonials(user_id,user_name,paket_id,order_id,rating,review,status) VALUES (42,'Legacy Name','p','native-order',4,'Legacy review','pending')")
    assert not await b.run_db(b._claim_public_testimonial_sync,'native-order')
    assert await b.run_db(b._claim_public_testimonial_sync,'native-order',True)
    assert query(b,'SELECT user_name,rating,review FROM testimonials')==[
        {'user_name':'Legacy Name','rating':4,'review':'Legacy review'}]

@pytest.mark.asyncio
async def test_native_payment_cancel_race_remains_atomic(native_bot):
    b=native_bot;order(b,'waiting','not_sent',None)
    paid,cancelled=await asyncio.gather(b.mark_order_completed('native-order'),
                                      b.update_order_status('native-order','cancelled'))
    assert int(paid)+int(cancelled is not None)==1
    s=sender(b)
    assert await b._publish_public_testimonial(s,'native-order')=='not_ready'
    s.send_message.assert_not_awaited()
