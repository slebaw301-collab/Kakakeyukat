import asyncio
import ast
from html.parser import HTMLParser
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
import telegram.error
from support import ROOT,load_bot,close_bot,update
from test_postgres_sql import bridge,sql_bot,row


@pytest.fixture
def public_bot():
    b=load_bot()
    b.get_setting=AsyncMock(side_effect=lambda key,default=None: {
        'testimoni_channel_id':'-10042','notif_channel_id':'-10043',
        'link_admin':'https://t.me/ExistingAdminFromBot',
    }.get(key,default))
    b.get_order_by_id=AsyncMock(return_value={
        'order_id':'order-42','user_id':42,'user_name':'PRIVATE_BUYER_NAME',
        'username':'PRIVATE_BUYER_USERNAME','paket_id':'p','status':'completed',
        'delivery_status':'sent','sent_link':'https://example.com/PRIVATE_ACCESS',
        'harga_dibayar':10000,'waktu':'05:00 - 08/11/2026',
    })
    b.get_product=AsyncMock(return_value={'nama':'Paket & <VIP>','harga':10000})
    b.public_ledger={}
    async def run(fn,*args):
        if fn is b._claim_public_testimonial_sync:
            oid,manual=args
            await asyncio.sleep(0)
            if oid in b.public_ledger and not (manual and b.public_ledger[oid] in {'pending','publish_failed'}):
                return False
            b.public_ledger[oid]='publishing'
            return True
        if fn is b._finish_public_testimonial_sync:
            oid,status=args
            b.public_ledger[oid]=status
            return
        raise AssertionError('Unexpected DB operation')
    b.run_db=AsyncMock(side_effect=run)
    yield b
    close_bot(b)


def sender():return NS(username='HyperDemoBot',send_message=AsyncMock(return_value=NS(message_id=91)))
async def drain(b):
    while b._background_tasks:await asyncio.gather(*list(b._background_tasks))


@pytest.mark.asyncio
async def test_public_quote_privacy_and_exact_two_urls(public_bot):
    b=public_bot;s=sender()
    assert await b._publish_public_testimonial(s,'order-42')=='sent'
    kw=s.send_message.call_args.kwargs
    assert kw['chat_id']==-10042 and kw['parse_mode']=='HTML'
    text=kw['text']
    assert '<blockquote>' in text and '</blockquote>' in text
    assert '<code>42</code>' in text and 'Paket &amp; &lt;VIP&gt;' in text and 'Rp 10.000' in text
    for secret in ('PRIVATE_BUYER_NAME','PRIVATE_BUYER_USERNAME','PRIVATE_ACCESS','order-42','@HyperDemoBot','Rating'):
        assert secret not in text
    buttons=[button for line in kw['reply_markup'].inline_keyboard for button in line]
    assert [x.text for x in buttons]==['🛒 Order Sekarang','💬 Hubungi Admin']
    assert [x.url for x in buttons]==['https://t.me/HyperDemoBot?start=order','https://t.me/ExistingAdminFromBot']
    assert all(x.callback_data is None for x in buttons)
    class HTML(HTMLParser):
        def __init__(self):super().__init__();self.stack=[]
        def handle_starttag(self,tag,attrs):
            assert tag in {'b','i','code','blockquote'};self.stack.append(tag)
        def handle_endtag(self,tag):assert self.stack.pop()==tag
    parser=HTML();parser.feed(text);parser.close();assert not parser.stack
    assert b.public_ledger=={'order-42':'approved'}


def test_receipt_uses_order_amount_not_current_product_price(public_bot):
    order=dict(public_bot.get_order_by_id.return_value)
    assert 'Rp 10.000' in public_bot._format_public_transaction(order,{'nama':'P','harga':99999})
    order.update(harga_dibayar=None,waktu=None)
    text=public_bot._format_public_transaction(order,{'nama':'P','harga':99999})
    assert 'Total: <b>—</b>' in text and 'Waktu order: —' in text and '99.999' not in text


