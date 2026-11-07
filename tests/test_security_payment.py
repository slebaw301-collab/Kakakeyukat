import asyncio
from io import BytesIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import pytest
import telegram.error
from support import ROOT, update

PRIVILEGED = (ROOT/'evidence/protected_callbacks.txt').read_text().splitlines()

@pytest.mark.asyncio
@pytest.mark.parametrize('handler', PRIVILEGED)
async def test_every_privileged_callback_rejects_non_admin(bot, handler):
    bot.is_admin = AsyncMock(return_value=False)
    u = update('forged_callback')
    await getattr(bot, handler)(u, NS(user_data={}, bot_data={}))
    bot.is_admin.assert_awaited_once()
    assert u.callback_query.answer.call_args.kwargs['show_alert'] is True
    u.callback_query.edit_message_text.assert_not_awaited()

class Response:
    status = 200
    def __init__(self, payload): self.payload=payload
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass

@pytest.mark.asyncio
@pytest.mark.parametrize('payload,accepted', [
    ({'transaction_id':'tx', 'amount':10000, 'status':'settlement'}, True),
    ({'transaction_id':'OTHER', 'amount':10000, 'status':'settlement'}, False),
    ({'transaction_id':'tx', 'amount':1, 'status':'settlement'}, False),
    ({'transaction_id':'tx', 'status':'settlement'}, True),
    ({'transaction_id':'tx', 'status':'pending'}, True),
])
async def test_provider_status_validation(bot, payload, accepted):
    response = Response(payload)
    bot.get_http_session = AsyncMock(return_value=NS(post=lambda *a,**kw:response))
    bot._read_json_response = AsyncMock(return_value={'success':True, 'data':payload})
    result = await bot._autogopay_get_status('tx',10000)
    assert (result is not None) is accepted

@pytest.mark.asyncio
@pytest.mark.parametrize('error,may_retry', [(telegram.error.TimedOut(), False),
    (telegram.error.NetworkError('test'), False), (telegram.error.Forbidden('test'), True),
    (telegram.error.BadRequest('test'), True), (RuntimeError('DB after send'), False)])
async def test_delivery_unknown_result_is_not_requeued(bot, error, may_retry):
    bot.reset_delivery_claim = AsyncMock()
    bot.set_delivery_status = AsyncMock()
    await bot._record_delivery_failure('order',error)
    assert bool(bot.reset_delivery_claim.await_count) is may_retry
    if not may_retry:
        assert bot.set_delivery_status.call_args.args[1] == 'pending'

@pytest.mark.asyncio
async def test_post_send_db_failure_never_unlocks_delivery(bot):
    bot.reset_delivery_claim = AsyncMock()
    bot.set_delivery_status = AsyncMock()
    await bot._record_delivery_failure('order',RuntimeError(),sent=True)
    bot.reset_delivery_claim.assert_not_awaited()
    bot.set_delivery_status.assert_awaited_once()

@pytest.mark.asyncio
async def test_saved_invoice_monitored_even_when_telegram_send_fails(bot):
    bot._reject_new_invoice_during_maintenance = AsyncMock(return_value=False)
    bot.create_transaction_qris = AsyncMock(return_value=dict(qr_string='fake',amount=10000,fee=0,
                                                            provider_transaction_id='tx'))
    bot.save_order = AsyncMock()
    bot.run_cpu = AsyncMock(return_value=BytesIO(b'fake image'))
    bot._start_payment_task = Mock()
    u = update()
    u.callback_query.message.photo = ('photo',)
    fakebot=NS(send_message=AsyncMock(return_value=NS(message_id=1)),
               send_photo=AsyncMock(side_effect=telegram.error.TimedOut()))
    with pytest.raises(telegram.error.TimedOut):
        await bot._buat_order_baru(u, NS(bot=fakebot,user_data={}),u.callback_query,42,'Buyer',
                                   dict(paket_id='p',nama='Product',emoji='📦',harga=10000))
    bot.save_order.assert_awaited_once()
    bot._start_payment_task.assert_called_once()

