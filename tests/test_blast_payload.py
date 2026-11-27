import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
import json
from telegram import MessageEntity
from telegram.error import BadRequest, Forbidden, TimedOut
from blast_payload import (parse_buttons, button_lines, build_markup, send_blast_payload,
                           capture_blast_body, custom_emoji_count)

class BlastButtonsTests(unittest.TestCase):
    def test_roundtrip_and_order(self):
        text = "First | https://example.com/a | biru\nSecond | @example | hijau\nThird | https://example.com/c | merah | 12345"
        buttons = parse_buttons(text)
        self.assertEqual(parse_buttons(button_lines(buttons)), buttons)
        self.assertEqual(buttons[1]["url"], "https://t.me/example")
        rows = build_markup({"buttons":buttons,"columns":2}).inline_keyboard
        self.assertEqual([len(r) for r in rows], [2,1])
        self.assertEqual(rows[0][0].api_kwargs["style"], "primary")
        self.assertEqual(rows[1][0].api_kwargs["icon_custom_emoji_id"], "12345")
    def test_default_and_empty(self):
        self.assertEqual(parse_buttons("-"), [])
        self.assertIsNone(build_markup({}))
        button = build_markup({"buttons":parse_buttons("A | https://example.com")}).inline_keyboard[0][0]
        self.assertNotIn("style", button.api_kwargs)
    def test_reject_invalid(self):
        for text in ["", "a", "a | javascript:alert(1)", "a | https://", "a | https://u:p@example.com",
                     "a | https://example.com | purple", "a | https://example.com | default | x",
                     "a | https://example.com\n"*11]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_buttons(text)
    def test_maximum(self):
        buttons = parse_buttons("\n".join(f"{i} | https://example.com/{i}" for i in range(10)))
        self.assertEqual(len(build_markup({"buttons":buttons,"columns":1}).inline_keyboard),10)

class BlastDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_text(self):
        bot=AsyncMock()
        await send_blast_payload(bot, 42, "<b>Hello</b>", None, read_timeout=15)
        bot.send_message.assert_awaited_once_with(chat_id=42,text="<b>Hello</b>",parse_mode="HTML",read_timeout=15)
    async def test_media_and_buttons(self):
        for kind in ("photo","video"):
            bot=AsyncMock()
            payload=dict(kind=kind,file_id="stored-id",buttons=parse_buttons("Info | https://example.com"),columns=2)
            await send_blast_payload(bot, 42, "caption", payload)
            send=getattr(bot,"send_"+kind)
            self.assertEqual(send.await_args.kwargs[kind],"stored-id")
            self.assertEqual(send.await_args.kwargs["caption"],"caption")
            self.assertEqual(send.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url,"https://example.com")
            bot.send_message.assert_not_awaited()
    async def test_media_without_caption(self):
        bot=AsyncMock()
        await send_blast_payload(bot, 42, "", dict(kind="photo",file_id="stored-id"))
        self.assertIsNone(bot.send_photo.await_args.kwargs["caption"])
    async def test_text_buttons(self):
        bot=AsyncMock()
        await send_blast_payload(bot,42,"hello",{"buttons":parse_buttons("One | https://example.com\nTwo | https://example.org"),"columns":2})
        self.assertEqual(len(bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0]),2)