@pytest.mark.asyncio
async def test_concurrent_callbacks_only_publish_once(public_bot):
    s=sender()
    results=await asyncio.gather(*(public_bot._publish_public_testimonial(s,'order-42') for _ in range(30)))
    assert results.count('sent')==1 and results.count('already')==29
    s.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('error,state',[
    (telegram.error.TimedOut(),'publish_uncertain'),
    (telegram.error.NetworkError('network'),'publish_uncertain'),
    (telegram.error.BadRequest('bad request'),'publish_failed'),
    (telegram.error.Forbidden('forbidden'),'publish_failed'),
])
async def test_public_send_failure_never_repeats_or_falls_back_to_admin(public_bot,error,state):
    b=public_bot;s=sender();s.send_message.side_effect=error
    await b._publish_public_testimonial(s,'order-42')
    await b._publish_public_testimonial(s,'order-42')
    assert b.public_ledger['order-42']==state
    s.send_message.assert_awaited_once()
    assert s.send_message.call_args.kwargs['chat_id']==-10042


@pytest.mark.asyncio
async def test_post_send_db_failure_keeps_reservation(public_bot):
    b=public_bot;s=sender();original=b.run_db.side_effect
    async def run(fn,*args):
        if fn is b._finish_public_testimonial_sync:raise RuntimeError('DB after send')
        return await original(fn,*args)
    b.run_db.side_effect=run
    assert await b._publish_public_testimonial(s,'order-42')=='sent'
    assert await b._publish_public_testimonial(s,'order-42')=='already'
    assert b.public_ledger['order-42']=='publishing'
    s.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('state',['publishing','publish_uncertain','approved'])
async def test_even_admin_cannot_repeat_an_uncertain_or_sent_post(public_bot,state):
    b=public_bot;s=sender();b.public_ledger['order-42']=state
    assert await b._publish_public_testimonial(s,'order-42',manual=True)=='already'
    s.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_admin_retry_only_after_definite_rejection(public_bot):
    b=public_bot;s=sender();b.public_ledger['order-42']='publish_failed'
    assert await b._publish_public_testimonial(s,'order-42',manual=True)=='sent'
    assert b.public_ledger['order-42']=='approved'
    s.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_during_send_never_unclaims(public_bot):
    b=public_bot;s=sender();entered=asyncio.Event()
    async def send(**kw):
        entered.set();await asyncio.Event().wait()
    s.send_message.side_effect=send
    task=asyncio.create_task(b._publish_public_testimonial(s,'order-42'))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert b.public_ledger['order-42']=='publishing'
    assert await b._publish_public_testimonial(s,'order-42')=='already'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,delivery,link',[
    ('waiting','not_sent',None),('cancelled','not_sent',None),
    ('completed','held',None),('completed','pending','pending'),
    ('completed','failed',None),('completed','sent','pending'),
])
async def test_unpaid_or_undelivered_never_posts(public_bot,status,delivery,link):
    b=public_bot;s=sender()
    b.get_order_by_id.return_value.update(status=status,delivery_status=delivery,sent_link=link)
    assert await b._publish_public_testimonial(s,'order-42')=='not_ready'
    b.run_db.assert_not_awaited();s.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('channel',['',None,'42','-10043'])
