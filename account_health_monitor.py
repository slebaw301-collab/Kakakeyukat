import asyncio
import base64
import hashlib
import hmac
import json
from io import BytesIO
import logging
import os
import re
from datetime import datetime, timezone
from html import escape
from time import monotonic, time
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from telegram import ForceReply, InputMediaPhoto
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters


logger = logging.getLogger(__name__)
PREFIX = "am:"
BACK = "admpanel_back"
STATES = {
    "normal": "🟢 Normal", "frozen": "🔴 Frozen", "deactivated": "⚫ Deactivated",
    "restricted": "🟠 Restricted", "spam_limited": "🟡 Spam Limited", "unknown": "⚪ Unknown",
    "connected": "Terhubung", "disconnected": "🔵 Disconnected", "invalid": "🔑 Session Invalid",
    "valid": "Valid", "limited": "🟡 Spam Limited", "clear": "Tidak ada pembatasan terkonfirmasi",
    "lost": "Akses hilang", "left": "Keluar", "member": "Anggota", "admin": "Admin", "owner": "Pemilik",
}
CATEGORIES = {
    "frozen": "Akun Frozen", "deactivated": "Akun Deactivated", "restricted": "Pembatasan akun",
    "spam": "Spam Limited", "invalid": "Session invalid", "offline": "Koneksi akun",
    "peer": "Akses channel atau grup", "role": "Perubahan peran", "recovered": "Akun kembali normal",
    "system": "Gangguan service", "appeal": "Tenggat banding", "daily": "Laporan harian",
}
APPEALS = {
    "submitted": "Diajukan", "pending": "Menunggu", "accepted": "Diterima",
    "rejected": "Ditolak", "withdrawn": "Ditarik",
}
INCIDENTS = {
    "all": "Semua", "critical": "Critical", "warning": "Peringatan", "unknown": "Belum pasti", "resolved": "Selesai",
}
SETTINGS = {
    "automatic": "Automatic Monitoring", "channels": "Channel Monitor",
    "notifications": "Instant Notification", "daily": "Daily Report",
    "interval_minutes": "Interval monitoring", "retention_days": "Retensi riwayat",
    "peer_limit": "Batas channel dan grup", "concurrency": "Pemeriksaan paralel", "max_clients": "Batas client", "kill_switch": "Kill Switch",
}
ACCOUNT_FILTERS = {"all": "Semua akun", "normal": "Normal", "spam_limited": "Spam Limited",
                   "restricted": "Restricted", "frozen": "Frozen", "deactivated": "Deactivated",
                   "invalid": "Session Invalid", "disconnected": "Disconnected", "unknown": "Unknown"}
PEER_TYPES = {"all": "Semua jenis", "channel": "Channel", "group": "Grup"}
PEER_ROLES = {"all": "Semua peran", "owner": "Owner", "admin": "Admin", "member": "Member"}
CHECK_REASONS = {
    "invalid": "Session Invalid. Autentikasi ulang akun.",
    "deactivated": "Telegram mengonfirmasi akun dinonaktifkan.",
    "disconnected": "Koneksi Telegram terputus. Coba lagi nanti.",
    "no_session": "Session hilang. Autentikasi ulang akun.",
    "removed": "Item telah dihapus dari monitor.",
    "unknown": "Status belum terkonfirmasi. Lihat detail akun.",
    "service_error": "Service sempat terganggu. Coba ulang.",
    "peer_unknown": "Akses belum terverifikasi. Coba periksa ulang.",
}
SPAM_REASONS = {
    "identitas": "Identitas bot resmi belum terverifikasi.",
    "menunggu": "Telegram belum mengirim respons terbaru.",
    "kedaluwarsa": "Respons terbaru sudah lebih dari 24 jam.",
    "format": "Respons Telegram belum dapat dikenali dengan pasti.",
    "belum_ada": "Belum ada respons di chat @SpamBot.",
    "akun": "Sesi atau kondisi akun tidak mendukung pemeriksaan.",
    "koneksi": "Telegram gagal dijangkau. Coba lagi.",
}

UNAVAILABLE = "Account Monitor sedang tidak tersedia."


class MonitorUnavailable(Exception):
    pass


class MonitorInput(Exception):
    pass


class MonitorPaused(Exception):
    pass


class MonitorClient:
    def __init__(self, url, key):
        address = urlsplit(url)
        if (address.scheme != "https" or not address.hostname or address.username is not None
                or address.path not in {"", "/"} or address.query or address.fragment):
            raise ValueError("URL Account Monitor harus HTTPS.")
        self.url = url.rstrip("/") + "/v1/admin"
        self.key = base64.b64decode(key, altchars=b"-_", validate=True)
        if len(self.key) != 32:
            raise ValueError("Panjang kunci Account Monitor tidak valid.")
        self.session = None
        self.failures = 0
        self.reopen_at = 0
        self.probing = False
        self.summary_cache = None
        self.summary_cached_at = 0

    async def close(self):
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def call(self, action, **fields):
        current = monotonic()
        if action == "summary" and self.summary_cache is not None and current - self.summary_cached_at < 4:
            return self.summary_cache
        half_open = self.reopen_at > 0
        if current < self.reopen_at or self.probing:
            raise MonitorUnavailable(UNAVAILABLE)
        if half_open:
            self.probing = True
        try:
            if self.session is None:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=1.6, connect=0.75),
                    connector=aiohttp.TCPConnector(limit=2, limit_per_host=2),
                )
            body = json.dumps({"action": action, **fields}, ensure_ascii=False, separators=(",", ":")).encode()
            stamp, nonce = str(int(time())), str(uuid4())
            formatted = f"POST\n/v1/admin\n{stamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()
            headers = {
                "Content-Type": "application/json", "X-Monitor-Time": stamp, "X-Monitor-Nonce": nonce,
                "X-Monitor-Signature": hmac.new(self.key, formatted, hashlib.sha256).hexdigest(),
            }
            async with self.session.post(self.url, data=body, headers=headers, allow_redirects=False) as reply:
                if reply.status not in {200, 400, 423, 429}:
                    raise MonitorUnavailable(UNAVAILABLE)
                raw = bytearray()
                async for chunk in reply.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > 32768:
                        raise MonitorUnavailable(UNAVAILABLE)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise MonitorUnavailable(UNAVAILABLE)
                self.failures = self.reopen_at = 0
                if reply.status == 423:
                    self.summary_cache = None
                    raise MonitorPaused("Kill Switch aktif.")
                if reply.status != 200:
                    raise MonitorInput(str(result.get("error", "Aksi tidak tersedia."))[:200])
                if action == "summary":
                    self.summary_cache, self.summary_cached_at = result, monotonic()
                elif action not in {"settings", "system", "lease", "begin", "finish"}:
                    self.summary_cache = None
                return result
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError, MonitorUnavailable):
            self.failures += 1
            self.summary_cache = None
            if self.failures >= 3:
                self.reopen_at = monotonic() + 30
            raise MonitorUnavailable(UNAVAILABLE) from None
        finally:
            if half_open:
                self.probing = False


def button(label, route, *parts):
    return Button(label, callback_data=PREFIX + ":".join(map(str, (route, *parts))))


def link(value):
    if isinstance(value, str) and value.startswith(("https://t.me/", "https://telegram.org/",
                                                     "https://telegram.me/", "https://www.telegram.org/")):
        return value
    return None


def fmt_time(value):
    return datetime.fromisoformat(value).strftime("%d/%m/%Y %H:%M WIB") if value else "—"