@pytest.mark.asyncio
async def test_invoice_claim_released_if_ack_cancelled(bot):
    u = update('confirm_buy_p')
    entered = asyncio.Event()
    async def blocking(**kw):
        entered.set()
        await asyncio.Event().wait()
    u.callback_query.answer = blocking
    task = asyncio.create_task(bot.confirm_buy_handler(u,NS()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert 42 not in bot._invoice_creation_in_progress

@pytest.mark.asyncio
async def test_stale_order_cannot_stop_new_order_monitor(bot):
    task = asyncio.create_task(asyncio.Event().wait())
    bot._payment_tasks[42] = task
    bot._payment_task_orders[42] = 'new-order'
    await bot._stop_payment_task(42, 'old-order')
    assert not task.cancelled()
    assert bot._payment_tasks[42] is task
    await bot._stop_payment_task(42, 'new-order')
    assert task.cancelled()

@pytest.mark.asyncio
async def test_bad_webhook_signature_does_not_query_db(bot):
    bot.get_order_by_provider_transaction_id = AsyncMock()
    response = await bot.autogopay_webhook_handler(NS(read=AsyncMock(return_value=b'{}'),headers={'X-Signature':'forged'}))
    assert response.status == 401
    bot.get_order_by_provider_transaction_id.assert_not_awaited()

@pytest.mark.asyncio
async def test_uncertain_provider_create_does_not_fallback_or_double_charge(bot):
    bot._autogopay_create_transaction_detailed = AsyncMock(return_value=(None,'uncertain'))
    bot._autogopay_create_shopeepay_transaction = AsyncMock()
    assert await bot.create_transaction_qris('order',10000,'test') is None
    bot._autogopay_create_shopeepay_transaction.assert_not_awaited()

@pytest.mark.asyncio
async def test_new_invoice_monitor_does_not_cancel_old_delivery(bot):
    delivery_started,release=asyncio.Event(),asyncio.Event()
    async def delivery(*args,**kwargs):
        delivery_started.set()
        await release.wait()
    bot.get_order_by_id=AsyncMock(return_value={'provider_transaction_id':'tx','payment_provider':'gopay'})
    bot._check_order_status=AsyncMock(return_value={'status':'waiting'})
    bot._autogopay_get_status=AsyncMock(return_value={'status':'completed','amount':10000,'provider_status':'settlement'})
    bot.update_order_provider_status=AsyncMock()
    bot._handle_payment_success=delivery
    bot.AUTOGOPAY_POLL_FAST_SECONDS=.001
    old=asyncio.create_task(bot._payment_poll_loop(None,'old','p',42,'Buyer',10000,2))
    bot._payment_tasks[42]=old
    bot._payment_task_orders[42]='old'
    await delivery_started.wait()
    await asyncio.wait_for(asyncio.shield(old),.5)
    async def next_monitor(*args,**kwargs): await asyncio.Event().wait()
    bot._payment_poll_loop=next_monitor
    await bot._start_payment_task_async(None,'new','p',42,'Buyer',10000,2)
    pending=list(bot._background_tasks)
    assert pending and all(not t.cancelled() for t in pending)
    release.set()
    await asyncio.gather(*pending)
    await bot._stop_payment_task(42,'new')

@pytest.mark.asyncio
@pytest.mark.parametrize('active,parent_user',[(False,42),(True,99)])
async def test_prerequisite_purchase_enforces_availability_and_parent_owner(bot,active,parent_user):
    bot.is_banned=AsyncMock(return_value=False)
    bot.get_product=AsyncMock(return_value={'paket_id':'p','aktif':active})
    bot.get_active_order=AsyncMock(return_value=None)
    bot.get_cooldown_sisa_db=AsyncMock(return_value=0)
    bot._reject_new_invoice_during_maintenance=AsyncMock(return_value=False)
    bot.get_order_by_id=AsyncMock(return_value={'user_id':parent_user,'paket_id':'parent'})
    bot._buat_order_baru=AsyncMock()
    u=update('prereq_buy_p|parent-order')
    await bot.prereq_buy_handler(u,NS(user_data={}))
    bot._buat_order_baru.assert_not_awaited()
    assert u.callback_query.message.reply_text.await_count==1

@pytest.mark.asyncio
async def test_authorized_admin_can_still_toggle_product(bot):
    bot.is_admin=AsyncMock(return_value=True)
    product={'paket_id':'p','nama':'Product','emoji':'📦','harga':10000,'deskripsi':'Description',
             'link':'https://example.com','aktif':True}
    bot.get_product=AsyncMock(return_value=product)
    bot.update_product_field=AsyncMock(return_value=True)
    u=update('pd_toggle_p')
    await bot.produk_toggle_aktif(u,NS())
    bot.update_product_field.assert_awaited_once_with('p','aktif',False)
    u.callback_query.edit_message_text.assert_awaited_once()
