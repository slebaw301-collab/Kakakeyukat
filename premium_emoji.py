"""Render and send Hyper Store's verified Telegram custom emoji."""

import asyncio
from collections import Counter, OrderedDict
from copy import copy
from contextvars import ContextVar
from functools import lru_cache
from html import escape
import json
import logging
import re
from time import monotonic
from uuid import uuid4

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ExtBot

from premium_emoji_catalog import EMOJI_CATALOG
from premium_emoji_slots import button_role, glyph_key, surface

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
# Internal annotations identify a product or an exact slot without inserting
# custom HTML into the original payload used by OFF and automatic fallback.
_MARKER_PREFIX = '\ue000' + uuid4().hex
_MARKER_PATTERN = re.escape(_MARKER_PREFIX) + r'(?P<slot>[0-9a-f]+)\ue001(?P<original>[^\ue002]*)\ue002'
_MARKER = re.compile(_MARKER_PATTERN)
_RENDER_SYMBOL = re.compile(_MARKER_PATTERN + '|' + _SYMBOL.pattern)


def mark_emoji(key, original):
    return f'{_MARKER_PREFIX}{key.encode().hex()}\ue001{original}\ue002'


def _marked_key(match):
    return bytes.fromhex(match['slot']).decode()


def _plain_emoji(text):
    return _MARKER.sub(lambda match: match['original'], text) if _MARKER_PREFIX in text else text


def _emoji_tag(emoji_id, alt):
    return f'<tg-emoji emoji-id="{emoji_id}">{escape(alt)}</tg-emoji>'


