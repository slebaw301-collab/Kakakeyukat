import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from html import escape
from time import monotonic, time
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler


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
    "peer": "Akses channel atau grup", "recovered": "Akun kembali normal",
    "system": "Gangguan service", "appeal": "Tenggat banding", "daily": "Laporan harian",
}
APPEALS = {
    "submitted": "Diajukan", "pending": "Menunggu", "accepted": "Diterima",
    "rejected": "Ditolak", "withdrawn": "Ditarik",
}
INCIDENTS = {
    "all": "Semua", "critical": "Critical", "warning": "Warning", "unknown": "Unknown", "resolved": "Resolved",
}
SETTINGS = {
    "automatic": "Automatic Monitoring", "channels": "Channel Monitor",
    "notifications": "Instant Notification", "daily": "Daily Report",
    "interval_minutes": "Interval monitoring", "retention_days": "Retensi riwayat",
    "concurrency": "Pemeriksaan paralel", "max_clients": "Batas client", "kill_switch": "Kill Switch",
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
    value = row["health"]
    spam_at, until = row["spam_at"], row["spam_until"]
    now = datetime.now(timezone.utc)
    if value == "normal" and row["spam_status"] == "limited" and spam_at and (
        (now - datetime.fromisoformat(spam_at)).total_seconds() < 604800
        and (until is None or datetime.fromisoformat(until) > now)
    ):
        value = "spam_limited"
    return state(value)


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
    header = f"<b>{title}</b>\n========================"
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
        try:
            text, markup = await self.render(route, args)
        except (MonitorUnavailable, MonitorInput, MonitorPaused) as exc:
            text, markup = panel("📡 Account Monitor", escape(str(exc)),
                                 [[button("🖥 Status Sistem", "system"), button("⚙️ Pengaturan", "settings")]],
                                 back=Button("⬅️ Kembali", callback_data=BACK))
        except (ValueError, TypeError, KeyError, IndexError):
            text, markup = panel("📡 Account Monitor", "Menu sudah berubah. Buka ulang Account Monitor.",
                                 back=button("⬅️ Account Monitor", "home"))
        with self.scope("admin"):
            try:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            except BadRequest as exc:
                if "Message is not modified" not in str(exc):
                    raise

    async def render(self, route, args):
        call = self.client.call
        home = button("⬅️ Account Monitor", "home")
        arg = args[0] if args else None

        if route == "home":
            rows = [
                [button("👥 Kelola Akun", "accounts", "account", 0), button("🔄 Check All Accounts", "checkall")],
                [button("⚠️ Incident Center", "incidents", "all", 0)],
                [button("📡 Channel & Group Monitor", "accounts", "peers", 0)],
                [button("📜 Riwayat Status", "accounts", "history", 0), button("📝 Appeal Manager", "accounts", "appeals", 0)],
                [button("🔔 Notifikasi", "notify"), button("🖥 Status Sistem", "system")],
                [button("⚙️ Pengaturan", "settings")],
            ]
            try:
                summary = await call("summary")
                counts = " • ".join(f"{escape(state(key))} {value}" for key, value in summary["counts"].items())
                text = f"Total akun <b>{summary['total']}</b>\n{counts or 'Belum ada akun'}\nTerakhir diperiksa {fmt_time(summary['latest'])}"
            except (MonitorUnavailable, MonitorPaused) as exc:
                text = escape(str(exc))
            return panel("📡 Account Monitor", text, rows, Button("⬅️ Kembali", callback_data=BACK))

        if route == "checkall":
            result = await call("check")
            return panel("🔄 Check All Accounts", f"{result['queued']} akun dijadwalkan. Akun yang sedang FloodWait menunggu giliran.", back=home)

        if route == "accounts":
            category, page = arg, int(args[1])
            result = await call("accounts", page=page)
            rows = []
            for item in result["rows"]:
                suffix = () if category == "account" else (0,) if category in {"peers", "appeals"} else (7, "all", 0)
                rows.append([button(f"{account_line(item)} · {item['alias']}"[:62], category, item["id"], *suffix)])
            rows += pagination(route, (category,), page, result["next"])
            if category == "account":
                rows.append([button("➕ Tambah akun", "enroll")])
            return panel("👥 Pilih akun", "Belum ada akun terdaftar." if not result["rows"] else "", rows, home)

        if route == "enroll":
            fields = {"id": int(arg)} if arg is not None else {}
            ssh = "python -m monitor enroll --alias &quot;Akun utama&quot;" if arg is None else (
                f"python -m monitor enroll --replace {int(arg)} --alias &quot;Akun yang sama&quot;")
            rows = []
            try:
                ticket = await call("enroll_ticket", **fields)
                token = ticket.get("token", "")
                if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                    raise MonitorUnavailable(UNAVAILABLE)
                origin = self.client.url.removesuffix("/v1/admin")
                rows = [[Button("Buka autentikasi aman", url=f"{origin}/auth#ticket={token}")]]
                text = "Pilih login nomor telepon atau QR. Tautan privat berlaku 2 menit dan sekali pakai."
            except (MonitorUnavailable, MonitorInput, MonitorPaused):
                text = "Autentikasi web belum tersedia. Gunakan Railway SSH atau coba lagi."
            return panel("🔐 Hubungkan akun Telegram", f"{text}\n\nCadangan lewat Railway SSH\n<code>{ssh}</code>", rows,
                         back=button("⬅️ Kelola Akun", "accounts", "account", 0))

        if route in {"account", "checkone", "toggle", "remove", "confirmremove", "alias", "spam"}:
            return await self.account_page(route, args)

        if route in {"peers", "peer", "discover", "choices", "peeradd", "peerremove", "peerdelete"}:
            return await self.peer_page(route, args)

        if route == "history":
            return await self.history_page(args)

        if route in {"incidents", "incident", "read"}:
            return await self.incident_page(route, args)

        if route in {"appeals", "appealstate"}:
            return await self.appeal_page(route, args)

        if route in {"settings", "set", "notify", "mute", "interval", "time", "kill"}:
            return await self.settings_page(route, args)

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
        notice = ""
        if route == "checkone":
            checked = await call("check", id=account_id)
            notice = "Pemeriksaan dijadwalkan.\n\n" if checked["queued"] else "Pemeriksaan belum bisa dijadwalkan.\n\n"
        if route == "toggle":
            await call("toggle", id=account_id, value=int(args[1]))
        if route == "remove":
            return panel("🗑 Hapus akun",
                         "Session, channel atau grup, incident, riwayat status, dan catatan banding akun ini akan dihapus dari monitor. "
                         "Untuk mencabut sesi di Telegram, buka Pengaturan → Perangkat.",
                         [[button("Hapus dari monitor", "confirmremove", account_id)]], button("⬅️ Batal", "account", account_id))
        if route == "confirmremove":
            await call("remove", id=account_id)
            return panel("👥 Kelola Akun", "Akun telah dihapus dari monitor.", back=back)
        row = await call("account", id=account_id)
        if route == "alias":
            return panel("✏️ Ubah alias", f"Kirim <code>/monitor_input alias {account_id} Alias baru</code>.",
                         back=button("⬅️ Kembali", "account", account_id))
        if route == "spam":
            return panel(
                "🟡 Hasil @SpamBot",
                f"Periksa dari sesi {escape(row['alias'])}, lalu catat hasil yang dikonfirmasi.\n\n"
                f"<code>/monitor_input spam {account_id} limited</code>\n"
                f"<code>/monitor_input spam {account_id} limited | jenis | YYYY-MM-DD HH:MM</code>\n"
                f"<code>/monitor_input spam {account_id} clear</code>\n"
                f"<code>/monitor_input spam {account_id} unknown</code>",
                [[Button("Buka @SpamBot", url="https://t.me/SpamBot")]], button("⬅️ Kembali", "account", account_id),
            )

        settings = await call("settings")
        freshness = max(7200 if row["health"] == "frozen" else 0, settings["interval_minutes"] * 120)
        text = (
            f"{notice}<b>{escape(row['alias'])}</b> • ID <code>{row['user_id']}</code>\n"
            f"Akun {escape(account_line(row))} • Koneksi {escape(state(row['connection']))}\n"
            f"Sesi {escape(state(row['auth']))} • Monitor {yn(row['enabled'])}\n"
            f"Status akun diperbarui {fmt_time(row['health_at'])} ({fresh(row['health_at'], freshness)})\n"
            f"Pemeriksaan terakhir {fmt_time(row['checked_at'])} • {escape(row['check_source'] or '—')}\n"
            f"Sumber status akun {escape(row['health_source'] or '—')}"
        )
        if row["flood_until"] and datetime.fromisoformat(row["flood_until"]) > datetime.now(timezone.utc):
            text += f"\nFloodWait sampai {fmt_time(row['flood_until'])}"
        if row["health"] == "frozen":
            text += (f"\n\nFrozen sejak {fmt_time(row['freeze_since'])} • Pertama terdeteksi {fmt_time(row['first_frozen_at'])}"
                     f"\nTenggat {fmt_time(row['freeze_until'])} • {count_down(row['freeze_until'])}")
        text += (f"\n\n@SpamBot {escape(state(row['spam_status']))} • {fmt_time(row['spam_at'])}"
                 f" ({fresh(row['spam_at'], 604800)})"
                 f"\nJenis {escape(row['spam_kind'] or '—')} • Sampai {fmt_time(row['spam_until'])}")
        rows = [
            [button("🔄 Periksa status & koneksi", "checkone", account_id)],
            [button("✏️ Alias", "alias", account_id), button("Monitor " + yn(not row["enabled"]), "toggle", account_id, int(not row["enabled"]))],
            [button("🔑 Autentikasi ulang", "enroll", account_id), button("🟡 Hasil @SpamBot", "spam", account_id)],
            [button("📜 Riwayat", "history", account_id, 7, "all", 0), button("📝 Banding", "appeals", account_id, 0)],
            [button("📡 Channel & grup", "peers", account_id, 0), button("🗑 Hapus", "remove", account_id)],
        ]
        if row["health"] == "frozen" and link(row["appeal_url"]):
            rows.insert(0, [Button("Buka tautan banding Telegram", url=row["appeal_url"])])
        return panel("👤 Detail akun", text, rows, back)

    async def peer_page(self, route, args):
        item_id = int(args[0])
        call = self.client.call
        back = button("⬅️ Pilih akun", "accounts", "peers", 0)
        if route == "peers":
            page = int(args[1])
            result = await call("peers", id=item_id, page=page)
            rows = [[button(f"{state(row['access'])} · {row['title']}"[:64], "peer", row["id"])] for row in result["rows"]]
            rows += pagination(route, (item_id,), page, result["next"])
            rows.append([button("➕ Pilih channel atau grup", "discover", item_id)])
            return panel("📡 Channel & Group Monitor", "Belum ada yang dipantau." if not result["rows"] else "", rows, back)
        if route in {"discover", "choices"}:
            if route == "discover":
                queued = await call("discover", id=item_id)
                return panel("📡 Pilih channel atau grup",
                             "Daftar sedang dimuat." if queued["queued"] else "Akun belum bisa diperiksa.",
                             [[button("🔄 Tampilkan daftar", "choices", item_id, 0)]], button("⬅️ Kembali", "peers", item_id, 0))
            page = int(args[1])
            result = await call("choices", id=item_id, page=page)
            rows = [[button(row["title"][:64], "peeradd", item_id, row["index"], row["chat_id"])] for row in result["rows"]]
            rows += pagination("choices", (item_id,), page, result["next"])
            text = ""
            if result["pending"]:
                text = "Pemeriksaan masih dalam antrean."
                if result.get("flood_until"):
                    text += f" FloodWait sampai {fmt_time(result['flood_until'])}."
                rows.append([button("🔄 Perbarui", "choices", item_id, page)])
            elif not result["available"]:
                text = "Daftar belum tersedia atau sudah kedaluwarsa."
                rows.append([button("🔄 Muat ulang dari Telegram", "discover", item_id)])
            elif not result["rows"]:
                text = "Tidak ada channel atau grup di halaman ini."
            return panel("📡 Pilih channel atau grup", text, rows, button("⬅️ Kembali", "peers", item_id, 0))

        if route == "peeradd":
            row = await call("peer_add", id=item_id, index=int(args[1]), chat=int(args[2]))
        else:
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
                     f"Akses {escape(state(row['access']))} • Keanggotaan {escape(state(row['membership']))}\n"
                     f"Peran {escape(state(row['role']))}\n"
                     f"Sumber {escape(row['source'] or '—')}\nTerakhir diperiksa {fmt_time(row['checked_at'])}",
                     [[button("📜 Riwayat akses", "history", account_id, 7, "all", 0, peer_id),
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
        category = "Resolved" if row["resolved_at"] else INCIDENTS.get(row["category"], row["category"])
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
                   f"{history or 'Belum ada catatan banding.'}\n\nStatus banding dicatat oleh admin.\n"
                   f"Untuk mencatat tanggal sebelumnya, kirim "
                   f"<code>/monitor_input appeal {account_id} submitted YYYY-MM-DD HH:MM</code> (WIB).")
        rows = [[button(title, "appealstate", account_id, key) for key, title in list(APPEALS.items())[i:i+3]]
                for i in (0, 3)]
        if link(info["appeal_url"]):
            rows.insert(0, [Button("Buka tautan banding Telegram", url=info["appeal_url"])])
        rows.append([button("🔄 Periksa status akun", "checkone", account_id)])
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
                         "Untuk interval 5–1440 menit, kirim <code>/monitor_input interval 20</code>.",
                         rows, back)
        if route == "time":
            return panel("🕗 Jam laporan harian",
                         f"Saat ini {settings['daily_hour']:02}:{settings['daily_minute']:02} WIB. "
                         "Untuk mengubahnya, kirim <code>/monitor_input daily 08:30</code>.",
                         back=back)
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
        text = (f"Retensi {settings['retention_days']} hari • Pemeriksaan paralel {settings['concurrency']}\n"
                f"Batas client terhubung {settings['max_clients']}\n"
                "Untuk mengatur retensi 30–365 hari, kirim <code>/monitor_input retention 90</code>.\n"
                "Untuk mengatur 1–4 pemeriksaan paralel, kirim <code>/monitor_input concurrency 2</code>.\n"
                "Untuk mengatur batas client, kirim <code>/monitor_input clients 8</code>.")
        return panel("⚙️ Pengaturan", text, rows, home)

    async def message(self, update, context):
        if not self.authorized(update):
            return
        parts = (update.effective_message.text or "").split(maxsplit=2)
        try:
            if len(parts) != 3:
                raise MonitorInput("Buka menu Account Monitor untuk melihat format perintah.")
            action, values = parts[1:]
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
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await self.client.close()

    async def send(self, bot, text):
        with self.scope("admin"):
            await bot.send_message(
                self.owner, text, protect_content=True, disable_web_page_preview=True,
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
                    await self.send(bot, notice["text"])
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
