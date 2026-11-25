"""Admin navigation for contextual Premium emoji settings."""

from dataclasses import dataclass
from html import escape
import logging

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

from premium_emoji import input_custom_emoji, mark_emoji
from premium_emoji_catalog import EMOJI_CATALOG
from premium_emoji_slots import BUTTONS, CATEGORIES, button_key, emoji_surface, glyph_key

logger = logging.getLogger(__name__)
STATE = 'awaiting_custom_emoji'
PAGE_SIZE = 10
SCREENS = {screen: (category, name, glyphs) for category, (_, entries) in CATEGORIES.items()
           for screen, (name, glyphs) in entries.items()}


@dataclass(frozen=True)
class Slot:
    key: str
    glyph: str
    label: str
    field: str

    @property
    def token(self):
        _, kind, value = self.key.split('/', 2)
        return ('a_' if kind == 'action' else 'g_') + value


def slots_for(screen, tab):
    _, _, glyphs = SCREENS[screen]
    if screen in ('global', 'other') or tab.endswith('a'):
        glyphs = [*glyphs, *EMOJI_CATALOG]
    field = 'text' if tab.startswith('t') else 'button'
    buttons = [
        Slot(button_key(screen, role), glyph, label, field)
        for role, label, glyph, _ in BUTTONS.get(screen, ())
    ] if field == 'button' else []
    return buttons + [
        Slot(glyph_key(screen, field, glyph), glyph, glyph, field)
        for glyph in dict.fromkeys(glyphs) if glyph in EMOJI_CATALOG
    ]


