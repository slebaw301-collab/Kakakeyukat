"""Mobile administration of buyer emoji functions and optional placement overrides."""

from html import escape
import logging
from uuid import uuid4

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

from premium_emoji import input_custom_emoji, preview_emoji
from premium_emoji_slots import LOCATIONS, ROLES, emoji_surface

logger = logging.getLogger(__name__)
STATE = 'awaiting_custom_emoji'
PAGE_SIZE = 9


class EmojiManager:
    def __init__(self, engine, save, is_admin, clear_state, button):
        self.engine = engine
        self.save = save
        self.is_admin = is_admin
        self.clear_state = clear_state
        self.button = button

    def _button(self, label, data):
        return self.button(label, callback_data=data)

    def _global(self, role):
        return self.engine().overrides.get(f'role/{role}')

    def _special(self, location):
        engine = self.engine()
        return engine.special_choice(location, *location.legacy_keys) if location.action != 'badge' else (
            engine.special_choice(location) or next(iter(engine.badge_legacy(location)), None)
        )

    def _affected(self, role):
        return tuple(loc.key for loc in LOCATIONS[role] if self._special(loc))

    def _icons(self, role, place=None, *, defaults=False):
        engine = self.engine()
        default = self._global(role) if not defaults else None
        locations = LOCATIONS[role] if place is None else (place,)
        icons = {}
        for location in locations:
            picked = self._special(location) if place is not None else None
            choice = picked or default or engine.default_choice(location.glyph)
            key = choice[0] if choice else location.glyph
            icons.setdefault(key, preview_emoji(choice, location.glyph))
            if picked and location.action == 'badge' and not engine.overrides.get(location.key):
                for inherited in engine.badge_legacy(location):
                    icons.setdefault(inherited[0], preview_emoji(inherited, location.glyph))
        return tuple(icons.values())

    def _page(self, entries, page, prefix):
        page = min(page, max(0, (len(entries) - 1) // PAGE_SIZE))
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        rows = [[button] for button in entries[start:end]]
        nav = []
        if start:
            nav.append(self._button('⬅️ Sebelumnya', f'{prefix}|{page - 1}'))
        if end < len(entries):
            nav.append(self._button('Berikutnya ➡️', f'{prefix}|{page + 1}'))
        if nav:
            rows.append(nav)
        return rows

    def _functions(self, mode, page=0, status=None):
        special = mode == 'x'
        entries = []
        for role in LOCATIONS:
            if special:
                label = ROLES[role]
            else:
                label = f'{self._icons(role)[0]} {ROLES[role]}'
            action = f'cem|l|{role}|0' if special else f'cem|e|{role}|{page}'
            entries.append(self._button(label, action))
        rows = self._page(entries, page, f'cem|{mode}')
        if special:
            rows.append([self._button('⬅️ Kembali', 'cem')])
        else:
            rows.extend([
                [self._button('Pengaturan Khusus', 'cem|x|0')],
                [self._button('Edit Emoji per Produk', 'admpanel_produk')],
                [self._button('⬅️ Kembali', 'admpanel_emoji')],
            ])
        title = 'Pengaturan Khusus' if special else 'Custom Emoji Manager'
        text = f'<b>{title}</b>\n\nPilih fungsi emoji.'
        return self._with_status(text, status), rows

    @staticmethod
    def _with_status(text, status):
        return f'{text}\n\n{status}' if status else text

    def _locations(self, role, page, status=None):
        entries = []
        for index, location in enumerate(LOCATIONS[role]):
            glyph = self._icons(role, location)[0]
            label = f'{glyph} {location.label}'
            if self._special(location):
                label += ' · Khusus'
            entries.append(self._button(label, f'cem|p|{role}|{index}'))
        rows = self._page(entries, page, f'cem|l|{role}')
        rows.append([self._button('⬅️ Kembali', 'cem|x|0')])
        return self._with_status(f'<b>Pengaturan Khusus · {escape(ROLES[role])}</b>', status), rows

    def _edit(self, role, page, index):
        location = LOCATIONS[role][index] if index is not None else None
        current = ' '.join(escape(icon) for icon in self._icons(role, location))
        text = f'<b>{escape(ROLES[role])}</b>\n\nSaat ini: {current}'
        if location:
            text += f'\n{escape(location.label)}'
            back = f'cem|l|{role}|{index // PAGE_SIZE}'
            reset = f'cem|d|{role}|{index}'
        else:
            affected = len(self._affected(role))
            if affected:
                text += f'\n{affected} lokasi memakai pengaturan khusus.'
            back = f'cem|g|{page}'
            reset = f'cem|r|{role}|{page}'
        text += '\n\nKirim satu emoji Premium dari keyboard Telegram.'
        rows = [
            [self._button('Reset ke Default', reset)],
            [self._button('Batal', back), self._button('⬅️ Kembali', back)],
        ]
        return text, rows

    def _next_view(self, role, page, index, status):
        if index is None:
            return self._functions('g', page, status)
        return self._locations(role, index // PAGE_SIZE, status)

    @staticmethod
    def _markup(rows):
        return InlineKeyboardMarkup(rows)

    async def _authorised(self, update, context):
        user = update.effective_user
        chat = update.effective_chat
        return bool(user and chat and chat.type == 'private' and await self.is_admin(user.id, context))

    async def _show(self, send, chat_id, view):
        text, rows = view
        engine = self.engine()
        token = engine.preview_target.set(chat_id)
        try:
            return await send(text, parse_mode='HTML', reply_markup=self._markup(rows))
        finally:
            engine.preview_target.reset(token)

    async def _save(self, role, index, choice, clear=(), revision=None):
        key = f'role/{role}' if index is None else LOCATIONS[role][index].key
        await self.save(key, choice, clear, revision=revision)

    @emoji_surface('manager')
    async def callback(self, update, context):
        query = update.callback_query
        if not await self._authorised(update, context):
            await query.answer('Buka pengaturan ini sebagai admin di chat pribadi bot.', show_alert=True)
            return
        pending = context.user_data.get(STATE)
        self.clear_state(context)
        args = query.data.split('|')[1:]
        view = None
        if len(args) == 2 and args[0] in ('g', 'x') and args[1].isdigit():
            view = self._functions(args[0], int(args[1]))
        elif len(args) == 3 and args[0] == 'a':
            _, mode, nonce = args
            if pending and pending.get('nonce') == nonce and mode in ('keep', 'clear'):
                role, page = pending['role'], pending['page']
                affected = pending['affected']
                if mode == 'clear' and (
                    affected != self._affected(role) or pending['revision'] != self.engine().revision
                ):
                    await query.answer('Pengaturan telah berubah. Pilih ulang emoji.', show_alert=True)
                    await self._show(query.edit_message_text, update.effective_chat.id, self._functions('g', page))
                    return
                else:
                    try:
                        await self._save(
                            role, None, pending['choice'], affected if mode == 'clear' else (),
                            revision=pending['revision'] if mode == 'clear' else None,
                        )
                    except Exception as exc:
                        logger.warning('Failed to save emoji function: %s', type(exc).__name__)
                        context.user_data[STATE] = pending
                        await query.answer('Emoji gagal disimpan. Coba lagi.', show_alert=True)
                        return
                    view = self._functions('g', page, self._confirmation(pending['choice']))
            else:
                await query.answer('Pilihan sudah tidak aktif.', show_alert=True)
                return
        elif len(args) == 3 and args[1] in LOCATIONS and args[2].isdigit():
            action, role, raw = args
            number = int(raw)
            if action == 'l':
                view = self._locations(role, number)
            elif action in ('e', 'p', 'r', 'd'):
                index = number if action in ('p', 'd') else None
                page = number if index is None else 0
                if index is not None and index >= len(LOCATIONS[role]):
                    await query.answer('Lokasi emoji tidak tersedia.', show_alert=True)
                    return
                if action in ('e', 'p'):
                    context.user_data[STATE] = {'role': role, 'page': page, 'index': index}
                    view = self._edit(role, page, index)
                else:
                    try:
                        await self._save(role, index, None)
                    except Exception as exc:
                        logger.warning('Failed to reset emoji selection: %s', type(exc).__name__)
                        await query.answer('Emoji gagal direset. Coba lagi.', show_alert=True)
                        return
                    status = '✅ Emoji dikembalikan ke Premium bawaan.' if index is None else (
                        '✅ Lokasi ini kembali mengikuti pengaturan global.'
                    )
                    if index is None and self._affected(role):
                        status += f' {len(self._affected(role))} lokasi masih memakai pengaturan khusus.'
                    view = self._next_view(role, page, index, status)
        await query.answer()
        try:
            await self._show(query.edit_message_text, update.effective_chat.id, view or self._functions('g'))
        except BadRequest as exc:
            if 'message is not modified' not in str(exc).lower():
                raise

    @staticmethod
    def _confirmation(choice):
        return f'✅ Emoji disimpan. {escape(preview_emoji(choice, choice[1]))}'

    @emoji_surface('manager')
    async def text_input(self, update, context):
        pending = context.user_data.get(STATE)
        if pending is None:
            return False
        message = update.effective_message
        if message is None or not await self._authorised(update, context):
            context.user_data.pop(STATE, None)
            return False
        if 'nonce' in pending:
            await message.reply_text('Pilih salah satu tombol konfirmasi atau Batal.')
            return True
        role, page, index = pending['role'], pending['page'], pending['index']
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
        affected = self._affected(role) if index is None else ()
        if affected:
            nonce = uuid4().hex[:12]
            context.user_data[STATE] = {
                **pending, 'choice': choice, 'affected': affected, 'nonce': nonce,
                'revision': self.engine().revision,
            }
            text = (
                f'<b>{escape(ROLES[role])}</b>\nEmoji baru: {escape(preview_emoji(choice, choice[1]))}\n\n'
                f'{len(affected)} lokasi memakai emoji khusus. '
                'Terapkan ke semua lokasi akan menghapus pengaturan khusus untuk fungsi ini.'
            )
            rows = [
                [self._button('Pertahankan Pengaturan Khusus', f'cem|a|keep|{nonce}')],
                [self._button('Konfirmasi Terapkan ke Semua Lokasi', f'cem|a|clear|{nonce}')],
                [self._button('Batal', f'cem|g|{page}')],
            ]
            await self._show(message.reply_text, update.effective_chat.id, (text, rows))
            return True
        try:
            await self._save(role, index, choice)
        except Exception as exc:
            logger.warning('Failed to save emoji selection: %s', type(exc).__name__)
            await message.reply_text('❌ Emoji gagal disimpan. Coba lagi.')
            return True
        context.user_data.pop(STATE, None)
        view = self._next_view(role, page, index, self._confirmation(choice))
        await self._show(message.reply_text, update.effective_chat.id, view)
        return True
