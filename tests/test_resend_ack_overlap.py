import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from support import update


def setup(bot):
    bot._resend_access_allowed=AsyncMock(return_value=True)
    bot.get_resend_order=AsyncMock(return_value={'order_id':'synthetic','paket_id':'p','delivery_status':'sent'})
    bot.get_resend_product=AsyncMock(return_value={'paket_id':'p','nama':'Product','group_chat_id':-100,'emoji':'📦'})
    bot.generate_group_link=AsyncMock(return_value='https://example.com/synthetic-invite')
    bot.set_sent_link=AsyncMock()
    context=NS(user_data={},bot_data={},bot=NS(send_message=AsyncMock()))
    return update('resend_select|123',query_id='resend-once'),context


@pytest.mark.asyncio
async def test_resend_validates_and_delivers_while_ack_is_in_flight(bot):
    u,context=setup(bot)
    ack_entered=asyncio.Event()
    release=asyncio.Event()
    async def answer(**kwargs):
        assert kwargs['text']=='⏳ Mengambil akses terbaru...'
        ack_entered.set()
        await release.wait()
    u.callback_query.answer.side_effect=answer
    task=asyncio.create_task(bot.resend_group_link(u,context))
    await ack_entered.wait()
    await asyncio.wait_for(task,timeout=.5)
    assert not release.is_set()
    bot.get_resend_order.assert_awaited_once_with(42,123)
    bot.generate_group_link.assert_awaited_once()
    context.bot.send_message.assert_awaited_once()
    bot.set_sent_link.assert_awaited_once_with('synthetic','https://example.com/synthetic-invite')
    assert '_resend_busy' not in context.user_data and '_resend_last_sent' in context.user_data
    release.set()
    await asyncio.gather(*list(bot._background_tasks))
    assert u.callback_query.answer.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['denied','busy','cooldown','not_paid'])
async def test_resend_guards_still_prevent_delivery(bot,guard):
    u,context=setup(bot)
    if guard=='denied':bot._resend_access_allowed.return_value=False
    elif guard=='busy':context.user_data['_resend_busy']=True
    elif guard=='cooldown':context.user_data['_resend_last_sent']=bot._time.monotonic()
    else:bot.get_resend_order.return_value=None
    await bot.resend_group_link(u,context)
    await asyncio.gather(*list(bot._background_tasks))
    bot.generate_group_link.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()
    bot.set_sent_link.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlapping_ack_does_not_allow_duplicate_resend(bot):
    u,context=setup(bot)
    entered=asyncio.Event()
    release=asyncio.Event()
    async def invite(*args,**kwargs):
        entered.set()
        await release.wait()
        return 'https://example.com/synthetic-invite'
    bot.generate_group_link.side_effect=invite
    first=asyncio.create_task(bot.resend_group_link(u,context))
    await entered.wait()
    other=update('resend_select|123',query_id='another-query')
    await bot.resend_group_link(other,context)
    release.set()
    await first
    await asyncio.gather(*list(bot._background_tasks))
    context.bot.send_message.assert_awaited_once()
    bot.set_sent_link.assert_awaited_once()
    await bot.resend_group_link(other,context)
    context.bot.send_message.assert_awaited_once()
