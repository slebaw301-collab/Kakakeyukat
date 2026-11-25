"""Isolated Claim Free feature. No purchase/order tables are modified."""
import asyncio
import html
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import TelegramError, RetryAfter

log = logging.getLogger(__name__)
LINE = '━━━━━━━━━━━━━━━━━━━━━━'
UNAVAILABLE = '🎁 Claim Grup Free belum tersedia. Silakan coba kembali nanti.'
INTRO = (
    f'🎁 <b>CLAIM GRUP FREE</b>\n{LINE}\n\n'
    'Dapatkan akses <b>GRUP FREE</b> secara gratis dengan menyelesaikan persyaratan berikut.\n\n'
    '📋 <b>SYARAT CLAIM</b>\n✅ Bergabung ke Grup/Channel Testimoni Hyper Store.\n\n'
    f'{LINE}\n\n📌 <b>CARA CLAIM</b>\n\n'
    '1. Tekan tombol <b>Gabung Testimoni</b>.\n'
    '2. Bergabung ke grup/channel tersebut.\n'
    '3. Kembali ke bot dan tekan <b>Verifikasi &amp; Claim</b>.\n\n'
    f'{LINE}\n🔒 Akses hanya diberikan kepada member yang telah memenuhi persyaratan.'
)
FAILED = (
    f'❌ <b>VERIFIKASI GAGAL</b>\n{LINE}\n\n'
    'Kamu belum bergabung ke Grup/Channel Testimoni Hyper Store.\n\n'
    'Silakan bergabung terlebih dahulu, kemudian tekan <b>Verifikasi &amp; Claim</b> kembali.'
)
SUCCESS = (
    f'✅ <b>CLAIM BERHASIL!</b>\n{LINE}\n\n'
    'Selamat! Persyaratan telah berhasil diverifikasi.\n\n'
    '🎁 <b>Akses:</b> GRUP FREE\n✅ <b>Status:</b> Berhasil\n\n'
    f'{LINE}\n\nTekan tombol di bawah untuk bergabung ke grup/channel gratis.'
)