def count_down(value):
    if not value:
        return "—"
    minutes = int((datetime.fromisoformat(value) - datetime.now(timezone.utc)).total_seconds() // 60)
    if minutes < 0:
        return "Sudah lewat"
    days, minutes = divmod(minutes, 1440)
    hours, mins = divmod(minutes, 60)
    return f"{days} hari {hours} jam {mins} menit"


def yn(value):
    return "ON" if value else "OFF"


def state(value):
    return STATES.get(value, value or "—")


def account_line(row):
    return state(row.get("display_status", row.get("latest_health", row["health"])))


def fresh(value, seconds):
    if not value:
        return "belum diperiksa"
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    return "aktual" if age <= seconds else "kedaluwarsa"


def pagination(route, args, page, more):
    rows = []
    if page:
        rows.append(button("⬅️", route, *args, page - 1))
    if more:
        rows.append(button("➡️", route, *args, page + 1))
    return [rows] if rows else []


def panel(title, text="", rows=(), back=None):
    header = f"<b>{escape(title)}</b>\n========================"
    markup = list(rows)
    if back:
        markup.append([back])
    return header + (f"\n\n{text}" if text else ""), InlineKeyboardMarkup(markup)


class AccountMonitorPanel:
    def __init__(self, owner, client, scope, ack, background_options):
        self.owner = owner
        self.client = client
        self.scope = scope
        self.ack = ack
        self.background_options = background_options
        self.task = None
        self.active_notice = None
        self.selections = {}
        self.view_version = 0
        self.refresh_task = None
        self.prompt = None
        self.confirm_remove = None
        self.confirm_account = None
        self.run_back = {}
        self.qr_flow = None
        self.qr_runner = None
        self.qr_start_lock = asyncio.Lock()
        self.offline_count = 0
        self.offline_announced = False
        self.notify_system = False
        self.last_health = 0

    @classmethod
    def from_env(cls, owner, scope, ack, background_options):
        if os.environ.get("ACCOUNT_MONITOR_ENABLED") != "1":
            return None
        try:
            client = MonitorClient(os.environ["ACCOUNT_MONITOR_URL"], os.environ["ACCOUNT_MONITOR_API_KEY"])
        except (KeyError, ValueError):
            logger.warning("Integrasi Account Monitor dilewati karena konfigurasi belum lengkap.")
            return None
        return cls(owner, client, scope, ack, background_options)

    def register(self, app):
        app.add_handler(CallbackQueryHandler(self.callback, pattern=r"^am:"))
        app.add_handler(CommandHandler("monitor_input", self.message))
        panel = self

        class PanelReply(filters.MessageFilter):
            def filter(self, message):
                return bool(panel.prompt and message.reply_to_message
                            and message.reply_to_message.message_id == panel.prompt[0]
                            and panel.prompt[3].message.chat_id == message.chat_id
                            and panel.prompt[4] == panel.view_version and panel.prompt[5] > monotonic())

        app.add_handler(MessageHandler(
            filters.User(self.owner) & filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND & PanelReply(),
            self.reply_input,
        ))

    def authorized(self, update):
        return (update.effective_user is not None and update.effective_user.id == self.owner
                and update.effective_chat is not None and update.effective_chat.type == "private"
                and update.effective_chat.id == self.owner)

    async def callback(self, update, context):
        query = update.callback_query
        if not self.authorized(update):
            await self.ack(query, "⛔ Akses ditolak.")
            return
        await self.ack(query)
        route, *args = query.data[len(PREFIX):].split(":")
        if route == "qr_generate":
            try:
                fields = {"id": int(args[0])} if args else {"alias": "Akun Telegram"}
                await self.start_qr(context.bot, **fields)
            except (MonitorInput, MonitorPaused, MonitorUnavailable) as exc:
                await query.message.reply_text(str(exc))
            except (ValueError, IndexError):
                await query.message.reply_text("Pilih akun lagi dari Panel Admin.")
            return
        if route == "qr_cancel":
            if args and re.fullmatch(r"[A-Za-z0-9_-]{43}", args[0]):
                try:
                    await self.client.call("enroll_qr_cancel", flow=args[0])
                    if args[0] != self.qr_flow and query.message:
                        await self.delete_enrollment_message(context.bot, query.message.message_id)
                except (MonitorInput, MonitorPaused, MonitorUnavailable) as exc:
                    await query.message.reply_text(str(exc))
            return
        self.view_version += 1
        if route != "confirmremove":
            self.confirm_account = None
        if route != "pickconfirm":
            self.confirm_remove = None
        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()
        try:
            if route == "input":
                await self.request_input(query, args)
                return
            if route in {"checkall", "checkone", "runpick", "peercheck"}:
                fields = {"kind": "account"}
                destination = ("home", [])
                if route == "checkone":
                    fields["ids"] = [int(args[0])]
                    destination = ("account", args)
                if route == "peercheck":
                    fields = {"kind": "peer", "ids": [int(args[0])]}
                    destination = ("peer", args)
                if route == "runpick":
                    key = (args[0], int(args[1]), args[2])
                    fields = {"kind": "account" if args[0] == "a" else "peer",
                              "ids": sorted(self.view_state(key)["selected"])}
                    destination = ("picks", args)
                request = await self.client.call("run_start", **fields)
                if len(self.run_back) >= 20:
                    self.run_back.pop(next(iter(self.run_back)))
                self.run_back[request["run"]] = destination
                route, args = "run", [str(request["run"]), "0"]
            if route == "spamcheck":
                await self.client.call("read_spam", id=int(args[0]))
                route = "spam"
            text, markup = await self.render(route, args)
        except (MonitorUnavailable, MonitorInput, MonitorPaused) as exc:
            back = button("⬅️ Account Monitor", "home")
            if route.startswith("pick") and len(args) >= 3:
                back = button("⬅️ Kembali ke pilihan", "picks", *args[:3])
            text, markup = panel("📡 Account Monitor", escape(str(exc)),
                                 [[button("🖥 Diagnostik", "system"), button("⚙️ Pengaturan", "settings")]],
                                 back=back)
        except (ValueError, TypeError, KeyError, IndexError):
            text, markup = panel("📡 Account Monitor", "Menu sudah berubah. Buka ulang Account Monitor.",
                                 back=button("⬅️ Account Monitor", "home"))
        await self.edit(query, text, markup)
        if route in {"run", "pickdiscover", "spam"}:
            self.refresh_task = asyncio.create_task(self.refresh_panel(query, route, args, self.view_version))

    async def edit(self, query, text, markup):
        with self.scope("admin"):
            try:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise

    async def render(self, route, args):
        call = self.client.call
        home = button("⬅️ Account Monitor", "home")
        arg = args[0] if args else None

        if route == "home":
            rows = [
                [button("👥 Kelola Akun", "accounts", "account", 0), button("🔄 Periksa semua", "checkall")],
                [button("⚠️ Incident Center", "incidents", "all", 0)],
                [button("📡 Channel & Group Monitor", "accounts", "peers", 0)],
                [button("📜 Riwayat Status", "accounts", "history", 0), button("📝 Appeal Manager", "accounts", "appeals", 0)],
                [button("🔔 Pusat notifikasi", "notices", "all", 0), button("🖥 Diagnostik", "system")],
                [button("⚙️ Pengaturan", "settings")],
            ]
            try:
                summary = await call("summary")
                counts = " • ".join(f"{escape(state(key))} {value}" for key, value in summary["counts"].items())
                text = f"<b>{summary['total']} akun</b> · {summary.get('peers', 0)} channel &amp; grup\n{counts or 'Belum ada akun'}"
                if "automatic" in summary:
                    text += f"\nGlobal {yn(summary['automatic'])} · Individual {summary['enabled']}/{summary['total']}"
                    text += f"\nChannel {yn(summary['channels'])} · Kill Switch {yn(summary['kill_switch'])}"
                    text += f"\nInsiden aktif {summary['open_incidents']}"
                text += f"\nTerakhir diperiksa {fmt_time(summary['latest'])}"
            except (MonitorUnavailable, MonitorPaused) as exc:
                text = escape(str(exc))
            return panel("📡 Account Monitor", text, rows, Button("⬅️ Kembali", callback_data=BACK))

        if route == "run":
            return await self.run_page(args)
        if route == "runback":
            target_route, target_args = self.run_back.get(int(arg), ("home", []))
            return await self.render(target_route, target_args)
        if route.startswith("pick"):
            if route == "picksearchclear":
                self.view_state((args[0], int(args[1]), args[2])).update(query=None, page=0)
                route = "picks"
            return await self.selection_page(route, args)
        if route == "accounts":
            category, page = arg, int(args[1])
            if category in {"account", "peers"}:
                key = ("a", 0, category)
                return await self.selection_page("picks", ["a", "0", category,
                                                            str(self.view_state(key)["page"] if not page else page)])
            result = await call("accounts", page=page)
            rows = []
            for row in result["rows"]:
                suffix = (0,) if category == "appeals" else (7, "all", 0)
                rows.append([button(f"{account_line(row)} · {row['alias']}"[:62], category, row["id"], *suffix)])
            rows += pagination(route, (category,), page, result["next"])
            return panel("👥 Pilih akun", "Belum ada akun terdaftar." if not result["rows"] else "", rows, home)

        if route == "enroll":
            suffix = (int(arg),) if arg is not None else ()
            return panel("🔐 Hubungkan akun Telegram", "Pilih metode login.",
                         [[button("📱 Nomor telepon", "enroll_phone", *suffix)],
                          [button("📷 QR Code", "qr_generate" if suffix else "enroll_qr", *suffix)]],
                         back=button("⬅️ Kelola Akun", "accounts", "account", 0))

        if route == "enroll_phone":
            fields = {"id": int(arg)} if arg is not None else {}
            ticket = await call("enroll_ticket", **fields)
            return panel("🔐 Login nomor telepon", "Masukkan nomor, kode, dan 2FA bila diminta di halaman aman.",
                         [[Button("Buka input aman", url=self.enrollment_url(ticket["token"]))]],
                         back=button("⬅️ Kembali", "enroll", *((int(arg),) if arg is not None else ())))

        if route == "enroll_qr":
            return panel("📷 Login QR Code",
                         "QR muncul di chat ini dan diperbarui otomatis.",
                         [[button("Pilih alias & tampilkan QR", "input", "qr")],
                          [button("Tampilkan QR · Akun Telegram", "qr_generate")]], back=button("⬅️ Kembali", "enroll"))

        if route in {"account", "checkone", "toggle", "remove", "confirmremove", "alias", "spam", "accountmore"}:
            return await self.account_page(route, args)

        if route in {"peers", "peer", "discover", "choices", "peeradd", "peerremove", "peerdelete"}:
            return await self.peer_page(route, args)

        if route == "history":
            return await self.history_page(args)

        if route in {"incidents", "incident", "read"}:
            return await self.incident_page(route, args)

        if route in {"appeals", "appealstate"}:
            return await self.appeal_page(route, args)

        if route == "limits":
            conf = await call("settings")
            return panel("◈ Batas & retensi", "Perubahan berlaku saat pemeriksaan berikutnya.",
                         [[button(f"Retensi · {conf['retention_days']} hari", "input", "retention")],
                          [button(f"Channel/grup per akun · {conf['peer_limit']}", "input", "peer_limit")],
                          [button(f"Pemeriksaan paralel · {conf['concurrency']}", "input", "concurrency"),
                           button(f"Batas client · {conf['max_clients']}", "input", "clients")]],
                         button("⬅️ Kembali", "settings"))
        if route in {"settings", "set", "notify", "mute", "interval", "time", "kill"}:
            return await self.settings_page(route, args)

        if route in {"notices", "notice"}:
            return await self.notification_page(route, args)

        if route == "system":
            health = await call("system")
            queue = health["queue"]
            return panel("🖥 Status Sistem",
                         f"Service aktif • Uptime {health['uptime_s']} detik\n"
                         f"CPU proses {health['cpu_percent']:.1f}% • RAM {health['ram_mib']:.1f} MiB\n"
                         f"Database terhubung • Scheduler {escape(health['scheduler'])}\n"
                         f"Client Telegram terhubung {health['clients']}\n"
                         f"Pemeriksaan terakhir {fmt_time(health['last_check'])}\n"
                         f"Respons API {health['api_ms']:.2f} ms • Error worker {health['errors']}\n"
                         f"Antrean notifikasi {queue['pending']} • Perlu ditinjau {queue['failed']}\n"
                         f"Backup diverifikasi {fmt_time(health['backup_verified_at'])}",
                         [[button("🔄 Diagnostik ulang", "system")]], home)
        raise MonitorInput("Menu Account Monitor tidak tersedia.")

    async def account_page(self, route, args):
        account_id = int(args[0])
        call = self.client.call
        back = button("⬅️ Kelola Akun", "accounts", "account", 0)
        if route == "toggle":
            await call("toggle", id=account_id, value=int(args[1]))
        if route == "remove":
            self.confirm_account = account_id
            return panel("🗑 Hapus akun",
                         "Session, channel atau grup, incident, riwayat status, dan catatan banding akun ini akan dihapus dari monitor. "
                         "Untuk mencabut sesi di Telegram, buka Pengaturan → Perangkat.",
                         [[button("Hapus dari monitor", "confirmremove", account_id)]], button("⬅️ Batal", "account", account_id))
        if route == "confirmremove":
            if self.confirm_account != account_id:
                raise MonitorInput("Buka ulang konfirmasi penghapusan akun.")
            self.confirm_account = None
            await call("remove", id=account_id)
            return panel("👥 Kelola Akun", "Akun telah dihapus dari monitor.", back=back)
        row = await call("account", id=account_id)
        if route == "alias":
            return panel("✏️ Ubah alias", escape(row["alias"]),
                         [[button("Masukkan alias baru", "input", "alias", account_id)]],
                         button("⬅️ Kembali", "account", account_id))
        if route == "spam":
            evidence = row["spam_at"]
            expiry = row["spam_until"]
            last = row.get("spam_check_reason")
            status = state(row["spam_status"]) if (evidence and fresh(evidence, 86400) == "aktual"
                and (not expiry or datetime.fromisoformat(expiry) > datetime.now(timezone.utc))
                and last in {None, "terkonfirmasi"}) else state("unknown")
            text = (f"<b>{escape(row['alias'])} · {escape(status)}</b>\n"
                    f"Pemeriksaan terakhir {fmt_time(row.get('spam_checked_at'))}\n"
                    f"Bukti terakhir {escape(state(row['spam_status']))} · {fmt_time(evidence)}")
            if expiry:
                text += f"\nTenggat terakhir {fmt_time(expiry)}"
            if row.get("spam_pending"):
                text += "\n⏳ Menunggu pemeriksaan."
            elif status == state("unknown"):
                text += "\n" + SPAM_REASONS.get(last, "Belum ada respons terkini yang terkonfirmasi.")
            text += ("\n\nBaca hasil mengambil balasan terbaru dari @SpamBot resmi. "
                     "Kalau belum ada, buka bot tersebut dari akun yang dipantau dan jalankan /start.")
            return panel("🟡 Status spam", text,
                         [[button("🔄 Baca hasil @SpamBot", "spamcheck", account_id)],
                          [Button("Buka @SpamBot", url="https://t.me/SpamBot")]],
                         button("⬅️ Kembali", "account", account_id))

        settings = await call("settings")
        interval = row.get("interval_minutes") or settings["interval_minutes"]
        freshness = max(7200 if row["health"] == "frozen" else 0, interval * 120)
        last_status = escape(state(row["health"]))
        current = account_line(row)
        active = settings["automatic"] and row["enabled"] and not settings["kill_switch"] and row["auth"] != "invalid" and row["health"] != "deactivated"
        text = (
            f"<b>{escape(row['alias'])}</b>\n"
            f"Nama Telegram {escape(row.get('telegram_name') or '—')}\n"
            f"Username {escape('@' + row['telegram_username'] if row.get('telegram_username') else '—')}\n"
            f"User ID <code>{row['user_id']}</code>\n\n"
            f"Terbaru {escape(current)} · {fmt_time(row['checked_at'])}\n"
            f"Terkonfirmasi {last_status} · {fmt_time(row['health_at'])} ({fresh(row['health_at'], freshness)})\n"
            f"Koneksi {escape(state(row['connection']))} · Sesi {escape(state(row['auth']))}\n"
            f"Global {yn(settings['automatic'])} · Individual {yn(row['enabled'])} · Kill Switch {yn(settings['kill_switch'])}\n"
            f"Interval {interval} menit {'(individual)' if row.get('interval_minutes') else '(global)'}\n"
            f"Pemeriksaan berikutnya {fmt_time(row.get('next_check')) if active else 'Dijeda'}"
        )
        if row["health"] == "normal":
            text += "\nNormal berarti pemeriksaan kesehatan tidak menemukan pembatasan yang dikenali. Status spam terpisah."
        if row["flood_until"] and datetime.fromisoformat(row["flood_until"]) > datetime.now(timezone.utc):
            text += f"\nTelegram menunda pemeriksaan sampai {fmt_time(row['flood_until'])}."
        if row["health"] == "frozen":
            text += (f"\n\nFrozen sejak {fmt_time(row['freeze_since'])} · Pertama terdeteksi {fmt_time(row['first_frozen_at'])}"
                     f"\nTenggat {fmt_time(row['freeze_until'])} · {count_down(row['freeze_until'])}")
        rows = [
            [button("🔄 Periksa sekarang", "checkone", account_id), button("🟡 Status spam", "spam", account_id)],
            [button("✏️ Ubah alias", "input", "alias", account_id), button("⏱ Interval", "input", "account_interval", account_id)],
            [button("Individual " + yn(not row["enabled"]), "toggle", account_id, int(not row["enabled"])),
             button("🔑 Autentikasi ulang", "enroll", account_id)],
            [button("📜 Riwayat", "history", account_id, 7, "all", 0), button("📝 Banding", "appeals", account_id, 0)],
            [button("📡 Channel & grup", "peers", account_id, 0), button("⋯ Lainnya", "accountmore", account_id)],
        ]
        if route == "accountmore":
            return panel("⋯ Detail lanjutan", f"Sumber pemeriksaan {escape(row['check_source'] or '—')}\n"
                         f"Sumber status {escape(row['health_source'] or '—')}\n"
                         "Cabut sesi Telegram melalui Pengaturan → Perangkat pada akun terkait.",
                         [[button("🗑 Hapus dari monitor", "remove", account_id)]],
                         button("⬅️ Kembali", "account", account_id))
        if row["health"] == "frozen" and link(row["appeal_url"]):
            rows.insert(0, [Button("Buka tautan banding Telegram", url=row["appeal_url"])])
        return panel("👤 Detail akun", text, rows, back)

    async def peer_page(self, route, args):
        item_id = int(args[0])
        call = self.client.call
        back = button("⬅️ Pilih akun", "accounts", "peers", 0)
        if route == "peers":
            return await self.selection_page("picks", ["p", str(item_id), "saved"])
        if route in {"discover", "choices", "peeradd"}:
            return await self.selection_page("pickdiscover" if route == "discover" else "picks",
                                             ["p", str(item_id), "add"])
        row = await call("peer", id=item_id)
        account_id = row["account_id"]
        peer_id = row["id"]
        back_to_peers = button("⬅️ Kembali", "peers", account_id, 0)
        if route == "peerremove":
            return panel("🗑 Hentikan monitoring", f"Hapus {escape(row['title'])} dan riwayatnya dari monitor?",
                         [[button("Hapus dari monitor", "peerdelete", peer_id)]], button("⬅️ Batal", "peer", peer_id))
        if route == "peerdelete":
            await call("peer_remove", id=peer_id)
            return panel("📡 Channel & Group Monitor", "Channel atau grup telah dihapus dari monitor.", back=back_to_peers)
        return panel("📡 Akses channel atau grup",
                     f"<b>{escape(row['title'])}</b> • <code>{row['chat_id']}</code>\n"
                     f"Terbaru {escape(state(row.get('latest_access')))} · {fmt_time(row['checked_at'])}\n"
                     f"Akses terkonfirmasi {escape(state(row['access']))} · {fmt_time(row.get('confirmed_at'))}\n"
                     f"Keanggotaan {escape(state(row['membership']))} · Peran {escape(state(row['role']))}",
                     [[button("🔄 Periksa akses", "peercheck", peer_id)],
                      [button("📜 Riwayat akses", "history", account_id, 7, "all", 0, peer_id),
                       button("🗑 Hapus", "peerremove", peer_id)]], back_to_peers)

    async def history_page(self, args):
        account_id, days, status, page = int(args[0]), int(args[1]), args[2], int(args[3])
        peer_id = int(args[4]) if len(args) > 4 else None
        fields = {"id": account_id, "days": days, "status": status, "page": page}
        if peer_id is not None:
            fields["peer"] = peer_id
        result = await self.client.call("history", **fields)
        lines = []
        for row in result["rows"]:
            duration = ""
            if row["ended_at"]:
                delta = datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(row["observed_at"])
                duration = f" • {int(delta.total_seconds() // 60)} mnt"
            lines.append(f"{fmt_time(row['observed_at'])} • {escape(row['dimension'])}{duration}\n"
                         f"{escape(state(row['previous']))} → {escape(state(row['status']))}\n"
                         f"{escape(row['source'])}")
        suffix = (peer_id,) if peer_id is not None else ()
        rows = [[button(label, "history", account_id, window, status, 0, *suffix)
                 for window, label in ((1, "24 jam"), (7, "7 hari"), (30, "30 hari"), (9999, "Semua"))]]
        frames = (("all", "Semua status"), ("normal", "Normal"), ("frozen", "Frozen"),
                  ("restricted", "Restricted"), ("deactivated", "Deactivated"), ("limited", "Spam"),
                  ("disconnected", "Disconnected"), ("invalid", "Session Invalid"), ("unknown", "Unknown"))
        if peer_id is None:
            rows.extend([button(label, "history", account_id, days, key, 0) for key, label in frames[i:i+3]]
                        for i in range(0, len(frames), 3))
        nav = []
        if page:
            nav.append(button("⬅️", "history", account_id, days, status, page - 1, *suffix))
        if result["next"]:
            nav.append(button("➡️", "history", account_id, days, status, page + 1, *suffix))
        if nav:
            rows.append(nav)
        back = button("⬅️ Kembali", "peer", peer_id) if peer_id else button("⬅️ Kembali", "account", account_id)
        return panel("📜 Riwayat Status", "\n\n".join(lines) or "Belum ada perubahan dalam periode ini.", rows, back)

    async def incident_page(self, route, args):
        call = self.client.call
        back = button("⬅️ Account Monitor", "home")
        if route == "incidents":
            category, page = args[0], int(args[1])
            result = await call("incidents", category=category, page=page)
            rows = [[button(label, "incidents", name, 0) for name, label in list(INCIDENTS.items())[i:i+3]]
                    for i in (0, 3)]
            for row in result["rows"]:
                mark = "✓" if row["resolved_at"] else "●"
                subject = row["alias"] or "Sistem"
                rows.append([button(f"{mark} #{row['id']} {row['kind']} · {subject}"[:64], "incident", row["id"])])
            rows += pagination(route, (category,), page, result["next"])
            return panel("⚠️ Incident Center", "Tidak ada incident di tampilan ini." if not result["rows"] else "", rows, back)

        item_id = int(args[0])
        row = await call("incident" if route == "incident" else "read", id=item_id)
        subject = row["alias"] or "Sistem"
        if row["title"]:
            subject += f" • {row['title']}"
        category = "Selesai" if row["resolved_at"] else INCIDENTS.get(row["category"], row["category"])
        text = (f"<b>#{item_id} {escape(category)}</b> • {escape(row['kind'])}\n"
                f"Komponen {escape(subject)}\n"
                f"Pertama terdeteksi {fmt_time(row['created_at'])}\nTerakhir diperiksa {fmt_time(row['checked_at'])}\n"
                f"Status {'Selesai' if row['resolved_at'] else 'Terbuka'} • {'Sudah dibaca' if row['read_at'] else 'Belum dibaca'}\n"
                f"Pulih {fmt_time(row['resolved_at'])}\nDetail {escape(row['detail'])}")
        rows = [] if row["read_at"] else [[button("✓ Tandai sudah dibaca", "read", item_id)]]
        return panel("⚠️ Detail incident", text, rows, button("⬅️ Incident Center", "incidents", "all", 0))

    async def appeal_page(self, route, args):
        account_id = int(args[0])
        if route == "appealstate":
            result = await self.client.call("appeal_update", id=account_id, status=args[1])
            page = 0
        else:
            page = int(args[1])
            result = await self.client.call("appeals", id=account_id, page=page)
        info = result["account"]
        history = "\n".join(f"{fmt_time(item['recorded_at'])} • {escape(APPEALS.get(item['status'], item['status']))}"
                            f" • Pengajuan {fmt_time(item['submitted_at'])}" for item in result["rows"])
        content = (f"<b>{escape(info['alias'])}</b>\n"
                   f"Tenggat resmi {fmt_time(info['freeze_until'])} • {count_down(info['freeze_until'])}\n\n"
                   f"{history or 'Belum ada catatan banding.'}\n\nStatus banding dicatat oleh admin berdasarkan informasi Telegram.")
        rows = [[button(title, "appealstate", account_id, key) for key, title in list(APPEALS.items())[i:i+3]]
                for i in (0, 3)]
        if link(info["appeal_url"]):
            rows.insert(0, [Button("Buka tautan banding Telegram", url=info["appeal_url"])])
        rows.append([button("🗓 Catat waktu pengajuan", "input", "appeal", account_id),
                     button("🔄 Periksa akun", "checkone", account_id)])
        rows += pagination("appeals", (account_id,), page, result["next"])
        return panel("📝 Appeal Manager", content, rows, button("⬅️ Account Monitor", "home"))

    async def settings_page(self, route, args):
        call = self.client.call
        home = button("⬅️ Account Monitor", "home")
        back = button("⬅️ Pengaturan", "settings")
        if route == "set":
            settings = await call("setting", key=args[0], value=int(args[1]))
        elif route == "mute":
            settings = await call("category", key=args[0], value=int(args[1]))
        else:
            settings = await call("settings")
        if route in {"notify", "mute"}:
            rows = [[button(f"{name} · {yn(key not in settings['muted'])}", "mute", key, int(key in settings["muted"]))]
                    for key, name in CATEGORIES.items()]
            rows.insert(0, [button(f"Instant Notification · {yn(settings['notifications'])}", "set",
                                   "notifications", int(not settings["notifications"]))])
            return panel("🔔 Notifikasi", "", rows, home)
        if route == "interval":
            rows = [[button(f"{value} menit", "set", "interval_minutes", value) for value in (5, 15, 30, 60)]]
            return panel("⏱ Interval monitoring",
                         "5–1440 menit.",
                         rows + [[button("Interval khusus", "input", "interval")]], back)
        if route == "time":
            return panel("🕗 Jam laporan harian",
                         f"Saat ini {settings['daily_hour']:02}:{settings['daily_minute']:02} WIB.",
                         [[button("Ubah jam", "input", "daily")]], back)
        if route == "kill":
            value = int(not settings["kill_switch"])
            return panel("⛔ Kill Switch", "Jeda pemeriksaan dan notifikasi Account Monitor." if value else "Lanjutkan Account Monitor.",
                         [[button("Aktifkan" if value else "Matikan", "set", "kill_switch", value)]], back)

        rows = [[button(f"{SETTINGS[key]} · {yn(settings[key])}", "set", key, int(not settings[key]))]
                for key in ("automatic", "channels", "notifications", "daily")]
        rows.extend((
            [button(f"Interval · {settings['interval_minutes']} menit", "interval"),
             button(f"Jam laporan · {settings['daily_hour']:02}:{settings['daily_minute']:02} WIB", "time")],
            [button(f"Kill Switch · {yn(settings['kill_switch'])}", "kill")],
        ))
        rows.append([button("🔔 Kategori notifikasi", "notify"), button("◈ Batas & retensi", "limits")])
        return panel("⚙️ Pengaturan", "Global mengatur jadwal otomatis. Pemeriksaan manual tetap tersedia saat Global OFF.", rows, home)

    async def notification_page(self, route, args):
        outcomes = {None: "Dalam antrean", "sent": "Terkirim", "failed": "Gagal", "retry": "Akan dicoba lagi",
                    "queue_full": "Antrean penuh", "expired": "Kedaluwarsa", "uncertain": "Pengiriman belum terkonfirmasi", "sending": "Mengirim"}
        if route == "notice":
            item = await self.client.call("notification_detail", id=int(args[0]))
            rows = []
            if item["peer_id"]:
                rows.append([button("Buka detail channel/grup", "peer", item["peer_id"])])
            elif item["account_id"]:
                rows.append([button("Buka detail akun", "account", item["account_id"])])
            return panel("🔔 Detail notifikasi",
                         f"{fmt_time(item['created_at'])} · {outcomes.get(item['outcome'], '—')}\n\n{escape(item['text'][:1600])}",
                         rows, button("⬅️ Kembali", "notices", args[1], args[2]))
        mode, page = args[0], int(args[1])
        result = await self.client.call("notification_list", mode=mode, page=page)
        rows = [[button(f"{CATEGORIES.get(row['category'], row['category'])} · {outcomes.get(row['outcome'], '—')}"[:62],
                        "notice", row["id"], mode, page)] for row in result["rows"]]
        rows += pagination("notices", (mode,), page, result["next"])
        rows.append([button("Semua", "notices", "all", 0), button("Kegagalan", "notices", "failed", 0),
                     button("⚙️ Pengaturan", "notify")])
        return panel("🔔 Pusat notifikasi", "Belum ada notifikasi." if not result["rows"] else "", rows,
                     button("⬅️ Account Monitor", "home"))

    def view_state(self, key):
        return self.selections.setdefault(key, {"query": None, "status": "all", "kind": "all", "role": "all",
                                                "page": 0, "selected": set()})

    async def selection_page(self, route, args):
        scope = args[0]
        account_id = int(args[1])
        mode = args[2]
        values = args[3:]
        context_key = (scope, account_id, mode)
        state_view = self.view_state(context_key)
        selected = state_view["selected"]
        accounts = scope == "a"
        fields = {"query": state_view["query"]}
        action = "accounts" if accounts else "select_peers"
        if accounts:
            fields["status"] = state_view["status"]
            list_route = "picks"
            back = button("⬅️ Account Monitor", "home")
        else:
            fields.update(id=account_id, mode=mode, kind=state_view["kind"], role=state_view["role"])
            list_route = "picks"
            back = button("⬅️ Channel & grup" if mode == "add" else "⬅️ Pilih akun",
                          "peers" if mode == "add" else "accounts",
                          *((account_id, 0) if mode == "add" else ("peers", 0)))
        target = (scope, account_id, mode)
        feedback = ""
        if route == "picks" and values:
            state_view["page"] = int(values[0])
        if route == "pick":
            item_id = int(values[0])
            if item_id in selected:
                selected.remove(item_id)
            else:
                selected.add(item_id)
        if route == "pickclear":
            selected.clear()
        if route == "pickall":
            data = await self.client.call(action, select_all=True, **fields)
            available = list(dict.fromkeys([*sorted(selected), *data["ids"]]))
            limit = data["limit"] if not accounts and mode == "add" else 120 if not accounts else 50
            if len(available) > limit:
                raise MonitorInput(f"Hasil filter melebihi {limit} item. Persempit pencarian untuk memilih semuanya.")
            selected.update(available)
        if route == "pickfilter":
            name, value = values
            options = {"status": ACCOUNT_FILTERS, "kind": PEER_TYPES, "role": PEER_ROLES}
            if name not in options or value not in options[name]:
                raise MonitorInput("Filter tidak tersedia.")
            state_view.update({name: value, "page": 0})
            fields[name] = value
        if route == "pickfilters":
            parts = ["status"] if accounts else ["kind", "role"]
            options = {"status": ACCOUNT_FILTERS, "kind": PEER_TYPES, "role": PEER_ROLES}
            rows = []
            for name in parts:
                found = list(options[name].items())
                for offset in range(0, len(found), 2):
                    rows.append([button(("✓ " if state_view[name] == key else "") + value,
                                        "pickfilter", *target, name, key) for key, value in found[offset:offset+2]])
            return panel("Filter", f"{len(selected)} pilihan tetap tersimpan.", rows, button("⬅️ Kembali", list_route, *target))
        if route == "picktoggle":
            if not selected:
                raise MonitorInput("Centang akun terlebih dahulu.")
            result = await self.client.call("batch_accounts", ids=sorted(selected), value=int(values[0]))
            feedback = f"✅ Pengaturan {result['updated']} akun diperbarui.\n"
            selected.clear()
        if route == "picksave":
            if not selected:
                raise MonitorInput("Centang channel atau grup terlebih dahulu.")
            result = await self.client.call("save_peers", id=account_id, ids=sorted(selected))
            feedback = f"✅ {result['added']} item ditambahkan.\n"
            selected.clear()
        if route == "pickremove":
            if not selected:
                raise MonitorInput("Centang item terlebih dahulu.")
            self.confirm_remove = (account_id, frozenset(selected))
            return panel("🗑 Hapus dari pemantauan",
                         f"Hapus {len(selected)} item beserta riwayat pemantauannya? Keanggotaan Telegram tetap aktif.",
                         [[button("Ya, hapus dari monitor", "pickconfirm", *target)]],
                         button("⬅️ Batal", list_route, *target))
        if route == "pickconfirm":
            confirmation = self.confirm_remove
            if confirmation != (account_id, frozenset(selected)):
                raise MonitorInput("Pilihan berubah. Ulangi konfirmasi.")
            self.confirm_remove = None
            result = await self.client.call("remove_peers", id=account_id, ids=sorted(selected))
            feedback = f"✅ {result['removed']} item dihapus dari monitor.\n"
            selected.clear()
            state_view["page"] = 0
        if route == "pickdiscover":
            result = await self.client.call("discover", id=account_id)
            if result["queued"]:
                feedback = "⏳ Telegram sedang memuat daftar. Halaman akan diperbarui otomatis.\n"
        page = state_view["page"]
        result = await self.client.call(action, page=page, **fields)
        if page and not result["rows"]:
            page = state_view["page"] = 0
            result = await self.client.call(action, page=0, **fields)
        rows = []
        for row in result["rows"]:
            item_id = row["id"]
            checked = "✓" if item_id in selected else "☐"
            title = row["alias"] if accounts else row["title"]
            info = state(row.get("display_status")) if accounts else (
                "dipantau" if row.get("monitored") else state(row.get("role")))
            label = f"{info} · {title}"[:59]
            detail = button(label, "account" if accounts else "peer", item_id)
            if accounts or mode == "saved":
                rows.append([button(checked, "pick", *target, item_id), detail])
            elif row.get("monitored"):
                rows.append([button(f"✓ {label}", "picks", *target)])
            else:
                rows.append([button(f"{checked} {label}", "pick", *target, item_id)])
        rows += pagination(list_route, target, page, result["next"])
        rows.append([button("🔎 Cari", "input", "find", *target),
                     button("◈ Filter", "pickfilters", *target)])
        if state_view["query"]:
            rows.append([button("Hapus pencarian", "picksearchclear", *target)])
        if (result["rows"] or selected) and not (accounts and mode == "peers"):
            rows.append([button("Pilih semua hasil", "pickall", *target),
                         button("Batalkan semua", "pickclear", *target)])
        if accounts and mode == "peers":
            rows = [[button(row["alias"][:58], "peers", row["id"], 0)] for row in result["rows"]] + rows[len(result["rows"]):]
        if accounts and mode == "account":
            if selected:
                rows.extend([[button(f"🔄 Periksa {len(selected)} akun", "runpick", *target)],
                             [button("Monitor ON", "picktoggle", *target, 1),
                              button("Monitor OFF", "picktoggle", *target, 0)]])
            rows.append([button("➕ Tambah akun", "enroll")])
        elif not accounts and mode == "add":
            rows.append([button("🔄 Muat daftar Telegram", "pickdiscover", *target)])
            if selected:
                rows.append([button(f"Simpan {len(selected)} pilihan", "picksave", *target)])
        elif not accounts:
            if selected:
                rows.append([button(f"Periksa {len(selected)}", "runpick", *target),
                             button("Hapus pilihan", "pickremove", *target)])
            rows.append([button("➕ Pilih dari Telegram", "picks", "p", account_id, "add")])
        summary = " • ".join(str(x) for x in (
            f"Halaman {page+1}", f"{len(selected)} dipilih",
            f"cari «{escape(state_view['query'])}»" if state_view["query"] else None,
            ACCOUNT_FILTERS.get(state_view["status"]) if accounts else
                f"{PEER_TYPES[state_view['kind']]} / {PEER_ROLES[state_view['role']]}",
        ) if x)
        if not accounts:
            summary += f"\nDipantau {result['saved']} dari batas {result['limit']}"
            if mode == "add" and result["pending"]:
                summary += "\n⏳ Menunggu daftar Telegram."
                if result["flood_until"]:
                    summary += f" Tertunda sampai {fmt_time(result['flood_until'])}."
            elif mode == "add" and not result["available"]:
                summary += "\nMuat daftar dari akun Telegram."
        if not result["rows"] and (accounts or mode == "saved" or result["available"]):
            summary += "\nTidak ada hasil yang cocok."
        title = "👥 Kelola Akun" if accounts and mode == "account" else "👥 Pilih akun" if accounts else (
            "➕ Pilih channel & grup" if mode == "add" else "📡 Channel & grup dipantau")
        return panel(title, feedback + summary, rows, back)

    async def run_page(self, args, data=None):
        run, page = int(args[0]), int(args[1])
        if data is None:
            data = await self.client.call("run_status", id=run, page=page)
        complete = data["success"] + data["failure"]
        heading = "⏳ Pemeriksaan berlangsung" if data["pending"] else "✅ Pemeriksaan selesai"
        if data.get("paused") and data["pending"]:
            heading = "⏸ Pemeriksaan dijeda oleh Kill Switch"
        text = (f"<b>{complete} / {data['total']} selesai</b>\n"
                f"Berhasil {data['success']} · Gagal {data['failure']}\n"
                f"Menunggu {data['pending'] - data['deferred']} · Tertunda {data['deferred']}")
        lines = []
        rows = []
        for row in data["rows"]:
            reason = CHECK_REASONS.get(row["detail"], state(row["detail"]))
            if row["outcome"] is None:
                flood = row["flood_until"]
                reason = f"Tertunda sampai {fmt_time(flood)}" if flood else "Dalam antrean"
            lines.append(f"<b>{escape(row['title'][:65])}</b>\n{escape(reason)}")
        if data["rows"] and data["total"] == 1:
            row = data["rows"][0]
            rows.append([button("Buka detail", "account" if data["kind"] == "account" else "peer", row["target_id"])])
        rows += pagination("run", (run,), page, data["next"])
        if data["pending"]:
            rows.append([button("🔄 Lanjutkan pembaruan", "run", run, page)])
        return panel(heading, text + ("\n\n" + "\n\n".join(lines) if lines else ""), rows,
                     button("⬅️ Kembali", "runback", run))

    async def refresh_panel(self, query, route, args, version):
        for _ in range(30):
            await asyncio.sleep(4)
            if self.view_version != version:
                return
            try:
                if route == "run":
                    result = await self.client.call("run_status", id=int(args[0]), page=int(args[1]))
                    pending = result["pending"] and not result.get("paused")
                elif route in {"picks", "pickdiscover"}:
                    row = await self.client.call("account", id=int(args[1]))
                    pending = row["discover"] and not row["flood_until"]
                else:
                    row = await self.client.call("account", id=int(args[0]))
                    pending = row["spam_pending"] and not row["flood_until"]
                if self.view_version != version:
                    return
                if route == "run" or not pending:
                    if route == "run":
                        text, markup = await self.run_page(args, result)
                    else:
                        text, markup = await self.render("picks" if route == "pickdiscover" else route, args)
                    if self.view_version != version:
                        return
                    await self.edit(query, text, markup)
                if not pending:
                    return
            except (MonitorUnavailable, MonitorInput, MonitorPaused, TelegramError):
                return

    def set_prompt(self, prompt, route, args, query):
        self.prompt = (prompt.message_id, route, args, query, self.view_version, monotonic() + 300)

    async def request_input(self, query, args):
        field, *target = args
        prompts = {
            "find": "Balas dengan nama atau ID yang ingin dicari.",
            "alias": "Balas dengan alias baru, maksimal 48 karakter.",
            "qr": "Balas dengan alias untuk akun baru. Kode Telegram dan 2FA hanya di formulir aman.",
            "interval": "Balas dengan interval dalam menit, 5–1440.",
            "account_interval": "Balas dengan 5–1440 menit, atau 0 untuk mengikuti interval global.",
            "retention": "Balas dengan jumlah hari penyimpanan, 30–365.",
            "clients": "Balas dengan batas client, 1–12.",
            "concurrency": "Balas dengan pemeriksaan paralel, 1–4.",
            "peer_limit": "Balas dengan batas channel dan grup per akun, 10–120.",
            "daily": "Balas dengan jam laporan. Gunakan format HH:MM WIB.",
            "appeal": "Balas dengan waktu pengajuan berdasarkan informasi Telegram. Format YYYY-MM-DD HH:MM WIB.",
        }
        if field not in prompts:
            raise MonitorInput("Formulir tidak tersedia.")
        message = await query.message.reply_text(prompts[field], reply_markup=ForceReply())
        self.set_prompt(message, field, target, query)

    async def reply_input(self, update, context):
        if not self.authorized(update) or not self.prompt:
            return
        _, field, args, query, version, expires = self.prompt
        value = update.message.text.strip()
        if self.view_version != version or monotonic() > expires:
            self.prompt = None
            return
        try:
            if field == "qr":
                await self.start_qr(context.bot, alias=value)
                self.prompt = None
                for message_id in (update.message.message_id, update.message.reply_to_message.message_id):
                    await self.delete_enrollment_message(context.bot, message_id)
                return
            if field == "find":
                key = (args[0], int(args[1]), args[2])
                if len(value) > 80 or not value:
                    raise MonitorInput("Kata pencarian harus 1–80 karakter.")
                self.view_state(key).update(query=value, page=0)
                route, target = "picks", args
            elif field in {"alias", "account_interval"}:
                account_id = int(args[0])
                fields = {"alias": value} if field == "alias" else {"value": int(value)}
                await self.client.call(field, id=account_id, **fields)
                route, target = "account", args
            elif field == "appeal":
                await self.client.call("appeal_update", id=int(args[0]), status="submitted", submitted_at=value)
                route, target = "appeals", [args[0], "0"]
            elif field == "daily":
                hour, minute = map(int, value.split(":"))
                await self.client.call("report_time", hour=hour, minute=minute)
                route, target = "settings", []
            else:
                name = {"interval": "interval_minutes", "retention": "retention_days", "clients": "max_clients"}.get(field, field)
                await self.client.call("setting", key=name, value=int(value))
                route, target = "settings", []
            text, markup = await self.render(route, target)
            await self.edit(query, text, markup)
        except (MonitorUnavailable, MonitorInput, MonitorPaused) as exc:
            await update.message.reply_text(str(exc))
            return
        except (ValueError, TypeError):
            await update.message.reply_text("Format belum sesuai. Balas lagi pada pesan permintaan input.")
            return
        self.prompt = None
        for message_id in (update.message.message_id, update.message.reply_to_message.message_id):
            await self.delete_enrollment_message(context.bot, message_id)

    def enrollment_url(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise MonitorUnavailable(UNAVAILABLE)
        return self.client.url.removesuffix("/v1/admin") + "/auth#ticket=" + token

    async def start_qr(self, bot, **fields):
        async with self.qr_start_lock:
            if self.qr_runner is not None and not self.qr_runner.done():
                raise MonitorInput("Selesaikan atau batalkan QR yang masih aktif.")
            result = await self.client.call("enroll_qr_start", **fields)
            key = result.get("flow")
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", key):
                raise MonitorUnavailable(UNAVAILABLE)
            self.qr_flow = key
            self.qr_runner = asyncio.create_task(self.watch_enrollment(bot, key))

    async def watch_enrollment(self, bot, key):
        photo_id = prompt_id = None
        revision = 0
        last_seconds = None
        retry_count = 0
        deadline = monotonic() + 370
        cancel = button("Batalkan login", "qr_cancel", key)
        markup = InlineKeyboardMarkup([[cancel]])
        caption = ("Pindai lewat Telegram → Pengaturan → Perangkat → Hubungkan Perangkat Desktop. "
                   "QR diperbarui otomatis.")
        final = "Sesi QR berakhir. Mulai lagi dari Panel Admin."
        terminal = False
        try:
            while monotonic() < deadline:
                try:
                    reply = await self.client.call("enroll_qr_status", flow=key, revision=revision)
                    retry_count = 0
                except MonitorUnavailable:
                    retry_count += 1
                    if retry_count >= 3:
                        break
                    await asyncio.sleep(3)
                    continue
                phase = reply.get("phase")
                if phase in {"done", "failed", "cancelled"}:
                    terminal = True
                    final = "✅ Akun tersimpan." if phase == "done" else reply.get("message") or final
                    break
                if phase == "qr" and reply.get("image"):
                    photo = BytesIO(base64.b64decode(reply["image"], validate=True))
                    photo.name = "qr.png"
                    text = f"{caption}\n\nMasa berlaku QR {reply['qr_seconds']} detik."
                    if photo_id is None:
                        message = await bot.send_photo(self.owner, photo, caption=text, reply_markup=markup,
                                                       protect_content=True, **self.background_options(bot))
                        photo_id = message.message_id
                    else:
                        await bot.edit_message_media(chat_id=self.owner, message_id=photo_id,
                                                     media=InputMediaPhoto(photo, caption=text), reply_markup=markup,
                                                     **self.background_options(bot))
                    revision = reply["qr_revision"]
                    last_seconds = reply["qr_seconds"]
                elif phase == "qr" and photo_id is not None:
                    seconds = reply["qr_seconds"]
                    if last_seconds is None or last_seconds - seconds >= 6:
                        await bot.edit_message_caption(
                            chat_id=self.owner, message_id=photo_id,
                            caption=f"{caption}\n\nMasa berlaku QR {seconds} detik.",
                            reply_markup=markup, **self.background_options(bot),
                        )
                        last_seconds = seconds
                if phase == "password":
                    if photo_id is not None:
                        await self.delete_enrollment_message(bot, photo_id)
                        photo_id = None
                    if prompt_id is None and reply.get("password_ticket"):
                        secure = Button("Buka input 2FA aman", url=self.enrollment_url(reply["password_ticket"]))
                        notice = await bot.send_message(
                            self.owner, "Telegram meminta password 2FA. Buka formulir aman untuk menyelesaikan login.",
                            protect_content=True, reply_markup=InlineKeyboardMarkup([[secure], [cancel]]),
                            **self.background_options(bot),
                        )
                        prompt_id = notice.message_id
                await asyncio.sleep(3)
        except (MonitorInput, MonitorPaused, MonitorUnavailable, TelegramError, KeyError, ValueError, TypeError) as exc:
            logger.warning("QR login berhenti (%s).", type(exc).__name__)
        finally:
            for message_id in (photo_id, prompt_id):
                await self.delete_enrollment_message(bot, message_id)
            if not terminal:
                try:
                    await self.client.call("enroll_qr_cancel", flow=key)
                except (MonitorInput, MonitorPaused, MonitorUnavailable):
                    pass
            self.qr_flow = None
        try:
            await bot.send_message(self.owner, final, protect_content=True,
                                   reply_markup=InlineKeyboardMarkup([[button("Kelola Akun", "accounts", "account", 0)]]),
                                   **self.background_options(bot))
        except TelegramError:
            pass

    async def delete_enrollment_message(self, bot, message_id):
        if message_id is not None:
            try:
                await bot.delete_message(self.owner, message_id, **self.background_options(bot))
            except TelegramError:
                pass

    async def message(self, update, context):
        if not self.authorized(update):
            return
        parts = (update.effective_message.text or "").split(maxsplit=2)
        try:
            if len(parts) != 3:
                raise MonitorInput("Buka menu Account Monitor untuk melihat format perintah.")
            action, values = parts[1:]
            if action == "qr":
                await self.start_qr(context.bot, alias=values.strip())
                return
            if action in {"alias", "spam", "appeal"}:
                item, value = values.split(maxsplit=1)
                account_id = int(item)
                if action == "alias":
                    await self.client.call("alias", id=account_id, alias=value)
                if action == "spam":
                    fields = [part.strip() or None for part in value.split("|")]
                    if len(fields) > 3:
                        raise ValueError
                    fields += [None] * (3 - len(fields))
                    await self.client.call("spam", id=account_id, status=fields[0], kind=fields[1], until=fields[2])
                if action == "appeal":
                    status, submitted_at = value.split(maxsplit=1)
                    await self.client.call("appeal_update", id=account_id, status=status, submitted_at=submitted_at)
            elif action == "daily":
                hour, minute = (int(number) for number in values.split(":"))
                await self.client.call("report_time", hour=hour, minute=minute)
            else:
                setting = {"interval": "interval_minutes", "retention": "retention_days", "concurrency": "concurrency",
                           "clients": "max_clients"}.get(action)
                if setting is None:
                    raise ValueError
                await self.client.call("setting", key=setting, value=int(values))
            text = "✅ Pengaturan Account Monitor tersimpan."
        except (MonitorInput, MonitorPaused, MonitorUnavailable) as exc:
            text = str(exc)
        except (TypeError, ValueError):
            text = "Format tidak valid. Lihat format perintah di menu Account Monitor."
        with self.scope("admin"):
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup([
                [button("⬅️ Account Monitor", "home")]
            ]))

    def start(self, bot):
        self.task = asyncio.create_task(self._notifier(bot), name="account-monitor-notifications")

    async def stop(self):
        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()
            await asyncio.gather(self.refresh_task, return_exceptions=True)
        if self.qr_runner is not None and not self.qr_runner.done():
            self.qr_runner.cancel()
            await asyncio.gather(self.qr_runner, return_exceptions=True)
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await self.client.close()

    async def send(self, bot, text, *, account_id=None, peer_id=None):
        rows = [[button("Buka detail", "peer" if peer_id else "account", peer_id or account_id)]] if peer_id or account_id else []
        with self.scope("admin"):
            await bot.send_message(
                self.owner, text, reply_markup=InlineKeyboardMarkup(rows) if rows else None, protect_content=True, disable_web_page_preview=True,
                read_timeout=4, write_timeout=2, connect_timeout=2, pool_timeout=1,
                **self.background_options(bot),
            )

    async def _send_notice(self, bot):
        if self.active_notice is None:
            notice = (await self.client.call("lease")).get("notice")
            if notice is None:
                return
            fields = {"id": notice["id"], "token": notice["lease_token"]}
            if not (await self.client.call("begin", **fields)).get("ok"):
                return
            outcome, delay = "sent", 0
            try:
                async with asyncio.timeout(12):
                    await self.send(bot, notice["text"], account_id=notice.get("account_id"), peer_id=notice.get("peer_id"))
            except RetryAfter as exc:
                outcome, delay = "retry", min(3600, int(exc.retry_after) + 2)
            except (Forbidden, BadRequest):
                outcome = "failed"
            except (TelegramError, TimeoutError):
                outcome = "uncertain"
            self.active_notice = {**fields, "outcome": outcome, "delay": delay}
        await self.client.call("finish", **self.active_notice)
        self.active_notice = None

    async def _system_check(self, bot):
        self.last_health = monotonic()
        try:
            system = await self.client.call("system")
        except MonitorUnavailable:
            self.offline_count += 1
            if self.notify_system and self.offline_count >= 3 and not self.offline_announced:
                self.offline_announced = True
                await self.send(bot, "🖥 Service Account Monitor sedang tidak merespons.")
            return
        self.notify_system = system["notify_system"] and system["scheduler"] != "paused"
        self.offline_count = 0
        if self.offline_announced and self.notify_system:
            await self.send(bot, "🖥 Service Account Monitor kembali merespons.")
        self.offline_announced = False

    async def _notifier(self, bot):
        await asyncio.sleep(3)
        while True:
            try:
                if monotonic() - self.last_health >= 30:
                    await self._system_check(bot)
                if self.offline_count == 0:
                    await self._send_notice(bot)
            except (MonitorInput, MonitorUnavailable, MonitorPaused, TelegramError, TimeoutError):
                pass
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Respons notifikasi Account Monitor tidak valid (%s).", type(exc).__name__)
            await asyncio.sleep(8)