async def test_disabled_or_private_admin_destination_has_no_post(public_bot,channel):
    b=public_bot;s=sender();original=b.get_setting.side_effect
    b.get_setting.side_effect=lambda key,default=None: channel if key=='testimoni_channel_id' else original(key,default)
    assert await b._publish_public_testimonial(s,'order-42')=='disabled'
    b.run_db.assert_not_awaited();s.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_completes_before_optional_public_work(public_bot):
    b=public_bot
    b.hapus_qris_buyer_lama=AsyncMock();b.atomic_claim_for_delivery=AsyncMock(return_value=True)
    b._send_buyer_product_link=AsyncMock(return_value=('link',True));b._retain_delivery_result=AsyncMock()
    b.hapus_notif_lama=AsyncMock();b.kirim_notif=AsyncMock(return_value=None)
    b._auto_deliver_pending_prereq_orders=AsyncMock()
    blocked=asyncio.Event();entered=asyncio.Event()
    async def publish(*args):
        b._retain_delivery_result.assert_awaited_once()
        entered.set();await blocked.wait()
    b._publish_public_testimonial=AsyncMock(side_effect=publish)
    await asyncio.wait_for(b._process_completed_order_delivery(
        sender(),'order-42','p',42,'PRIVATE_BUYER_NAME',10000,paket={'nama':'Paket','harga':10000}),.5)
    await entered.wait();assert not blocked.is_set()
    blocked.set();await drain(b)
    b._publish_public_testimonial.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_delivery_does_not_create_public_success(public_bot):
    b=public_bot
    b.hapus_qris_buyer_lama=AsyncMock();b.atomic_claim_for_delivery=AsyncMock(return_value=True)
    b._send_buyer_product_link=AsyncMock(side_effect=telegram.error.TimedOut())
    b._record_delivery_failure=AsyncMock();b._send_delivery_failed_alert=AsyncMock()
    b.hapus_notif_lama=AsyncMock();b.kirim_notif=AsyncMock(return_value=None)
    b._publish_public_testimonial=AsyncMock()
    await b._process_completed_order_delivery(sender(),'o','p',42,'Buyer',10000,paket={'nama':'Paket','harga':10000})
    await drain(b);b._publish_public_testimonial.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_review_buttons_do_not_collect_names_or_reviews(public_bot):
    b=public_bot;u=update('rate_start|order-42')
    ctx=NS(user_data={'awaiting_review_text':True,'temp_rating':{}},bot=sender())
    b._callback_feedback=AsyncMock();b.save_testimonial=AsyncMock()
    await b._legacy_review_notice(u,ctx)
    assert not ctx.user_data
    b.save_testimonial.assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_claim_persists_in_existing_schema(sql_bot,bridge):
    b=sql_bot
    row(bridge,'completed','sent','https://example.com/access')
    claims=await asyncio.gather(*(b.run_db(b._claim_public_testimonial_sync,'test-order') for _ in range(20)))
    assert sum(claims)==1
    record=bridge.query('SELECT * FROM testimonials')['rows'][0]
    assert record['user_name']=='' and record['rating'] is None and record['status']=='publishing'
    await b.run_db(b._finish_public_testimonial_sync,'test-order','approved')
    assert not await b.run_db(b._claim_public_testimonial_sync,'test-order',True)
    # Existing moderation callbacks cannot reset an approved publication.
    await b.update_testimonial_status('test-order','rejected')
    assert bridge.query('SELECT status FROM testimonials')['rows'][0]['status']=='approved'
    assert bridge.query('SELECT status,delivery_status FROM orders')['rows'][0]=={'status':'completed','delivery_status':'sent'}


@pytest.mark.asyncio
async def test_legacy_reviews_preserved_and_only_explicit_admin_can_claim(sql_bot,bridge):
    b=sql_bot;row(bridge,'completed','sent','https://example.com/access')
    bridge.query("INSERT INTO testimonials(user_id,user_name,paket_id,order_id,rating,review,status) VALUES (42,'Old Buyer','p','test-order',4,'Old review','pending')")
    assert not await b.run_db(b._claim_public_testimonial_sync,'test-order')
    assert await b.run_db(b._claim_public_testimonial_sync,'test-order',True)
    r=bridge.query('SELECT user_name,rating,review FROM testimonials')['rows'][0]
    assert r=={'user_name':'Old Buyer','rating':4,'review':'Old review'}


def test_private_admin_template_and_payment_guards_restored():
    old=ast.parse((ROOT/'before.py').read_text());new=ast.parse((ROOT/'bot.py').read_text())
    def functions(t):return {x.name:ast.dump(x,include_attributes=False) for x in t.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef))}
    a,b=functions(old),functions(new)
    for name in ('_format_order_notif','kirim_notif','mark_order_completed','_mark_order_completed_sync',
                 'atomic_claim_for_delivery','_handle_payment_success','autogopay_webhook_handler','resend_group_link'):
        assert a[name]==b[name],name
    for name in ('handle_rate_start','handle_rate_val','handle_rate_text_skip','handle_rate_back_stars'):
        node=next(x for x in new.body if isinstance(x,ast.AsyncFunctionDef) and x.name==name)
        assert len(node.body)==1 and isinstance(node.body[0],ast.Return)
        assert node.body[0].value.value.func.id=='_legacy_review_notice'
