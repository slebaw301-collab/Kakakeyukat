"""Render and send Hyper Store's verified Telegram custom emoji."""

import asyncio
from collections import Counter, OrderedDict
from contextvars import ContextVar
from functools import lru_cache
from html import escape
import logging
import re
from time import monotonic

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ExtBot

from premium_emoji_catalog import EMOJI_CATALOG

logger = logging.getLogger(__name__)
SETTING = 'premium_emoji_enabled'
COOLDOWN = 300
PREVIEW_VALIDITY = 600
_TEXT_FIELDS = {
    'sendMessage': 'text',
    'editMessageText': 'text',
    'sendPhoto': 'caption',
    'sendDocument': 'caption',
    'editMessageCaption': 'caption',
    'editMessageReplyMarkup': None,
}
_SUPPORTED_CHATS = {'private', 'group', 'supergroup'}
_SKIP_TAGS = {'a', 'code', 'pre', 'tg-emoji'}
_SYMBOL = re.compile(
    r'(?<!\u200d)[' + re.escape(''.join(EMOJI_CATALOG))
    + r']\ufe0f?(?![\u200d\ufe0e\ufe0f\u20e3\U0001f3fb-\U0001f3ff])'
)
# Product emoji are editable in the admin panel. Unknown product glyphs use
# the catalog icon while retaining the original button payload for OFF/fallback.
_PRODUCT_CALLBACKS = ('pilih_', 'ganti_paket_konfirm|', 'resend_select|', 'pd_detail_',
                      'pd_req_toggle|', 'prereq_buy_')
_EMOJI_PART = r'[\u2194-\u2199\u21a9\u21aa\u23e9-\u23fa\u25aa-\u27bf\u2b05-\u2b55\U0001f300-\U0001faff]\ufe0f?[\U0001f3fb-\U0001f3ff]?'
_PRODUCT_EMOJI = re.compile(
    rf'^(?:[\U0001f1e6-\U0001f1ff]{{2}}|[#*0-9]\ufe0f?\u20e3|'
    rf'{_EMOJI_PART}(?:\u200d{_EMOJI_PART})*)\s(?=\s*\S)'
)
_HTML_TAG = re.compile(r"""<(/?)([a-z][a-z0-9-]*)(?=[\s/>])(?:[^<"'>]|"[^"]*"|'[^']*')*>""", re.I)
_BARE_URL = re.compile(r'(?:https?://|tg://)[^\s<>]+', re.I)
_REJECTION = re.compile(
    r'custom[\s_ -]*emoji|icon_custom_emoji_id|tg-emoji|emoji[-_ ]id|emoji.{0,28}premium',
    re.I,
)


def _base(symbol):
    return symbol.rstrip('\ufe0f')


