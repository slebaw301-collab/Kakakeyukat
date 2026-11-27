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
