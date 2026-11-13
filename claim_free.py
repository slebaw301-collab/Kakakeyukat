"""Isolated Claim Free feature. No purchase/order tables are modified."""
import asyncio
import html
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import TelegramError

log = logging.getLogger(__name__)
LINE = '━━━━━━━━━━━━━━━━━━━━━━'
UNAVAILABLE = '🎁 Claim Grup Free belum tersedia. Silakan coba kembali nanti.'
INTRO = (
    f'🎁 <b>CLAIM GRUP FREE</b>\n{LINE}\n\n'
    'Dapatkan akses <b>GRUP FREE</b> secara gratis dengan menyelesaikan persyaratan berikut.\n\n'
    '📋 <b>SYARAT CLAIM</b>\n✅ Bergabung ke Grup Testimoni Hyper Store.\n\n'
    f'{LINE}\n\n📌 <b>CARA CLAIM</b>\n\n'
    '1. Tekan tombol <b>Gabung Grup Testimoni</b>.\n'
    '2. Bergabung ke grup tersebut.\n'
    '3. Kembali ke bot dan tekan <b>Verifikasi &amp; Claim</b>.\n\n'
    f'{LINE}\n🔒 Akses hanya diberikan kepada member yang telah memenuhi persyaratan.'
)
FAILED = (
    f'❌ <b>VERIFIKASI GAGAL</b>\n{LINE}\n\n'
    'Kamu belum bergabung ke Grup Testimoni Hyper Store.\n\n'
    'Silakan bergabung terlebih dahulu, kemudian tekan <b>Verifikasi &amp; Claim</b> kembali.'
)
SUCCESS = (
    f'✅ <b>CLAIM BERHASIL!</b>\n{LINE}\n\n'
    'Selamat! Persyaratan telah berhasil diverifikasi.\n\n'
    '🎁 <b>Akses:</b> GRUP FREE\n✅ <b>Status:</b> Berhasil\n\n'
    f'{LINE}\n\nTekan tombol di bawah untuk bergabung ke grup gratis.'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_free_config (
 id SMALLINT PRIMARY KEY CHECK (id=1), enabled BOOLEAN NOT NULL DEFAULT FALSE,
 testi_chat BIGINT, testi_link TEXT, free_chat BIGINT,
 version BIGINT NOT NULL DEFAULT 0
);
INSERT INTO claim_free_config(id) VALUES(1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS claim_free_claims (
 user_id BIGINT PRIMARY KEY, claimed_at TIMESTAMPTZ,
 lease_token TEXT, lease_until TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS claim_free_invites (
 invite_link TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES claim_free_claims(user_id),
 chat_id BIGINT NOT NULL, config_version BIGINT NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claim_free_invites_user_idx ON claim_free_invites(user_id);
"""


def member_ok(member):
    return member.status in ('creator', 'administrator', 'member') or (
        member.status == 'restricted' and member.is_member is True)


def parse_chat(value):
    if not re.fullmatch(r'-[1-9][0-9]{1,18}', value.strip()):
        raise ValueError('Chat ID harus berupa angka negatif, contoh format: -100…')
    result = int(value)
    if result < -(2**63):
        raise ValueError('Chat ID tidak valid.')
    return result


def parse_link(value):
    p = urlparse(value.strip())
    if (p.scheme != 'https' or p.netloc != 't.me' or p.query or p.fragment
            or not re.fullmatch(r'/(?:[A-Za-z][A-Za-z0-9_]{3,31}|\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+)', p.path)):
        raise ValueError('Gunakan link grup Telegram https://t.me/… tanpa parameter tambahan.')
    return value.strip()


class ClaimFree:
    def __init__(self, session, run_db, is_admin, is_banned):
        self.session, self.run_db = session, run_db
        self.is_admin, self.is_banned = is_admin, is_banned
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.config_lock = asyncio.Lock()

    def migrate(self):
        with self.session() as conn:
            with conn:  # Single atomic, additive migration even with host autocommit.
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout='5s'")
                    cur.execute(SCHEMA)

    def db(self, action, *args):
        with self.session() as conn:
            with conn:
                with conn.cursor() as c:
                    if action == 'config':
                        c.execute('SELECT * FROM claim_free_config WHERE id=1')
                        return dict(c.fetchone())
                    if action == 'save':
                        testi, link, free, version = args
                        c.execute('UPDATE claim_free_config SET testi_chat=%s,testi_link=%s,free_chat=%s,enabled=FALSE,version=version+1 WHERE id=1 AND version=%s RETURNING id', (testi, link, free, version))
                        return bool(c.fetchone())
                    if action == 'toggle':
                        enabled, version = args
                        c.execute('UPDATE claim_free_config SET enabled=%s,version=version+1 WHERE id=1 AND version=%s RETURNING id', (enabled, version))
                        return bool(c.fetchone())
                    if action == 'reserve':
                        uid, token = args
                        c.execute("""INSERT INTO claim_free_claims(user_id,lease_token,lease_until)
                            VALUES(%s,%s,now()+interval '90 seconds')
                            ON CONFLICT(user_id) DO UPDATE SET lease_token=EXCLUDED.lease_token,
                            lease_until=EXCLUDED.lease_until
                            WHERE claim_free_claims.lease_until IS NULL OR claim_free_claims.lease_until<now()
                            RETURNING user_id""", (uid, token))
                        return bool(c.fetchone())
                    if action == 'release':
                        c.execute('UPDATE claim_free_claims SET lease_token=NULL,lease_until=NULL WHERE user_id=%s AND lease_token=%s', args)
                    if action == 'latest':
                        c.execute('''SELECT invite_link FROM claim_free_invites WHERE user_id=%s
                            AND config_version=%s AND expires_at>now()+interval '30 seconds'
                            AND consumed_at IS NULL ORDER BY created_at DESC LIMIT 1''', args)
                        row = c.fetchone()
                        return row['invite_link'] if row else None
                    if action == 'consume':
                        c.execute('UPDATE claim_free_invites SET consumed_at=now() WHERE invite_link=%s', args)
                    if action == 'finish':
                        uid, token, link, chat, version, expires = args
                        c.execute('SELECT version,enabled FROM claim_free_config WHERE id=1 FOR SHARE')
                        cfg = c.fetchone()
                        if not cfg['enabled'] or cfg['version'] != version:
                            return False
                        c.execute('''UPDATE claim_free_claims SET claimed_at=COALESCE(claimed_at,now()),
                            lease_token=NULL,lease_until=NULL WHERE user_id=%s AND lease_token=%s
                            AND lease_until>now() RETURNING user_id''', (uid, token))
                        if not c.fetchone():
                            return False
                        c.execute('''INSERT INTO claim_free_invites(invite_link,user_id,chat_id,config_version,expires_at)
                            VALUES(%s,%s,%s,%s,%s)''', (link, uid, chat, version, expires))
                        return True
                    if action == 'invite':
                        c.execute('''SELECT i.*, (i.expires_at>now() AND consumed_at IS NULL) AS valid FROM claim_free_invites i
                            JOIN claim_free_claims c USING(user_id) WHERE invite_link=%s AND claimed_at IS NOT NULL''', args)
                        r = c.fetchone()
                        return dict(r) if r else None
                    if action == 'stats':
                        c.execute("""SELECT count(*) AS total,
                            count(*) FILTER (WHERE (claimed_at AT TIME ZONE 'Asia/Jakarta')::date=
                                (now() AT TIME ZONE 'Asia/Jakarta')::date) AS today,
                            count(DISTINCT user_id) AS unique_users
                            FROM claim_free_claims WHERE claimed_at IS NOT NULL""")
                        return dict(c.fetchone())

    async def call(self, action, *args):
        return await self.run_db(self.db, action, *args)

    async def config(self):
        return await self.call('config')

    async def permissions(self, bot, cfg):
        if not all(cfg.get(k) for k in ('testi_chat', 'testi_link', 'free_chat')):
            raise ValueError('Lengkapi kedua grup terlebih dahulu.')
        if cfg['testi_chat'] == cfg['free_chat']:
            raise ValueError('Grup Testimoni dan Grup Free harus berbeda.')
        await self.validate_chat(bot, cfg['testi_chat'], cfg['testi_link'])
        await self.validate_chat(bot, cfg['free_chat'], free=True)

    async def validate_chat(self, bot, chat_id, link=None, free=False):
        chat, me = await asyncio.gather(bot.get_chat(chat_id), bot.get_chat_member(chat_id, bot.id))
        if chat.type not in ('group', 'supergroup'):
            raise ValueError('Chat tujuan harus berupa grup atau supergroup.')
        if me.status not in ('creator', 'administrator'):
            raise ValueError('Jadikan bot admin di grup terlebih dahulu.')
        if free and not (me.status == 'creator' or me.can_invite_users):
            raise ValueError('Bot memerlukan izin Undang Pengguna di Grup Free.')
        if free and chat.username:
            raise ValueError('Grup Free harus privat agar akses hanya melalui verifikasi bot.')
        if link:
            public = 'https://t.me/' + chat.username if chat.username else None
            primary = getattr(chat, 'invite_link', None)
            def normalized(s):
                return s.replace('/joinchat/', '/+') if s else s
            if not ((public and link.lower() == public.lower()) or normalized(link) == normalized(primary)):
                raise ValueError('Link tidak cocok dengan grup. Gunakan link publik grup atau link undangan utama yang terbaca oleh bot.')

    def keyboard(self, cfg):
        return Markup([[Button('📢 Gabung Grup Testimoni', url=cfg['testi_link'])],
                       [Button('✅ Verifikasi & Claim', callback_data='cf_verify')]])

    async def reply(self, update, text, markup=None):
        # Dedicated messages; never edits the normal catalog or purchased access.
        await update.effective_message.reply_text(text, parse_mode='HTML', reply_markup=markup,
                                                  protect_content=True, disable_web_page_preview=True)

    async def show(self, update, context):
        if update.effective_chat.type != 'private':
            return
        cfg = await self.config()
        if await self.is_banned(update.effective_user.id) or not cfg['enabled']:
            return await self.reply(update, UNAVAILABLE)
        await self.reply(update, INTRO, self.keyboard(cfg))

    async def verified_member(self, bot, chat, uid):
        me, member = await asyncio.gather(bot.get_chat_member(chat, bot.id), bot.get_chat_member(chat, uid))
        if me.status not in ('administrator', 'creator'):
            raise ValueError('Bot bukan admin Grup Testimoni.')
        return member_ok(member)

    async def verify(self, update, context):
        uid = update.effective_user.id
        if await self.is_banned(uid):
            return await self.reply(update, UNAVAILABLE)
        lock = self.locks[uid % len(self.locks)]
        if lock.locked():
            return await self.reply(update, '⏳ Claim sedang diproses. Tunggu sebentar, lalu coba kembali.')
        async with lock:
            cfg = await self.config()
            if not cfg['enabled']:
                return await self.reply(update, UNAVAILABLE)
            token, link = uuid.uuid4().hex, None
            reserved = False
            try:
                if not await self.verified_member(context.bot, cfg['testi_chat'], uid):
                    return await self.reply(update, FAILED, self.keyboard(cfg))
                reserved = await self.call('reserve', uid, token)
                if not reserved:
                    return await self.reply(update, '⏳ Claim sedang diproses. Coba kembali sebentar lagi.')
                cached = await self.call('latest', uid, cfg['version'])
                if cached:
                    return await self.reply(update, SUCCESS, Markup([[Button('🔗 Masuk Grup Free', url=cached)]]))
                expires = datetime.now(timezone.utc) + timedelta(minutes=15)
                invitation = await context.bot.create_chat_invite_link(
                    chat_id=cfg['free_chat'], name='hf-free-' + token[:16],
                    expire_date=expires, creates_join_request=True)
                link = invitation.invite_link
                if not await self.call('finish', uid, token, link, cfg['free_chat'], cfg['version'], expires):
                    raise ValueError('Konfigurasi berubah saat claim diproses. Silakan ulangi.')
                await self.reply(update, SUCCESS, Markup([[Button('🔗 Masuk Grup Free', url=link)]]))
            except Exception as exc:
                # Only type is logged: Telegram exceptions may include private links.
                log.warning('[CLAIM FREE] verify failed: %s', type(exc).__name__)
                if link:
                    await self.revoke(context.bot, cfg['free_chat'], link)
                await self.reply(update, '❌ Claim belum dapat diselesaikan. Pastikan bot masih admin dan coba lagi. Hak claim tetap dapat dipulihkan.', self.keyboard(cfg))
            finally:
                if reserved:
                    await self.call('release', uid, token)

    async def revoke(self, bot, chat, link):
        try:
            await bot.revoke_chat_invite_link(chat, link)
        except TelegramError:
            log.warning('[CLAIM FREE] revoke failed')

    async def join_request(self, update, context):
        req = update.chat_join_request
        invite = req.invite_link if req else None
        if not invite:
            return False
        # Only feature-owned links are intercepted; existing paid handler is untouched.
        if not (invite.name or '').startswith('hf-free-'):
            return False
        try:
            row = await self.call('invite', invite.invite_link)
            cfg = await self.config()
            uid = req.from_user.id
            allowed = (row and row['valid'] and row['user_id'] == uid
                       and row['chat_id'] == req.chat.id == cfg['free_chat']
                       and row['config_version'] == cfg['version'] and cfg['enabled'])
            if allowed:
                allowed = not await self.is_banned(uid) and await self.verified_member(
                    context.bot, cfg['testi_chat'], uid)
            if allowed:
                # Refresh after API call so admin changes during verification fail closed.
                fresh = await self.config()
                allowed = fresh['enabled'] and fresh['version'] == cfg['version']
            if allowed:
                await req.approve()
                await self.call('consume', invite.invite_link)
                await self.revoke(context.bot, req.chat.id, invite.invite_link)
            else:
                await req.decline()
        except Exception as exc:
            log.warning('[CLAIM FREE] join verification failed: %s', type(exc).__name__)
            try:
                await req.decline()
            except TelegramError:
                pass
        return True

    async def callback(self, update, context):
        q = update.callback_query
        if update.effective_chat.type != 'private':
            await q.answer('Buka bot melalui chat pribadi.', show_alert=True)
            return
        if q.data != 'cf_verify' and not await self.is_admin(update.effective_user.id, context):
            await q.answer('⛔ Akses ditolak.', show_alert=True)
            return
        await q.answer()  # Acknowledge before DB/API work.
        try:
            if q.data == 'cf_verify':
                return await self.verify(update, context)
            context.user_data.pop('awaiting_claim_free_edit', None)
            if q.data in ('cf_testi', 'cf_free'):
                context.user_data['awaiting_claim_free_edit'] = q.data
                prompt = ('Kirim Chat ID Grup Testimoni pada baris pertama dan link grup pada baris kedua.'
                          if q.data == 'cf_testi' else 'Kirim Chat ID Grup Free privat. Bot harus admin dengan izin Undang Pengguna.')
                return await self.reply(update, prompt, Markup([[Button('❌ Batal', callback_data='cf_cancel')]]))
            if q.data == 'cf_stats':
                r = await self.call('stats')
                return await self.reply(update, f"📊 <b>Statistik Claim</b>\n{LINE}\nTotal berhasil: {r['total']}\nHari ini (WIB): {r['today']}\nPengguna unik berhak akses: {r['unique_users']}", Markup([[Button('⬅️ Kembali', callback_data='cf_admin')]]))
            if q.data == 'cf_toggle':
                async with self.config_lock:
                    cfg = await self.config()
                    if not cfg['enabled']:
                        await self.permissions(context.bot, cfg)
                    if not await self.call('toggle', not cfg['enabled'], cfg['version']):
                        raise ValueError('Konfigurasi berubah. Silakan ulangi.')
            await self.admin_panel(update, context)
        except (ValueError, TelegramError) as exc:
            text = str(exc) if isinstance(exc, ValueError) else 'Telegram gagal memvalidasi grup atau izin bot. Coba lagi.'
            await self.reply(update, '❌ ' + html.escape(text))

    async def admin_panel(self, update, context):
        cfg = await self.config()
        text = (f"🎁 <b>Pengaturan Claim Free</b>\n{LINE}\n"
                f"Status: {'✅ Aktif' if cfg['enabled'] else '🔴 Nonaktif'}\n\n"
                f"📢 Grup Testimoni: <code>{cfg['testi_chat'] or 'Belum diatur'}</code>\n"
                f"Link: {html.escape(cfg['testi_link'] or 'Belum diatur')}\n\n"
                f"🎁 Grup Free: <code>{cfg['free_chat'] or 'Belum diatur'}</code>\n\n"
                'Perubahan grup otomatis menonaktifkan fitur. Validasi ulang sebelum mengaktifkan.')
        await self.reply(update, text, Markup([
            [Button('📢 Atur Grup Testimoni', callback_data='cf_testi')],
            [Button('🎁 Atur Grup Free', callback_data='cf_free')],
            [Button('📊 Statistik Claim', callback_data='cf_stats')],
            [Button('🔘 Nonaktifkan' if cfg['enabled'] else '🔘 Aktifkan', callback_data='cf_toggle')],
            [Button('⬅️ Kembali ke Panel', callback_data='admpanel_back')]]))

    async def text_input(self, update, context):
        mode = context.user_data.get('awaiting_claim_free_edit')
        if not mode:
            return False
        if not await self.is_admin(update.effective_user.id, context):
            context.user_data.pop('awaiting_claim_free_edit', None)
            return True
        try:
            async with self.config_lock:
                cfg = await self.config()
                lines = (update.effective_message.text or '').strip().splitlines()
                if mode == 'cf_testi':
                    if len(lines) != 2:
                        raise ValueError('Kirim tepat dua baris: Chat ID lalu link Grup Testimoni.')
                    cfg['testi_chat'], cfg['testi_link'] = parse_chat(lines[0]), parse_link(lines[1])
                    await self.validate_chat(context.bot, cfg['testi_chat'], cfg['testi_link'])
                else:
                    cfg['free_chat'] = parse_chat('\n'.join(lines))
                    await self.validate_chat(context.bot, cfg['free_chat'], free=True)
                if cfg['free_chat'] and cfg['testi_chat'] == cfg['free_chat']:
                    raise ValueError('Grup Testimoni dan Grup Free harus berbeda.')
                if not await self.call('save', cfg['testi_chat'], cfg['testi_link'], cfg['free_chat'], cfg['version']):
                    raise ValueError('Konfigurasi berubah. Silakan ulangi.')
            context.user_data.pop('awaiting_claim_free_edit', None)
            await self.reply(update, '✅ Pengaturan tersimpan permanen. Fitur nonaktif sampai diaktifkan kembali.')
            await self.admin_panel(update, context)
        except (ValueError, TelegramError) as exc:
            msg = str(exc) if isinstance(exc, ValueError) else 'Grup atau izin bot belum dapat divalidasi.'
            await self.reply(update, '❌ ' + html.escape(msg), Markup([[Button('❌ Batal', callback_data='cf_cancel')]]))
        return True