class PremiumEmoji:
    def __init__(self):
        self.enabled = False
        self.revision = 0
        self.ready = False
        self.ids = {}
        self.tags = {}
        self.chats = OrderedDict()
        self.blocked = {}
        self.preview_target = ContextVar('premium_emoji_preview', default=None)
        self.preview_verified_at = None
        self.validation_lock = asyncio.Lock()
        self.button_cache = lru_cache(maxsize=512)(self._button_icon)

    async def validate(self, bot):
        """One batch at startup or from the admin preview after a startup failure."""
        async with self.validation_lock:
            if self.ready:
                return
            candidates = dict(EMOJI_CATALOG.values())
            try:
                stickers = await bot.get_custom_emoji_stickers(list(candidates))
            except TelegramError as exc:
                logger.warning('[PREMIUM EMOJI] Catalog validation failed: %s', type(exc).__name__)
                return
            returned = {
                s.custom_emoji_id: s.emoji for s in stickers
                if s.custom_emoji_id in candidates and s.emoji
                and (s.is_animated or s.is_video) and not s.needs_repainting
                and _base(s.emoji) == _base(candidates[s.custom_emoji_id])
            }
            self.ids = {
                symbol: emoji_id for symbol, (emoji_id, _) in EMOJI_CATALOG.items()
                if emoji_id in returned
            }
            self.tags = {
                symbol: f'<tg-emoji emoji-id="{emoji_id}">{escape(returned[emoji_id])}</tg-emoji>'
                for symbol, emoji_id in self.ids.items()
            }
            self.ready = bool(returned)
            self.button_cache.cache_clear()
            self.revision += 1
            logger.info('[PREMIUM EMOJI] Catalog verified=%d/%d', len(returned), len(candidates))

    def set_enabled(self, enabled):
        if self.enabled != enabled:
            self.enabled = enabled
            self.revision += 1

    def remember_chat(self, chat_id, chat_type):
        if not isinstance(chat_id, int) or chat_id >= 0:
            return
        self.chats[chat_id] = chat_type
        self.chats.move_to_end(chat_id)
        if len(self.chats) > 512:
            self.chats.popitem(last=False)

    @staticmethod
    def chat_id(chat_id):
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            return None

    def chat_type(self, chat_id):
        if chat_id is None:
            return None
        return 'private' if chat_id > 0 else self.chats.get(chat_id)

    def _block(self, chat_type, feature):
        until = self.blocked.get((chat_type, feature), 0.0)
        return bool(until and until > monotonic())

    def fallback(self, chat_type, features):
        until = monotonic() + COOLDOWN
        for feature in features:
            if not self._block(chat_type, feature):
                logger.info('[PREMIUM EMOJI] Automatic fallback scope=%s feature=%s', chat_type, feature)
                self.revision += 1
            self.blocked[chat_type, feature] = until
        if chat_type == 'private':
            self.preview_verified_at = None

    def record_preview(self, verified):
        self.preview_verified_at = monotonic() if verified else None
        if verified:
            for feature in ('text', 'caption', 'buttons'):
                if self.blocked.pop(('private', feature), None):
                    self.revision += 1

    def preview_allowed(self):
        return (self.preview_verified_at is not None
                and monotonic() - self.preview_verified_at < PREVIEW_VALIDITY)

    def _format_html(self, text):
        """Replace visible emoji while preserving Telegram HTML and its attributes."""
        ids = Counter()
        parts = []
        skipped = []
        position = 0

        def replace(match):
            symbol = _base(match[0])
            tag = self.tags.get(symbol)
            if tag is None:
                return match[0]
            ids[self.ids[symbol]] += 1
            return tag

        def visible(segment):
            position = 0
            pieces = []
            for url in _BARE_URL.finditer(segment):
                pieces.extend((_SYMBOL.sub(replace, segment[position:url.start()]), url[0]))
                position = url.end()
            pieces.append(_SYMBOL.sub(replace, segment[position:]))
            return ''.join(pieces)

        for tag in _HTML_TAG.finditer(text):
            segment = text[position:tag.start()]
            parts.append(segment if skipped else visible(segment))
            parts.append(tag[0])
            name = tag[2].lower()
            if name in _SKIP_TAGS:
                if not tag[1]:
                    skipped.append(name)
                elif skipped and skipped[-1] == name:
                    skipped.pop()
            position = tag.end()
        tail = text[position:]
        parts.append(tail if skipped else visible(tail))
        return ''.join(parts), ids

    def render_text(self, text, parse_mode):
        if not text or not _SYMBOL.search(text):
            return text, None
        if parse_mode and parse_mode.upper() != 'HTML':
            return text, None
        value, ids = self._format_html(text if parse_mode else escape(text, quote=False))
        return (value, ids) if ids else (text, None)

    def _button_icon(self, label, product_fallback=False):
        if product_fallback:
            prefix = _PRODUCT_EMOJI.match(label)
            emoji_id = self.ids.get('📦')
            return (label[prefix.end():], emoji_id) if prefix and emoji_id else None
        # Telegram has room for one custom icon before the label. Retain other
        # symbols, and keep labels consisting only of an emoji unchanged.
        for match in _SYMBOL.finditer(label):
            before, after = label[:match.start()], label[match.end():]
            if before.strip() and after.strip():
                continue
            remaining = after.lstrip() if not before.strip() else before.rstrip()
            emoji_id = self.ids.get(_base(match[0]))
            if emoji_id and any(character.isalnum() for character in remaining):
                return remaining, emoji_id
        return None

    def render_markup(self, markup):
        icons = {}
        if not isinstance(markup, InlineKeyboardMarkup):
            return markup, icons
        rows = None
        for row_index, row in enumerate(markup.inline_keyboard):
            for column, button in enumerate(row):
                if button.api_kwargs.get('icon_custom_emoji_id'):
                    continue
                icon = self.button_cache(button.text)
                callback = button.callback_data
                if icon is None and isinstance(callback, str) and callback.startswith(_PRODUCT_CALLBACKS):
                    icon = self.button_cache(button.text, True)
                if icon is None:
                    continue
                if rows is None:
                    rows = markup.to_dict()['inline_keyboard']
                label, emoji_id = icon
                rows[row_index][column].update(text=label, icon_custom_emoji_id=emoji_id)
                icons[row_index, column] = emoji_id
        if rows is None:
            return markup, icons
        return {'inline_keyboard': rows}, icons

    def prepare(self, endpoint, data, chat_type, preview):
        changed = {}
        field = _TEXT_FIELDS[endpoint]
        expected = {}
        if field and (preview or not self._block(chat_type, field)):
            entity_field = 'entities' if field == 'text' else 'caption_entities'
            if not data.get(entity_field):
                text, ids = self.render_text(data.get(field), data.get('parse_mode'))
                if ids:
                    changed.update({field: text, 'parse_mode': 'HTML'})
                    expected[field] = ids
        if 'reply_markup' in data and (preview or not self._block(chat_type, 'buttons')):
            markup, icons = self.render_markup(data['reply_markup'])
            if icons:
                changed['reply_markup'] = markup
                expected['buttons'] = icons
        return changed, expected

    @staticmethod
    def missing_features(result, expected):
        missing = []
        for field, expected_values in expected.items():
            if field == 'buttons':
                rows = (result.get('reply_markup') or {}).get('inline_keyboard', [])
                if any(row >= len(rows) or col >= len(rows[row])
                       or rows[row][col].get('icon_custom_emoji_id') != emoji_id
                       for (row, col), emoji_id in expected_values.items()):
                    missing.append(field)
            else:
                entities = result.get('entities' if field == 'text' else 'caption_entities') or []
                actual_ids = Counter(e.get('custom_emoji_id') for e in entities if e.get('type') == 'custom_emoji')
                if expected_values - actual_ids:
                    missing.append(field)
        return missing