class BlastNativeEmojiTests(unittest.IsolatedAsyncioTestCase):
    def native(self):
        # Leading whitespace and astral Unicode make trimming/code-point offsets wrong.
        raw = " 😀✨ text"
        entities = [MessageEntity("custom_emoji", 1, 2, custom_emoji_id="1234567890"),
                    MessageEntity("custom_emoji", 3, 1, custom_emoji_id="9876543210"),
                    MessageEntity("bold", 5, 4)]
        return raw, entities

    async def test_native_text_utf16_and_restart(self):
        raw, entities = self.native()
        payload = {"kind": "text", "buttons": parse_buttons("Open | https://example.com | hijau")}
        captured = capture_blast_body(SimpleNamespace(text=raw, entities=entities), payload, raw.strip())
        self.assertEqual(captured, raw)
        restored = json.loads(json.dumps(payload))
        self.assertEqual(custom_emoji_count(restored["entities"]), 2)
        bot = AsyncMock()
        await send_blast_payload(bot, 42, captured, restored)
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["text"], raw)
        self.assertIsNone(kwargs["parse_mode"])
        self.assertEqual([e.to_dict() for e in kwargs["entities"]], [e.to_dict() for e in entities])
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][0].api_kwargs["style"], "success")

    async def test_native_caption_photo_and_video(self):
        raw, entities = self.native()
        entities.append(MessageEntity("text_link", 5, 4, url="https://example.com"))
        for kind in ("photo", "video"):
            payload = dict(kind=kind, file_id="saved")
            captured = capture_blast_body(SimpleNamespace(caption=raw, caption_entities=entities), payload)
            bot = AsyncMock()
            await send_blast_payload(bot, 42, captured, payload)
            kwargs = getattr(bot, "send_" + kind).await_args.kwargs
            self.assertEqual(kwargs["caption"], raw)
            self.assertEqual([e.to_dict() for e in kwargs["caption_entities"]], [e.to_dict() for e in entities])
            self.assertIsNone(kwargs["parse_mode"])

    async def test_rejected_custom_emoji_falls_back_once_preserving_format(self):
        raw, entities = self.native()
        payload = dict(kind="text")
        capture_blast_body(SimpleNamespace(text=raw, entities=entities), payload)
        bot = AsyncMock()
        bot.send_message.side_effect = [BadRequest("CUSTOM_EMOJI_INVALID"), "sent"]
        self.assertEqual(await send_blast_payload(bot, 42, raw, payload), "sent")
        self.assertEqual(bot.send_message.await_count, 2)
        first, second = bot.send_message.await_args_list
        self.assertEqual(custom_emoji_count(first.kwargs["entities"]), 2)
        self.assertEqual([e.type for e in second.kwargs["entities"]], ["bold"])
        self.assertEqual(second.kwargs["text"], raw)
        self.assertEqual(custom_emoji_count(payload["entities"]), 2)

    async def test_no_retry_for_uncertain_or_unrelated_errors(self):
        raw, entities = self.native()
        payload = dict(kind="text")
        capture_blast_body(SimpleNamespace(text=raw, entities=entities), payload)
        for exc in (TimedOut(), Forbidden("bot was blocked"), BadRequest("chat not found")):
            bot = AsyncMock()
            bot.send_message.side_effect = exc
            with self.assertRaises(type(exc)):
                await send_blast_payload(bot, 42, raw, payload)
            self.assertEqual(bot.send_message.await_count, 1)

    async def test_retype_clears_previous_offsets_and_preserves_html(self):
        raw, entities = self.native()
        payload = dict(kind="text")
        capture_blast_body(SimpleNamespace(text=raw, entities=entities), payload)
        body = capture_blast_body(SimpleNamespace(text="<b>New</b>", entities=[]), payload, "<b>New</b>")
        self.assertNotIn("entities", payload)
        bot = AsyncMock()
        await send_blast_payload(bot, 42, body, payload)
        bot.send_message.assert_awaited_once_with(chat_id=42, text="<b>New</b>", parse_mode="HTML")

    async def test_format_only_no_premium(self):
        raw = "link"
        payload = dict(kind="text")
        capture_blast_body(SimpleNamespace(text=raw, entities=[MessageEntity("text_link", 0, 4, url="https://example.org")]), payload, raw)
        bot=AsyncMock()
        await send_blast_payload(bot, 42, raw, payload)
        self.assertEqual(bot.send_message.await_args.kwargs["entities"][0].url, "https://example.org")
        self.assertEqual(bot.send_message.await_count, 1)

