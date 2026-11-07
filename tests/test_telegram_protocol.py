"""Real PTB request/response serialization with an offline Bot API transport."""
import asyncio
import hashlib
import hmac
import json
from datetime import datetime,timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import ExtBot
from telegram.request import BaseRequest
from pg_support import PgBridge,Pool

class OfflineBotAPI(BaseRequest):
    def __init__(self):
        self.calls=[]
        self.next_id=100
    async def initialize(self): pass
    async def shutdown(self): pass
    async def do_request(self,url,method,request_data=None,**kwargs):
        endpoint=url.rsplit('/',1)[-1]
        data=request_data.parameters if request_data else {}
        self.calls.append((endpoint,data))
        if endpoint=='getMe':
            result={'id':123456,'is_bot':True,'first_name':'Offline','username':'offline_audit_bot'}
        elif endpoint in ('answerCallbackQuery','deleteMessage','deleteMessages'):
            result=True
        else:
            self.next_id+=1
            result={'message_id':data.get('message_id',self.next_id),'date':1700000000,
                    'chat':{'id':int(data.get('chat_id',42)),'type':'private'}}
            if endpoint in ('sendPhoto','editMessageCaption'):
                result.update(caption=data.get('caption',''),photo=[{'file_id':'offline-photo','file_unique_id':'offline-unique','width':100,'height':100}])
            else: result['text']=data.get('text','')
        return 200,json.dumps({'ok':True,'result':result}).encode()

def telegram_update(bot,data='buy',qid='query',photo=True):
    message={'message_id':10,'date':1700000000,'chat':{'id':42,'type':'private'}}
    if photo:
        message.update(caption='Previous menu',photo=[{'file_id':'offline-photo','file_unique_id':'offline-unique','width':100,'height':100}])
    else:message['text']='Previous menu'
    return Update.de_json({'update_id':1,'callback_query':{'id':qid,'chat_instance':'offline',
         'from':{'id':42,'is_bot':False,'first_name':'Synthetic'},'data':data,'message':message}},bot)

async def drain(module):
    while module._background_tasks:
        await asyncio.gather(*list(module._background_tasks))
        await asyncio.sleep(0)

@pytest.mark.asyncio
async def test_real_ptb_menu_payload_and_extension_style_cache(bot):
    transport=OfflineBotAPI()
    api=ExtBot(token=bot.TOKEN,request=transport,get_updates_request=OfflineBotAPI())
    await api.initialize()
    product={'paket_id':'p','nama':'Product','emoji':'📦','harga':10000,'aktif':True}
    bot._products_cache=([product],bot._time.monotonic()+30)
    processor=bot.PerChatUpdateProcessor(24)
    await processor.initialize()
    u=telegram_update(api)
    await processor.process_update(u,bot.buy_callback(u,NS(bot=api,user_data={},bot_data={})))
    await drain(bot)
    assert sum(e=='answerCallbackQuery' for e,d in transport.calls)==1
    assert sum(e=='editMessageCaption' for e,d in transport.calls)==1
    for style in ('success','danger'):
        keyboard=bot.InlineKeyboardMarkup([[bot.styled_button('Action',style=style,callback_data='same')]])
        await bot._render_buyer_menu(u.callback_query,'Same text',keyboard)
    edits=[d for e,d in transport.calls if e=='editMessageCaption']
    assert len(edits)==3
    assert edits[-1]['reply_markup']['inline_keyboard'][0][0]['style']=='danger'
    await api.shutdown()

@pytest.mark.asyncio
async def test_invoice_to_duplicate_webhook_to_single_delivery_via_real_ptb(bot):
    bridge=PgBridge()
    api=ExtBot(token=bot.TOKEN,request=OfflineBotAPI(),get_updates_request=OfflineBotAPI())
    await api.initialize()
    bot._pool=Pool(bridge)
    bot.init_db()
    bridge.query("UPDATE products SET link='https://example.com/synthetic-product',group_chat_id=NULL")
    bot._current_bot=api
    bot.create_transaction_qris=AsyncMock(return_value={
        'payment_provider':'gopay','provider_transaction_id':'synthetic-tx','provider_order_id':'synthetic-provider-order',
        'provider_status':'pending','amount':5000,'fee':0,'qr_string':'SYNTHETIC-QR-NOT-A-PAYMENT',
        'expiry_time':'2030-01-01T00:00:00+00:00'})
    context=NS(bot=api,user_data={},bot_data={})
    try:
        u=telegram_update(api,'confirm_buy_gb_biasa')
        await bot.confirm_buy_handler(u,context)
        await drain(bot)
        rows=bridge.query('SELECT * FROM orders')['rows']
        assert len(rows)==1 and rows[0]['status']=='waiting'
        assert rows[0]['buyer_msg_id'] is not None
        body=json.dumps({'event':'transaction.received','transaction':{
            'transaction_id':'synthetic-tx','payment_method':'QRIS','status':'PAID','amount':5000}}).encode()
        signature=hmac.new(bot.AUTOGOPAY_API_KEY.encode(),body,hashlib.sha256).hexdigest()
        async def webhook():
            return await bot.autogopay_webhook_handler(NS(read=AsyncMock(return_value=body),headers={'X-Signature':signature}))
        replies=await asyncio.gather(*(webhook() for _ in range(8)))
        assert all(response.status==200 for response in replies)
        await drain(bot)
        final=bridge.query('SELECT status,delivery_status,sent_link FROM orders')['rows'][0]
        assert final=={'status':'completed','delivery_status':'sent','sent_link':'https://example.com/synthetic-product'}
        deliveries=[d for endpoint,d in api.request.calls if endpoint=='sendMessage'
                    and str(d.get('chat_id'))=='42' and 'synthetic-product' in d.get('text','')]
        assert len(deliveries)==1
        bot.create_transaction_qris.assert_awaited_once()
    finally:
        await bot._stop_payment_task(42)
        await drain(bot)
        await api.shutdown()
        bridge.close()
