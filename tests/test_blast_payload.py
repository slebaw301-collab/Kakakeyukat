import unittest
from unittest.mock import AsyncMock
from blast_payload import parse_buttons, button_lines, build_markup, send_blast_payload

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