async def input_custom_emoji(bot, message):
    """Return the actual Telegram ID and alt of one emoji sent by the admin."""
    entities = [entity for entity in message.entities if entity.type == 'custom_emoji']
    if not entities:
        return None
    if len(entities) != 1 or message.parse_entity(entities[0]) != message.text.strip():
        raise ValueError('Kirim tepat satu emoji Premium.')
    emoji_id = entities[0].custom_emoji_id
    if not emoji_id or not re.fullmatch(r'[0-9]{1,20}', emoji_id):
        raise ValueError('Emoji Premium tidak valid.')
    stickers = await bot.get_custom_emoji_stickers([emoji_id])
    for sticker in stickers:
        if sticker.custom_emoji_id == emoji_id and sticker.emoji:
            return emoji_id, sticker.emoji
    raise ValueError('Emoji Premium tidak ditemukan di Telegram.')


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
        self.overrides = {}
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
                symbol: _emoji_tag(emoji_id, returned[emoji_id])
                for symbol, emoji_id in self.ids.items()
            }
            self.ready = bool(returned)
            self.button_cache.cache_clear()
            self.revision += 1
            logger.info('[PREMIUM EMOJI] Catalog verified=%d/%d', len(returned), len(candidates))

    def load_overrides(self, values):
        overrides = {}
        for key, value in values.items():
            try:
                emoji_id, alt = json.loads(value)
                if not re.fullmatch(r'[0-9]{1,20}', emoji_id) or not isinstance(alt, str) or not alt:
                    continue
            except (ValueError, TypeError):
                continue
            overrides[key] = (emoji_id, alt, _emoji_tag(emoji_id, alt))
        self.overrides = overrides
        self.revision += 1

    def set_override(self, key, value):
        if value is None:
            self.overrides.pop(key, None)
        else:
            emoji_id, alt = value
            self.overrides[key] = (emoji_id, alt, _emoji_tag(emoji_id, alt))
        self.revision += 1

    def manual(self, screen, field, symbol):
        if not self.overrides or screen in ('preview', 'manager'):
            return None
        return (self.overrides.get(glyph_key(screen, field, symbol))
                or self.overrides.get(glyph_key('global', field, symbol)))

    def mark_product(self, product_id, original):
        if not self.enabled or not product_id or not self.overrides:
            return original
        key = f'product/{product_id}'
        return mark_emoji(key, str(original)) if key in self.overrides else original

    @staticmethod
    def clean_markup(markup):
        if not isinstance(markup, InlineKeyboardMarkup):
            return markup
        rows = None
        for row_index, row in enumerate(markup.inline_keyboard):
            for column, button in enumerate(row):
                label = _plain_emoji(button.text)
                if label == button.text:
                    continue
                if rows is None:
                    rows = [list(row) for row in markup.inline_keyboard]
                clean = copy(button)
                with clean._unfrozen():
                    clean.text = label
                rows[row_index][column] = clean
        return InlineKeyboardMarkup(rows) if rows is not None else markup

    @staticmethod
    def original_payload(data, field):
        value = data.get(field)
        text = _plain_emoji(value) if isinstance(value, str) else value
        markup = data.get('reply_markup')
        clean_markup = PremiumEmoji.clean_markup(markup) if 'reply_markup' in data else None
        if text is value and clean_markup is markup:
            return data
        original = dict(data)
        if text is not value:
            original[field] = text
        if clean_markup is not markup:
            original['reply_markup'] = clean_markup
        return original

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

    def _format_html(self, text, screen):
        """Replace visible emoji while preserving Telegram HTML and its attributes."""
        ids = Counter()
        parts = []
        skipped = []
        position = 0

        def replace(match):
            marked = match['slot']
            original = match['original'] if marked else match[0]
            symbol = _base(original)
            picked = (self.overrides.get(_marked_key(match)) if marked
                      else self.manual(screen, 'text', symbol))
            if picked:
                emoji_id, _, tag = picked
            else:
                tag = self.tags.get(symbol)
                if tag is None:
                    return original
                emoji_id = self.ids[symbol]
            ids[emoji_id] += 1
            return tag

        def visible(segment):
            position = 0
            pieces = []
            for url in _BARE_URL.finditer(segment):
                pieces.extend((_RENDER_SYMBOL.sub(replace, segment[position:url.start()]), url[0]))
                position = url.end()
            pieces.append(_RENDER_SYMBOL.sub(replace, segment[position:]))
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
        return _plain_emoji(''.join(parts)), ids

    def render_text(self, text, parse_mode, screen='other'):
        if not text or (_MARKER_PREFIX not in text and not _SYMBOL.search(text)):
            return text, None
        if parse_mode and parse_mode.upper() != 'HTML':
            return text, None
        value, ids = self._format_html(text if parse_mode else escape(text, quote=False), screen)
        return (value, ids) if ids else (text, None)

    def _button_icon(self, label, product_fallback=False):
        if product_fallback:
            prefix = _PRODUCT_EMOJI.match(label)
            emoji_id = self.ids.get('📦')
            return (label[prefix.end():], _base(label[:prefix.end()-1]), emoji_id) if prefix and emoji_id else None
        # Telegram has room for one custom icon before the label. Retain other
        # symbols, and keep labels consisting only of an emoji unchanged.
        for match in _SYMBOL.finditer(label):
            before, after = label[:match.start()], label[match.end():]
            if before.strip() and after.strip():
                continue
            remaining = after.lstrip() if not before.strip() else before.rstrip()
            emoji_id = self.ids.get(_base(match[0]))
            if any(character.isalnum() for character in remaining):
                return remaining, _base(match[0]), emoji_id
        return None

    def render_markup(self, markup, plain=None, screen='other'):
        icons = {}
        if not isinstance(markup, InlineKeyboardMarkup):
            return markup, icons
        if plain is None:
            plain = self.clean_markup(markup)
        rows = None
        for row_index, row in enumerate(markup.inline_keyboard):
            for column, button in enumerate(row):
                marked = _MARKER.search(button.text) if _MARKER_PREFIX in button.text else None
                exact = self.overrides.get(_marked_key(marked)) if marked else None
                label = plain.inline_keyboard[row_index][column].text
                if exact:
                    # Keep the rest of the label when replacing a marked badge.
                    before, after = button.text[:marked.start()], button.text[marked.end():]
                    label = _plain_emoji(before + after.removeprefix(' '))
                    emoji_id = exact[0]
                else:
                    key, role_glyph = button_role(screen, button, row_index) if self.overrides else (None, None)
                    picked = self.overrides.get(key)
                    if button.api_kwargs.get('icon_custom_emoji_id') and not picked:
                        continue
                    icon = self.button_cache(label)
                    callback = button.callback_data
                    if icon is None and isinstance(callback, str) and callback.startswith(_PRODUCT_CALLBACKS):
                        icon = self.button_cache(label, True)
                    if icon:
                        label, symbol, emoji_id = icon
                        picked = picked or self.manual(screen, 'button', symbol)
                    elif picked and role_glyph:
                        emoji_id = picked[0]
                    else:
                        continue
                    if picked:
                        emoji_id = picked[0]
                if not label or not emoji_id:
                    continue
                if rows is None:
                    rows = plain.to_dict()['inline_keyboard']
                rows[row_index][column].update(text=label, icon_custom_emoji_id=emoji_id)
                icons[row_index, column] = emoji_id
        if rows is None:
            return plain, icons
        return {'inline_keyboard': rows}, icons

    def prepare(self, endpoint, data, chat_type, preview, original=None):
        changed = {}
        field = _TEXT_FIELDS[endpoint]
        expected = {}
        screen = surface.get()
        if field and (preview or not self._block(chat_type, field)):
            entity_field = 'entities' if field == 'text' else 'caption_entities'
            if not data.get(entity_field):
                text, ids = self.render_text(data.get(field), data.get('parse_mode'), screen)
                if ids:
                    changed.update({field: text, 'parse_mode': 'HTML'})
                    expected[field] = ids
        if 'reply_markup' in data and (preview or not self._block(chat_type, 'buttons')):
            markup, icons = self.render_markup(data['reply_markup'], (original or data).get('reply_markup'), screen)
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
        if endpoint not in _TEXT_FIELDS and endpoint != 'getChat':
            return await send(endpoint, data, **timeouts)
        original = engine.original_payload(data, _TEXT_FIELDS.get(endpoint))
        if (not engine.ready and not engine.overrides) or (not engine.enabled and preview_target is None):
            return await send(endpoint, original, **timeouts)
        chat_id = engine.chat_id(data.get('chat_id'))
        chat_type = engine.chat_type(chat_id)
        preview = preview_target is not None and preview_target == chat_id
        if (endpoint in _TEXT_FIELDS and chat_type in _SUPPORTED_CHATS
                and (engine.enabled or preview)):
            changed, expected = engine.prepare(endpoint, data, chat_type, preview, original)
            if changed:
                return await self._post_emoji(send, endpoint, original, changed, expected, chat_type, timeouts)

        result = await send(endpoint, original, **timeouts)
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
