import asyncio
from io import BytesIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
import telegram.error
from support import load_bot,close_bot,update


class MediaAPI:
    def __init__(self):
        self.sent=[];self.edited=[];self.deleted=[];self.messages={};self.next_id=10
        self.edit_error=None;self.send_error=None
    async def send_photo(self,**kw):
        self.sent.append(kw)
        await asyncio.sleep(0)
        if self.send_error:raise self.send_error
        if kw['photo']=='invalid-id':raise telegram.error.BadRequest('Wrong remote file identifier specified')
        self.next_id+=1
        msg=NS(message_id=self.next_id,chat_id=kw['chat_id'],photo=[NS(file_id='canonical-id',file_unique_id='stable-image')],caption_html=kw['caption'],reply_markup=kw['reply_markup'])
        self.messages[(msg.chat_id,msg.message_id)]=msg
        return msg
    async def edit_message_caption(self,**kw):
        self.edited.append(kw)
        if self.edit_error:
            error=self.edit_error;self.edit_error=None
            raise error
        key=(kw['chat_id'],kw['message_id'])
        if key not in self.messages:raise telegram.error.BadRequest('Message to edit not found')
        msg=self.messages[key]
        msg.caption_html=kw['caption'];msg.reply_markup=kw['reply_markup']
        return msg
    async def delete_message(self,**kw):
        self.deleted.append(kw)
        self.messages.pop((kw['chat_id'],kw['message_id']),None)


def context(api):return NS(bot=api,user_data={},bot_data={})
async def drain(bot):
    while bot._background_tasks:await asyncio.gather(*list(bot._background_tasks))


@pytest.mark.asyncio
async def test_start_after_long_pause_and_client_reconnect_reuses_message(bot):
    api=MediaAPI();ctx=context(api)
    bot.set_setting=AsyncMock()
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True,photo_id='valid-id')
    mid=ctx.user_data['_home_photo_id']
    for _ in range(3):
        ctx.user_data['_home_photo_at']=bot._time.monotonic()-3600
        # Client reconnect does not reset running server state.
        ctx=NS(bot=api,user_data=ctx.user_data,bot_data=ctx.bot_data)
        bot._catalog_update_boundary(NS(effective_chat=NS(id=42),message=NS(text='/start'),callback_query=None))
        await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True)
    await drain(bot)
    assert len(api.sent)==1 and len(api.edited)==3 and not api.deleted
    assert {e['message_id'] for e in api.edited}=={mid}
    assert api.sent[0]['photo']=='valid-id' and bot._MENU_PHOTO_FILE_ID=='valid-id'
    bot.set_setting.assert_not_awaited()


@pytest.mark.asyncio
async def test_warm_file_id_is_not_replaced_by_a_different_valid_alias(bot):
    api=MediaAPI();bot.set_setting=AsyncMock()
    for uid in (42,43,44):await bot._send_main_menu_photo(context(api),uid,'Home',None,photo_id='valid-id')
    assert [x['photo'] for x in api.sent]==['valid-id']*3
    bot.set_setting.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_id_concurrent_users_upload_once_and_persist(bot):
    api=MediaAPI();bot.set_setting=AsyncMock()
    await asyncio.gather(*(bot._send_main_menu_photo(context(api),uid,'Home',None,photo_id='') for uid in (42,43,44)))
    assert sum(isinstance(x['photo'],BytesIO) for x in api.sent)==1
    assert len(api.sent)==3
    bot.set_setting.assert_awaited_once_with(bot._MENU_PHOTO_SETTING,'canonical-id')


@pytest.mark.asyncio
async def test_invalid_id_concurrent_fallback_uploads_once(bot):
    api=MediaAPI();bot.set_setting=AsyncMock()
    bot._MENU_PHOTO_FILE_ID='invalid-id'
    await asyncio.gather(*(bot._send_main_menu_photo(context(api),uid,'Home',None) for uid in (42,43,44)))
    assert sum(isinstance(x['photo'],BytesIO) for x in api.sent)==1
    assert bot._MENU_PHOTO_FILE_ID=='canonical-id'


@pytest.mark.asyncio
@pytest.mark.parametrize('error',[telegram.error.BadRequest('Can\'t parse entities'),telegram.error.TimedOut()])
async def test_generic_or_uncertain_send_failure_does_not_reupload(bot,error):
    api=MediaAPI();api.send_error=error
    with pytest.raises(type(error)):
        await bot._send_main_menu_photo(context(api),42,'Home',None,photo_id='valid-id')
    assert len(api.sent)==1 and not isinstance(api.sent[0]['photo'],BytesIO)
    assert bot._MENU_PHOTO_FILE_ID=='valid-id'


@pytest.mark.asyncio
async def test_deleted_catalog_uses_file_id_without_upload(bot):
    api=MediaAPI();ctx=context(api)
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True,photo_id='valid-id')
    api.messages.clear()
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True)
    assert len(api.sent)==2 and len(api.edited)==1
    assert all(isinstance(x['photo'],str) for x in api.sent)


@pytest.mark.asyncio
async def test_not_modified_or_uncertain_edit_does_not_create_photo(bot):
    api=MediaAPI();ctx=context(api)
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True,photo_id='valid-id')
    api.edit_error=telegram.error.BadRequest('Message is not modified')
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True)
    api.edit_error=telegram.error.TimedOut()
    with pytest.raises(telegram.error.TimedOut):await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True)
    assert len(api.sent)==1