ACCESS_REVOKED = (
    f'⚠️ <b>AKSES GRUP FREE DICABUT</b>\n{LINE}\n\n'
    'Akses kamu telah dicabut karena tidak lagi memenuhi persyaratan Claim Grup Free.\n\n'
    'Silakan bergabung kembali ke grup/channel persyaratan untuk memulihkan akses.\n\n'
    f'{LINE}'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_free_config (
 id SMALLINT PRIMARY KEY CHECK (id=1), enabled BOOLEAN NOT NULL DEFAULT FALSE,
 testi_chat BIGINT, testi_link TEXT, free_chat BIGINT,
 version BIGINT NOT NULL DEFAULT 0
);
ALTER TABLE claim_free_config ADD COLUMN IF NOT EXISTS backup_chat BIGINT;
ALTER TABLE claim_free_config ADD COLUMN IF NOT EXISTS backup_link TEXT;
ALTER TABLE claim_free_config ADD COLUMN IF NOT EXISTS backup_required BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE claim_free_config ADD COLUMN IF NOT EXISTS auto_kick BOOLEAN NOT NULL DEFAULT FALSE;
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
CREATE INDEX IF NOT EXISTS claim_free_approved_chat_user_idx
 ON claim_free_invites(chat_id,user_id) WHERE consumed_at IS NOT NULL;
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
    def __init__(self, session, run_db, is_admin, is_banned, paid_access=None):
        self.session, self.run_db = session, run_db
        self.is_admin, self.is_banned = is_admin, is_banned
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.config_lock = asyncio.Lock()
        self.paid_access = paid_access
        self.sweep_cursor = 0
        self.sweep_scope = None

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
                    if action == 'save_backup':
                        chat, link, version = args
                        c.execute('UPDATE claim_free_config SET backup_chat=%s,backup_link=%s,backup_required=TRUE,enabled=FALSE,version=version+1 WHERE id=1 AND version=%s RETURNING id', (chat, link, version))
                        return bool(c.fetchone())
                    if action == 'toggle_backup':
                        required, version = args
                        c.execute('UPDATE claim_free_config SET backup_required=%s,enabled=FALSE,version=version+1 WHERE id=1 AND version=%s RETURNING id', (required, version))
                        return bool(c.fetchone())
                    if action == 'toggle':
                        enabled, version = args
                        c.execute('UPDATE claim_free_config SET enabled=%s,version=version+1 WHERE id=1 AND version=%s RETURNING id', (enabled, version))
                        return bool(c.fetchone())
                    if action == 'toggle_auto_kick':
                        enabled, version = args
                        c.execute('UPDATE claim_free_config SET auto_kick=%s WHERE id=1 AND version=%s RETURNING id', (enabled, version))
                        return bool(c.fetchone())
                    if action == 'approved':
                        uid, chat = args
                        c.execute('SELECT 1 FROM claim_free_invites WHERE user_id=%s AND chat_id=%s AND consumed_at IS NOT NULL LIMIT 1', (uid, chat))
                        return bool(c.fetchone())
                    if action == 'approved_batch':
                        chat, cursor, limit = args
                        c.execute('''SELECT DISTINCT user_id FROM claim_free_invites
                            WHERE chat_id=%s AND user_id>%s AND consumed_at IS NOT NULL
                            ORDER BY user_id LIMIT %s''', (chat, cursor, limit))
                        return [r['user_id'] for r in c.fetchall()]
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
            raise ValueError('Lengkapi tujuan testimoni dan akses Free terlebih dahulu (grup/channel).')
        if cfg['testi_chat'] == cfg['free_chat']:
            raise ValueError('Tujuan Testimoni dan Free harus berbeda.')
        await self.validate_chat(bot, cfg['testi_chat'], cfg['testi_link'])
        await self.validate_chat(bot, cfg['free_chat'], free=True)
        if cfg.get('backup_required'):
            await self.validate_backup(bot, cfg)
        if cfg.get('auto_kick'):
            await self.validate_kick_permissions(bot, cfg)

    async def validate_kick_permissions(self, bot, cfg):
        if not cfg.get('free_chat'):
            raise ValueError('Atur Grup/Channel Free terlebih dahulu.')
        chat, me = await asyncio.gather(bot.get_chat(cfg['free_chat']),
                                       bot.get_chat_member(cfg['free_chat'], bot.id))
        if chat.type not in ('supergroup', 'channel'):
            raise ValueError('Auto-kick memerlukan supergroup/channel Free agar pengguna dapat bergabung kembali.')
        if me.status != 'creator' and not (me.status == 'administrator' and getattr(me, 'can_restrict_members', False)):
            raise ValueError('Aktifkan izin Ban Users / Batasi Anggota untuk bot di Grup/Channel Free.')

    async def validate_backup(self, bot, cfg):
        if not cfg.get('backup_chat') or not cfg.get('backup_link'):
            raise ValueError('Atur Chat ID dan link Backup terlebih dahulu.')
        if cfg['backup_chat'] in (cfg['testi_chat'], cfg['free_chat']):
            raise ValueError('Tujuan Backup harus berbeda dari Testimoni dan Free.')
        await self.validate_chat(bot, cfg['backup_chat'], cfg['backup_link'])

    async def validate_chat(self, bot, chat_id, link=None, free=False):
        chat, me = await asyncio.gather(bot.get_chat(chat_id), bot.get_chat_member(chat_id, bot.id))
        if chat.type not in ('group', 'supergroup', 'channel'):
            raise ValueError('Chat tujuan harus berupa grup, supergroup, atau channel.')
        if me.status not in ('creator', 'administrator'):
            raise ValueError('Jadikan bot admin di grup/channel terlebih dahulu.')
        if free and not (me.status == 'creator' or me.can_invite_users):
            raise ValueError('Bot memerlukan izin Undang Pengguna di grup/channel Free.')
        if free and chat.username:
            raise ValueError('Grup/channel Free harus privat agar akses hanya melalui verifikasi bot.')
        if link:
            public = 'https://t.me/' + chat.username if chat.username else None
            primary = getattr(chat, 'invite_link', None)
            def normalized(s):
                return s.replace('/joinchat/', '/+') if s else s
            if not ((public and link.lower() == public.lower()) or normalized(link) == normalized(primary)):
                raise ValueError('Link tidak cocok dengan grup/channel. Gunakan link publik atau link undangan utama yang terbaca oleh bot.')

    def keyboard(self, cfg):
        rows = [[Button('📢 Gabung Testimoni', url=cfg['testi_link'])]]
        if cfg.get('backup_required'):
            rows.append([Button('🔰 Gabung Backup', url=cfg['backup_link'])])
        rows.append([Button('✅ Verifikasi & Claim', callback_data='cf_verify')])
        return Markup(rows)

    def intro(self, cfg):
        if not cfg.get('backup_required'):
            return INTRO
        return (INTRO.replace('✅ Bergabung ke Grup/Channel Testimoni Hyper Store.',
                              '✅ Bergabung ke Grup/Channel Testimoni Hyper Store.\n✅ Bergabung ke Grup/Channel Backup Hyper Store.')
                     .replace('1. Tekan tombol <b>Gabung Testimoni</b>.\n2. Bergabung ke grup/channel tersebut.',
                              '1. Tekan tombol <b>Gabung Testimoni</b> dan <b>Gabung Backup</b>.\n2. Bergabung ke kedua grup/channel tersebut.'))

    def failed(self, cfg):
        if not cfg.get('backup_required'):
            return FAILED
        return FAILED.replace('Kamu belum bergabung ke Grup/Channel Testimoni Hyper Store.',
                              'Kamu belum memenuhi semua syarat. Pastikan sudah bergabung ke Grup/Channel Testimoni dan Backup Hyper Store.')

    async def verified_requirements(self, bot, cfg, uid):
        if not await self.verified_member(bot, cfg['testi_chat'], uid):
            return False
        if cfg.get('backup_required'):
            if not cfg.get('backup_chat') or not cfg.get('backup_link'):
                return False
            return await self.verified_member(bot, cfg['backup_chat'], uid)
        return True

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
        await self.reply(update, self.intro(cfg), self.keyboard(cfg))

    async def verified_member(self, bot, chat, uid):
        me, member = await asyncio.gather(bot.get_chat_member(chat, bot.id), bot.get_chat_member(chat, uid))
        if me.status not in ('administrator', 'creator'):
            raise ValueError('Bot bukan admin grup/channel persyaratan.')
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
                if not await self.verified_requirements(context.bot, cfg, uid):
                    return await self.reply(update, self.failed(cfg), self.keyboard(cfg))
                reserved = await self.call('reserve', uid, token)
                if not reserved:
                    return await self.reply(update, '⏳ Claim sedang diproses. Coba kembali sebentar lagi.')
                cached = await self.call('latest', uid, cfg['version'])
                if cached:
                    return await self.reply(update, SUCCESS, Markup([[Button('🔗 Masuk Grup/Channel Free', url=cached)]]))
                expires = datetime.now(timezone.utc) + timedelta(minutes=15)
                invitation = await context.bot.create_chat_invite_link(
                    chat_id=cfg['free_chat'], name='hf-free-' + token[:16],
                    expire_date=expires, creates_join_request=True)
                link = invitation.invite_link
                if not await self.call('finish', uid, token, link, cfg['free_chat'], cfg['version'], expires):
                    raise ValueError('Konfigurasi berubah saat claim diproses. Silakan ulangi.')
                await self.reply(update, SUCCESS, Markup([[Button('🔗 Masuk Grup/Channel Free', url=link)]]))
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
        async with self.locks[req.from_user.id % len(self.locks)]:
            return await self._owned_join_request(req, invite, context)

    async def _owned_join_request(self, req, invite, context):
        try:
            row = await self.call('invite', invite.invite_link)
            cfg = await self.config()
            uid = req.from_user.id
            allowed = (row and row['valid'] and row['user_id'] == uid
                       and row['chat_id'] == req.chat.id == cfg['free_chat']
                       and row['config_version'] == cfg['version'] and cfg['enabled'])
            if allowed:
                allowed = not await self.is_banned(uid) and await self.verified_requirements(
                    context.bot, cfg, uid)
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

    @staticmethod
    def kick_active(cfg):
        return bool(cfg.get('enabled') and cfg.get('auto_kick') and cfg.get('free_chat')
                    and cfg.get('testi_chat')
                    and (not cfg.get('backup_required') or
                         (cfg.get('backup_chat') and cfg.get('backup_link'))))

    async def enforce_member(self, bot, cfg, uid):
        if not self.kick_active(cfg) or uid == bot.id:
            return False
        async with self.locks[uid % len(self.locks)]:
            if not await self.call('approved', uid, cfg['free_chat']):
                return False
            current = await self.config()
            if not self.kick_active(current) or current['version'] != cfg['version']:
                return False
            target = await bot.get_chat_member(cfg['free_chat'], uid)
            if not member_ok(target) or target.status in ('administrator', 'creator'):
                return False
            # Fail closed if paid access cannot be checked; never remove a paid buyer.
            if self.paid_access is None or await self.paid_access(uid, cfg['free_chat']):
                return False
            if await self.verified_requirements(bot, current, uid):
                return False
            async with self.config_lock:
                fresh = await self.config()
                if not self.kick_active(fresh) or fresh['version'] != cfg['version']:
                    return False
                await self.validate_kick_permissions(bot, fresh)
                # Never unban a manually banned user, or kick an administrator.
                target = await bot.get_chat_member(fresh['free_chat'], uid)
                if not member_ok(target) or target.status in ('administrator', 'creator'):
                    return False
                if await self.paid_access(uid, fresh['free_chat']):
                    return False
                # Recheck current membership to avoid acting on stale leave updates.
                if await self.verified_requirements(bot, fresh, uid):
                    return False
                # Telegram unban with only_if_banned=False removes a current member
                # without leaving a permanent ban; no two-step ban/unban failure.
                if not await bot.unban_chat_member(fresh['free_chat'], uid, only_if_banned=False):
                    return False
            try:
                await bot.send_message(uid, ACCESS_REVOKED, parse_mode='HTML',
                    reply_markup=Markup([[Button('🔄 Pulihkan Akses', callback_data='cf_verify')]]),
                    protect_content=True, disable_web_page_preview=True)
            except TelegramError:
                log.info('[CLAIM FREE] removal notification unavailable')
            return True

    async def membership_changed(self, update, context):
        event = update.chat_member
        if not event or not member_ok(event.old_chat_member) or member_ok(event.new_chat_member):
            return
        try:
            cfg = await self.config()
            if not self.kick_active(cfg):
                return
            required = {cfg['testi_chat']}
            if cfg.get('backup_required'):
                required.add(cfg['backup_chat'])
            if event.chat.id not in required:
                return
            await self.enforce_member(context.bot, cfg, event.new_chat_member.user.id)
        except Exception as exc:
            log.warning('[CLAIM FREE] membership event deferred: %s', type(exc).__name__)

    async def reconcile(self, bot, batch_size=10):
        cfg = await self.config()
        if not self.kick_active(cfg):
            self.sweep_cursor, self.sweep_scope = 0, None
            return
        scope = (cfg['free_chat'], cfg['version'])
        if self.sweep_scope != scope:
            self.sweep_cursor, self.sweep_scope = 0, scope
        ids = await self.call('approved_batch', cfg['free_chat'], self.sweep_cursor, batch_size)
        for uid in ids:
            try:
                await self.enforce_member(bot, cfg, uid)
            except RetryAfter:
                # Resume from last completed user on next interval.
                raise
            except Exception as exc:
                log.warning('[CLAIM FREE] reconciliation deferred: %s', type(exc).__name__)
            self.sweep_cursor = uid
            await asyncio.sleep(0.5)
        if len(ids) < batch_size:
            self.sweep_cursor = 0

    async def reconciliation_loop(self, bot):
        # Bounded, sequential sweep; catches missed events after downtime.
        while True:
            delay = 30
            try:
                await self.reconcile(bot)
            except RetryAfter as exc:
                delay = max(delay, float(exc.retry_after) + 1)
            except Exception as exc:
                log.warning('[CLAIM FREE] reconciliation paused: %s', type(exc).__name__)
            await asyncio.sleep(delay)

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
            if q.data in ('cf_testi', 'cf_free', 'cf_backup'):
                context.user_data['awaiting_claim_free_edit'] = q.data
                prompt = ('Kirim Chat ID grup/channel Testimoni pada baris pertama dan link-nya pada baris kedua.'
                          if q.data == 'cf_testi' else
                          'Kirim Chat ID grup/channel Backup pada baris pertama dan link-nya pada baris kedua.'
                          if q.data == 'cf_backup' else 'Kirim Chat ID grup/channel Free privat. Bot harus admin dengan izin Undang Pengguna.')
                return await self.reply(update, prompt, Markup([[Button('❌ Batal', callback_data='cf_cancel')]]))
            if q.data == 'cf_stats':
                r = await self.call('stats')
                return await self.reply(update, f"📊 <b>Statistik Claim</b>\n{LINE}\nTotal berhasil: {r['total']}\nHari ini (WIB): {r['today']}\nPengguna unik berhak akses: {r['unique_users']}", Markup([[Button('⬅️ Kembali', callback_data='cf_admin')]]))
            if q.data == 'cf_backup_toggle':
                async with self.config_lock:
                    cfg = await self.config()
                    required = not cfg.get('backup_required', False)
                    if required:
                        await self.validate_backup(context.bot, cfg)
                    if not await self.call('toggle_backup', required, cfg['version']):
                        raise ValueError('Konfigurasi berubah. Silakan ulangi.')
            if q.data == 'cf_kick_toggle':
                async with self.config_lock:
                    cfg = await self.config()
                    active = not cfg.get('auto_kick', False)
                    if active:
                        await self.permissions(context.bot, cfg)
                        await self.validate_kick_permissions(context.bot, cfg)
                    if not await self.call('toggle_auto_kick', active, cfg['version']):
                        raise ValueError('Konfigurasi berubah. Silakan ulangi.')
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
                f"📢 Testimoni (grup/channel): <code>{cfg['testi_chat'] or 'Belum diatur'}</code>\n"
                f"Link: {html.escape(cfg['testi_link'] or 'Belum diatur')}\n\n"
                f"🔰 Backup (grup/channel): <code>{cfg.get('backup_chat') or 'Belum diatur'}</code>\n"
                f"Link: {html.escape(cfg.get('backup_link') or 'Belum diatur')}\n"
                f"Syarat Backup: {'✅ Aktif' if cfg.get('backup_required') else '🔴 Nonaktif'}\n\n"
                f"🎁 Free (grup/channel): <code>{cfg['free_chat'] or 'Belum diatur'}</code>\n"
                f"Auto-kick: {'✅ Aktif' if cfg.get('auto_kick') and cfg['enabled'] else '⏸ Dijeda (Claim nonaktif)' if cfg.get('auto_kick') else '🔴 Nonaktif'}\n\n"
                'Perubahan grup otomatis menonaktifkan fitur. Validasi ulang sebelum mengaktifkan.')
        await self.reply(update, text, Markup([
            [Button('📢 Atur Testimoni (Grup/Channel)', callback_data='cf_testi')],
            [Button('🔰 Atur Backup (Grup/Channel)', callback_data='cf_backup')],
            [Button('🔘 Nonaktifkan Syarat Backup' if cfg.get('backup_required') else '🔘 Aktifkan Syarat Backup', callback_data='cf_backup_toggle')],
            [Button('🎁 Atur Free (Grup/Channel)', callback_data='cf_free')],
            [Button('🔘 Nonaktifkan Auto-kick' if cfg.get('auto_kick') else '🔘 Aktifkan Auto-kick', callback_data='cf_kick_toggle')],
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
                        raise ValueError('Kirim tepat dua baris: Chat ID lalu link grup/channel Testimoni.')
                    cfg['testi_chat'], cfg['testi_link'] = parse_chat(lines[0]), parse_link(lines[1])
                    await self.validate_chat(context.bot, cfg['testi_chat'], cfg['testi_link'])
                elif mode == 'cf_backup':
                    if len(lines) != 2:
                        raise ValueError('Kirim tepat dua baris: Chat ID lalu link grup/channel Backup.')
                    cfg['backup_chat'], cfg['backup_link'] = parse_chat(lines[0]), parse_link(lines[1])
                    await self.validate_backup(context.bot, cfg)
                else:
                    cfg['free_chat'] = parse_chat('\n'.join(lines))
                    await self.validate_chat(context.bot, cfg['free_chat'], free=True)
                if cfg['free_chat'] and cfg['testi_chat'] == cfg['free_chat']:
                    raise ValueError('Tujuan Testimoni dan Free harus berbeda.')
                if cfg.get('backup_chat') and cfg['backup_chat'] in (cfg['testi_chat'], cfg['free_chat']):
                    raise ValueError('Tujuan Backup harus berbeda dari Testimoni dan Free.')
                if mode == 'cf_backup':
                    saved = await self.call('save_backup', cfg['backup_chat'], cfg['backup_link'], cfg['version'])
                else:
                    saved = await self.call('save', cfg['testi_chat'], cfg['testi_link'], cfg['free_chat'], cfg['version'])
                if not saved:
                    raise ValueError('Konfigurasi berubah. Silakan ulangi.')
            context.user_data.pop('awaiting_claim_free_edit', None)
            await self.reply(update, '✅ Pengaturan tersimpan permanen. Fitur nonaktif sampai diaktifkan kembali.')
            await self.admin_panel(update, context)
        except (ValueError, TelegramError) as exc:
            msg = str(exc) if isinstance(exc, ValueError) else 'Grup atau izin bot belum dapat divalidasi.'
            await self.reply(update, '❌ ' + html.escape(msg), Markup([[Button('❌ Batal', callback_data='cf_cancel')]]))
        return True