class BlastDestinationTests(unittest.IsolatedAsyncioTestCase):
    def test_audience_modes_and_legacy(self):
        from blast_payload import blast_audience
        dest = [{"id": -1001234567890}, {"id": -1001234567890}, {"id": -42}]
        self.assertEqual(blast_audience(None), (True, []))
        self.assertEqual(blast_audience({"audience":"buyers","destinations":dest}), (True, []))
        self.assertEqual(blast_audience({"audience":"chats","destinations":dest}), (False, [-1001234567890,-42]))
        self.assertEqual(blast_audience({"audience":"both","destinations":dest}), (True, [-1001234567890,-42]))
        for payload in ({"audience":"chats"}, {"audience":"unknown"}, {"audience":"both","destinations":[{"id":42}]}):
            with self.assertRaises(ValueError):
                blast_audience(payload)

    def test_parse_targets(self):
        from blast_payload import parse_destinations
        self.assertEqual(parse_destinations("@Example -1001234567890\n@example"), ["@example",-1001234567890])
        for raw in ("", "12345", "https://t.me/example", "@a", "-1 "*11):
            with self.assertRaises(ValueError):
                parse_destinations(raw)

    async def test_validation_resolves_and_deduplicates_without_posting(self):
        from blast_payload import resolve_destinations
        bot = AsyncMock()
        bot.id = 42
        bot.get_chat.return_value = SimpleNamespace(id=-1001234567890,type="channel",title="Example")
        bot.get_chat_member.return_value = SimpleNamespace(status="administrator",can_post_messages=True)
        result = await resolve_destinations(bot, "@example -1001234567890")
        self.assertEqual(result,[{"id":-1001234567890,"title":"Example","type":"channel"}])
        bot.send_message.assert_not_awaited()
        bot.send_photo.assert_not_awaited()
        bot.send_video.assert_not_awaited()

    async def test_reject_private_nonadmin_and_no_post_permission(self):
        from blast_payload import resolve_destinations
        bot = AsyncMock()
        bot.id = 42
        for chat,member in (
            (SimpleNamespace(id=12,type="private",title=None), SimpleNamespace(status="administrator")),
            (SimpleNamespace(id=-12,type="supergroup",title="Group"), SimpleNamespace(status="member")),
            (SimpleNamespace(id=-12,type="channel",title="Channel"), SimpleNamespace(status="administrator",can_post_messages=False)),
        ):
            bot.get_chat.return_value = chat
            bot.get_chat_member.return_value = member
            with self.assertRaises(ValueError):
                await resolve_destinations(bot,"@example")
        bot.send_message.assert_not_awaited()

    async def test_direct_channel_delivery_keeps_media_and_buttons(self):
        bot = AsyncMock()
        payload = {"kind":"photo","file_id":"saved","audience":"chats","destinations":[{"id":-1001234567890}],
                   "buttons":parse_buttons("Open | https://example.com | hijau")}
        await send_blast_payload(bot,-1001234567890,"Caption",payload)
        kwargs=bot.send_photo.await_args.kwargs
        self.assertEqual(kwargs["chat_id"],-1001234567890)
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[0][0].api_kwargs["style"],"success")
        bot.send_message.assert_not_awaited()

class BlastPickerTests(unittest.TestCase):
    def test_pages_are_sorted_and_bounded(self):
        from blast_payload import picker_page
        rows=[{"id":-i,"title":f"Chat {i:02d}"} for i in range(20,0,-1)]
        page,index,pages=picker_page(rows,0)
        self.assertEqual((len(page),index,pages),(8,0,3))
        self.assertEqual(page[0]["id"],-1)
        page,index,pages=picker_page(rows,99)
        self.assertEqual((len(page),index,pages),(4,2,3))
        self.assertEqual(picker_page([],0),([],0,1))

    def test_toggle_multiple_pages_reset_all_and_stale(self):
        from blast_payload import picker_select
        available=set(range(-10,0))
        selected=picker_select([],available,"toggle",-1)
        selected=picker_select(selected,available,"toggle",-10)
        self.assertEqual(selected,{-1,-10})
        self.assertEqual(picker_select(selected,available,"toggle",-1),{-10})
        self.assertEqual(picker_select(selected,available,"all"),available)
        self.assertEqual(picker_select(selected,available,"reset"),set())
        with self.assertRaises(ValueError):
            picker_select(selected,available,"toggle",-11)

    def test_limit_is_not_silently_truncated(self):
        from blast_payload import picker_select
        available=set(range(-11,0))
        with self.assertRaises(ValueError):
            picker_select([],available,"all")
        with self.assertRaises(ValueError):
            picker_select(set(range(-10,0)),available,"toggle",-11)

    def test_membership_permission_changes(self):
        from blast_payload import destination_from_membership
        chat=SimpleNamespace(id=-1001234567890,type="channel",title="Example")
        member=SimpleNamespace(status="administrator",can_post_messages=True)
        self.assertEqual(destination_from_membership(chat,member)["id"],chat.id)
        for invalid in (SimpleNamespace(status="left"),SimpleNamespace(status="member"),
                        SimpleNamespace(status="administrator",can_post_messages=False)):
            with self.assertRaises(ValueError):
                destination_from_membership(chat,invalid)

    def test_callback_length_with_maximum_id(self):
        token="a"*32
        self.assertLessEqual(len(f"bp|{token}|toggle|-9999999999999999".encode()),64)