class EmojiManager:
    def __init__(self, engine, save, is_admin, clear_state, button):
        self.engine = engine
        self.save = save
        self.is_admin = is_admin
        self.clear_state = clear_state
        self.button = button

    def _target(self, slot):
        overrides = self.engine().overrides
        if slot.key in overrides:
            return slot.key, 'Manual'
        screen = slot.key.split('/', 1)[0]
        for key in (glyph_key(screen, slot.field, slot.glyph), glyph_key('global', slot.field, slot.glyph)):
            if key in overrides:
                return key, 'Mengikuti mapping'
        return slot.key, 'Default'

    def _current(self, slot):
        key, source = self._target(slot)
        engine = self.engine()
        choice = engine.overrides.get(key)
        if choice and not engine.enabled:
            return choice[1], source
        return mark_emoji(key, slot.glyph), source

    def _root(self):
        rows = [[self.button(name, callback_data=f'cem|c|{category}')]
                for category, (name, _) in CATEGORIES.items()]
        rows.append([self.button('⬅️ Kembali', callback_data='admpanel_emoji')])
        return '<b>Custom Emoji Manager</b>\n\nPilih kategori.', rows

    def _category(self, category):
        name, screens = CATEGORIES[category]
        rows = [[self.button(label, callback_data=f'cem|s|{screen}|t|0')]
                for screen, (label, _) in screens.items()]
        if category == 'catalog':
            rows.append([self.button('Edit Emoji per Produk', callback_data='admpanel_produk')])
        rows.append([self.button('⬅️ Kembali', callback_data='cem')])
        return f'<b>{escape(name)}</b>\n\nPilih bagian.', rows

    def _list(self, screen, tab, page, status=None):
        category, name, _ = SCREENS[screen]
        slots = slots_for(screen, tab)
        end = min(len(slots), (page + 1) * PAGE_SIZE)
        start = page * PAGE_SIZE
        kind = 'Pesan' if tab.startswith('t') else 'Tombol'
        text = f'<b>{escape(name)}</b>\n{kind} · Halaman {page + 1}/{max(1, (len(slots) + PAGE_SIZE - 1) // PAGE_SIZE)}'
        if status:
            text += f'\n\n{status}'
        rows = []
        for index in range(start, end):
            slot = slots[index]
            current, source = self._current(slot)
            label = slot.label if slot.label != slot.glyph else f'{slot.label} · {source}'
            rows.append([self.button(f'{current} {label}', callback_data=f'cem|e|{screen}|{tab}|{slot.token}')])
        nav = []
        if page:
            nav.append(self.button('⬅️ Sebelumnya', callback_data=f'cem|s|{screen}|{tab}|{page - 1}'))
        if end < len(slots):
            nav.append(self.button('Berikutnya ➡️', callback_data=f'cem|s|{screen}|{tab}|{page + 1}'))
        if nav:
            rows.append(nav)
        other_tab, other_label = ('b', 'Tombol') if tab.startswith('t') else ('t', 'Pesan')
        rows.append([self.button(f'Lihat {other_label}', callback_data=f'cem|s|{screen}|{other_tab}|0')])
        if screen not in ('global', 'other'):
            all_tab, label = (tab[0], 'Emoji bagian ini') if tab.endswith('a') else (tab + 'a', 'Semua emoji')
            rows.append([self.button(label, callback_data=f'cem|s|{screen}|{all_tab}|0')])
        rows.append([self.button('⬅️ Kembali', callback_data=f'cem|c|{category}')])
        return text, rows

    def _edit(self, screen, tab, index, slot):
        current, source = self._current(slot)
        current = escape(current)
        text = (
            f'<b>Ubah emoji {escape(slot.label)}</b>\n\nSaat ini: {current} · {source}\n'
            'Kirim satu emoji Premium dari keyboard Telegram.'
        )
        rows = []
        if slot.key in self.engine().overrides:
            rows.append([self.button('Reset ke default', callback_data=f'cem|r|{screen}|{tab}|{slot.token}')])
        rows.append([self.button('❌ Batal', callback_data=f'cem|s|{screen}|{tab}|{index // PAGE_SIZE}')])
        return text, rows

    @emoji_surface('manager')
    async def callback(self, update, context):
        query = update.callback_query
        if update.effective_chat is None or update.effective_chat.type != 'private':
            await query.answer('Buka pengaturan ini di chat pribadi bot.', show_alert=True)
            return
        self.clear_state(context)
        args = query.data.split('|')[1:]
        if not args:
            view = self._root()
        elif len(args) == 2 and args[0] == 'c' and args[1] in CATEGORIES:
            view = self._category(args[1])
        elif len(args) == 4 and args[1] in SCREENS and args[2] in ('t', 'b', 'ta', 'ba'):
            action, screen, tab, token = args
            slots = slots_for(screen, tab)
            index = next((i for i, slot in enumerate(slots) if slot.token == token), None)
            if action == 's' and token.isdigit() and int(token) <= max(0, (len(slots) - 1) // PAGE_SIZE):
                view = self._list(screen, tab, int(token))
            elif action in ('e', 'r') and index is not None:
                slot = slots[index]
                if action == 'e':
                    context.user_data[STATE] = (screen, tab, index)
                    view = self._edit(screen, tab, index, slot)
                else:
                    try:
                        await self.save(slot.key, None)
                    except Exception as exc:
                        logger.warning('Failed to reset emoji slot: %s', type(exc).__name__)
                        await query.answer('Emoji gagal direset. Coba lagi.', show_alert=True)
                        return
                    view = self._list(screen, tab, index // PAGE_SIZE, '✅ Emoji dikembalikan ke default.')
            else:
                view = self._root()
        else:
            view = self._root()
        await query.answer()
        text, rows = view
        try:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(rows))
        except BadRequest as exc:
            if 'message is not modified' not in str(exc).lower():
                raise

    @emoji_surface('manager')
    async def text_input(self, update, context):
        pending = context.user_data.get(STATE)
        if pending is None:
            return False
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None or chat.type != 'private' or not await self.is_admin(user.id, context):
            context.user_data.pop(STATE, None)
            return False
        screen, tab, index = pending
        slot = slots_for(screen, tab)[index]
        try:
            choice = await input_custom_emoji(context.bot, message)
        except ValueError as exc:
            await message.reply_text(f'❌ {exc}')
            return True
        except TelegramError:
            await message.reply_text('❌ Telegram gagal memvalidasi emoji. Coba lagi.')
            return True
        if choice is None:
            await message.reply_text('❌ Kirim satu emoji Premium dari keyboard Telegram.')
            return True
        try:
            await self.save(slot.key, choice)
        except Exception as exc:
            logger.warning('Failed to save emoji slot: %s', type(exc).__name__)
            await message.reply_text('❌ Emoji gagal disimpan. Coba lagi.')
            return True
        context.user_data.pop(STATE, None)
        current, _ = self._current(slot)
        text, rows = self._list(screen, tab, index // PAGE_SIZE, f'✅ Emoji disimpan. {escape(current)}')
        await message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(rows))
        return True