class PremiumEmojiBot(ExtBot):
    """PTB 20.7 hook below handlers and above the existing rate limiter."""

    __slots__ = ('premium_emoji',)

    def __init__(self, *, premium_emoji, **kwargs):
        super().__init__(**kwargs)
        with self._unfrozen():
            self.premium_emoji = premium_emoji

    async def _do_post(self, endpoint, data, **timeouts):
        engine = self.premium_emoji
        send = super()._do_post
        preview_target = engine.preview_target.get()
        if not engine.ready or (not engine.enabled and preview_target is None):
            return await send(endpoint, data, **timeouts)
        if endpoint not in _TEXT_FIELDS and endpoint != 'getChat':
            return await send(endpoint, data, **timeouts)
        chat_id = engine.chat_id(data.get('chat_id'))
        chat_type = engine.chat_type(chat_id)
        preview = preview_target is not None and preview_target == chat_id
        if (endpoint in _TEXT_FIELDS and chat_type in _SUPPORTED_CHATS
                and (engine.enabled or preview)):
            changed, expected = engine.prepare(endpoint, data, chat_type, preview)
            if changed:
                return await self._post_emoji(send, endpoint, data, changed, expected, chat_type, timeouts)

        result = await send(endpoint, data, **timeouts)
        if (chat_id is None or chat_id < 0) and isinstance(result, dict):
            chat = result if endpoint == 'getChat' else result.get('chat') or {}
            engine.remember_chat(chat.get('id'), chat.get('type'))
        return result

    async def _post_emoji(self, send, endpoint, data, changed, expected, chat_type, timeouts):
        engine = self.premium_emoji
        enhanced = {**data, **changed}
        try:
            result = await send(endpoint, enhanced, **timeouts)
        except (BadRequest, Forbidden) as exc:
            # Telegram definitively rejected the operation. Timeouts, 429,
            # generic Forbidden, and network errors retain the caller's semantics.
            description = str(exc)
            bad_button = 'buttons' in expected and 'button_type_invalid' in description.lower()
            if not (bad_button or _REJECTION.search(description)):
                raise
            engine.fallback(chat_type, expected)
            return await send(endpoint, data, **timeouts)

        if not isinstance(result, dict) or not result.get('message_id'):
            return result
        missing = engine.missing_features(result, expected)
        if not missing:
            return result
        # Some permissions cause Telegram to accept a message but strip emoji.
        # Restore the original presentation in place without resending it.
        engine.fallback(chat_type, missing)
        field = _TEXT_FIELDS[endpoint]
        edit = {'chat_id': data['chat_id'], 'message_id': result['message_id']}
        edit_endpoint = 'editMessageReplyMarkup'
        markup_source = data if 'buttons' in missing else enhanced
        if 'reply_markup' in markup_source:
            edit['reply_markup'] = markup_source['reply_markup']
        if field in missing:
            edit_endpoint = 'editMessageText' if field == 'text' else 'editMessageCaption'
            edit[field] = data[field]
            for key in ('parse_mode', 'disable_web_page_preview', 'link_preview_options'):
                if key in data:
                    edit[key] = data[key]
        try:
            async with asyncio.timeout(1.5):
                edited = await send(edit_endpoint, edit, **timeouts)
        except (TelegramError, TimeoutError) as exc:
            # Delivery was acknowledged. Keep the original result and its ID
            # even if the presentation edit cannot be confirmed.
            logger.info('[PREMIUM EMOJI] Fallback edit unavailable: %s', type(exc).__name__)
            return result
        return edited if isinstance(edited, dict) and edited.get('message_id') else result