@pytest.mark.asyncio
async def test_transient_cleanup_preserves_catalog_but_business_flow_invalidates_reuse(bot):
    api=MediaAPI();ctx=context(api)
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True,photo_id='valid-id')
    bot.simpan_msg_user(ctx,42,999)
    await bot.hapus_msg_user_lama(ctx,42,keep_last=0)
    assert 42 in bot._catalog_photo_messages
    # Deleting an unrelated notice must not delete the catalog photo.
    await drain(bot)
    assert [x['message_id'] for x in api.deleted]==[999]
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True)
    bot._catalog_update_boundary(update('confirm_buy_p'))
    assert 42 not in bot._catalog_photo_messages
    assert len(api.edited)==1
    await drain(bot)


@pytest.mark.asyncio
async def test_catalog_navigation_then_start_restores_same_photo(bot):
    api=MediaAPI();ctx=context(api)
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True,photo_id='valid-id')
    mid=ctx.user_data['_home_photo_id']
    u=update('buy',message_id=mid)
    u.callback_query.message.photo=api.messages[(42,mid)].photo
    async def edit(**kw):return await api.edit_message_caption(chat_id=42,message_id=mid,**kw)
    u.callback_query.edit_message_caption=edit
    bot._products_cache=([dict(paket_id='p',nama='Product',aktif=True)],bot._time.monotonic()+60)
    proc=bot.PerChatUpdateProcessor(24);await proc.initialize()
    await proc.process_update(u,bot.buy_callback(u,ctx))
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True)
    assert len(api.sent)==1 and api.messages[(42,mid)].caption_html=='Home'
    assert (42,mid) not in bot._menu_render_cache
    await drain(bot);await proc.shutdown()


@pytest.mark.asyncio
async def test_restart_uses_persisted_id_and_registry_is_bounded(bot):
    api=MediaAPI();bot.set_setting=AsyncMock()
    await bot._send_main_menu_photo(context(api),42,'Home',None,photo_id='')
    bot._MENU_PHOTO_FILE_ID=None;bot._catalog_photo_messages.clear()
    bot.get_setting=AsyncMock(return_value='canonical-id')
    await bot._send_main_menu_photo(context(api),43,'Home',None,reuse_recent=True)
    assert sum(isinstance(x['photo'],BytesIO) for x in api.sent)==1
    bot._catalog_photo_messages.clear()
    for i in range(4096):bot._catalog_photo_messages[i]={'message_id':1,'media_key':bot._MENU_PHOTO_SETTING,'identity':'test'}
    await bot._send_main_menu_photo(context(api),9000,'Home',None)
    assert len(bot._catalog_photo_messages)==4096 and 0 not in bot._catalog_photo_messages


@pytest.mark.asyncio
async def test_persistence_failure_keeps_current_media_usable(bot):
    api=MediaAPI();bot.set_setting=AsyncMock(side_effect=RuntimeError('offline'))
    ctx=context(api)
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True,photo_id='')
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True)
    assert len(api.sent)==1 and len(api.edited)==1


@pytest.mark.asyncio
async def test_every_explicit_start_sends_visible_reply_and_preserves_buyer_checks(bot):
    api=MediaAPI();ctx=context(api)
    bot.get_start_snapshot=AsyncMock(return_value={'banned':False,'cooldown_minutes':0,'active':None})
    settings=bot._menu_setting_defaults();settings[bot._MENU_PHOTO_SETTING]='valid-id'
    bot.get_settings=AsyncMock(return_value=settings)
    u=update();u.callback_query=None;u.message=NS(text='/start',reply_text=AsyncMock())
    for _ in range(5):
        await bot.start(u,ctx)
    await drain(bot)
    assert len(api.sent)==5 and len(api.edited)==0
    assert not api.deleted and len(api.messages)==5
    assert len({m['photo'] for m in api.sent})==1 and api.sent[0]['photo']=='valid-id'
    assert bot.get_start_snapshot.await_count==5
    bot.get_start_snapshot.return_value={'banned':True,'cooldown_minutes':0,'active':None}
    await bot.start(u,ctx)
    u.message.reply_text.assert_awaited_once()
    assert len(api.sent)==5 and len(api.edited)==0


@pytest.mark.asyncio
async def test_start_preserves_catalog_history_but_cleans_transient_notice(bot):
    api=MediaAPI();ctx=context(api)
    await bot._send_main_menu_photo(ctx,42,'Home',None,photo_id='valid-id')
    first=ctx.user_data['_home_photo_id']
    bot.simpan_msg_user(ctx,42,999)
    await bot._send_main_menu_photo(ctx,42,'Home',None)
    await drain(bot)
    assert len(api.messages)==2 and (42,first) in api.messages
    assert [d['message_id'] for d in api.deleted]==[999]


@pytest.mark.asyncio
async def test_description_keeps_active_catalog_but_other_message_does_not(bot):
    api=MediaAPI();ctx=context(api)
    await bot._send_main_menu_photo(ctx,42,'Home',None,reuse_recent=True,photo_id='valid-id')
    mid=ctx.user_data['_home_photo_id']
    bot._catalog_update_boundary(update('buyer_desc_p',message_id=mid))
    assert 42 in bot._catalog_photo_messages
    bot._catalog_update_boundary(update('buy',message_id=mid+1))
    assert 42 not in bot._catalog_photo_messages
