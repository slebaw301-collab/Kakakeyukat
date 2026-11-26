"""Render and send Hyper Store's verified Telegram custom emoji."""

import asyncio
from collections import Counter, OrderedDict
from copy import copy
from contextvars import ContextVar
from functools import lru_cache
from html import escape
import logging
import re
from time import monotonic
from uuid import uuid4

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ExtBot

from premium_emoji_catalog import EMOJI_CATALOG
from premium_emoji_slots import (
    PLAIN_SURFACES, PRODUCT_LOCATIONS, button_location, button_role, glyph_key, surface,
    decode_choice, legacy_screen, text_location,
)

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


def preview_emoji(choice, fallback):
    return mark_emoji(f'preview/{choice[0]}', choice[1]) if choice else fallback


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
        self.suppressed = set()
        self.badge_configured = False
        self.product_glyphs = {}
        self.chats = OrderedDict()
        self.blocked = {}
        self.rejected_ids = OrderedDict()
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
        suppressed = set()
        for key, value in values.items():
            if key.startswith('special/') and value == 'null':
                suppressed.add(key)
                continue
            choice = decode_choice(value)
            if choice:
                emoji_id, alt = choice
                overrides[key] = (emoji_id, alt, _emoji_tag(emoji_id, alt))
        self.overrides = overrides
        self.suppressed = suppressed
        self._update_badge_configured()
        self.revision += 1

    def set_override(self, key, value):
        self.suppressed.discard(key)
        if value is None:
            self.overrides.pop(key, None)
            if key.startswith('special/'):
                self.suppressed.add(key)
        else:
            emoji_id, alt = value
            self.overrides[key] = (emoji_id, alt, _emoji_tag(emoji_id, alt))
        self._update_badge_configured()
        self.revision += 1

    def manual(self, screen, field, symbol):
        if not self.overrides or screen in ('preview', 'manager') or not symbol:
            return None
        return (self.overrides.get(glyph_key(legacy_screen(screen), field, symbol))
                or self.overrides.get(glyph_key('global', field, symbol)))

    def special_choice(self, location, *legacy):
        if location.key in self.suppressed:
            return None
        exact = self.overrides.get(location.key)
        if exact:
            return exact
        return next((self.overrides[key] for key in legacy if key in self.overrides), None)

    def choice_at(self, location, *legacy):
        return (self.special_choice(location, *legacy)
                or self.overrides.get(f'role/{location.role}'))

    def badge_legacy(self, location):
        # A product's fallback glyph may differ from the catalog badge. Keep
        # each existing glyph-specific selection until this placement is reset.
        if location.key in self.suppressed:
            return ()
        screen, field = legacy_screen(location.screen), location.field
        results = {}
        for original in dict.fromkeys(self.product_glyphs.values()):
            if not original:
                continue
            symbol = _base(original)
            if field == 'button':
                label = f'{original} Produk'
                icon = self.button_cache(label) or self.button_cache(label, True)
                if not icon:
                    continue
                symbol = icon[1]
            elif symbol not in EMOJI_CATALOG:
                continue
            if choice := self.manual(screen, field, symbol):
                results[choice[0]] = choice
        return tuple(results.values())

    def default_choice(self, glyph):
        glyph = _base(glyph)
        return EMOJI_CATALOG.get(glyph)

    def marked_preview(self, match):
        key = _marked_key(match)
        if key.startswith('preview/'):
            emoji_id = key[8:]
            alt = match['original']
            return emoji_id, alt, _emoji_tag(emoji_id, alt)
        return None

    def mark_product(self, product_id, original):
        if not self.enabled or not product_id or surface.get() in PLAIN_SURFACES:
            return original
        key = f'product/{product_id}'
        if key in self.overrides or self.badge_configured:
            return mark_emoji(key, str(original))
        return original

    def _update_badge_configured(self):
        self.badge_configured = ('role/package' in self.overrides or any(
            key.startswith('special/') and key.endswith('/package/badge') for key in self.overrides
        ))

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

    def blocked_ids(self, chat_type, feature, preview):
        if preview or not self.rejected_ids:
            return ()
        now = monotonic()
        return {emoji_id for (scope, field, emoji_id), until in self.rejected_ids.items()
                if scope == chat_type and field == feature and until > now}

    def reject_ids(self, chat_type, feature, ids):
        until = monotonic() + COOLDOWN
        for emoji_id in ids:
            key = chat_type, feature, emoji_id
            self.rejected_ids[key] = until
            self.rejected_ids.move_to_end(key)
        while len(self.rejected_ids) > 128:
            self.rejected_ids.popitem(last=False)
        self.revision += 1

    def record_preview(self, verified):
        self.preview_verified_at = monotonic() if verified else None
        if verified:
            for feature in ('text', 'caption', 'buttons'):
                if self.blocked.pop(('private', feature), None):
                    self.revision += 1

    def preview_allowed(self):
        return (self.preview_verified_at is not None
                and monotonic() - self.preview_verified_at < PREVIEW_VALIDITY)

    def _format_html(self, text, screen, blocked):
        """Replace visible emoji while preserving Telegram HTML and its attributes."""
        ids = Counter()
        parts = []
        skipped = []
        position = 0

        def replace(match, start):
            marked = match['slot']
            original = match['original'] if marked else match[0]
            symbol = _base(original)
            if screen == 'manager':
                picked = self.marked_preview(match) if marked else None
                if not picked:
                    return original
            elif marked:
                key = _marked_key(match)
                picked = self.overrides.get(key)
                if not picked and key.startswith('product/'):
                    location = PRODUCT_LOCATIONS.get((screen, 'text'))
                    if location and symbol:
                        picked = self.choice_at(location, glyph_key(legacy_screen(screen), 'text', symbol),
                                                glyph_key('global', 'text', symbol))
                    else:
                        picked = self.manual(screen, 'text', symbol)
            elif self.overrides or self.suppressed:
                following = start + match.end()
                location = text_location(screen, symbol, text, following)
                picked = (self.choice_at(location, *location.legacy_keys) if location
                          else self.manual(screen, 'text', symbol))
            else:
                picked = None
            if picked:
                emoji_id, _, tag = picked
            else:
                tag = self.tags.get(symbol)
                if tag is None:
                    return original
                emoji_id = self.ids[symbol]
            if emoji_id in blocked:
                return original
            ids[emoji_id] += 1
            return tag

        def visible(segment, start):
            position = 0
            pieces = []
            for url in _BARE_URL.finditer(segment):
                piece = _RENDER_SYMBOL.sub(lambda m: replace(m, start + position), segment[position:url.start()])
                pieces.extend((piece, url[0]))
                position = url.end()
            pieces.append(_RENDER_SYMBOL.sub(lambda m: replace(m, start + position), segment[position:]))
            return ''.join(pieces)

        for tag in _HTML_TAG.finditer(text):
            segment = text[position:tag.start()]
            parts.append(segment if skipped else visible(segment, position))
            parts.append(tag[0])
            name = tag[2].lower()
            if name in _SKIP_TAGS:
                if not tag[1]:
                    skipped.append(name)
                elif skipped and skipped[-1] == name:
                    skipped.pop()
            position = tag.end()
        tail = text[position:]
        parts.append(tail if skipped else visible(tail, position))
        return _plain_emoji(''.join(parts)), ids

    def render_text(self, text, parse_mode, screen='other', blocked=()):
        if not text or (_MARKER_PREFIX not in text and not _SYMBOL.search(text)):
            return text, None
        if parse_mode and parse_mode.upper() != 'HTML':
            return text, None
        value, ids = self._format_html(text if parse_mode else escape(text, quote=False), screen, blocked)
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

    def render_markup(self, markup, plain=None, screen='other', blocked=()):
        icons = {}
        if not isinstance(markup, InlineKeyboardMarkup):
            return markup, icons
        if plain is None:
            plain = self.clean_markup(markup)
        rows = None
        for row_index, row in enumerate(markup.inline_keyboard):
            for column, button in enumerate(row):
                marked = _MARKER.search(button.text) if _MARKER_PREFIX in button.text else None
                marked_key = _marked_key(marked) if marked else ''
                label = plain.inline_keyboard[row_index][column].text
                if screen == 'manager':
                    exact = self.marked_preview(marked) if marked else None
                    if not exact:
                        continue
                else:
                    exact = self.overrides.get(marked_key) if marked else None
                if exact:
                    # Keep the rest of the label when replacing a marked badge.
                    before, after = button.text[:marked.start()], button.text[marked.end():]
                    label = _plain_emoji(before + after.removeprefix(' '))
                    emoji_id = exact[0]
                else:
                    key, role_glyph = button_role(screen, button, row_index)
                    icon = self.button_cache(label)
                    callback = button.callback_data
                    product = isinstance(callback, str) and callback.startswith(_PRODUCT_CALLBACKS)
                    if icon is None and product:
                        icon = self.button_cache(label, True)
                    bare_label, symbol, emoji_id = icon or (label, role_glyph, None)
                    location = (PRODUCT_LOCATIONS.get((screen, 'button')) if product else
                                button_location(screen, bare_label, symbol, row_index, button, key))
                    if location and product:
                        picked = self.choice_at(location, glyph_key(legacy_screen(screen), 'button', symbol),
                                                glyph_key('global', 'button', symbol)) if symbol else None
                    elif location:
                        picked = self.choice_at(location, *location.legacy_keys)
                        # A custom group label can use a different Unicode glyph.
                        if location.role == 'group' and symbol != location.glyph:
                            legacy_choice = self.overrides.get(key) or (self.manual(screen, 'button', symbol) if symbol else None)
                            picked = self.special_choice(location) or legacy_choice or self.overrides.get('role/group')
                    else:
                        picked = self.overrides.get(key) or (self.manual(screen, 'button', symbol) if symbol else None)
                    if button.api_kwargs.get('icon_custom_emoji_id') and not picked:
                        continue
                    if not icon and not picked:
                        continue
                    label = bare_label
                    if picked:
                        emoji_id = picked[0]
                if not label or not emoji_id or emoji_id in blocked:
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
                text, ids = self.render_text(data.get(field), data.get('parse_mode'), screen,
                                             self.blocked_ids(chat_type, field, preview))
                if ids:
                    changed.update({field: text, 'parse_mode': 'HTML'})
                    expected[field] = ids
        if 'reply_markup' in data and (preview or not self._block(chat_type, 'buttons')):
            markup, icons = self.render_markup(data['reply_markup'], (original or data).get('reply_markup'), screen,
                                               self.blocked_ids(chat_type, 'buttons', preview))
            if icons:
                changed['reply_markup'] = markup
                expected['buttons'] = icons
        return changed, expected

    @staticmethod
    def missing_features(result, expected):
        missing = {}
        for field, expected_values in expected.items():
            if field == 'buttons':
                rows = (result.get('reply_markup') or {}).get('inline_keyboard', [])
                rejected = {emoji_id for (row, col), emoji_id in expected_values.items()
                            if row >= len(rows) or col >= len(rows[row])
                            or rows[row][col].get('icon_custom_emoji_id') != emoji_id}
            else:
                entities = result.get('entities' if field == 'text' else 'caption_entities') or []
                actual_ids = Counter(e.get('custom_emoji_id') for e in entities if e.get('type') == 'custom_emoji')
                rejected = set(expected_values - actual_ids)
            if rejected:
                missing[field] = rejected
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
        if preview_target is None and (not engine.enabled or (not engine.ready and not engine.overrides)):
            return await send(endpoint, original, **timeouts)
        chat_id = engine.chat_id(data.get('chat_id'))
        chat_type = engine.chat_type(chat_id)
        preview = preview_target is not None and preview_target == chat_id
        if endpoint in _TEXT_FIELDS and surface.get() in PLAIN_SURFACES and not preview:
            return await send(endpoint, original, **timeouts)
        if (endpoint in _TEXT_FIELDS and chat_type in _SUPPORTED_CHATS
                and (preview or (engine.enabled and surface.get() != 'manager'))):
            changed, expected = engine.prepare(endpoint, data, chat_type, preview, original)
            if changed:
                return await self._post_emoji(send, endpoint, data, original, changed, expected, chat_type, preview, timeouts)

        result = await send(endpoint, original, **timeouts)
        if (chat_id is None or chat_id < 0) and isinstance(result, dict):
            chat = result if endpoint == 'getChat' else result.get('chat') or {}
            engine.remember_chat(chat.get('id'), chat.get('type'))
        return result

    async def _post_emoji(self, send, endpoint, source, data, changed, expected, chat_type, preview, timeouts):
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
            if not preview:
                named = set(re.findall(r'(?<!\d)\d{8,20}(?!\d)', description))
                for feature, values in expected.items():
                    candidate_ids = set(values.values() if feature == 'buttons' else values)
                    failed = candidate_ids & named
                    if failed or len(candidate_ids) == 1:
                        engine.reject_ids(chat_type, feature, failed or candidate_ids)
                    elif not named:
                        engine.fallback(chat_type, (feature,))
            return await send(endpoint, data, **timeouts)

        if not isinstance(result, dict) or not result.get('message_id'):
            return result
        missing = engine.missing_features(result, expected)
        if not missing:
            return result
        # Some permissions cause Telegram to accept a message but strip emoji.
        # Restore the original presentation in place without resending it.
        repaired = data
        if not preview:
            for feature, rejected in missing.items():
                feature_values = expected[feature]
                ids = set(feature_values.values() if feature == 'buttons' else feature_values)
                if rejected == ids and len(ids) > 1:
                    engine.fallback(chat_type, (feature,))
                else:
                    engine.reject_ids(chat_type, feature, rejected)
            replacement, _ = engine.prepare(endpoint, source, chat_type, False, data)
            repaired = {**data, **replacement}
        field = _TEXT_FIELDS[endpoint]
        edit = {'chat_id': data['chat_id'], 'message_id': result['message_id']}
        edit_endpoint = 'editMessageReplyMarkup'
        markup_source = repaired if 'buttons' in missing else enhanced
        if 'reply_markup' in markup_source:
            edit['reply_markup'] = markup_source['reply_markup']
        if field in missing:
            edit_endpoint = 'editMessageText' if field == 'text' else 'editMessageCaption'
            edit[field] = repaired[field]
            for key in ('parse_mode', 'disable_web_page_preview', 'link_preview_options'):
                if key in repaired:
                    edit[key] = repaired[key]
        try:
            async with asyncio.timeout(1.5):
                edited = await send(edit_endpoint, edit, **timeouts)
        except (TelegramError, TimeoutError) as exc:
            # Delivery was acknowledged. Keep the original result and its ID
            # even if the presentation edit cannot be confirmed.
            logger.info('[PREMIUM EMOJI] Fallback edit unavailable: %s', type(exc).__name__)
            return result
        return edited if isinstance(edited, dict) and edited.get('message_id') else result
