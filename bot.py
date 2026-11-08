import os
import base64
import asyncio
import re
import json
import csv
import zipfile
import html as html_module
import hmac
import hashlib
import logging
import math
import random
import string
import functools
import urllib.parse
import time as _time
import copy
import uuid
import weakref
from contextvars import ContextVar
import httpx
from telegram.request import HTTPXRequest
from concurrent.futures import ThreadPoolExecutor
from collections import deque, OrderedDict
from aiohttp import web as aio_web
import aiohttp
import qrcode
import psycopg2

try:
    import asyncpg
except ImportError:
    asyncpg = None

try:
    import uvloop
except ImportError:
    uvloop = None
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from io import BytesIO
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager, AsyncExitStack
import telegram.error
from telegram import (
    Update, BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
    InlineKeyboardButton as TelegramInlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ChatJoinRequestHandler, filters, ContextTypes
)

try:
    from telegram.ext import AIORateLimiter
except Exception:
    AIORateLimiter = None

try:
    from telegram.ext import BaseRateLimiter
except ImportError:
    BaseRateLimiter = None

try:
    # Tersedia mulai python-telegram-bot 20.4.
    from telegram.ext import BaseUpdateProcessor
except ImportError:  # Fallback aman untuk PTB 20.0-20.3.
    BaseUpdateProcessor = None

# =================== LOGGING SETUP ===================
_LOG_LEVEL_NAME = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=_LOG_LEVEL
)
logger = logging.getLogger("HyperFamilyBot")
# Library jaringan cukup WARNING agar I/O log rutin tidak memenuhi stdout.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.ExtBot").setLevel(logging.WARNING)

# =================== HELPERS TEKS ===================

import re as _re

ORDER_ID_PATTERN = _re.compile(r'^HFB-\d+-\d{14}-[A-Z0-9]{4}$')

def is_valid_order_id(order_id: str) -> bool:
    """Validasi format Order ID untuk mencegah input sembarangan."""
    return bool(ORDER_ID_PATTERN.match(order_id))

class UserStateManager:
    """State machine terpusat untuk mengelola state user di context.user_data."""
    IDLE = "idle"
    AWAITING_BAN = "awaiting_ban"
    AWAITING_UNBAN = "awaiting_unban"
    AWAITING_CHANNEL_ID = "awaiting_channel_id"
    AWAITING_TESTI_CHANNEL_ID = "awaiting_testi_channel_id"
    AWAITING_LINK_TESTI = "awaiting_link_testi"
    AWAITING_LINK_ADMIN = "awaiting_link_admin"
    AWAITING_MAIN_GROUP_NAME = "awaiting_main_group_name"
    AWAITING_MAIN_GROUP_LINK = "awaiting_main_group_link"
    AWAITING_CARI = "awaiting_cari"
    AWAITING_KICK_SEARCH = "awaiting_kick_search"
    AWAITING_ADD_GROUP = "awaiting_add_group"
    AWAITING_ADD_ADMIN = "awaiting_add_admin"
    AWAITING_JSON_IMPORT = "awaiting_json_import"
    AWAITING_REVIEW_TEXT = "awaiting_review_text"
    BLAST_TYPING = "blast_typing"
    BLAST_PREVIEW = "blast_preview"
    ADDING_PRODUCT = "adding_product"
    EDITING_PRODUCT = "editing_product"

_state_manager = UserStateManager()

def get_user_state(context) -> str:
    """Ambil state saat ini dari user."""
    ud = context.user_data
    if ud.get('awaiting_review_text'):
        return UserStateManager.AWAITING_REVIEW_TEXT
    if ud.get('awaiting_json_import'):
        return UserStateManager.AWAITING_JSON_IMPORT
    if ud.get('awaiting_ban'):
        return UserStateManager.AWAITING_BAN
    if ud.get('awaiting_unban'):
        return UserStateManager.AWAITING_UNBAN
    if ud.get('awaiting_channel_id'):
        return UserStateManager.AWAITING_CHANNEL_ID
    if ud.get('awaiting_testi_channel_id'):
        return UserStateManager.AWAITING_TESTI_CHANNEL_ID
    if ud.get('awaiting_link_testi'):
        return UserStateManager.AWAITING_LINK_TESTI
    if ud.get('awaiting_link_admin'):
        return UserStateManager.AWAITING_LINK_ADMIN
    if ud.get('awaiting_main_group_name'):
        return UserStateManager.AWAITING_MAIN_GROUP_NAME
    if ud.get('awaiting_main_group_link'):
        return UserStateManager.AWAITING_MAIN_GROUP_LINK
    if ud.get('awaiting_cari'):
        return UserStateManager.AWAITING_CARI
    if ud.get('awaiting_kick_search'):
        return UserStateManager.AWAITING_KICK_SEARCH
    if ud.get('awaiting_add_group'):
        return UserStateManager.AWAITING_ADD_GROUP
    if ud.get('awaiting_add_admin'):
        return UserStateManager.AWAITING_ADD_ADMIN
    bs = ud.get('blast_state', {})
    if bs.get('step') == 'typing':
        return UserStateManager.BLAST_TYPING
    if bs.get('step') == 'preview':
        return UserStateManager.BLAST_PREVIEW
    if ud.get('adding_product'):
        return UserStateManager.ADDING_PRODUCT
    if ud.get('editing_product'):
        return UserStateManager.EDITING_PRODUCT
    return UserStateManager.IDLE

def clear_user_state(context):
    """Bersihkan semua state user."""
    ud = context.user_data
    for key in list(ud.keys()):
        if key.startswith('awaiting_') or key in ('blast_state', 'adding_product', 'editing_product', 'temp_rating', 'prereq_ctx'):
            ud.pop(key, None)

def samarkan_nama(nama: str) -> str:
    if not nama:
        return "Pembeli"
    parts = nama.split()
    masked_parts = []
    for part in parts:
        if len(part) == 1:
            masked_parts.append(part[0])
        elif len(part) == 2:
            masked_parts.append(part[0] + "*")
        else:
            masked_parts.append(part[:2] + "*" * (len(part) - 2))
    return " ".join(masked_parts)

def esc(text) -> str:
    """Escape karakter spesial HTML untuk parse_mode=HTML."""
    return html_module.escape(str(text))


def InlineKeyboardButton(text, *args, **kwargs):
    """Gunakan warna pada seluruh tombol inline, dengan warna navigasi konsisten."""
    extra = dict(kwargs.pop('api_kwargs', None) or {})
    label = str(text).casefold()
    if 'kembali' in label or 'sebelumnya' in label:
        extra['style'] = 'danger'
    elif 'berikutnya' in label:
        extra['style'] = 'success'
    elif not extra.get('style'):
        extra['style'] = 'primary'
    kwargs['api_kwargs'] = extra
    return TelegramInlineKeyboardButton(text, *args, **kwargs)


def styled_button(text: str, *, style: str = None,
                  icon_custom_emoji_id: str = None, **kwargs):
    """Kirim field tombol baru lewat Bot API, termasuk pada PTB versi lama."""
    extra = {}
    if style in {'primary', 'success', 'danger'}:
        extra['style'] = style
    emoji_id = str(icon_custom_emoji_id or '').strip()
    if emoji_id:
        extra['icon_custom_emoji_id'] = emoji_id
        # Saat ikon Premium aktif, hilangkan emoji Unicode di awal agar tidak dobel.
        text = re.sub(r'^\S+\s+', '', text, count=1)
    return InlineKeyboardButton(text, api_kwargs=extra or None, **kwargs)


def normalize_button_url(value):
    """Normalisasi URL tombol dan tolak skema yang tidak didukung Telegram."""
    val = str(value or '').strip()
    if val.startswith('@') and len(val) > 1:
        val = f"https://t.me/{val[1:]}"
    if not val or len(val) > 512:
        return None
    parsed = urllib.parse.urlparse(val)
    if parsed.scheme not in ('http', 'https', 'tg') or not parsed.netloc:
        return None
    return val


def tg_user_link(user_id, name: str = None) -> str:
    """Membuat nama buyer clickable ke profil Telegram."""
    display_name = esc(name or "Buyer")
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return display_name
    return f'<a href="tg://user?id={uid}">{display_name}</a>'

def _infer_order_status_label(judul: str) -> str:
    title = (judul or "").upper()
    if "EXPIRED" in title:
        return "⏰ Expired"
    if "BATAL" in title or "CANCEL" in title:
        return "❌ Cancelled"
    if "REJECT" in title or "TOLAK" in title:
        return "❌ Rejected"
    if "BARU" in title or "WAIT" in title or "MENUNGGU" in title:
        return "⏳ Waiting"
    if "BERHASIL" in title or "KONFIRM" in title or "LINK" in title or "COMPLETED" in title:
        return "✅ Completed"
    return "📌 Updated"

def _safe_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 1000) -> int:
    value = _safe_int(os.environ.get(name), default)
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float = 0.0, maximum: float = 300.0) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))

# =================== TIMEZONE ===================
WIB = timezone(timedelta(hours=7))

def now_wib() -> datetime:
    return datetime.now(WIB)

def now_utc() -> datetime:
    """Mengembalikan objek datetime UTC dengan timezone-aware."""
    return datetime.now(timezone.utc)

# =================== KONFIGURASI ===================
TOKEN = os.environ.get("BOT_TOKEN")
_ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "")
DATABASE_URL = os.environ.get("DATABASE_URL")
APP_ENV = os.environ.get("APP_ENV") or os.environ.get("ENV") or "development"
BOT_RELEASE = "2026-11-08-hyper-store-compact-menu"

# AutoGoPay gateway: GoPay utama, ShopeePay merchant sebagai cadangan.
AUTOGOPAY_API_KEY = (os.environ.get("AUTOGOPAY_API_KEY") or "").strip()
AUTOGOPAY_BASE_URL = (os.environ.get("AUTOGOPAY_BASE_URL") or "https://v1-gateway.autogopay.site").rstrip("/")
AUTOGOPAY_DEFAULT_TIMEOUT_SECONDS = 15 * 60
# GoPay tetap provider utama. ShopeePay hanya dipakai bila GoPay mengembalikan
# penolakan JSON eksplisit, bukan 5xx/timeout/JSON rusak yang masih
# mungkin berarti transaksi GoPay sebenarnya sudah tercipta.
SHOPEEPAY_FALLBACK_ENABLED = (
    os.environ.get("SHOPEEPAY_FALLBACK_ENABLED", "1").strip() == "1"
)

# =================== PERFORMANCE TUNING ===================
# Berbeda user/chat diproses paralel, tetapi update dari chat yang sama tetap berurutan.
BOT_CONCURRENT_UPDATES = _env_int("BOT_CONCURRENT_UPDATES", 24, 4, 128)
TELEGRAM_CONNECTION_POOL_SIZE = _env_int("TELEGRAM_CONNECTION_POOL_SIZE", 64, 16, 256)
TELEGRAM_POOL_TIMEOUT_SECONDS = _env_float("TELEGRAM_POOL_TIMEOUT_SECONDS", 15.0, 1.0, 60.0)
TELEGRAM_KEEPALIVE_SECONDS = _env_float("TELEGRAM_KEEPALIVE_SECONDS", 60.0, 5.0, 120.0)
TELEGRAM_TRANSPORT_PROBE = os.environ.get("TELEGRAM_TRANSPORT_PROBE", "0").strip() == "1"
CALLBACK_TRACE_ENABLED = os.environ.get("CALLBACK_TRACE_ENABLED", "1").strip() == "1"
CALLBACK_TRACE_MAX = _env_int("CALLBACK_TRACE_MAX", 2000, 100, 10000)
DB_POOL_MIN = _env_int("DB_POOL_MIN", 4, 1, 10)
DB_POOL_MAX = _env_int("DB_POOL_MAX", 20, DB_POOL_MIN, 50)
# Jalur DB admin/background dibatasi agar query berat tidak menghabiskan semua worker buyer.
DB_ADMIN_THREAD_WORKERS = _env_int("DB_ADMIN_THREAD_WORKERS", 2, 1, 6)
# Worker buyer default menyisakan slot untuk worker admin pada pool psycopg2 yang sama.
# Jika nilainya diubah manual, pastikan DB_POOL_MAX >= jumlah seluruh worker DB.
DB_THREAD_WORKERS = _env_int(
    "DB_THREAD_WORKERS",
    min(18, max(4, DB_POOL_MAX - DB_ADMIN_THREAD_WORKERS)),
    4,
    50,
)
CPU_THREAD_WORKERS = _env_int("CPU_THREAD_WORKERS", 4, 2, 12)
ASYNCPG_POOL_MAX = _env_int("ASYNCPG_POOL_MAX", 4, 1, 12)
ASYNCPG_ACQUIRE_TIMEOUT_SECONDS = _env_float("ASYNCPG_ACQUIRE_TIMEOUT_SECONDS", 2.0, 0.5, 10.0)
ASYNCPG_READ_TIMEOUT_SECONDS = _env_float("ASYNCPG_READ_TIMEOUT_SECONDS", 4.0, 1.0, 15.0)
DB_STATEMENT_TIMEOUT_MS = _env_int("DB_STATEMENT_TIMEOUT_MS", 8000, 1000, 60000)
ACTIVE_ORDER_CACHE_TTL_SECONDS = _env_float("ACTIVE_ORDER_CACHE_TTL_SECONDS", 2.5, 0.5, 10.0)
WAITING_ORDERS_CACHE_TTL_SECONDS = _env_float("WAITING_ORDERS_CACHE_TTL_SECONDS", 3.0, 0.5, 15.0)
ADMIN_CACHE_TTL_SECONDS = _env_float("ADMIN_CACHE_TTL_SECONDS", 30.0, 5.0, 300.0)
LATENCY_REPORT_INTERVAL_SECONDS = _env_float("LATENCY_REPORT_INTERVAL_SECONDS", 300.0, 30.0, 1800.0)
DB_SLOW_SESSION_SECONDS = _env_float("DB_SLOW_SESSION_SECONDS", 1.0, 0.1, 30.0)
SLOW_UPDATE_THRESHOLD_SECONDS = _env_float("SLOW_UPDATE_THRESHOLD_SECONDS", 1.5, 0.2, 30.0)
PERF_TRACE_THRESHOLD_SECONDS = _env_float("PERF_TRACE_THRESHOLD_SECONDS", 1.0, 0.1, 30.0)
EVENT_LOOP_LAG_WARNING_SECONDS = _env_float("EVENT_LOOP_LAG_WARNING_SECONDS", 0.75, 0.1, 10.0)
STATS_CACHE_TTL_SECONDS = _env_float("STATS_CACHE_TTL_SECONDS", 10.0, 1.0, 120.0)
AUTOGOPAY_MAX_CONCURRENT_REQUESTS = _env_int("AUTOGOPAY_MAX_CONCURRENT_REQUESTS", 20, 2, 50)
AUTOGOPAY_CIRCUIT_FAILURES = _env_int("AUTOGOPAY_CIRCUIT_FAILURES", 5, 2, 20)
AUTOGOPAY_CIRCUIT_COOLDOWN_SECONDS = _env_float("AUTOGOPAY_CIRCUIT_COOLDOWN_SECONDS", 15.0, 5.0, 120.0)
TELEGRAM_RETRY_ATTEMPTS = _env_int("TELEGRAM_RETRY_ATTEMPTS", 2, 1, 4)
# Panel admin sengaja memakai halaman kecil supaya satu klik tidak membangun payload Telegram besar.
ADMIN_ORDER_PAGE_SIZE = _env_int("ADMIN_ORDER_PAGE_SIZE", 8, 5, 15)
ADMIN_LIST_PAGE_SIZE = _env_int("ADMIN_LIST_PAGE_SIZE", 10, 5, 20)
ADMIN_PANEL_CACHE_TTL_SECONDS = _env_float("ADMIN_PANEL_CACHE_TTL_SECONDS", 3.0, 0.5, 15.0)
COOLDOWN_EMPTY_TTL_SECONDS = _env_float("COOLDOWN_EMPTY_TTL_SECONDS", 30.0, 5.0, 300.0)

# Set 1 jika syarat dianggap terpenuhi setelah link paket syarat benar-benar terkirim.
PREREQUISITE_REQUIRES_DELIVERY = (
    os.environ.get("PREREQUISITE_REQUIRES_DELIVERY", "0").strip() == "1"
)
# Set 0 setelah migrasi pertama/deployment selesai agar DDL tidak menghambat cold start.
RUN_DB_MIGRATIONS_ON_STARTUP = (
    os.environ.get("RUN_DB_MIGRATIONS_ON_STARTUP", "1").strip() == "1"
)

# Timeout dibuat lebih pendek agar gangguan provider tidak menahan update Telegram terlalu lama.
AUTOGOPAY_HTTP_TIMEOUT = aiohttp.ClientTimeout(
    total=12,
    connect=5,
    sock_connect=5,
    sock_read=8,
)

# Webhook adalah jalur utama. Polling hanya safety-net dan dibuat makin jarang.
AUTOGOPAY_POLL_FAST_SECONDS = _env_int("AUTOGOPAY_POLL_FAST_SECONDS", 5, 3, 30)
AUTOGOPAY_POLL_MEDIUM_SECONDS = _env_int("AUTOGOPAY_POLL_MEDIUM_SECONDS", 10, 5, 60)
AUTOGOPAY_POLL_SLOW_SECONDS = _env_int("AUTOGOPAY_POLL_SLOW_SECONDS", 20, 10, 120)
AUTOGOPAY_POLL_VERY_SLOW_SECONDS = _env_int("AUTOGOPAY_POLL_VERY_SLOW_SECONDS", 30, 15, 180)

ADMIN_USERNAME = "@Gilbiie"
ADMIN_URL = "https://t.me/Gilbiie"
DEFAULT_LINK = ADMIN_URL

if not TOKEN:
    raise ValueError("BOT_TOKEN tidak di-set!")
if not _ADMIN_ID_RAW or not _ADMIN_ID_RAW.strip().isdigit():
    raise ValueError("ADMIN_ID tidak di-set atau bukan angka valid!")
ADMIN_ID = int(_ADMIN_ID_RAW.strip())
if not DATABASE_URL:
    raise ValueError("DATABASE_URL tidak di-set!")
if not AUTOGOPAY_API_KEY:
    raise ValueError("AUTOGOPAY_API_KEY tidak di-set!")

# =================== CONCURRENCY & BACKGROUND TASKS ===================
_background_tasks: set = set()
_invoice_creation_in_progress: set = set()
_user_notice_locks = None


def _get_user_notice_lock(user_id: int) -> asyncio.Lock:
    """Serialisasi notifikasi status per buyer agar spam command tidak membuat duplikat."""
    global _user_notice_locks
    if _user_notice_locks is None:
        _user_notice_locks = weakref.WeakValueDictionary()
    uid = int(user_id)
    lock = _user_notice_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _user_notice_locks[uid] = lock
    return lock


def claim_invoice_creation(user_id: int) -> bool:
    """Single-flight per buyer untuk mencegah invoice ganda akibat double-click."""
    if user_id in _invoice_creation_in_progress:
        return False
    _invoice_creation_in_progress.add(user_id)
    return True


def release_invoice_creation(user_id: int):
    _invoice_creation_in_progress.discard(user_id)


_callback_acks = {}
_callback_ack_tasks = {}

async def _fast_callback_ack(query, text: str = None):
    """Hentikan spinner callback Telegram sedini mungkin tanpa menggagalkan handler jika jaringan sedang fluktuatif."""
    try:
        query_id = getattr(query, 'id', None)
        pending = _callback_ack_tasks.get(query_id)
        # Menu ACK is already in flight; rendering must not wait for its RTT.
        if text is None and pending is not None and pending is not asyncio.current_task():
            return
        now_mono = _time.monotonic()
        if text is None and query_id and _callback_acks.get(query_id, 0) > now_mono:
            return
        await query.answer(text=text)
        if query_id:
            _callback_acks[query_id] = now_mono + 60
            if len(_callback_acks) > 4096:
                for old_id, expires_at in list(_callback_acks.items()):
                    if expires_at <= now_mono or len(_callback_acks) > 4096:
                        _callback_acks.pop(old_id, None)
    except Exception as exc:
        logger.debug(f"[CALLBACK] ACK gagal/terlambat: {exc}")


def _schedule_navigation_ack(query, text=None):
    query_id = getattr(query, 'id', None)
    if not query_id or query_id in _callback_ack_tasks:
        return
    if _callback_acks.get(query_id, 0) > _time.monotonic():
        return
    trace = _callback_trace_state.get()
    if trace is not None:
        trace['ack_expected'] = True
    task = spawn_background(_fast_callback_ack(query, text=text), name="menu-callback-ack", trace_callback=True)
    _callback_ack_tasks[query_id] = task

    def done(completed):
        if _callback_ack_tasks.get(query_id) is completed:
            _callback_ack_tasks.pop(query_id, None)
        if trace is not None:
            trace['ack_task_done_ns'] = _time.perf_counter_ns()
            _callback_trace_emit(trace)
    task.add_done_callback(done)


async def _callback_feedback(query, context, text: str):
    """Feedback setelah callback sudah di-ACK. Dipakai hanya untuk jalur validasi/error yang jarang."""
    try:
        await query.message.reply_text(text)
    except Exception:
        await context.bot.send_message(chat_id=query.from_user.id, text=text)


async def _show_cooldown_alert(query, sisa: int):
    """Feedback cooldown lewat alert callback agar tidak menambah bubble chat."""
    try:
        await query.answer(
            f"⏳ COOLDOWN AKTIF\n\nTunggu {sisa} menit lagi sebelum membuat invoice baru.",
            show_alert=True,
        )
    except Exception as exc:
        logger.debug(f"[COOLDOWN] Gagal menampilkan alert: {exc}")


# Pisahkan antrean query database dari pekerjaan CPU/file (QR, ZIP, JSON).
# Dengan begitu backup/export admin tidak ikut menahan respons buyer.
_db_executor = ThreadPoolExecutor(
    max_workers=DB_THREAD_WORKERS,
    thread_name_prefix="hyperfamily-db",
)
_db_admin_executor = ThreadPoolExecutor(
    max_workers=DB_ADMIN_THREAD_WORKERS,
    thread_name_prefix="hyperfamily-db-admin",
)
_cpu_executor = ThreadPoolExecutor(
    max_workers=CPU_THREAD_WORKERS,
    thread_name_prefix="hyperfamily-cpu",
)


_executor_admission = {}
_executor_limits = {_db_executor: DB_THREAD_WORKERS,
                    _db_admin_executor: DB_ADMIN_THREAD_WORKERS,
                    _cpu_executor: CPU_THREAD_WORKERS}

_callback_trace_state = ContextVar('callback_trace_state', default=None)
_callback_http_span = ContextVar('callback_http_span', default=None)
_callback_ingress = OrderedDict()
_callback_trace_created = 0
_last_poll_read_ns = 0
_loop_stalls = deque(maxlen=512)


def _callback_route(update):
    query = getattr(update, 'callback_query', None)
    if query is None:
        return 'message'
    data = str(query.data or '')
    for prefix, route in (('buy|', 'buyer:list'), ('pilih_', 'buyer:product'),
                          ('confirm_buy_', 'payment:create'), ('prereq_buy_', 'payment:prerequisite'),
                          ('resend_menu', 'buyer:resend'), ('resend_select|', 'buyer:delivery'),
                          ('buyer_desc_', 'buyer:description'), ('admpanel_', 'admin:panel'),
                          ('produk_', 'admin:products')):
        if data.startswith(prefix):
            return route
    return {'buy':'buyer:list', 'back_to_menu':'buyer:back', 'buyer_how':'buyer:how',
            'buyer_history':'buyer:history', 'cancel_order':'payment:cancel'}.get(data, 'callback:other')


def _new_callback_trace(update, source='processor'):
    global _callback_trace_created
    if (not CALLBACK_TRACE_ENABLED or getattr(update, 'callback_query', None) is None
            or _callback_trace_created >= CALLBACK_TRACE_MAX):
        return None
    _callback_trace_created += 1
    now = _time.perf_counter_ns()
    return {'id': uuid.uuid4().hex[:12], 'route': _callback_route(update), 'source': source,
            'epoch_ns': _time.time_ns(), 'ingress_ns': now,
            'poll_ns': _last_poll_read_ns if source == 'queue' and 0 <= now-_last_poll_read_ns < 2_000_000_000 else None,
            'api': [], 'db': [], 'pending': 0, 'closed': False}


def _callback_trace_emit(trace):
    if (not trace or trace['closed'] or 'done_ns' not in trace or trace['pending']
            or trace.get('ack_expected') and 'ack_task_done_ns' not in trace):
        return
    trace['closed'] = True
    origin = trace['ingress_ns']
    def elapsed(start, end):
        return round((trace[end]-trace[start])/1e6, 3) if start in trace and end in trace else None
    stages = {
        'poll_decode_enqueue_ms': elapsed('poll_ns', 'ingress_ns') if trace.get('poll_ns') is not None else None,
        'queue_ms': elapsed('ingress_ns', 'dequeue_ns'),
        'dispatch_ms': elapsed('dequeue_ns', 'processor_ns'),
        'chat_lock_ms': elapsed('processor_ns', 'chat_lock_ns'),
        'admission_ms': elapsed('chat_lock_ns', 'handler_ns'),
        'build_ms': elapsed('handler_ns', 'render_ns'),
        'handler_ms': elapsed('handler_ns', 'done_ns'),
        'total_ms': elapsed('ingress_ns', 'done_ns'),
        'ack_task_done_ms': elapsed('ingress_ns', 'ack_task_done_ns'),
    }
    record = dict(id=trace['id'], route=trace['route'], source=trace['source'], epoch_ns=trace['epoch_ns'],
                  stages=stages, coalesced=trace.get('coalesced', False), outcome=trace.get('outcome','ok'),
                  api=trace['api'], db=trace['db'], overflow=trace.get('overflow',0),
                  loop_lag_ms=round(max((lag for ts,lag in _loop_stalls if origin <= ts <= trace['done_ns']),default=0),3))
    logger.info('[CBTRACE] %s', json.dumps(record,separators=(',',':')))


class CallbackTimingQueue(asyncio.Queue):
    """Observe real updater enqueue/dequeue without changing queue capacity or delivery."""
    def put_nowait(self, item):
        result = super().put_nowait(item)
        trace = _new_callback_trace(item, 'queue')
        if trace is not None:
            _callback_ingress[id(item)] = trace
            while len(_callback_ingress) > 4096:
                _callback_ingress.popitem(last=False)
        return result

    def get_nowait(self):
        item = super().get_nowait()
        trace = _callback_ingress.get(id(item))
        if trace is not None:
            trace['dequeue_ns'] = _time.perf_counter_ns()
        return item


class PollingHTTPXRequest(HTTPXRequest):
    __slots__ = ()
    async def do_request(self, *args, **kwargs):
        global _last_poll_read_ns
        result = await super().do_request(*args, **kwargs)
        if result[0] == 200:
            _last_poll_read_ns = _time.perf_counter_ns()
        return result

async def _run_executor(executor, func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    slots = _executor_admission.get(executor)
    if slots is None:
        slots = asyncio.BoundedSemaphore(_executor_limits[executor])
        _executor_admission[executor] = slots
    label = ('db-admin' if executor is _db_admin_executor else
             'db-buyer' if executor is _db_executor else 'cpu')
    queued_at = _time.monotonic()
    trace = _callback_trace_state.get()
    span = None
    if trace is not None and not trace['closed'] and len(trace['db']) < 16:
        span = {'kind': label, 'fn': getattr(func,'__name__','callable')[:50],
                'at_ms': round((_time.perf_counter_ns()-trace['ingress_ns'])/1e6,3)}
        trace['db'].append(span)
    await slots.acquire()
    if span is not None:
        span['admission_ms'] = round((_time.monotonic()-queued_at)*1000,3)
    _record_latency(label + '-queue', _time.monotonic() - queued_at)
    try:
        submitted_ns = _time.perf_counter_ns()
        def work():
            began = _time.perf_counter_ns()
            if span is not None:
                span['thread_wait_ms'] = round((began-submitted_ns)/1e6,3)
            try:
                return func(*args, **kwargs)
            finally:
                if span is not None:
                    span['execution_ms'] = round((_time.perf_counter_ns()-began)/1e6,3)
        future = loop.run_in_executor(executor, work)
    except BaseException:
        slots.release()
        raise
    started = _time.monotonic()
    def completed(done):
        slots.release()
        _record_latency(label + '-work', _time.monotonic() - started)
        if span is not None:
            span['work_ms'] = round((_time.monotonic()-started)*1000,3)
        # Retrieve exceptions even when the caller stopped awaiting a running thread.
        if not done.cancelled():
            done.exception()
    future.add_done_callback(completed)
    return await asyncio.shield(future)


async def run_db(func, *args, **kwargs):
    """Jalankan operasi database buyer/kritis pada executor utama."""
    return await _run_executor(_db_executor, func, *args, **kwargs)


async def run_db_admin(func, *args, **kwargs):
    """Jalankan query admin/background pada executor kecil agar buyer tetap prioritas."""
    return await _run_executor(_db_admin_executor, func, *args, **kwargs)


async def run_cpu(func, *args, **kwargs):
    """Jalankan proses CPU/file pada executor terpisah dari query database."""
    return await _run_executor(_cpu_executor, func, *args, **kwargs)


_latency_samples: dict = {}

def _record_latency(label: str, seconds: float):
    samples = _latency_samples.get(label)
    if samples is None:
        samples = deque(maxlen=500)
        _latency_samples[label] = samples
    samples.append(max(0.0, float(seconds)))

def _percentile(values, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[index]

async def _latency_reporter_loop():
    while True:
        await asyncio.sleep(LATENCY_REPORT_INTERVAL_SECONDS)
        for label, samples in list(_latency_samples.items()):
            if not samples:
                continue
            values = list(samples)
            samples.clear()
            logger.info(
                f"[LATENCY] {label} n={len(values)} mean={sum(values)/len(values):.3f}s "
                f"p50={_percentile(values, 50):.3f}s "
                f"p95={_percentile(values, 95):.3f}s "
                f"p99={_percentile(values, 99):.3f}s"
            )

_telegram_transport_state = ContextVar("telegram_transport_state", default=None)
_telegram_transport_probe = ContextVar("telegram_transport_probe", default=False)
_navigation_timing = ContextVar("navigation_timing", default=None)


class TelegramHTTPXRequest(HTTPXRequest):
    """PTB 20.7 adapter: preserve request semantics, measure connection setup safely."""
    __slots__ = ()

    def _build_client(self):
        # PTB 20.7 has no public keepalive option. Preserve its other client options.
        limits = self._client_kwargs['limits']
        self._client_kwargs['limits'] = httpx.Limits(
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=TELEGRAM_KEEPALIVE_SECONDS,
        )
        self._client_kwargs['event_hooks'] = {'request': [self._trace_request]}
        return super()._build_client()

    @staticmethod
    async def _trace_request(request):
        stats = _telegram_transport_state.get()
        if stats is None:
            return

        async def trace(event, info):
            # Never record URL, headers, request/response bodies, token or user data.
            for operation, label in (('connect_tcp', 'tcp'), ('start_tls', 'tls')):
                if event.endswith('.' + operation + '.started'):
                    stats[label + '_at'] = _time.monotonic()
                    if label == 'tcp':
                        stats['cold'] = True
                elif event.endswith('.' + operation + '.complete') or event.endswith('.' + operation + '.failed'):
                    began = stats.pop(label + '_at', None)
                    if began is not None:
                        stats[label] = stats.get(label, 0.0) + _time.monotonic() - began
            now_ns = _time.perf_counter_ns()
            for operation, label in (('send_request_headers','send_headers'), ('send_request_body','send_body'),
                                     ('receive_response_headers','recv_headers'), ('receive_response_body','recv_body')):
                if event.endswith('.' + operation + '.started'):
                    stats[label + '_ns'] = now_ns
                elif event.endswith('.' + operation + '.complete'):
                    stats[label + '_done_ns'] = now_ns
            stats.setdefault('first_event_ns', now_ns)

        stats['hook_ns'] = _time.perf_counter_ns()
        request.extensions['trace'] = trace

    async def do_request(self, *args, **kwargs):
        stats = {'started_ns': _time.perf_counter_ns()}
        state_token = _telegram_transport_state.set(stats)
        started = _time.monotonic()
        success = False
        try:
            result = await super().do_request(*args, **kwargs)
            stats['http_status'] = result[0]
            success = result[0] == 200
            return result
        finally:
            elapsed = _time.monotonic() - started
            probe = _telegram_transport_probe.get()
            prefix = 'telegram-probe' if probe else 'telegram-http'
            connection = 'cold' if stats.get('cold') else 'warm'
            _record_latency(prefix + '-' + connection, elapsed)
            for part in ('tcp', 'tls'):
                if part in stats:
                    _record_latency(prefix + '-' + part, stats[part])
            if not success:
                _record_latency(prefix + '-failed', elapsed)
            if probe:
                logger.info('[TRANSPORT PROBE] keepalive_s=%.1f connection=%s total_ms=%.1f tcp_ms=%.1f tls_ms=%.1f ok=%s',
                            TELEGRAM_KEEPALIVE_SECONDS, connection, elapsed * 1000,
                            stats.get('tcp', 0) * 1000, stats.get('tls', 0) * 1000, success)
            span = _callback_http_span.get()
            if span is not None:
                span.update(http_ms=round(elapsed*1000,3),cold=bool(stats.get('cold')),
                            tcp_ms=round(stats.get('tcp',0)*1000,3),tls_ms=round(stats.get('tls',0)*1000,3),
                            http_status=stats.get('http_status'))
                for label,begin,end in (
                    ('httpx_prepare_ms','started_ns','hook_ns'),
                    ('httpx_dispatch_ms','hook_ns','first_event_ns'),
                    ('send_ms','send_headers_ns','send_body_done_ns'),
                    ('wait_headers_ms','send_body_done_ns','recv_headers_done_ns'),
                    ('read_body_ms','recv_body_ns','recv_body_done_ns')):
                    if begin in stats and end in stats:
                        span[label] = round((stats[end]-stats[begin])/1e6,3)
            _telegram_transport_state.reset(state_token)


async def _probe_telegram_transport(bot):
    """Opt-in, finite read-only probe. Never creates an order or sends a message."""
    await asyncio.sleep(2)
    for group in range(4):
        if group:
            await asyncio.sleep(6.5)
        for _ in range(3):
            token = _telegram_transport_probe.set(True)
            try:
                await bot.get_me()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('[TRANSPORT PROBE] getMe failed: %s', type(exc).__name__)
            finally:
                _telegram_transport_probe.reset(token)
    logger.info('[TRANSPORT PROBE] Completed 12 read-only samples.')


class PerfTrace:
    """Profiling ringan per alur; hanya menulis log saat proses terasa lambat."""

    def __init__(self, label: str, **context):
        self.label = label
        self.context = context
        self.started = _time.monotonic()
        self.last_mark = self.started
        self.stages = []

    def mark(self, stage: str):
        now_mono = _time.monotonic()
        self.stages.append((stage, now_mono - self.last_mark))
        self.last_mark = now_mono

    def finish(self):
        total = _time.monotonic() - self.started
        _record_latency(f"flow:{self.label}", total)
        if total < PERF_TRACE_THRESHOLD_SECONDS:
            return
        stage_text = ", ".join(f"{name}={duration:.2f}s" for name, duration in self.stages)
        context_text = " ".join(f"{key}={value}" for key, value in self.context.items())
        logger.info(
            f"[PERF] {self.label} total={total:.2f}s"
            f"{(' ' + context_text) if context_text else ''}"
            f"{(' | ' + stage_text) if stage_text else ''}"
        )


async def telegram_retry(operation, *, label: str, attempts: int = None):
    """Retry terkontrol untuk operasi Telegram yang aman diulang, seperti edit pesan."""
    max_attempts = attempts or TELEGRAM_RETRY_ATTEMPTS
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except telegram.error.BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return None
            raise
        except telegram.error.RetryAfter as exc:
            if attempt >= max_attempts:
                raise
            retry_after = exc.retry_after
            delay = (
                retry_after.total_seconds()
                if hasattr(retry_after, "total_seconds")
                else float(retry_after)
            )
            delay = max(0.1, delay)
            logger.warning(f"[TELEGRAM RETRY] {label} rate-limit; retry {delay:.2f}s")
            await asyncio.sleep(delay)
        except (telegram.error.TimedOut, telegram.error.NetworkError) as exc:
            if attempt >= max_attempts:
                raise
            delay = 0.35 * attempt
            logger.warning(
                f"[TELEGRAM RETRY] {label} gagal sementara ({type(exc).__name__}); "
                f"retry {delay:.2f}s"
            )
            await asyncio.sleep(delay)


async def telegram_send_rate_retry(operation, *, label: str, attempts: int = None):
    """Retry send hanya saat Telegram memastikan rate-limit; timeout tidak diulang agar tidak dobel kirim."""
    max_attempts = attempts or TELEGRAM_RETRY_ATTEMPTS
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except telegram.error.RetryAfter as exc:
            if attempt >= max_attempts:
                raise
            retry_after = exc.retry_after
            delay = retry_after.total_seconds() if hasattr(retry_after, "total_seconds") else float(retry_after)
            delay = max(0.1, delay)
            logger.warning(f"[TELEGRAM RATE] {label}; retry {delay:.2f}s")
            await asyncio.sleep(delay)


def spawn_background(coro, *, name: str = None, trace_callback=False):
    """Jalankan coroutine tanpa menahan handler dan tetap laporkan exception-nya."""
    token = _callback_trace_state.set(_callback_trace_state.get() if trace_callback else None)
    try:
        task = asyncio.create_task(coro, name=name)
    finally:
        _callback_trace_state.reset(token)
    _background_tasks.add(task)

    def _done(done_task):
        _background_tasks.discard(done_task)
        if done_task.cancelled():
            return
        try:
            exc = done_task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.error(
                f"[BACKGROUND] Task {done_task.get_name()} gagal: {exc}",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    task.add_done_callback(_done)
    return task


# Request buyer didahulukan; blast/cleanup tetap memakai batas Telegram yang sama.
if BaseRateLimiter is not None:
    class BuyerPriorityRateLimiter(BaseRateLimiter):
        def __init__(self, overall_max_rate=28, overall_time_period=1.0,
                     background_max_rate=18, group_max_rate=20, group_time_period=60.0):
            self._overall_rate = overall_max_rate
            self._period = overall_time_period
            self._background_rate = min(background_max_rate, overall_max_rate)
            self._group_rate = group_max_rate
            self._group_period = group_time_period
            self._sent = deque()
            self._background_sent = deque()
            self._groups = {}
            self._waiting = {}
            self._sequence = 0
            self._condition = None
            self._blocked_until = 0.0
            self._closed = False

        async def initialize(self):
            self._condition = asyncio.Condition()
            self._closed = False

        async def shutdown(self):
            if self._condition is not None:
                async with self._condition:
                    self._closed = True
                    self._condition.notify_all()

        @staticmethod
        def _group_id(chat_id):
            if chat_id is None:
                return None
            try:
                return str(chat_id) if int(chat_id) < 0 else None
            except (TypeError, ValueError):
                return str(chat_id) if str(chat_id).startswith('@') else None

        def _delay(self, item, now_mono):
            _, _, counted, background, group_id = item
            delay = max(0.0, self._blocked_until - now_mono)
            if counted and len(self._sent) >= self._overall_rate:
                delay = max(delay, self._sent[0] + self._period - now_mono)
            if counted and background and len(self._background_sent) >= self._background_rate:
                delay = max(delay, self._background_sent[0] + self._period - now_mono)
            history = self._groups.get(group_id)
            if group_id is not None and history and len(history) >= self._group_rate:
                delay = max(delay, history[0] + self._group_period - now_mono)
            return delay

        async def _acquire(self, endpoint, data, background):
            if self._condition is None:
                await self.initialize()
            counted = data.get('chat_id') is not None
            group_id = self._group_id(data.get('chat_id'))
            priority = 0 if endpoint == 'answerCallbackQuery' else (2 if background else 1)
            token = object()
            started = _time.monotonic()
            async with self._condition:
                self._sequence += 1
                self._waiting[token] = (priority, self._sequence, counted, background, group_id)
                self._condition.notify_all()
                try:
                    while True:
                        if self._closed:
                            raise RuntimeError('Telegram request limiter is shutting down')
                        now_mono = _time.monotonic()
                        for history in (self._sent, self._background_sent):
                            while history and history[0] <= now_mono - self._period:
                                history.popleft()
                        for key, history in list(self._groups.items()):
                            while history and history[0] <= now_mono - self._group_period:
                                history.popleft()
                            if not history:
                                self._groups.pop(key, None)
                        eligible = [
                            (item[0], item[1], key) for key, item in self._waiting.items()
                            if self._delay(item, now_mono) <= 0
                        ]
                        if eligible and min(eligible, key=lambda row: row[:2])[2] is token:
                            if counted:
                                self._sent.append(now_mono)
                                if background:
                                    self._background_sent.append(now_mono)
                            if group_id is not None:
                                self._groups.setdefault(group_id, deque()).append(now_mono)
                            _record_latency('telegram-limiter-wait', now_mono - started)
                            return
                        delays = [self._delay(item, now_mono) for item in self._waiting.values()]
                        positive = [d for d in delays if d > 0]
                        timeout = max(0.001, min(positive)) if positive else 0.05
                        try:
                            await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                        except asyncio.TimeoutError:
                            pass
                finally:
                    self._waiting.pop(token, None)
                    self._condition.notify_all()

        async def _request_attempt(self, callback, args, kwargs, endpoint, data, background, attempt):
            trace = _callback_trace_state.get()
            span = None
            if trace is not None and not trace['closed']:
                if len(trace['api']) < 24:
                    span = {'method':endpoint,'attempt':attempt+1,
                            'at_ms':round((_time.perf_counter_ns()-trace['ingress_ns'])/1e6,3)}
                    trace['api'].append(span)
                    trace['pending'] += 1
                else:
                    trace['overflow'] = trace.get('overflow',0)+1
            token = _callback_http_span.set(span)
            began_ns = _time.perf_counter_ns()
            started = None
            try:
                await self._acquire(endpoint, data, background)
                if span is not None:
                    span['limiter_ms'] = round((_time.perf_counter_ns()-began_ns)/1e6,3)
                started = _time.monotonic()
                return await callback(*args, **kwargs)
            except BaseException as exc:
                if span is not None:
                    span['outcome'] = type(exc).__name__
                    if isinstance(exc,telegram.error.RetryAfter):
                        delay=exc.retry_after
                        span['retry_after_s'] = delay.total_seconds() if isinstance(delay,timedelta) else float(delay)
                raise
            finally:
                if started is not None:
                    elapsed = _time.monotonic()-started
                    _record_latency('telegram-api:' + endpoint, elapsed)
                    if span is not None:
                        span['api_ms'] = round(elapsed*1000,3)
                if span is not None:
                    span.setdefault('outcome','ok')
                    span['total_ms'] = round((_time.perf_counter_ns()-began_ns)/1e6,3)
                    trace['pending'] -= 1
                _callback_http_span.reset(token)
                _callback_trace_emit(trace)

        async def process_request(self, callback, args, kwargs, endpoint, data, rate_limit_args):
            options = rate_limit_args if isinstance(rate_limit_args, dict) else {}
            # Legacy rate_limit_args=0 digunakan oleh durable blast: nol retry, bukan bypass.
            background = (options.get('priority') == 'background' or rate_limit_args == 0
                          or endpoint in {'deleteMessage', 'deleteMessages', 'sendDocument'}
                          or self._group_id(data.get('chat_id')) is not None)
            retries = options.get('max_retries', rate_limit_args if type(rate_limit_args) is int else 1)
            retries = max(0, min(3, int(retries)))
            for attempt in range(retries + 1):
                try:
                    return await self._request_attempt(callback,args,kwargs,endpoint,data,background,attempt)
                except telegram.error.RetryAfter as exc:
                    delay = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else float(exc.retry_after)
                    # Hormati cooldown Telegram. Tidak menghindari limit dengan bot/client lain.
                    async with self._condition:
                        self._blocked_until = max(self._blocked_until, _time.monotonic() + max(0, delay) + 0.1)
                        self._condition.notify_all()
                    if attempt >= retries:
                        raise
else:
    BuyerPriorityRateLimiter = None


def _telegram_background_options(bot, *, retries=0):
    limiter = getattr(bot, 'rate_limiter', None)
    if BuyerPriorityRateLimiter is not None and isinstance(limiter, BuyerPriorityRateLimiter):
        return {'rate_limit_args': {'priority': 'background', 'max_retries': retries}}
    if limiter is not None:
        return {'rate_limit_args': retries}
    return {}


# Hanya navigasi yang tidak mengubah transaksi yang boleh digabung.
def _navigation_key(update):
    query = getattr(update, 'callback_query', None)
    if query is not None:
        data = str(query.data or '')
        if (data in {'buy', 'back_to_menu', 'buyer_how', 'buyer_history', 'resend_menu'}
                or data.startswith(('buy|', 'pilih_', 'resend_menu|'))):
            return ('menu', getattr(getattr(query, 'message', None), 'message_id', None))
    message = getattr(update, 'message', None)
    if message is not None and re.fullmatch(r'/start(?:@\w+)?(?:\s.*)?', message.text or ''):
        return ('start',)
    return None


if BaseUpdateProcessor is not None:
    class _PerChatLockEntry:
        __slots__ = ("lock", "users", "navigation")
        def __init__(self):
            self.lock = asyncio.Lock()
            self.users = 0
            self.navigation = {}

    class PerChatUpdateProcessor(BaseUpdateProcessor):
        """Slot eksekusi diambil setelah antrean chat; chat sibuk tidak memonopoli slot."""
        def __init__(self, max_concurrent_updates: int):
            # Base semaphore tidak dipakai untuk membatasi task yang hanya menunggu chat.
            # Batas kerja aktif yang sebenarnya dijaga _execution_slots di bawah.
            super().__init__(2 ** 31 - 1)
            self._execution_slots = asyncio.Semaphore(max_concurrent_updates)
            # Reserve a quarter of active slots for menus during slow provider/admin work.
            reserved = max(1, max_concurrent_updates // 4)
            self._non_navigation_slots = asyncio.Semaphore(max(1, max_concurrent_updates - reserved))
            self._chat_locks = {}
            self._locks_guard = None

        async def initialize(self):
            self._locks_guard = asyncio.Lock()

        async def shutdown(self):
            self._chat_locks.clear()

        async def _get_entry(self, key):
            if self._locks_guard is None:
                self._locks_guard = asyncio.Lock()
            async with self._locks_guard:
                entry = self._chat_locks.get(key)
                if entry is None:
                    entry = _PerChatLockEntry()
                    self._chat_locks[key] = entry
                entry.users += 1
                return entry

        async def _release_entry(self, key, entry):
            async with self._locks_guard:
                entry.users = max(0, entry.users - 1)
                if entry.users == 0 and self._chat_locks.get(key) is entry:
                    self._chat_locks.pop(key, None)

        async def do_process_update(self, update, coroutine):
            trace = _callback_ingress.pop(id(update),None) or _new_callback_trace(update)
            if trace is not None:
                trace['processor_ns'] = _time.perf_counter_ns()
            chat = getattr(update, "effective_chat", None)
            user = getattr(update, "effective_user", None)
            key = (("chat", chat.id) if chat is not None else ("user", user.id) if user is not None
                   else ("update", getattr(update, "update_id", id(update))))
            entry = await self._get_entry(key)
            nav_key = _navigation_key(update)
            query = getattr(update, 'callback_query', None)
            if query is not None and nav_key is None and not str(query.data or '').startswith('confirm_buy_'):
                _forget_menu_render(query)
            token = object()
            if nav_key is not None:
                entry.navigation[nav_key] = token
            started = _time.monotonic()
            executed = False
            navigation_context = None
            trace_token = _callback_trace_state.set(trace)
            try:
                query = getattr(update, 'callback_query', None)
                if nav_key is not None and query is not None:
                    _schedule_navigation_ack(query)
                async with entry.lock:
                    if trace is not None:
                        trace['chat_lock_ns'] = _time.perf_counter_ns()
                    if nav_key is not None and entry.navigation.get(nav_key) is not token:
                        return
                    async with AsyncExitStack() as admission:
                        if nav_key is None:
                            await admission.enter_async_context(self._non_navigation_slots)
                        await admission.enter_async_context(self._execution_slots)
                        if nav_key is not None and entry.navigation.get(nav_key) is not token:
                            return
                        _catalog_update_boundary(update)
                        waited = _time.monotonic() - started
                        _record_latency("update-queue-wait", waited)
                        executed = True
                        if trace is not None:
                            trace['handler_ns'] = _time.perf_counter_ns()
                        if nav_key is not None:
                            navigation_context = _navigation_timing.set({'started': _time.monotonic(), 'rendered': False})
                        await coroutine
            except BaseException as exc:
                if trace is not None:
                    trace['outcome'] = type(exc).__name__
                raise
            finally:
                if trace is not None:
                    trace['done_ns'] = _time.perf_counter_ns()
                    trace['coalesced'] = not executed
                _callback_trace_state.reset(trace_token)
                _callback_trace_emit(trace)
                if navigation_context is not None:
                    _navigation_timing.reset(navigation_context)
                if not executed and hasattr(coroutine, 'close'):
                    coroutine.close()
                if nav_key is not None and entry.navigation.get(nav_key) is token:
                    entry.navigation.pop(nav_key, None)
                elapsed = _time.monotonic() - started
                _record_latency("telegram-update" if executed else "navigation-coalesced", elapsed)
                await self._release_entry(key, entry)
                if elapsed >= SLOW_UPDATE_THRESHOLD_SECONDS:
                    logger.warning(f"[SLOW UPDATE] key={key} update_id={getattr(update, 'update_id', '-')} durasi={elapsed:.2f}s")
else:
    PerChatUpdateProcessor = None

# =================== SINGLETON HTTP SESSION ===================
_http_session: aiohttp.ClientSession = None
_http_session_lock: asyncio.Lock = None
_autogopay_request_semaphore: asyncio.Semaphore = None


async def _get_http_session_lock() -> asyncio.Lock:
    global _http_session_lock
    if _http_session_lock is None:
        _http_session_lock = asyncio.Lock()
    return _http_session_lock


async def _get_autogopay_semaphore() -> asyncio.Semaphore:
    global _autogopay_request_semaphore
    if _autogopay_request_semaphore is None:
        _autogopay_request_semaphore = asyncio.Semaphore(AUTOGOPAY_MAX_CONCURRENT_REQUESTS)
    return _autogopay_request_semaphore


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is not None and not _http_session.closed:
        return _http_session

    lock = await _get_http_session_lock()
    async with lock:
        if _http_session is None or _http_session.closed:
            connector = aiohttp.TCPConnector(
                limit=50,
                limit_per_host=AUTOGOPAY_MAX_CONCURRENT_REQUESTS,
                ttl_dns_cache=600,
                keepalive_timeout=45,
                enable_cleanup_closed=True,
            )
            _http_session = aiohttp.ClientSession(
                connector=connector,
                timeout=AUTOGOPAY_HTTP_TIMEOUT,
            )
    return _http_session

# =================== PAYMENT PROVIDER API ===================

AUTOGOPAY_STATUS_MAP = {
    "pending": "waiting",
    "settlement": "completed",
    "expire": "expired",
    "expired": "expired",
    "cancel": "cancelled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    # ShopeePay memakai status success + paid=true, sedangkan GoPay memakai
    # settlement. Keduanya harus masuk ke status internal completed.
    "success": "completed",
    "paid": "completed",
}


def normalize_autogopay_status(status: str) -> str:
    """Ubah status AutoGoPay menjadi status internal bot."""
    raw = str(status or "").strip().lower()
    return AUTOGOPAY_STATUS_MAP.get(raw, raw or "waiting")


def parse_provider_datetime(value):
    """Parse waktu provider menjadi datetime timezone-aware WIB."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=WIB) if value.tzinfo is None else value.astimezone(WIB)

    text = str(value).strip()
    if not text:
        return None
    try:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            logger.warning(f"[PAYMENT] Format waktu provider tidak dikenali: {text}")
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=WIB)
    return parsed.astimezone(WIB)


async def _read_json_response(response: aiohttp.ClientResponse) -> dict:
    """Baca respons JSON tanpa membuat bot crash jika provider mengirim HTML/plain text."""
    raw_text = await response.text()
    if not raw_text:
        return {}
    try:
        result = json.loads(raw_text)
        return result if isinstance(result, dict) else {"data": result}
    except json.JSONDecodeError:
        logger.error(
            f"[PAYMENT] Respons non-JSON dari provider (HTTP {response.status}): "
            f"{raw_text[:500]}"
        )
        return {"success": False, "message": raw_text[:500], "_invalid_json": True}


def _autogopay_headers() -> dict:
    return {
        "Authorization": f"Bearer {AUTOGOPAY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _extract_autogopay_data(result: dict) -> dict:
    data = result.get("data")
    if isinstance(data, dict):
        nested = data.get("transaction")
        return nested if isinstance(nested, dict) else data
    transaction = result.get("transaction")
    if isinstance(transaction, dict):
        return transaction
    return result


def _normalize_autogopay_transaction(data: dict, fallback_amount: int = 0) -> dict:
    provider_status = (
        data.get("transaction_status")
        or data.get("status")
        or "pending"
    )
    transaction_id = data.get("transaction_id") or data.get("id")
    return {
        "payment_provider": "gopay",
        "provider_transaction_id": transaction_id,
        "provider_order_id": data.get("order_id"),
        "provider_status": str(provider_status).lower(),
        "status": normalize_autogopay_status(provider_status),
        "amount": _safe_int(data.get("amount"), _safe_int(fallback_amount)),
        "fee": _safe_int(data.get("fee"), 0),
        # Alias payment_number dipertahankan agar alur QR lama tetap kompatibel.
        "payment_number": data.get("qr_string") or data.get("payment_number") or "",
        "qr_string": data.get("qr_string") or data.get("payment_number") or "",
        "qr_url": data.get("qr_url"),
        "checkout_url": data.get("checkout_url"),
        "transaction_time": data.get("transaction_time") or data.get("time"),
        "expiry_time": data.get("expiry_time"),
        "raw": data,
    }


def _normalize_shopeepay_transaction(data: dict, fallback_amount: int = 0) -> dict:
    """Normalisasi respons ShopeePay yang memakai order_sn, bukan transaction_id."""
    order_sn = data.get("order_sn") or data.get("transaction_id") or data.get("id")
    paid = data.get("paid") is True or _safe_int(data.get("order_status"), 0) == 1
    if "paid" in data and data.get("paid") is not True:
        paid = False
    if "order_status" in data and _safe_int(data.get("order_status"), 0) != 1:
        paid = False
    provider_status = str(data.get("status") or ("success" if paid else "pending")).lower()
    return {
        "payment_provider": "shopeepay",
        "provider_transaction_id": str(order_sn) if order_sn is not None else None,
        "provider_order_id": str(order_sn) if order_sn is not None else None,
        "provider_status": provider_status,
        "status": "completed" if paid else "waiting",
        "amount": _safe_int(data.get("amount"), _safe_int(fallback_amount)),
        "fee": _safe_int(data.get("fee"), 0),
        "payment_number": data.get("qr_string") or "",
        "qr_string": data.get("qr_string") or "",
        "qr_url": data.get("qr_url"),
        "checkout_url": data.get("checkout_url"),
        "transaction_time": data.get("transaction_time") or data.get("paid_at"),
        "expiry_time": data.get("expiry_time"),
        "raw": data,
    }


_autogopay_circuit_failures = 0
_autogopay_circuit_open_until = 0.0
_autogopay_circuit_last_log = 0.0

def _autogopay_circuit_is_open() -> bool:
    global _autogopay_circuit_last_log
    now_mono = _time.monotonic()
    if now_mono >= _autogopay_circuit_open_until:
        return False
    if now_mono - _autogopay_circuit_last_log >= 5.0:
        _autogopay_circuit_last_log = now_mono
        logger.warning(
            f"[AUTOGOPAY] Circuit breaker aktif; skip request selama "
            f"{max(0.0, _autogopay_circuit_open_until - now_mono):.1f}s"
        )
    return True

def _autogopay_record_success():
    global _autogopay_circuit_failures, _autogopay_circuit_open_until
    _autogopay_circuit_failures = 0
    _autogopay_circuit_open_until = 0.0

def _autogopay_record_failure():
    global _autogopay_circuit_failures, _autogopay_circuit_open_until
    _autogopay_circuit_failures += 1
    if _autogopay_circuit_failures >= AUTOGOPAY_CIRCUIT_FAILURES:
        _autogopay_circuit_open_until = _time.monotonic() + AUTOGOPAY_CIRCUIT_COOLDOWN_SECONDS
        _autogopay_circuit_failures = 0
        logger.warning(
            f"[AUTOGOPAY] Circuit breaker dibuka {AUTOGOPAY_CIRCUIT_COOLDOWN_SECONDS:.0f}s "
            f"setelah kegagalan berulang."
        )


async def _autogopay_create_transaction_detailed(amount: int):
    """Return (QR, failure); timeout/5xx/malformed JSON may have created a QR."""
    if not AUTOGOPAY_API_KEY:
        logger.error("[AUTOGOPAY] AUTOGOPAY_API_KEY tidak tersedia.")
        return None, "definitive"
    if _autogopay_circuit_is_open():
        return None, "circuit"
    started = _time.monotonic()
    try:
        semaphore = await _get_autogopay_semaphore()
        async with semaphore:
            session = await get_http_session()
            async with session.post(
                f"{AUTOGOPAY_BASE_URL}/qris/generate",
                json={"amount": int(amount)},
                headers=_autogopay_headers(),
                timeout=AUTOGOPAY_HTTP_TIMEOUT,
            ) as response:
                result = await _read_json_response(response)
                if response.status < 200 or response.status >= 300 or result.get("success") is False:
                    _autogopay_record_failure()
                    logger.error(f"[AUTOGOPAY] Gagal generate QRIS HTTP {response.status}: {result}")
                    failure_data = _extract_autogopay_data(result)
                    uncertain = (
                        response.status >= 500
                        or result.get("_invalid_json")
                        or any(failure_data.get(key) for key in (
                            "transaction_id", "order_id", "id", "qr_string", "checkout_url"
                        ))
                        or result.get("success") is not False
                    )
                    return None, "uncertain" if uncertain else "definitive"
                data = _extract_autogopay_data(result)
                normalized = _normalize_autogopay_transaction(data, amount)
                if not normalized.get("provider_transaction_id") or not normalized.get("qr_string"):
                    logger.error(f"[AUTOGOPAY] Respons generate tidak lengkap: {result}")
                    _autogopay_record_failure()
                    return None, "uncertain"
                _autogopay_record_success()
                return normalized, None
    except asyncio.TimeoutError:
        _autogopay_record_failure()
        logger.error("[AUTOGOPAY] Timeout saat membuat transaksi QRIS.")
    except aiohttp.ClientError as e:
        _autogopay_record_failure()
        logger.error(f"[AUTOGOPAY] Gangguan koneksi saat membuat QRIS: {e}")
    except Exception as e:
        logger.error(f"[AUTOGOPAY] Error saat membuat QRIS: {e}", exc_info=True)
    finally:
        _record_latency("autogopay-create", _time.monotonic() - started)
    return None, "uncertain"


async def _autogopay_create_transaction(amount: int):
    """Kompatibilitas untuk call-site lama yang hanya membutuhkan data QR."""
    data, _failure = await _autogopay_create_transaction_detailed(amount)
    return data


async def _autogopay_create_shopeepay_transaction(amount: int):
    """Buat QRIS ShopeePay merchant melalui endpoint AutoGoPay."""
    if not AUTOGOPAY_API_KEY:
        return None
    started = _time.monotonic()
    try:
        semaphore = await _get_autogopay_semaphore()
        async with semaphore:
            session = await get_http_session()
            async with session.post(
                f"{AUTOGOPAY_BASE_URL}/shopeepay/qris/create",
                json={"amount": int(amount)},
                headers=_autogopay_headers(),
                timeout=AUTOGOPAY_HTTP_TIMEOUT,
            ) as response:
                result = await _read_json_response(response)
                if response.status < 200 or response.status >= 300 or result.get("success") is False:
                    logger.error(f"[SHOPEEPAY] Gagal generate QRIS HTTP {response.status}: {result}")
                    return None
                data = _extract_autogopay_data(result)
                normalized = _normalize_shopeepay_transaction(data, amount)
                if not normalized.get("provider_transaction_id") or not normalized.get("qr_string"):
                    logger.error(f"[SHOPEEPAY] Respons generate tidak lengkap: {result}")
                    return None
                return normalized
    except asyncio.TimeoutError:
        logger.error("[SHOPEEPAY] Timeout saat membuat transaksi QRIS.")
    except aiohttp.ClientError as e:
        logger.error(f"[SHOPEEPAY] Gangguan koneksi saat membuat QRIS: {e}")
    except Exception as e:
        logger.error(f"[SHOPEEPAY] Error saat membuat QRIS: {e}", exc_info=True)
    finally:
        _record_latency("shopeepay-create", _time.monotonic() - started)
    return None


async def _autogopay_get_shopeepay_status(order_sn: str, fallback_amount: int = 0):
    """Cek status QR ShopeePay berdasarkan order_sn."""
    if not AUTOGOPAY_API_KEY or not order_sn:
        return None
    started = _time.monotonic()
    try:
        semaphore = await _get_autogopay_semaphore()
        async with semaphore:
            session = await get_http_session()
            async with session.get(
                f"{AUTOGOPAY_BASE_URL}/shopeepay/qris/status",
                params={"order_sn": str(order_sn)},
                headers=_autogopay_headers(),
                timeout=AUTOGOPAY_HTTP_TIMEOUT,
            ) as response:
                result = await _read_json_response(response)
                if response.status < 200 or response.status >= 300 or result.get("success") is False:
                    logger.warning(f"[SHOPEEPAY] Gagal cek status HTTP {response.status}: {result}")
                    return None
                data = _extract_autogopay_data(result)
                if data.get("order_sn") is not None and str(data["order_sn"]) != str(order_sn):
                    logger.error("[SHOPEEPAY] ID status tidak cocok; respons ditolak.")
                    return None
                normalized = _normalize_shopeepay_transaction(data, fallback_amount)
                if normalized["status"] == "completed" and (
                    _safe_int(data.get("amount"), -1) != _safe_int(fallback_amount)
                    or _safe_int(fallback_amount) <= 0
                ):
                    logger.error("[SHOPEEPAY] Nominal status tidak cocok; respons ditolak.")
                    return None
                normalized["provider_transaction_id"] = (
                    normalized.get("provider_transaction_id") or str(order_sn)
                )
                return normalized
    except asyncio.TimeoutError:
        logger.warning(f"[SHOPEEPAY] Timeout cek status {order_sn}")
    except aiohttp.ClientError as e:
        logger.warning(f"[SHOPEEPAY] Gangguan koneksi cek status {order_sn}: {e}")
    except Exception as e:
        logger.error(f"[SHOPEEPAY] Error cek status {order_sn}: {e}", exc_info=True)
    finally:
        _record_latency("shopeepay-status", _time.monotonic() - started)
    return None


async def _autogopay_get_status(transaction_id: str, fallback_amount: int = 0):
    if not AUTOGOPAY_API_KEY or not transaction_id:
        return None
    if _autogopay_circuit_is_open():
        return None
    started = _time.monotonic()
    try:
        semaphore = await _get_autogopay_semaphore()
        async with semaphore:
            session = await get_http_session()
            async with session.post(
                f"{AUTOGOPAY_BASE_URL}/qris/status",
                json={"transaction_id": transaction_id},
                headers=_autogopay_headers(),
                timeout=AUTOGOPAY_HTTP_TIMEOUT,
            ) as response:
                result = await _read_json_response(response)
                if response.status < 200 or response.status >= 300 or result.get("success") is False:
                    if response.status == 429 or response.status >= 500:
                        _autogopay_record_failure()
                    logger.warning(f"[AUTOGOPAY] Gagal cek status HTTP {response.status}: {result}")
                    return None
                data = _extract_autogopay_data(result)
                response_id = data.get("transaction_id") or data.get("id")
                if response_id is not None and str(response_id) != str(transaction_id):
                    logger.error("[AUTOGOPAY] ID status tidak cocok; respons ditolak.")
                    return None
                normalized = _normalize_autogopay_transaction(data, fallback_amount)
                # The status docs do not require an amount field. Preserve the
                # authenticated transaction lookup contract when it is absent;
                # an explicit amount must still match the persisted invoice.
                if normalized["status"] == "completed" and (
                    _safe_int(fallback_amount) <= 0 or
                    ("amount" in data and _safe_int(data.get("amount"), -1) != _safe_int(fallback_amount))
                ):
                    logger.error("[AUTOGOPAY] Nominal status tidak cocok; respons ditolak.")
                    return None
                normalized["provider_transaction_id"] = normalized.get("provider_transaction_id") or transaction_id
                _autogopay_record_success()
                return normalized
    except asyncio.TimeoutError:
        _autogopay_record_failure()
        logger.warning(f"[AUTOGOPAY] Timeout cek status transaksi {transaction_id}")
    except aiohttp.ClientError as e:
        _autogopay_record_failure()
        logger.warning(f"[AUTOGOPAY] Gangguan koneksi cek status {transaction_id}: {e}")
    except Exception as e:
        logger.error(f"[AUTOGOPAY] Error cek status {transaction_id}: {e}", exc_info=True)
    finally:
        _record_latency("autogopay-status", _time.monotonic() - started)
    return None


async def _autogopay_cancel_transaction(transaction_id: str):
    if not AUTOGOPAY_API_KEY or not transaction_id:
        return None
    # Cancel tetap dicoba saat circuit open karena ini operasi kompensasi penting.
    started = _time.monotonic()
    try:
        semaphore = await _get_autogopay_semaphore()
        async with semaphore:
            session = await get_http_session()
            async with session.post(
                f"{AUTOGOPAY_BASE_URL}/qris/cancel",
                json={"transaction_id": transaction_id},
                headers=_autogopay_headers(),
                timeout=AUTOGOPAY_HTTP_TIMEOUT,
            ) as response:
                result = await _read_json_response(response)
                if response.status < 200 or response.status >= 300 or result.get("success") is False:
                    if response.status == 429 or response.status >= 500:
                        _autogopay_record_failure()
                    logger.warning(f"[AUTOGOPAY] Gagal cancel HTTP {response.status}: {result}")
                    return None
                _autogopay_record_success()
                return result
    except asyncio.TimeoutError:
        _autogopay_record_failure()
        logger.warning(f"[AUTOGOPAY] Timeout cancel transaksi {transaction_id}")
    except aiohttp.ClientError as e:
        _autogopay_record_failure()
        logger.warning(f"[AUTOGOPAY] Gangguan koneksi cancel {transaction_id}: {e}")
    except Exception as e:
        logger.error(f"[AUTOGOPAY] Error cancel {transaction_id}: {e}", exc_info=True)
    finally:
        _record_latency("autogopay-cancel", _time.monotonic() - started)
    return None


async def create_transaction_qris(order_id, amount, description):
    """Buat QR GoPay sebagai utama, ShopeePay sebagai fallback aman."""
    del order_id, description
    gopay_data, failure_kind = await _autogopay_create_transaction_detailed(amount)
    if gopay_data:
        return gopay_data
    if SHOPEEPAY_FALLBACK_ENABLED and failure_kind in {"definitive", "circuit"}:
        logger.warning("[PAYMENT] GoPay gagal; mencoba fallback ShopeePay.")
        return await _autogopay_create_shopeepay_transaction(amount)
    logger.error(
        "[PAYMENT] GoPay tidak memberi hasil pasti (%s); fallback ShopeePay dilewati.",
        failure_kind or "unknown",
    )
    return None


async def get_transaction_detail(order_id, amount):
    """Cek status transaksi sesuai provider yang tersimpan."""
    order = await get_order_by_id(order_id)
    if not order:
        return None
    transaction_id = order.get("provider_transaction_id")
    if str(order.get("payment_provider") or "").lower() == "shopeepay":
        detail = await _autogopay_get_shopeepay_status(transaction_id, amount)
    else:
        detail = await _autogopay_get_status(transaction_id, amount)
    if detail:
        await update_order_provider_status(order_id, detail.get("provider_status"))
    return detail


async def cancel_transaction(order_id, amount=0):
    """Batalkan transaksi GoPay; ShopeePay tidak memiliki endpoint cancel di docs."""
    del amount
    order = await get_order_by_id(order_id)
    if not order:
        return None
    transaction_id = order.get("provider_transaction_id")
    if str(order.get("payment_provider") or "").lower() == "shopeepay":
        logger.info("[SHOPEEPAY] Tidak ada endpoint cancel; perubahan status hanya lokal di bot.")
        return None
    result = await _autogopay_cancel_transaction(transaction_id)
    if result is not None:
        await update_order_provider_status(order_id, "cancel")
    return result

# =================== QR CODE GENERATOR ===================

def generate_qr_image(qris_string):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qris_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

# =================== DATABASE POOL & CONTEXT MANAGER ===================

_pool: ThreadedConnectionPool = None
_async_db_pool = None
_async_db_failures = 0
_async_db_backoff_until = 0.0


async def init_async_db_pool():
    """Aktifkan native async PostgreSQL untuk hot read jika asyncpg tersedia; fallback aman jika tidak."""
    global _async_db_pool
    if asyncpg is None:
        logger.info("[DB ASYNC] asyncpg tidak terpasang; memakai executor psycopg2 yang stabil.")
        return False
    try:
        _async_db_pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=1,
            max_size=ASYNCPG_POOL_MAX,
            command_timeout=8,
        )
        logger.info(f"[DB ASYNC] Native async pool aktif (max={ASYNCPG_POOL_MAX}).")
        return True
    except Exception as exc:
        _async_db_pool = None
        logger.warning(f"[DB ASYNC] Gagal aktifkan asyncpg; fallback psycopg2: {exc}")
        return False


async def _asyncpg_read(method, query, args):
    global _async_db_failures, _async_db_backoff_until
    if _async_db_pool is None or _time.monotonic() < _async_db_backoff_until:
        return None, False
    started = _time.monotonic()
    try:
        async with _async_db_pool.acquire(timeout=ASYNCPG_ACQUIRE_TIMEOUT_SECONDS) as conn:
            _record_latency('db-acquire', _time.monotonic() - started)
            query_started = _time.monotonic()
            try:
                result = await getattr(conn, method)(query, *args, timeout=ASYNCPG_READ_TIMEOUT_SECONDS)
            finally:
                _record_latency('db-query', _time.monotonic() - query_started)
        _async_db_failures = 0
        if method == 'fetch':
            return [dict(row) for row in result], True
        return (dict(result) if result else None), True
    except Exception as exc:
        _async_db_failures += 1
        if _async_db_failures >= 2:
            _async_db_backoff_until = _time.monotonic() + 10.0
        logger.warning('[DB ASYNC] Read fallback (%s); failures=%s', type(exc).__name__, _async_db_failures)
        return None, False


async def _asyncpg_fetchrow(query: str, *args):
    return await _asyncpg_read('fetchrow', query, args)


async def _asyncpg_fetch(query: str, *args):
    return await _asyncpg_read('fetch', query, args)


def init_pool():
    global _pool
    _pool = ThreadedConnectionPool(
        DB_POOL_MIN,
        DB_POOL_MAX,
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=10,
        options=(
            f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS} "
            "-c idle_in_transaction_session_timeout=10000"
        ),
        application_name="hyperfamily_bot",
    )
    logger.info(
        f"[DB] Connection pool aktif (min={DB_POOL_MIN}, max={DB_POOL_MAX}, "
        f"workers={DB_THREAD_WORKERS})"
    )


@contextmanager
def db_session_safe(retries=3, delay=0.15):
    """
    Koneksi memakai autocommit agar query SELECT tidak melakukan COMMIT tambahan.
    Semua fungsi database tetap dijalankan di thread worker melalui async_wrap/to_thread.
    """
    conn = None
    started = _time.monotonic()
    for attempt in range(retries):
        try:
            conn = _pool.getconn()
            if conn.closed:
                _pool.putconn(conn, close=True)
                conn = None
                raise psycopg2.InterfaceError("Koneksi database tertutup")
            # Pastikan koneksi yang kembali dari pool tidak membawa transaksi lama.
            try:
                conn.rollback()
            except Exception:
                pass
            conn.autocommit = True
            break
        except (psycopg2.pool.PoolError, psycopg2.InterfaceError, psycopg2.OperationalError):
            if attempt == retries - 1:
                logger.error("[DB CRITICAL] Gagal memperoleh koneksi database setelah retry.")
                raise
            time_sleep = delay * (2 ** attempt)
            _time.sleep(time_sleep)
    try:
        yield conn
    except Exception as e:
        if conn and not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error(f"[DB ERROR] Operasi database gagal: {e}", exc_info=True)
        raise
    finally:
        if conn and _pool:
            try:
                conn.autocommit = True
            except Exception:
                pass
            _pool.putconn(conn, close=bool(conn.closed))
        elapsed = _time.monotonic() - started
        if elapsed >= DB_SLOW_SESSION_SECONDS:
            logger.warning(f"[SLOW DB] Sesi database memakan {elapsed:.2f}s")

def async_wrap(func):
    @functools.wraps(func)
    async def run(*args, **kwargs):
        return await run_db(func, *args, **kwargs)
    return run

def async_wrap_admin(func):
    @functools.wraps(func)
    async def run(*args, **kwargs):
        return await run_db_admin(func, *args, **kwargs)
    return run

def init_db():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    paket_id TEXT PRIMARY KEY,
                    nama TEXT NOT NULL,
                    emoji TEXT DEFAULT '📦',
                    deskripsi TEXT DEFAULT '',
                    harga INTEGER NOT NULL,
                    link TEXT DEFAULT 'https://t.me/Gilbiie',
                    group_chat_id TEXT DEFAULT NULL,
                    aktif BOOLEAN DEFAULT TRUE
                )
            """)
            c.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS link TEXT DEFAULT 'https://t.me/Gilbiie'")
            c.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS group_chat_id TEXT DEFAULT NULL")
            c.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS aktif BOOLEAN DEFAULT TRUE")
            c.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS requires_paket_ids TEXT DEFAULT NULL")

            c.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id BIGINT PRIMARY KEY,
                    reason TEXT DEFAULT '',
                    banned_at TEXT NOT NULL
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    user_name TEXT,
                    paket_id TEXT,
                    order_id TEXT UNIQUE,
                    status TEXT DEFAULT 'waiting',
                    waktu TEXT,
                    admin_msg_id BIGINT DEFAULT NULL,
                    buyer_msg_id BIGINT DEFAULT NULL,
                    sent_link TEXT DEFAULT NULL,
                    harga_dibayar INTEGER DEFAULT 0,
                    order_changes INTEGER DEFAULT 0,
                    payment_provider TEXT DEFAULT 'autogopay',
                    provider_transaction_id TEXT DEFAULT NULL,
                    provider_order_id TEXT DEFAULT NULL,
                    provider_status TEXT DEFAULT NULL,
                    payment_expires_at TIMESTAMPTZ DEFAULT NULL,
                    checkout_url TEXT DEFAULT NULL,
                    qr_url TEXT DEFAULT NULL,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS admin_msg_id BIGINT DEFAULT NULL")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS buyer_msg_id BIGINT DEFAULT NULL")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS sent_link TEXT DEFAULT NULL")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_status TEXT DEFAULT 'not_sent'")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_error TEXT DEFAULT NULL")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS harga_dibayar INTEGER DEFAULT 0")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_changes INTEGER DEFAULT 0")
            # Metadata provider dipertahankan untuk audit transaksi AutoGoPay.
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_provider TEXT DEFAULT 'autogopay'")
            c.execute("ALTER TABLE orders ALTER COLUMN payment_provider SET DEFAULT 'autogopay'")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS provider_transaction_id TEXT DEFAULT NULL")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS provider_order_id TEXT DEFAULT NULL")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS provider_status TEXT DEFAULT NULL")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_expires_at TIMESTAMPTZ DEFAULT NULL")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS checkout_url TEXT DEFAULT NULL")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS qr_url TEXT DEFAULT NULL")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP")


            c.execute("""
                CREATE TABLE IF NOT EXISTS testimonials (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    user_name TEXT,
                    paket_id TEXT,
                    order_id TEXT UNIQUE,
                    rating INTEGER,
                    review TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS cooldowns (
                    user_id BIGINT PRIMARY KEY,
                    expires_at TIMESTAMPTZ NOT NULL
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY,
                    nama TEXT DEFAULT '',
                    added_by BIGINT NOT NULL,
                    added_at TEXT NOT NULL
                )
            """)

            # Indexes dibuat setelah semua tabel ada agar fresh deploy tidak gagal.
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status)")
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_active_user_latest
                ON orders(user_id, id DESC)
                WHERE status IN ('waiting','pending')
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_delivery_status ON orders(delivery_status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_payment_provider ON orders(payment_provider)")
            c.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_provider_transaction_id
                ON orders(provider_transaction_id)
                WHERE provider_transaction_id IS NOT NULL
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_testimonials_status ON testimonials(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cooldowns_user_id ON cooldowns(user_id)")

            for key, val in [
                ('link_testimoni', 'https://t.me/+7zsdSrwYIG8wOTg1'),
                ('link_admin', 'https://t.me/Gilbiie'),
                ('main_group_button_name', '👥 Grup Hyper Family'),
                ('main_group_button_link', ''),
                ('testimoni_channel_id', ''),
            ]:
                c.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (key, val)
                )

            # Migrasikan link admin lama agar tombol/menu yang tersimpan di DB
            # ikut menunjuk ke username baru.
            c.execute(
                "UPDATE settings SET value=%s WHERE key='link_admin' AND value IN (%s, %s)",
                (ADMIN_URL, 'https://t.me/Kikukkvd', '@Kikukkvd')
            )
            c.execute(
                "UPDATE products SET link=%s WHERE link IN (%s, %s)",
                (ADMIN_URL, 'https://t.me/Kikukkvd', '@Kikukkvd')
            )

            c.execute("SELECT COUNT(*) as cnt FROM products")
            row = c.fetchone()
            if row["cnt"] == 0:
                c.executemany(
                    """INSERT INTO products (paket_id, nama, emoji, deskripsi, harga, link)
                       VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                    [
                        ("gb_biasa", "GB Biasa", "🔥", "160+ Video Premium", 5000, DEFAULT_LINK),
                        ("gb_vip",   "GB VIP",   "👑", "6.800+ Video Premium", 25000, DEFAULT_LINK),
                    ]
                )

# =================== TESTIMONIAL DB FUNCTIONS ===================

@async_wrap
def save_testimonial(user_id, user_name, paket_id, order_id, rating, review):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO testimonials (user_id, user_name, paket_id, order_id, rating, review, status)
                   VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                   ON CONFLICT (order_id) DO UPDATE SET rating=EXCLUDED.rating, review=EXCLUDED.review, status='pending'""",
                (user_id, user_name, paket_id, order_id, rating, review)
            )

@async_wrap
def update_testimonial_status(order_id, status):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE testimonials SET status=%s WHERE order_id=%s AND status='pending'", (status, order_id))

@async_wrap
def get_testimonial_by_order(order_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM testimonials WHERE order_id=%s", (order_id,))
            row = c.fetchone()
            return dict(row) if row else None


def _claim_public_testimonial_sync(order_id, manual=False):
    """Use the existing unique order_id as a durable publication claim; no DDL."""
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO testimonials (user_id, user_name, paket_id, order_id, rating, review, status)
                SELECT user_id, '', paket_id, order_id, NULL, '', 'publishing'
                FROM orders
                WHERE order_id=%s AND status='completed'
                ON CONFLICT (order_id) DO UPDATE SET status='publishing'
                WHERE %s AND testimonials.status IN ('pending', 'publish_failed')
                RETURNING order_id
            """, (order_id, bool(manual)))
            return c.fetchone() is not None


def _finish_public_testimonial_sync(order_id, status):
    if status not in {'approved', 'publish_failed', 'publish_uncertain'}:
        raise ValueError('Status publikasi tidak valid')
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE testimonials SET status=%s WHERE order_id=%s AND status='publishing'",
                      (status, order_id))


def _format_public_transaction(order, paket):
    amount = order.get('harga_dibayar')
    total = format_harga(amount) if amount is not None else '—'
    waktu = f"{_compact_waktu(order['waktu'])} WIB" if order.get('waktu') else '—'
    product = str(paket.get('nama') or 'Produk')[:300]
    return (
        "💠 <b>Hyper Store</b>\n"
        "✅ <b>Transaksi berhasil</b>\n\n"
        "<blockquote><b>RINCIAN PEMBELIAN</b>\n"
        f"ID Buyer: <code>{esc(order['user_id'])}</code>\n"
        f"Produk: <b>{esc(product)}</b>\n"
        f"Total: <b>{total}</b>\n"
        "Pembayaran: Diterima\n"
        "Akses: Berhasil dikirim\n"
        f"Waktu order: {esc(waktu)}</blockquote>\n\n"
        "<i>Terima kasih telah berbelanja di Hyper Store.</i>"
    )


async def _publish_public_testimonial(bot, order_id, *, manual=False):
    """Best effort public receipt, independent of payment/delivery and admin routing.

    An uncertain send stays claimed. Never retry a timeout or reset an order.
    Existing review rows and schemas are retained for compatibility.
    """
    try:
        channel_id, admin_channel, admin_link, order = await asyncio.gather(
            get_setting('testimoni_channel_id'), get_setting('notif_channel_id'),
            get_setting('link_admin', ADMIN_URL), get_order_by_id(order_id),
        )
        if not channel_id or not str(channel_id).strip():
            return 'disabled'
        target = int(channel_id)
        if target >= 0 or target == ADMIN_ID or str(target) == str(admin_channel or '').strip():
            logger.warning('[PUBLIC TESTI] Channel publik harus berbeda dari tujuan notifikasi admin.')
            return 'disabled'
        # The public receipt follows payment confirmation. Its requested display
        # label does not alter the actual delivery state or prerequisite checks.
        if not order or order.get('status') != 'completed':
            return 'not_ready'
        paket = await get_product(order['paket_id']) or {'nama':'Produk','harga':0}
        username = getattr(bot, 'username', None)
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_]+', username):
            return 'not_ready'
        text = _format_public_transaction(order, paket)
        keyboard = InlineKeyboardMarkup([[
            styled_button('🛒 Order Sekarang', style='success', url=f'https://t.me/{username}?start=order'),
            styled_button('💬 Hubungi Admin', style='primary', url=admin_link or ADMIN_URL),
        ]])
        if not await run_db(_claim_public_testimonial_sync, order_id, manual):
            return 'already'
        try:
            await telegram_send_rate_retry(
                lambda: bot.send_message(chat_id=target, text=text, parse_mode='HTML',
                                         reply_markup=keyboard, disable_web_page_preview=True,
                                         **_telegram_background_options(bot)),
                label='testimoni transaksi publik',
            )
        except asyncio.CancelledError:
            # The durable publishing claim remains; reconciliation must be manual.
            raise
        except Exception as exc:
            definite = isinstance(exc, (telegram.error.BadRequest, telegram.error.Forbidden, telegram.error.RetryAfter))
            await run_db(_finish_public_testimonial_sync, order_id,
                         'publish_failed' if definite else 'publish_uncertain')
            logger.warning('[PUBLIC TESTI] Publikasi gagal: %s', type(exc).__name__)
            return 'failed' if definite else 'uncertain'
        try:
            await run_db(_finish_public_testimonial_sync, order_id, 'approved')
        except Exception as exc:
            # Already sent: leaving publishing reserved is safer than a second post.
            logger.warning('[PUBLIC TESTI] Terkirim; pencatatan belum pasti: %s', type(exc).__name__)
        return 'sent'
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning('[PUBLIC TESTI] Tidak dapat mempublikasikan: %s', type(exc).__name__)
        return 'failed'

# =================== PRODUCT DB FUNCTIONS ===================

# In-memory TTL cache: cache hit dibaca langsung dari event loop tanpa antre executor DB.
_settings_cache: dict = {}
_SETTINGS_TTL = 60
_settings_cache_locks: dict = {}
_products_cache: tuple = None
_products_by_id_cache: tuple = None
_PRODUCTS_TTL = 30
_products_cache_lock: asyncio.Lock = None
_products_cache_generation = 0
_buyer_products_view = None
_settings_cache_versions: dict = {}

def _settings_cache_get(key):
    entry = _settings_cache.get(key)
    if entry and _time.monotonic() < entry[1]:
        return True, entry[0]
    if entry:
        _settings_cache.pop(key, None)
    return False, None

def _settings_cache_set(key, value):
    # Settings jumlahnya kecil; hapus entry kedaluwarsa agar cache tidak tumbuh tanpa batas.
    now_mono = _time.monotonic()
    if len(_settings_cache) > 256:
        for old_key, old_entry in list(_settings_cache.items()):
            if now_mono >= old_entry[1]:
                _settings_cache.pop(old_key, None)
    _settings_cache[key] = (value, now_mono + _SETTINGS_TTL)

def _settings_cache_del(key):
    _settings_cache_versions[key] = _settings_cache_versions.get(key, 0) + 1
    _settings_cache.pop(key, None)

def _load_all_products_sync():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM products ORDER BY harga ASC")
            return [dict(r) for r in c.fetchall()]

async def get_all_products():
    global _products_cache, _products_by_id_cache, _products_cache_lock
    now_mono = _time.monotonic()
    cached = _products_cache
    if cached and now_mono < cached[1]:
        return copy.deepcopy(cached[0])

    if _products_cache_lock is None:
        _products_cache_lock = asyncio.Lock()
    async with _products_cache_lock:
        now_mono = _time.monotonic()
        cached = _products_cache
        if cached and now_mono < cached[1]:
            return copy.deepcopy(cached[0])

        while True:
            generation = _products_cache_generation
            data, used_async = await _asyncpg_fetch("SELECT * FROM products ORDER BY harga ASC")
            if not used_async:
                data = await run_db(_load_all_products_sync)
            # Jika admin mengubah produk saat query berjalan, ulangi agar cache tidak berisi snapshot lama.
            if generation == _products_cache_generation:
                _products_cache = (data, _time.monotonic() + _PRODUCTS_TTL)
                # Lookup detail paket tidak perlu menyalin dan memindai seluruh daftar produk.
                # Cache ini selalu dibuat bersamaan dengan cache list agar snapshot konsisten.
                _products_by_id_cache = (
                    {item.get('paket_id'): item for item in data if item.get('paket_id') is not None},
                    _time.monotonic() + _PRODUCTS_TTL,
                )
                return copy.deepcopy(data)

def _invalidate_products_cache():
    global _products_cache, _products_by_id_cache, _products_cache_generation, _buyer_products_view
    _products_cache_generation += 1
    _products_cache = None
    _products_by_id_cache = None
    _buyer_products_view = None


async def _get_buyer_products_page(page, page_size=8):
    """Use the same TTL/invalidation as products; copy only the visible page."""
    global _buyer_products_view
    snapshot = _products_cache
    if snapshot is None or _time.monotonic() >= snapshot[1]:
        products = await get_all_products()
        snapshot = _products_cache
    else:
        products = snapshot[0]
    if snapshot is not None and _time.monotonic() < snapshot[1]:
        if _buyer_products_view is None or _buyer_products_view[0] is not snapshot:
            _buyer_products_view = (snapshot, tuple(p for p in snapshot[0] if p.get('aktif', True)))
        active = _buyer_products_view[1]
    else:
        active = tuple(p for p in products if p.get('aktif', True))
    pages = max(1, (len(active) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    shown = [dict(p) for p in active[page * page_size:(page + 1) * page_size]]
    return page, pages, shown

async def get_product(paket_id):
    # Jalur tombol detail memakai index in-memory sehingga tidak perlu deep-copy + scan semua produk.
    # Saat cache belum terisi, fallback ke get_all_products tetap menjaga perilaku lama.
    cached_by_id = _products_by_id_cache
    if cached_by_id and _time.monotonic() < cached_by_id[1]:
        item = cached_by_id[0].get(paket_id)
        return copy.deepcopy(item) if item is not None else None

    # Single-flight get_all_products memastikan banyak callback saat TTL habis hanya memicu satu query.
    products = await get_all_products()
    for item in products:
        if item.get('paket_id') == paket_id:
            return item
    return None

def _add_product_sync(paket_id, nama, emoji, deskripsi, harga, link=None, group_chat_id=None):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO products (paket_id, nama, emoji, deskripsi, harga, link, group_chat_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (paket_id) DO UPDATE SET
                       nama=EXCLUDED.nama, emoji=EXCLUDED.emoji,
                       deskripsi=EXCLUDED.deskripsi, harga=EXCLUDED.harga,
                       link=EXCLUDED.link, group_chat_id=EXCLUDED.group_chat_id""",
                (paket_id, nama, emoji, deskripsi, harga, link or DEFAULT_LINK, group_chat_id)
            )

async def add_product(paket_id, nama, emoji, deskripsi, harga, link=None, group_chat_id=None):
    await run_db_admin(_add_product_sync, paket_id, nama, emoji, deskripsi, harga, link, group_chat_id)
    _invalidate_products_cache()

def _update_product_field_sync(paket_id, field, value):
    field_map = {
        "nama": "nama", "emoji": "emoji", "deskripsi": "deskripsi",
        "harga": "harga", "link": "link", "group_chat_id": "group_chat_id",
        "aktif": "aktif", "requires_paket_ids": "requires_paket_ids"
    }
    safe_field = field_map.get(field)
    if not safe_field:
        logger.warning(f"[SECURITY WARNING] Field SQL tidak diizinkan: {field}")
        return False
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(f"UPDATE products SET {safe_field}=%s WHERE paket_id=%s", (value, paket_id))
    return True

async def update_product_field(paket_id, field, value):
    updated = await run_db_admin(_update_product_field_sync, paket_id, field, value)
    if updated:
        _invalidate_products_cache()
    return updated

def _delete_product_sync(paket_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM products WHERE paket_id=%s", (paket_id,))

async def delete_product(paket_id):
    await run_db_admin(_delete_product_sync, paket_id)
    _invalidate_products_cache()

def make_paket_id(nama):
    pid = re.sub(r'[^a-z0-9]+', '_', nama.lower().strip()).strip('_')
    return pid or "produk"

# =================== ORDER DB FUNCTIONS ===================

_active_order_cache: dict = {}
_active_order_cache_locks = None
_active_order_cache_versions = [0] * 64
_active_order_cache_epoch = 0
_waiting_orders_cache = None
_waiting_orders_cache_lock: asyncio.Lock = None
_waiting_orders_cache_generation = 0
_start_snapshot_cache: dict = {}
_start_snapshot_cache_locks = None
_start_snapshot_versions = [0] * 256
START_SNAPSHOT_CACHE_TTL_SECONDS = _env_float("START_SNAPSHOT_CACHE_TTL_SECONDS", 2.0, 0.5, 10.0)

def _get_active_cache_lock(user_id: int) -> asyncio.Lock:
    global _active_order_cache_locks
    if _active_order_cache_locks is None:
        _active_order_cache_locks = [asyncio.Lock() for _ in range(64)]
    return _active_order_cache_locks[int(user_id) % len(_active_order_cache_locks)]

def _active_order_cache_get(user_id: int):
    entry = _active_order_cache.get(int(user_id))
    if entry and _time.monotonic() < entry[1]:
        return True, copy.deepcopy(entry[0])
    if entry:
        _active_order_cache.pop(int(user_id), None)
    return False, None

def _active_order_cache_set(user_id: int, order):
    now_mono = _time.monotonic()
    if len(_active_order_cache) > 10000:
        for uid, entry in list(_active_order_cache.items()):
            if now_mono >= entry[1]:
                _active_order_cache.pop(uid, None)
    _active_order_cache[int(user_id)] = (copy.deepcopy(order), now_mono + ACTIVE_ORDER_CACHE_TTL_SECONDS)

def _active_order_version_slot(user_id: int) -> int:
    return int(user_id) % len(_active_order_cache_versions)

def _active_order_cache_version(user_id: int):
    return (_active_order_cache_epoch, _active_order_cache_versions[_active_order_version_slot(user_id)])

def _invalidate_active_order_cache(user_id=None):
    global _active_order_cache_epoch
    _invalidate_start_snapshot_cache(user_id)
    if user_id is None:
        _active_order_cache_epoch += 1
        _active_order_cache.clear()
        for idx in range(len(_active_order_cache_versions)):
            _active_order_cache_versions[idx] += 1
    else:
        uid = int(user_id)
        slot = _active_order_version_slot(uid)
        _active_order_cache_versions[slot] += 1
        _active_order_cache.pop(uid, None)

def _invalidate_waiting_orders_cache():
    global _waiting_orders_cache, _waiting_orders_cache_generation
    _waiting_orders_cache_generation += 1
    _waiting_orders_cache = None

def _get_active_order_sync(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT * FROM orders WHERE user_id=%s AND status IN ('waiting','pending') ORDER BY id DESC LIMIT 1",
                (user_id,)
            )
            row = c.fetchone()
            return dict(row) if row else None

async def get_active_order(user_id):
    hit, value = _active_order_cache_get(user_id)
    if hit:
        return value

    lock = _get_active_cache_lock(user_id)
    async with lock:
        hit, value = _active_order_cache_get(user_id)
        if hit:
            return value
        while True:
            version = _active_order_cache_version(user_id)
            value, used_async = await _asyncpg_fetchrow(
                "SELECT * FROM orders WHERE user_id=$1 AND status IN ('waiting','pending') ORDER BY id DESC LIMIT 1",
                int(user_id),
            )
            if not used_async:
                value = await run_db(_get_active_order_sync, user_id)
            if version == _active_order_cache_version(user_id):
                _active_order_cache_set(user_id, value)
                return copy.deepcopy(value)


def _get_start_snapshot_lock(user_id: int) -> asyncio.Lock:
    global _start_snapshot_cache_locks
    if _start_snapshot_cache_locks is None:
        _start_snapshot_cache_locks = weakref.WeakValueDictionary()
    uid = int(user_id)
    lock = _start_snapshot_cache_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _start_snapshot_cache_locks[uid] = lock
    return lock


def _start_snapshot_version(user_id):
    return _start_snapshot_versions[int(user_id) % len(_start_snapshot_versions)]


def _start_snapshot_cache_get(user_id: int):
    uid = int(user_id)
    entry = _start_snapshot_cache.get(uid)
    if entry and _time.monotonic() < entry[1]:
        return True, copy.deepcopy(entry[0])
    if entry:
        _start_snapshot_cache.pop(uid, None)
    return False, None


def _start_snapshot_cache_set(user_id: int, snapshot: dict):
    uid = int(user_id)
    now_mono = _time.monotonic()
    if len(_start_snapshot_cache) > 10000:
        for old_uid, entry in list(_start_snapshot_cache.items()):
            if now_mono >= entry[1]:
                _start_snapshot_cache.pop(old_uid, None)
    _start_snapshot_cache[uid] = (
        copy.deepcopy(snapshot),
        now_mono + START_SNAPSHOT_CACHE_TTL_SECONDS,
    )


def _invalidate_start_snapshot_cache(user_id=None):
    if user_id is None:
        for index in range(len(_start_snapshot_versions)):
            _start_snapshot_versions[index] += 1
        _start_snapshot_cache.clear()
    else:
        uid = int(user_id)
        _start_snapshot_versions[uid % len(_start_snapshot_versions)] += 1
        _start_snapshot_cache.pop(uid, None)


_START_SNAPSHOT_SQL = """
SELECT
    EXISTS (
        SELECT 1 FROM banned_users
        WHERE user_id = $1
    ) AS banned,
    COALESCE(
        GREATEST(
            0,
            CEIL(EXTRACT(EPOCH FROM
                (c.expires_at - CURRENT_TIMESTAMP)
            ) / 60)
        ),
        0
    )::int AS cooldown_minutes,
    c.expires_at AS cooldown_expires_at,
    CASE
        WHEN o.id IS NULL THEN NULL
        ELSE row_to_json(o)::text
    END AS active_json
FROM (SELECT 1) AS seed
LEFT JOIN LATERAL (
    SELECT expires_at
    FROM cooldowns
    WHERE user_id = $1
    LIMIT 1
) c ON TRUE
LEFT JOIN LATERAL (
    SELECT *
    FROM orders
    WHERE user_id = $1
      AND status IN ('waiting', 'pending')
    ORDER BY id DESC
    LIMIT 1
) o ON TRUE
"""


def _decode_start_snapshot(row):
    if not row:
        return {
            "banned": False,
            "cooldown_minutes": 0,
            "cooldown_expires_at": None,
            "active": None,
        }
    active = None
    active_json = row.get("active_json")
    if active_json:
        try:
            active = json.loads(active_json)
        except (TypeError, ValueError) as exc:
            logger.warning(f"[START] Gagal membaca snapshot active order: {exc}")
    return {
        "banned": bool(row.get("banned")),
        "cooldown_minutes": int(row.get("cooldown_minutes") or 0),
        "cooldown_expires_at": row.get("cooldown_expires_at"),
        "active": active,
    }


def _prime_start_related_caches(user_id: int, snapshot: dict):
    """Isi cache lama juga agar klik berikutnya tidak mengulang query yang sama."""
    uid = int(user_id)
    _banned_cache[uid] = (
        bool(snapshot.get("banned")),
        _time.monotonic() + _BANNED_CACHE_TTL,
    )
    _active_order_cache_set(uid, snapshot.get("active"))

    expires_at = snapshot.get("cooldown_expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            expires_at = parse_provider_datetime(expires_at)
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at > now_utc():
            _set_cooldown_memory(uid, expires_at.astimezone(timezone.utc))
            _cooldown_empty_cache.pop(uid, None)
            return
    _clear_cooldown_memory(uid)
    _cooldown_empty_cache[uid] = _time.monotonic() + COOLDOWN_EMPTY_TTL_SECONDS


def _get_start_snapshot_sync(user_id: int):
    # psycopg2 memakai placeholder %s, tetapi tetap satu query/round-trip.
    query = _START_SNAPSHOT_SQL.replace("$1", "%s")
    with db_session_safe() as conn:
        with conn.cursor() as c:
            uid = int(user_id)
            c.execute(query, (uid, uid, uid))
            return _decode_start_snapshot(c.fetchone())


async def get_start_snapshot(user_id: int):
    hit, value = _start_snapshot_cache_get(user_id)
    if hit:
        _prime_start_related_caches(int(user_id), value)
        return value

    async with _get_start_snapshot_lock(user_id):
        hit, value = _start_snapshot_cache_get(user_id)
        if hit:
            _prime_start_related_caches(int(user_id), value)
            return value
        while True:
            version = _start_snapshot_version(user_id)
            row, used_async = await _asyncpg_fetchrow(_START_SNAPSHOT_SQL, int(user_id))
            if used_async:
                value = _decode_start_snapshot(row)
            else:
                value = await run_db(_get_start_snapshot_sync, int(user_id))
            if version == _start_snapshot_version(user_id):
                _prime_start_related_caches(int(user_id), value)
                _start_snapshot_cache_set(user_id, value)
                return copy.deepcopy(value)

@async_wrap_admin
def get_all_pending():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT o.*, p.nama as paket_nama, p.emoji as paket_emoji, p.harga as paket_harga,
                       p.deskripsi as paket_deskripsi
                FROM orders o
                LEFT JOIN products p ON o.paket_id = p.paket_id
                WHERE o.status='pending'
                ORDER BY o.id ASC
            """)
            return [dict(r) for r in c.fetchall()]

_admin_order_page_cache: dict = {}
_admin_order_page_cache_generation = 0

def _invalidate_admin_order_page_cache():
    global _admin_order_page_cache_generation
    _admin_order_page_cache_generation += 1
    _admin_order_page_cache.clear()

def _get_admin_order_page_sync(status: str, limit: int, offset: int):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                SELECT o.*, p.nama as paket_nama, p.emoji as paket_emoji, p.harga as paket_harga,
                       p.deskripsi as paket_deskripsi, COUNT(*) OVER() AS total_count
                FROM orders o
                LEFT JOIN products p ON o.paket_id = p.paket_id
                WHERE o.status=%s
                ORDER BY o.id ASC
                LIMIT %s OFFSET %s
                """,
                (status, limit, offset),
            )
            return [dict(r) for r in c.fetchall()]

async def get_admin_order_page(status: str, page: int = 0, page_size: int = ADMIN_ORDER_PAGE_SIZE):
    """Ambil hanya data yang benar-benar ditampilkan di panel, bukan seluruh tabel order."""
    if status not in {"waiting", "pending"}:
        raise ValueError("Status panel order tidak valid")
    page = max(0, int(page))
    page_size = max(1, min(20, int(page_size)))
    generation = _admin_order_page_cache_generation
    key = (status, page, page_size, generation)
    now_mono = _time.monotonic()
    cached = _admin_order_page_cache.get(key)
    if cached and now_mono < cached[1]:
        return copy.deepcopy(cached[0]), cached[2]

    offset = page * page_size
    rows, used_async = await _asyncpg_fetch(
        """
        SELECT o.*, p.nama as paket_nama, p.emoji as paket_emoji, p.harga as paket_harga,
               p.deskripsi as paket_deskripsi, COUNT(*) OVER() AS total_count
        FROM orders o
        LEFT JOIN products p ON o.paket_id = p.paket_id
        WHERE o.status=$1
        ORDER BY o.id ASC
        LIMIT $2 OFFSET $3
        """,
        status, page_size, offset,
    )
    if not used_async:
        rows = await run_db_admin(_get_admin_order_page_sync, status, page_size, offset)

    total = int(rows[0].get('total_count') or 0) if rows else 0
    # Callback pagination hanya menghasilkan halaman valid. Fallback ini menangani data yang berubah tepat saat klik Next.
    if not rows and page > 0:
        return await get_admin_order_page(status, page - 1, page_size)
    clean_rows = []
    for row in rows:
        row = dict(row)
        row.pop('total_count', None)
        clean_rows.append(row)
    _admin_order_page_cache[key] = (copy.deepcopy(clean_rows), now_mono + ADMIN_PANEL_CACHE_TTL_SECONDS, total)
    if len(_admin_order_page_cache) > 100:
        _admin_order_page_cache.clear()
        _admin_order_page_cache[key] = (copy.deepcopy(clean_rows), now_mono + ADMIN_PANEL_CACHE_TTL_SECONDS, total)
    return clean_rows, total

def _get_all_waiting_sync():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT o.*, p.nama as paket_nama, p.emoji as paket_emoji, p.harga as paket_harga
                FROM orders o
                LEFT JOIN products p ON o.paket_id = p.paket_id
                WHERE o.status='waiting'
                ORDER BY o.id ASC
            """)
            return [dict(r) for r in c.fetchall()]

async def get_all_waiting(force: bool = False):
    global _waiting_orders_cache, _waiting_orders_cache_lock
    now_mono = _time.monotonic()
    if not force and _waiting_orders_cache and now_mono < _waiting_orders_cache[1]:
        return copy.deepcopy(_waiting_orders_cache[0])
    if _waiting_orders_cache_lock is None:
        _waiting_orders_cache_lock = asyncio.Lock()
    async with _waiting_orders_cache_lock:
        now_mono = _time.monotonic()
        if not force and _waiting_orders_cache and now_mono < _waiting_orders_cache[1]:
            return copy.deepcopy(_waiting_orders_cache[0])
        data = []
        for _attempt in range(2):
            generation = _waiting_orders_cache_generation
            data, used_async = await _asyncpg_fetch("""
                SELECT o.*, p.nama as paket_nama, p.emoji as paket_emoji, p.harga as paket_harga
                FROM orders o
                LEFT JOIN products p ON o.paket_id = p.paket_id
                WHERE o.status='waiting'
                ORDER BY o.id ASC
            """)
            if not used_async:
                data = await run_db_admin(_get_all_waiting_sync)
            if generation == _waiting_orders_cache_generation:
                _waiting_orders_cache = (data, _time.monotonic() + WAITING_ORDERS_CACHE_TTL_SECONDS)
                return copy.deepcopy(data)
        # Trafik order sangat tinggi: kembalikan snapshot terbaru tanpa menyimpan cache stale.
        return copy.deepcopy(data)

@async_wrap
def get_buyer_history_with_products(user_id, limit=10):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT o.*, p.nama as paket_nama, p.emoji as paket_emoji, p.harga as paket_harga
                FROM orders o
                LEFT JOIN products p ON o.paket_id = p.paket_id
                WHERE o.user_id=%s
                ORDER BY o.id DESC
                LIMIT %s
            """, (user_id, limit))
            return [dict(r) for r in c.fetchall()]

@async_wrap_admin
def get_all_buyers():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT DISTINCT ON (o.user_id) o.user_id, o.user_name, o.id as max_id
                FROM orders o
                LEFT JOIN banned_users b ON o.user_id = b.user_id
                WHERE b.user_id IS NULL AND o.user_id > 0
                ORDER BY o.user_id, o.id DESC
            """)
            return [dict(r) for r in c.fetchall()]

@async_wrap_admin
def get_buyer_count():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT COUNT(DISTINCT o.user_id) AS total
                FROM orders o
                LEFT JOIN banned_users b ON o.user_id = b.user_id
                WHERE b.user_id IS NULL AND o.user_id IS NOT NULL
            """)
            row = c.fetchone()
            return int(row['total'] or 0) if row else 0

@async_wrap_admin
def search_buyers_sync(query: str) -> list:
    """Mencari pembeli menggunakan parameterized query aman."""
    with db_session_safe() as conn:
        with conn.cursor() as c:
            try:
                uid = int(query.strip())
                c.execute("""
                    SELECT DISTINCT o.user_id, o.user_name
                    FROM orders o
                    WHERE o.user_id = %s
                    LIMIT 10
                """, (uid,))
            except ValueError:
                c.execute("""
                    SELECT DISTINCT o.user_id, o.user_name
                    FROM orders o
                    WHERE LOWER(o.user_name) LIKE LOWER(%s)
                    ORDER BY o.user_name
                    LIMIT 10
                """, (f"%{query.strip()}%",))
            return [dict(r) for r in c.fetchall()]

def _get_managed_groups_sync() -> list:
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT value FROM settings WHERE key='managed_groups'")
            row = c.fetchone()
            if row and row['value']:
                return json.loads(row['value'])
            return []

def _set_managed_groups_sync(groups: list):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES ('managed_groups', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (json.dumps(groups),)
            )

async def get_managed_groups() -> list:
    return await run_db(_get_managed_groups_sync)

async def set_managed_groups(groups: list):
    return await run_db(_set_managed_groups_sync, groups)

@async_wrap_admin
def get_order_stats(today_start: datetime, month_start: datetime):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            yesterday_start = today_start - timedelta(days=1)
            if month_start.month == 1:
                last_month_start = month_start.replace(year=month_start.year - 1, month=12, day=1)
            else:
                last_month_start = month_start.replace(month=month_start.month - 1, day=1)
            last_month_end = month_start

            # Query 1: semua agregat per-periode dalam satu scan tabel
            c.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status='completed' AND created_at >= %(today)s)          AS today_completed,
                    COALESCE(SUM(harga_dibayar) FILTER (WHERE status='completed' AND created_at >= %(today)s), 0) AS today_revenue,
                    COUNT(*) FILTER (WHERE status='completed' AND created_at >= %(yest)s AND created_at < %(today)s) AS yesterday_completed,
                    COALESCE(SUM(harga_dibayar) FILTER (WHERE status='completed' AND created_at >= %(yest)s AND created_at < %(today)s), 0) AS yesterday_revenue,
                    COUNT(*) FILTER (WHERE status='completed' AND created_at >= %(month)s)          AS month_completed,
                    COALESCE(SUM(harga_dibayar) FILTER (WHERE status='completed' AND created_at >= %(month)s), 0) AS month_revenue,
                    COUNT(*) FILTER (WHERE status='completed' AND created_at >= %(lm_start)s AND created_at < %(lm_end)s) AS last_month_completed,
                    COALESCE(SUM(harga_dibayar) FILTER (WHERE status='completed' AND created_at >= %(lm_start)s AND created_at < %(lm_end)s), 0) AS last_month_revenue,
                    COUNT(*) FILTER (WHERE status='completed')                                      AS total_orders,
                    COUNT(*)                                                                        AS total_generated_raw,
                    COUNT(*) FILTER (WHERE status IN ('waiting','pending'))                         AS active_count,
                    COUNT(*) FILTER (WHERE status='cancelled')                                      AS cancelled_count,
                    COUNT(*) FILTER (WHERE status='expired')                                        AS expired_count,
                    COUNT(*) FILTER (WHERE status='rejected')                                       AS rejected_count,
                    COALESCE(SUM(harga_dibayar) FILTER (WHERE status='completed'), 0)              AS total_revenue
                FROM orders
            """, {
                'today':    today_start,
                'yest':     yesterday_start,
                'month':    month_start,
                'lm_start': last_month_start,
                'lm_end':   last_month_end,
            })
            r = c.fetchone()
            today_completed     = r['today_completed']
            today_revenue       = r['today_revenue']
            yesterday_completed = r['yesterday_completed']
            yesterday_revenue   = r['yesterday_revenue']
            month_completed     = r['month_completed']
            month_revenue       = r['month_revenue']
            last_month_completed = r['last_month_completed']
            last_month_revenue  = r['last_month_revenue']
            total_orders        = r['total_orders']
            total_generated     = r['total_generated_raw'] or 1
            active_count        = r['active_count']
            cancelled_count     = r['cancelled_count']
            expired_count       = r['expired_count']
            rejected_count      = r['rejected_count']
            total_revenue       = r['total_revenue']

            aov = round(total_revenue / total_orders) if total_orders > 0 else 0

            c.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM orders WHERE status='completed'")
            total_buyers = c.fetchone()['cnt']

            c.execute("""
                SELECT COUNT(*) as cnt FROM (
                    SELECT user_id FROM orders WHERE status='completed'
                    GROUP BY user_id HAVING COUNT(*) > 1
                ) t
            """)
            repeat_buyers = c.fetchone()['cnt']

            c.execute("""
                SELECT COUNT(DISTINCT o.user_id) as cnt FROM orders o
                WHERE o.status='completed' AND o.created_at >= %s AND o.user_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM orders old
                    WHERE old.status='completed' AND old.created_at < %s AND old.user_id=o.user_id
                )
            """, (month_start, month_start))
            new_buyers_month = c.fetchone()['cnt']

            c.execute("""
                SELECT
                    DATE(created_at AT TIME ZONE 'Asia/Jakarta') as hari,
                    COUNT(*) as cnt,
                    COALESCE(SUM(harga_dibayar), 0) as rev
                FROM orders
                WHERE status='completed' AND created_at >= %s
                GROUP BY DATE(created_at AT TIME ZONE 'Asia/Jakarta')
                ORDER BY hari ASC
            """, (today_start - timedelta(days=6),))
            trend_7d = [dict(row) for row in c.fetchall()]

            c.execute("""
                SELECT
                    EXTRACT(HOUR FROM (created_at AT TIME ZONE 'Asia/Jakarta'))::int as jam,
                    COUNT(*) as cnt
                FROM orders WHERE status='completed'
                GROUP BY jam ORDER BY cnt DESC LIMIT 1
            """)
            peak_row = c.fetchone()
            peak_hour = peak_row['jam'] if peak_row else None
            peak_count = peak_row['cnt'] if peak_row else 0

            c.execute("""
                SELECT COALESCE(SUM(harga_dibayar), 0) as rev
                FROM orders WHERE status='completed' AND created_at >= %s
            """, (today_start - timedelta(days=29),))
            rev_30d = c.fetchone()['rev']
            avg_daily_revenue = round(rev_30d / 30) if rev_30d else 0

            # Query: gabungkan statistik testimoni dalam satu scan
            c.execute("""
                SELECT
                    COALESCE(AVG(rating) FILTER (WHERE status='approved'), 0) AS avg_rating,
                    COUNT(*) FILTER (WHERE status='approved')                  AS total_testi,
                    COUNT(*) FILTER (WHERE status='pending')                   AS pending_testi
                FROM testimonials
            """)
            testi_row = c.fetchone()
            avg_rating    = round(float(testi_row['avg_rating']), 1)
            total_testi   = testi_row['total_testi']
            pending_testi = testi_row['pending_testi']

            c.execute("""
                SELECT o.paket_id, p.nama, p.emoji, COUNT(*) as cnt, COALESCE(SUM(o.harga_dibayar), 0) as total
                FROM orders o
                LEFT JOIN products p ON o.paket_id = p.paket_id
                WHERE o.status='completed'
                GROUP BY o.paket_id, p.nama, p.emoji
                ORDER BY cnt DESC
            """)
            products_breakdown = [dict(row) for row in c.fetchall()]

            best_product = None
            if products_breakdown:
                b = products_breakdown[0]
                emoji = b.get('emoji') or '📦'
                nama = b.get('nama') or b['paket_id']
                best_product = f"{emoji} {nama} ({b['cnt']}x)"

            # Statistik Mingguan (Senin s/d Hari ini)
            week_start = today_start - timedelta(days=today_start.weekday())
            last_week_start = week_start - timedelta(days=7)
            last_week_end = week_start

            c.execute("""
                SELECT COUNT(*) as cnt, COALESCE(SUM(harga_dibayar), 0) as rev
                FROM orders WHERE status='completed' AND created_at >= %s
            """, (week_start,))
            r = c.fetchone(); week_completed = r['cnt']; week_revenue = r['rev']

            c.execute("""
                SELECT COUNT(*) as cnt, COALESCE(SUM(harga_dibayar), 0) as rev
                FROM orders WHERE status='completed' AND created_at >= %s AND created_at < %s
            """, (last_week_start, last_week_end))
            r = c.fetchone(); last_week_completed = r['cnt']; last_week_revenue = r['rev']

            # Produk Terlaris Bulan Ini
            c.execute("""
                SELECT o.paket_id, p.nama, p.emoji, COUNT(*) as cnt, COALESCE(SUM(o.harga_dibayar), 0) as total
                FROM orders o
                LEFT JOIN products p ON o.paket_id = p.paket_id
                WHERE o.status='completed' AND o.created_at >= %s
                GROUP BY o.paket_id, p.nama, p.emoji
                ORDER BY cnt DESC
                LIMIT 5
            """, (month_start,))
            products_month = [dict(row) for row in c.fetchall()]

            # Top 3 Buyer Bulan Ini
            c.execute("""
                SELECT o.user_id, o.user_name, COUNT(*) as cnt, COALESCE(SUM(o.harga_dibayar), 0) as total
                FROM orders o
                WHERE o.status='completed' AND o.created_at >= %s
                GROUP BY o.user_id, o.user_name
                ORDER BY cnt DESC
                LIMIT 3
            """, (month_start,))
            top_buyers_month = [dict(row) for row in c.fetchall()]

            days_elapsed = max(1, (today_start - month_start).days + 1)

    return {
        'total_orders': total_orders,
        'today_completed': today_completed,
        'today_revenue': today_revenue,
        'yesterday_completed': yesterday_completed,
        'yesterday_revenue': yesterday_revenue,
        'week_completed': week_completed,
        'week_revenue': week_revenue,
        'last_week_completed': last_week_completed,
        'last_week_revenue': last_week_revenue,
        'month_completed': month_completed,
        'month_revenue': month_revenue,
        'last_month_completed': last_month_completed,
        'last_month_revenue': last_month_revenue,
        'total_generated': total_generated,
        'active_count': active_count,
        'cancelled_count': cancelled_count,
        'expired_count': expired_count,
        'rejected_count': rejected_count,
        'best_product': best_product,
        'total_revenue': total_revenue,
        'avg_daily_revenue': avg_daily_revenue,
        'aov': aov,
        'total_buyers': total_buyers,
        'repeat_buyers': repeat_buyers,
        'new_buyers_month': new_buyers_month,
        'trend_7d': trend_7d,
        'peak_hour': peak_hour,
        'peak_count': peak_count,
        'avg_rating': avg_rating,
        'total_testi': total_testi,
        'pending_testi': pending_testi,
        'products_breakdown': products_breakdown,
        'products_month': products_month,
        'top_buyers_month': top_buyers_month,
        'days_elapsed': days_elapsed,
    }

_stats_cache = None
_stats_cache_lock: asyncio.Lock = None

def _invalidate_stats_cache():
    global _stats_cache
    _stats_cache = None

def _invalidate_order_caches(user_id=None):
    _invalidate_start_snapshot_cache(user_id)
    _invalidate_active_order_cache(user_id)
    _invalidate_waiting_orders_cache()
    _invalidate_admin_order_page_cache()
    _invalidate_stats_cache()


async def get_order_stats_cached(today_start: datetime, month_start: datetime, force: bool = False):
    """Cache singkat agar permintaan statistik berulang tidak membebani DB."""
    global _stats_cache, _stats_cache_lock
    key = (today_start.isoformat(), month_start.isoformat())
    now_mono = _time.monotonic()
    if not force and _stats_cache:
        cache_key, expires_at, value = _stats_cache
        if cache_key == key and now_mono < expires_at:
            return copy.deepcopy(value)

    if _stats_cache_lock is None:
        _stats_cache_lock = asyncio.Lock()
    async with _stats_cache_lock:
        now_mono = _time.monotonic()
        if not force and _stats_cache:
            cache_key, expires_at, value = _stats_cache
            if cache_key == key and now_mono < expires_at:
                return copy.deepcopy(value)
        value = await get_order_stats(today_start, month_start)
        _stats_cache = (key, now_mono + STATS_CACHE_TTL_SECONDS, copy.deepcopy(value))
        return copy.deepcopy(value)


def _update_order_status_sync(order_id, status):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                "UPDATE orders SET status=%s WHERE order_id=%s AND status IN ('waiting','pending') RETURNING user_id",
                (status, order_id),
            )
            row = c.fetchone()
            return row.get('user_id') if row else None

async def update_order_status(order_id, status):
    if status not in {'cancelled', 'expired', 'rejected'}:
        raise ValueError("Transisi status harus memakai helper yang sesuai")
    user_id = await run_db(_update_order_status_sync, order_id, status)
    if user_id is not None:
        _invalidate_order_caches(user_id)
    return user_id

def _mark_order_completed_sync(order_id, expected_status="waiting"):
    """Atomic: completed hanya jika masih waiting; None berarti tidak ada row yang berubah."""
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                "UPDATE orders SET status='completed' WHERE order_id=%s AND status=%s RETURNING id, user_id",
                (order_id, expected_status)
            )
            row = c.fetchone()
            return dict(row) if row else None

async def mark_order_completed(order_id, *, expected_status="waiting") -> bool:
    if expected_status not in {'waiting', 'pending'}:
        raise ValueError("Status awal pembayaran tidak valid")
    updated = await run_db(_mark_order_completed_sync, order_id, expected_status)
    if not updated:
        return False
    _invalidate_order_caches(updated.get('user_id'))
    return True

def _get_order_by_id_sync(order_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE order_id=%s", (order_id,))
            row = c.fetchone()
            return dict(row) if row else None

async def get_order_by_id(order_id):
    row, used_async = await _asyncpg_fetchrow("SELECT * FROM orders WHERE order_id=$1", str(order_id))
    if used_async:
        return row
    return await run_db(_get_order_by_id_sync, order_id)

def _get_order_by_provider_transaction_id_sync(provider_transaction_id: str):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT * FROM orders WHERE provider_transaction_id=%s LIMIT 1",
                (provider_transaction_id,)
            )
            row = c.fetchone()
            return dict(row) if row else None

async def get_order_by_provider_transaction_id(provider_transaction_id: str):
    row, used_async = await _asyncpg_fetchrow(
        "SELECT * FROM orders WHERE provider_transaction_id=$1 LIMIT 1", str(provider_transaction_id)
    )
    if used_async:
        return row
    return await run_db(_get_order_by_provider_transaction_id_sync, provider_transaction_id)

@async_wrap
def update_order_provider_status(order_id: str, provider_status: str):
    if not provider_status:
        return
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                "UPDATE orders SET provider_status=%s WHERE order_id=%s AND provider_status IS DISTINCT FROM %s",
                (str(provider_status).lower(), order_id, str(provider_status).lower())
            )

@async_wrap
def get_completed_order_for_user(order_id: str, user_id: int):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT * FROM orders
                   WHERE order_id=%s AND user_id=%s AND status='completed'""",
                (order_id, user_id)
            )
            row = c.fetchone()
            return dict(row) if row else None

@async_wrap
def check_prerequisites_sync(user_id: int, requires_paket_ids_str: str) -> list:
    """Cek pemenuhan syarat pembelian paket."""
    if not requires_paket_ids_str:
        return []
    required_ids = [p.strip() for p in requires_paket_ids_str.split(",") if p.strip()]
    if not required_ids:
        return []
    delivery_clause = "AND delivery_status = 'sent'" if PREREQUISITE_REQUIRES_DELIVERY else ""
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                f"""SELECT DISTINCT paket_id FROM orders
                   WHERE user_id=%s AND status='completed'
                     {delivery_clause}
                     AND paket_id = ANY(%s)""",
                (user_id, required_ids)
            )
            done_ids = {row["paket_id"] for row in c.fetchall()}
            return [pid for pid in required_ids if pid not in done_ids]

@async_wrap
def set_admin_msg_id(order_id, msg_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE orders SET admin_msg_id=%s WHERE order_id=%s", (msg_id, order_id))

@async_wrap
def set_buyer_msg_id(order_id, msg_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE orders SET buyer_msg_id=%s WHERE order_id=%s", (msg_id, order_id))

@async_wrap
def set_sent_link(order_id, link):
    """Tandai link benar-benar sudah terkirim ke buyer."""
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE orders
                   SET sent_link=%s, delivery_status='sent', delivery_error=NULL
                   WHERE order_id=%s""",
                (link, order_id)
            )

@async_wrap
def set_delivery_status(order_id: str, status: str, error: str = None):
    allowed = {'not_sent', 'pending', 'sent', 'failed', 'held'}
    if status not in allowed:
        status = 'failed'
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE orders
                   SET delivery_status=%s, delivery_error=%s
                   WHERE order_id=%s""",
                (status, error, order_id)
            )

@async_wrap
def mark_delivery_held(order_id: str, reason: str = 'Syarat belum terpenuhi'):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE orders
                   SET delivery_status='held', delivery_error=%s
                   WHERE order_id=%s""",
                (reason, order_id)
            )

@async_wrap
def reset_delivery_claim(order_id: str, error: str = None):
    """Reset klaim pengiriman yang gagal agar bisa dicoba ulang."""
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE orders
                   SET sent_link=NULL, delivery_status='failed', delivery_error=%s
                   WHERE order_id=%s""",
                (error, order_id)
            )

@async_wrap
def get_completed_no_link_orders(user_id: int) -> list:
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT * FROM orders
                   WHERE user_id=%s AND status='completed'
                   AND (
                        delivery_status IS NULL
                        OR delivery_status IN ('not_sent', 'failed', 'held')
                        OR sent_link IS NULL
                        OR sent_link=''
                   )
                   ORDER BY id ASC""",
                (user_id,)
            )
            return [dict(r) for r in c.fetchall()]

@async_wrap
def atomic_claim_for_delivery(order_id: str) -> bool:
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""UPDATE orders
                SET sent_link='pending', delivery_status='pending', delivery_error=NULL
                WHERE order_id=%s AND status='completed'
                  AND COALESCE(delivery_status, 'not_sent') IN ('not_sent', 'failed', 'held')
                  AND COALESCE(sent_link, '')=''""", (order_id,))
            return c.rowcount > 0

def _save_order_sync(user_id, user_name, paket_id, order_id, harga_dibayar=0, order_changes=0,
                     payment_provider="autogopay", provider_transaction_id=None,
                     provider_order_id=None, provider_status=None,
                     payment_expires_at=None, checkout_url=None, qr_url=None):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO orders (
                       user_id, user_name, paket_id, order_id, status, waktu,
                       harga_dibayar, order_changes, payment_provider,
                       provider_transaction_id, provider_order_id, provider_status,
                       payment_expires_at, checkout_url, qr_url
                   ) VALUES (
                       %s, %s, %s, %s, 'waiting', %s,
                       %s, %s, %s,
                       %s, %s, %s,
                       %s, %s, %s
                   ) RETURNING *""",
                (
                    user_id, user_name, paket_id, order_id,
                    now_wib().strftime("%H:%M - %d/%m/%Y"),
                    harga_dibayar, order_changes, payment_provider,
                    provider_transaction_id, provider_order_id, provider_status,
                    payment_expires_at, checkout_url, qr_url,
                )
            )
            row = c.fetchone()
            return dict(row) if row else None

async def save_order(user_id, user_name, paket_id, order_id, harga_dibayar=0, order_changes=0,
                     payment_provider="autogopay", provider_transaction_id=None,
                     provider_order_id=None, provider_status=None,
                     payment_expires_at=None, checkout_url=None, qr_url=None):
    order = await run_db(
        _save_order_sync, user_id, user_name, paket_id, order_id, harga_dibayar, order_changes,
        payment_provider, provider_transaction_id, provider_order_id, provider_status,
        payment_expires_at, checkout_url, qr_url,
    )
    _invalidate_active_order_cache(user_id)
    _invalidate_waiting_orders_cache()
    _invalidate_admin_order_page_cache()
    _invalidate_stats_cache()
    _active_order_cache_set(user_id, order)
    return order

# =================== BAN DB FUNCTIONS ===================

_banned_cache: dict = {}
_BANNED_CACHE_TTL = 30
_banned_cache_locks = None

def _get_banned_lock(user_id: int) -> asyncio.Lock:
    global _banned_cache_locks
    if _banned_cache_locks is None:
        _banned_cache_locks = [asyncio.Lock() for _ in range(32)]
    return _banned_cache_locks[int(user_id) % len(_banned_cache_locks)]

def _is_banned_sync(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM banned_users WHERE user_id=%s", (user_id,))
            return c.fetchone() is not None

async def is_banned(user_id):
    uid = int(user_id)
    now_mono = _time.monotonic()
    cached = _banned_cache.get(uid)
    if cached and now_mono < cached[1]:
        return cached[0]
    if cached:
        _banned_cache.pop(uid, None)
    async with _get_banned_lock(uid):
        now_mono = _time.monotonic()
        cached = _banned_cache.get(uid)
        if cached and now_mono < cached[1]:
            return cached[0]
        row, used_async = await _asyncpg_fetchrow("SELECT 1 AS banned FROM banned_users WHERE user_id=$1", uid)
        result = bool(row) if used_async else await run_db(_is_banned_sync, uid)
        if len(_banned_cache) > 10000:
            for old_uid, entry in list(_banned_cache.items()):
                if now_mono >= entry[1]:
                    _banned_cache.pop(old_uid, None)
        _banned_cache[uid] = (result, now_mono + _BANNED_CACHE_TTL)
        return result

def _ban_user_sync(user_id, reason=""):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO banned_users (user_id, reason, banned_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET reason=EXCLUDED.reason, banned_at=EXCLUDED.banned_at""",
                (user_id, reason, now_wib().strftime("%H:%M - %d/%m/%Y"))
            )

async def ban_user(user_id, reason=""):
    uid = int(user_id)
    async with _get_banned_lock(uid):
        await run_db_admin(_ban_user_sync, uid, reason)
        _banned_cache[uid] = (True, _time.monotonic() + _BANNED_CACHE_TTL)
        _invalidate_start_snapshot_cache(uid)

def _unban_user_sync(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM banned_users WHERE user_id=%s", (user_id,))

async def unban_user(user_id):
    uid = int(user_id)
    async with _get_banned_lock(uid):
        await run_db_admin(_unban_user_sync, uid)
        _banned_cache[uid] = (False, _time.monotonic() + _BANNED_CACHE_TTL)
        _invalidate_start_snapshot_cache(uid)

@async_wrap_admin
def get_all_banned():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM banned_users ORDER BY banned_at DESC")
            return [dict(r) for r in c.fetchall()]

def _get_banned_page_sync(limit: int, offset: int):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT *, COUNT(*) OVER() AS total_count
                   FROM banned_users
                   ORDER BY banned_at DESC
                   LIMIT %s OFFSET %s""",
                (limit, offset),
            )
            return [dict(r) for r in c.fetchall()]

async def get_banned_page(page: int = 0, page_size: int = ADMIN_LIST_PAGE_SIZE):
    page = max(0, int(page))
    rows = await run_db_admin(_get_banned_page_sync, page_size, page * page_size)
    total = int(rows[0].get('total_count') or 0) if rows else 0
    clean = []
    for row in rows:
        row = dict(row)
        row.pop('total_count', None)
        clean.append(row)
    if not clean and page > 0:
        return await get_banned_page(page - 1, page_size)
    return clean, total

# =================== SETTINGS DB FUNCTIONS ===================

def _get_setting_sync(key):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = c.fetchone()
            return row['value'] if row else None

def _normalize_setting_value(key, value):
    if key == 'link_admin' and value in ('https://t.me/Kikukkvd', '@Kikukkvd'):
        return ADMIN_URL
    return value

def _setting_lock(key):
    """Single-flight per setting agar pembacaan key berbeda dapat paralel."""
    key = str(key)
    lock = _settings_cache_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _settings_cache_locks[key] = lock
    return lock

async def get_setting(key, default=None):
    key = str(key)
    hit, cached_val = _settings_cache_get(key)
    if hit:
        return cached_val if cached_val is not None else default
    async with _setting_lock(key):
        hit, cached_val = _settings_cache_get(key)
        if hit:
            return cached_val if cached_val is not None else default
        while True:
            version = _settings_cache_versions.get(key, 0)
            row, used_async = await _asyncpg_fetchrow("SELECT value FROM settings WHERE key=$1", str(key))
            if used_async:
                val = row.get('value') if row else None
            else:
                val = await run_db(_get_setting_sync, key)
            val = _normalize_setting_value(key, val)
            if version == _settings_cache_versions.get(key, 0):
                _settings_cache_set(key, val)
                return val if val is not None else default


def _get_settings_sync(keys):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute('SELECT key, value FROM settings WHERE key = ANY(%s)', (list(keys),))
            return [dict(row) for row in c.fetchall()]


async def get_settings(defaults):
    """Baca beberapa setting dalam satu query; cache dan lock sama dengan get_setting."""
    missing = [key for key in defaults if not _settings_cache_get(key)[0]]
    if missing:
        async with AsyncExitStack() as stack:
            for key in sorted(missing):
                await stack.enter_async_context(_setting_lock(key))
            missing = [key for key in missing if not _settings_cache_get(key)[0]]
            if missing:
                while True:
                    versions = {key: _settings_cache_versions.get(key, 0) for key in missing}
                    rows, used_async = await _asyncpg_fetch(
                        'SELECT key, value FROM settings WHERE key = ANY($1::text[])', missing)
                    if not used_async:
                        rows = await run_db(_get_settings_sync, missing)
                    if all(versions[key] == _settings_cache_versions.get(key, 0) for key in missing):
                        values = {row['key']: row['value'] for row in rows}
                        for key in missing:
                            _settings_cache_set(key, _normalize_setting_value(key, values.get(key)))
                        break
    result = {}
    for key, default in defaults.items():
        hit, value = _settings_cache_get(key)
        # Tanggal kedaluwarsa dapat lewat saat menunggu query lain.
        result[key] = value if hit and value is not None else (default if hit else await get_setting(key, default))
    return result

def _set_setting_sync(key, value):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            if value is None:
                c.execute("DELETE FROM settings WHERE key=%s", (key,))
            else:
                c.execute(
                    """INSERT INTO settings (key, value) VALUES (%s, %s)
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""",
                    (key, str(value))
                )

async def set_setting(key, value):
    key = str(key)
    async with _setting_lock(key):
        await run_db_admin(_set_setting_sync, key, value)
        _settings_cache_del(key)

# =================== COOLDOWN DB + IN-MEMORY CACHE ===================
COOLDOWN_MENIT = 5
COOLDOWN_MENIT_MAX = 60
_cooldown_memory_cache: dict = {}
# Cache negatif agar user tanpa cooldown tidak query PostgreSQL pada setiap /start.
_cooldown_empty_cache: dict = {}
# Cancel escalation disimpan 24 jam saja agar memory tidak tumbuh selamanya.
_cancel_count_cache: dict = {}
_CANCEL_COUNT_TTL_SECONDS = 24 * 60 * 60
_cooldown_cache_locks = None

def _get_cooldown_lock(user_id: int) -> asyncio.Lock:
    global _cooldown_cache_locks
    if _cooldown_cache_locks is None:
        _cooldown_cache_locks = [asyncio.Lock() for _ in range(32)]
    return _cooldown_cache_locks[int(user_id) % len(_cooldown_cache_locks)]

def _get_cancel_count(user_id: int) -> int:
    uid = int(user_id)
    entry = _cancel_count_cache.get(uid)
    if not entry:
        return 0
    count, expires_at = entry
    if _time.monotonic() >= expires_at:
        _cancel_count_cache.pop(uid, None)
        return 0
    return count

def _get_dynamic_cooldown_minutes(user_id: int) -> int:
    cancel_count = _get_cancel_count(user_id)
    if cancel_count >= 5:
        return COOLDOWN_MENIT_MAX
    if cancel_count >= 3:
        return 30
    if cancel_count >= 2:
        return 15
    return COOLDOWN_MENIT

def _increment_cancel_count(user_id: int):
    uid = int(user_id)
    count = _get_cancel_count(uid) + 1
    _cancel_count_cache[uid] = (count, _time.monotonic() + _CANCEL_COUNT_TTL_SECONDS)
    if len(_cancel_count_cache) > 10000:
        now_mono = _time.monotonic()
        for old_uid, entry in list(_cancel_count_cache.items()):
            if now_mono >= entry[1]:
                _cancel_count_cache.pop(old_uid, None)

def _get_cooldown_memory(user_id: int):
    uid = int(user_id)
    expires = _cooldown_memory_cache.get(uid)
    if expires is None:
        return None
    if now_utc() >= expires:
        _cooldown_memory_cache.pop(uid, None)
        return None
    return expires

def _set_cooldown_memory(user_id: int, expires_at: datetime):
    uid = int(user_id)
    _cooldown_memory_cache[uid] = expires_at
    if len(_cooldown_memory_cache) > 10000:
        now = now_utc()
        for old_uid, expires in list(_cooldown_memory_cache.items()):
            if now >= expires:
                _cooldown_memory_cache.pop(old_uid, None)

def _clear_cooldown_memory(user_id: int):
    _cooldown_memory_cache.pop(int(user_id), None)

def _set_cooldown_sync(user_id, expires_at):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO cooldowns (user_id, expires_at) VALUES (%s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET expires_at=EXCLUDED.expires_at""",
                (user_id, expires_at)
            )

async def set_cooldown_db(user_id):
    uid = int(user_id)
    async with _get_cooldown_lock(uid):
        _cooldown_empty_cache.pop(uid, None)
        cooldown_minutes = _get_dynamic_cooldown_minutes(uid)
        expires_at = now_utc() + timedelta(minutes=cooldown_minutes)
        await run_db(_set_cooldown_sync, uid, expires_at)
        _set_cooldown_memory(uid, expires_at)
        _increment_cancel_count(uid)
        _invalidate_start_snapshot_cache(uid)

def _get_cooldown_sync(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT expires_at FROM cooldowns WHERE user_id=%s", (user_id,))
            row = c.fetchone()
            return row['expires_at'] if row else None

async def get_cooldown_sisa_db(user_id):
    mem_expires = _get_cooldown_memory(user_id)
    if mem_expires is not None:
        sisa = (mem_expires - now_utc()).total_seconds()
        return math.ceil(sisa / 60) if sisa > 0 else 0

    uid = int(user_id)
    now_mono = _time.monotonic()
    empty_until = _cooldown_empty_cache.get(uid)
    if empty_until and now_mono < empty_until:
        return 0
    if empty_until:
        _cooldown_empty_cache.pop(uid, None)

    async with _get_cooldown_lock(uid):
        mem_expires = _get_cooldown_memory(uid)
        if mem_expires is not None:
            sisa = (mem_expires - now_utc()).total_seconds()
            return math.ceil(sisa / 60) if sisa > 0 else 0
        empty_until = _cooldown_empty_cache.get(uid)
        now_mono = _time.monotonic()
        if empty_until and now_mono < empty_until:
            return 0
        row, used_async = await _asyncpg_fetchrow("SELECT expires_at FROM cooldowns WHERE user_id=$1", uid)
        until = row.get('expires_at') if (used_async and row) else None
        if not used_async:
            until = await run_db(_get_cooldown_sync, uid)
        if until is None:
            _cooldown_empty_cache[uid] = _time.monotonic() + COOLDOWN_EMPTY_TTL_SECONDS
            return 0
        _cooldown_empty_cache.pop(uid, None)
        try:
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            until_utc = until.astimezone(timezone.utc)
            sisa = (until_utc - now_utc()).total_seconds()
            if sisa > 0:
                _set_cooldown_memory(uid, until_utc)
            return math.ceil(sisa / 60) if sisa > 0 else 0
        except Exception as exc:
            logger.error(f"[COOLDOWN] Gagal menghitung cooldown: {exc}")
            return 0

def _clear_cooldown_sync(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM cooldowns WHERE user_id=%s", (user_id,))

async def clear_cooldown_db(user_id):
    uid = int(user_id)
    async with _get_cooldown_lock(uid):
        await run_db(_clear_cooldown_sync, uid)
        _clear_cooldown_memory(uid)
        _cooldown_empty_cache[uid] = _time.monotonic() + COOLDOWN_EMPTY_TTL_SECONDS
        _invalidate_start_snapshot_cache(uid)

@async_wrap_admin
def cleanup_expired_cooldowns():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM cooldowns WHERE expires_at < NOW()")

# =================== ADMIN DB FUNCTIONS ===================

_admin_ids_cache = None
_admin_ids_cache_lock = None
_admin_ids_cache_generation = 0

def _invalidate_admin_ids_cache():
    global _admin_ids_cache, _admin_ids_cache_generation
    _admin_ids_cache_generation += 1
    _admin_ids_cache = None

def _load_admin_ids_sync():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT user_id FROM admins")
            return {int(r['user_id']) for r in c.fetchall()}

async def _get_admin_ids_cached():
    global _admin_ids_cache, _admin_ids_cache_lock
    now_mono = _time.monotonic()
    if _admin_ids_cache and now_mono < _admin_ids_cache[1]:
        return _admin_ids_cache[0]
    if _admin_ids_cache_lock is None:
        _admin_ids_cache_lock = asyncio.Lock()
    async with _admin_ids_cache_lock:
        now_mono = _time.monotonic()
        if _admin_ids_cache and now_mono < _admin_ids_cache[1]:
            return _admin_ids_cache[0]
        while True:
            generation = _admin_ids_cache_generation
            rows, used_async = await _asyncpg_fetch("SELECT user_id FROM admins")
            if used_async:
                ids = {int(r['user_id']) for r in rows}
            else:
                ids = await run_db_admin(_load_admin_ids_sync)
            if generation == _admin_ids_cache_generation:
                _admin_ids_cache = (frozenset(ids), _time.monotonic() + ADMIN_CACHE_TTL_SECONDS)
                return _admin_ids_cache[0]

@async_wrap_admin
def get_all_admins():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM admins ORDER BY added_at ASC")
            return [dict(r) for r in c.fetchall()]

def _add_admin_sync(user_id, nama, added_by):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO admins (user_id, nama, added_by, added_at)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET nama=EXCLUDED.nama""",
                (user_id, str(nama), added_by, now_wib().strftime("%H:%M - %d/%m/%Y"))
            )

async def add_admin(user_id, nama, added_by):
    await run_db_admin(_add_admin_sync, user_id, nama, added_by)
    _invalidate_admin_ids_cache()

def _remove_admin_sync(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM admins WHERE user_id=%s", (user_id,))

async def remove_admin(user_id):
    await run_db_admin(_remove_admin_sync, user_id)
    _invalidate_admin_ids_cache()

async def is_admin_in_db(user_id):
    return int(user_id) in await _get_admin_ids_cached()

# =================== ADMIN CHECK WITH SHARED TTL CACHE ===================

async def is_admin(user_id: int, context=None) -> bool:
    if int(user_id) == ADMIN_ID:
        return True
    return int(user_id) in await _get_admin_ids_cached()

def invalidate_admin_cache(context, user_id: int = None):
    if user_id is not None:
        context.bot_data.get('_admin_cache', {}).pop(user_id, None)
    else:
        context.bot_data['_admin_cache'] = {}

def is_super_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def admin_callback(handler):
    @functools.wraps(handler)
    async def guarded(update, context, *args, **kwargs):
        user = getattr(update, 'effective_user', None)
        if user is None or not await is_admin(user.id, context):
            query = getattr(update, 'callback_query', None)
            if query is not None:
                try:
                    await query.answer("⛔ Akses ditolak.", show_alert=True)
                except telegram.error.TelegramError:
                    pass
            return
        return await handler(update, context, *args, **kwargs)
    return guarded


# =================== HELPERS ===================

def format_harga(harga):
    return f"Rp {int(harga):,}".replace(",", ".")

def hitung_durasi(waktu_str):
    try:
        parts = re.split(r'\s*[-\-]\s*', waktu_str, maxsplit=1)
        if len(parts) != 2:
            return waktu_str
        time_part = parts[0].strip()
        date_part = parts[1].strip()
        order_time = datetime.strptime(f"{time_part} {date_part}", "%H:%M %d/%m/%Y")
        order_time = order_time.replace(tzinfo=WIB)
        delta = now_wib() - order_time
        total_minutes = int(delta.total_seconds() / 60)
        if total_minutes < 60:
            return f"{total_minutes} menit lalu"
        hours = total_minutes // 60
        minutes = total_minutes % 60
        return f"{hours} jam {minutes} menit lalu"
    except Exception as e:
        logger.debug(f"Gagal menghitung durasi: {e}")
        return waktu_str

def _build_link_section(group_link: str, fallback_link: str) -> str:
    if group_link:
        return (
            f"🔗 <b>Link Bergabung (Khusus Kamu)</b>\n"
            f"{group_link}\n\n"
            f"📋 <b>Cara gabung:</b>\n"
            f"1. Klik link di atas\n"
            f"2. Pencet <b>\"Minta Bergabung\"</b>\n"
            f"3. Bot langsung <b>approve otomatis</b> ✅\n\n"
            f"⚠️ <i>Jangan dishare ke orang lain!</i>"
        )
    return (
        f"🔗 <b>Link Produk</b>\n"
        f"{fallback_link}\n\n"
        f"💾 <i>Simpan link ini. Produk dapat diakses kapan saja.</i>"
    )

# =================== GENERATE GROUP LINK ===================

async def generate_group_link(bot, paket, order_id):
    group_id = paket.get('group_chat_id')
    if not group_id:
        return None
    try:
        chat_id = int(group_id) if str(group_id).lstrip('-').isdigit() else group_id
        nama_link = f"Order-{order_id}"[:32]
        link = await bot.create_chat_invite_link(
            chat_id=chat_id,
            name=nama_link,
            creates_join_request=True,
        )
        return link.invite_link
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[LINK] Gagal bikin link grup {group_id}: {e}")
        return None

async def get_product_link(bot, paket, order_id):
    group_link = await generate_group_link(bot, paket, order_id)
    return group_link or (paket.get("link") or DEFAULT_LINK)

# =================== MAINTENANCE ===================

async def is_maintenance() -> bool:
    return await get_setting('maintenance') == '1'


async def _reject_new_invoice_during_maintenance(query, context, user_id: int) -> bool:
    """Blokir invoice baru saat maintenance tanpa mengganggu invoice yang sudah ada."""
    if not await is_maintenance():
        return False

    notice = (
        "⚠️ BOT SEDANG MAINTENANCE\n\n"
        "Pembelian sementara ditutup.\n"
        "Silakan coba lagi nanti."
    )
    try:
        if query is not None:
            # Alert callback tidak membuat pesan baru sehingga chat tetap bersih.
            await query.answer(notice, show_alert=True)
        else:
            await context.bot.send_message(chat_id=user_id, text=notice)
    except Exception:
        if query is not None:
            try:
                if getattr(query.message, 'photo', None):
                    await query.message.edit_caption(notice)
                else:
                    await query.edit_message_text(notice)
            except Exception:
                await _callback_feedback(query, context, notice)
    return True

# =================== NOTIFIKASI ORDER ===================

def _compact_waktu(value=None) -> str:
    """Format waktu compact: HH:MM · DD/MM/YYYY."""
    if not value:
        return now_wib().strftime('%H:%M · %d/%m/%Y')
    text = str(value).strip()
    # format lama: "HH:MM - DD/MM/YYYY"
    text = re.sub(r'\s*-\s*', ' · ', text, count=1)
    text = text.replace(',', ' ·')
    return text

def _default_order_note(judul: str) -> str:
    title = (judul or '').upper()
    if 'DIBATALKAN BUYER' in title:
        return 'ℹ️ Dibatalkan oleh buyer'
    if 'DIBATALKAN ADMIN' in title:
        return 'ℹ️ Dibatalkan oleh admin'
    if 'DITOLAK' in title or 'REJECT' in title:
        return 'ℹ️ Order ditolak admin'
    if 'EXPIRED' in title:
        return 'ℹ️ Sesi pembayaran expired'
    if 'BARU' in title:
        return '⏳ Menunggu pembayaran'
    if 'BERHASIL' in title or 'COMPLETED' in title or 'KONFIRM' in title:
        return '✅ Order completed'
    if 'AUTO' in title or 'LINK' in title:
        return '🔗 Link produk terkirim'
    return ''

def _format_order_notif(judul: str, user_name: str, user_id: int,
                         paket: dict, order_id: str,
                         amount: int = None, extra: str = None) -> str:
    """Format notif admin compact, tetap rapi dan buyer clickable."""
    lines = [
        judul,
        "━━━━━━━━━━━━━━━━━━━━",
        f"👤 Buyer   : {tg_user_link(user_id, user_name)} · <code>{esc(user_id)}</code>",
        f"📦 Paket   : {esc(paket.get('emoji','📦'))} {esc(paket.get('nama','?'))}",
    ]
    if amount is not None:
        lines.append(f"💰 Total   : {format_harga(amount)}")
    lines.extend([
        f"🧾 Order   : <code>{esc(order_id)}</code>",
        f"🕒 Waktu   : {_compact_waktu()}",
    ])

    note = str(extra).strip() if extra else _default_order_note(judul)
    if note:
        lines.extend(["", note])
    return "\n".join(lines)

async def build_order_detail_text(order: dict, paket: dict) -> str:
    STATUS_TITLE = {
        'completed': '✅ ORDER COMPLETED',
        'waiting': '⏳ ORDER WAITING',
        'pending': '🔄 ORDER PENDING',
        'cancelled': '❌ ORDER CANCELLED',
        'expired': '⏰ ORDER EXPIRED',
        'rejected': '🚫 ORDER REJECTED',
    }
    DELIVERY_LABEL = {
        'sent': '✅ Sudah dikirim',
        'held': '⏸️ Link ditahan',
        'failed': '⚠️ Gagal dikirim',
        'pending': '⏳ Sedang diproses',
        'not_sent': '—',
        None: '—',
    }
    amount = order.get('harga_dibayar') or paket.get('harga', 0)
    delivery_label = DELIVERY_LABEL.get(order.get('delivery_status'), order.get('delivery_status') or '—')
    title = STATUS_TITLE.get(order.get('status'), '🧾 ORDER STATUS')
    text = (
        f"<b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Buyer   : {tg_user_link(order.get('user_id'), order.get('user_name', '-'))} · <code>{esc(order.get('user_id', '-'))}</code>\n"
        f"📦 Paket   : {esc(paket.get('emoji','📦'))} {esc(paket.get('nama','Produk'))}\n"
        f"💰 Total   : {format_harga(amount)}\n"
        f"🔗 Link    : {delivery_label}\n"
        f"🧾 Order   : <code>{esc(order.get('order_id', '-'))}</code>\n"
        f"🕒 Waktu   : {_compact_waktu(order.get('waktu', '-'))}"
    )

    requires_str = paket.get('requires_paket_ids') or ''
    if requires_str:
        progress = await _build_prereq_progress(order.get('user_id'), requires_str, parent_order_id=order.get('order_id'))
        if progress['missing']:
            text += (
                f"\n\n📋 Syarat  : {progress['fulfilled_count']}/{progress['total_count']} terpenuhi\n\n"
                f"{progress['text']}"
            )
        else:
            text += (
                f"\n\n📋 Syarat  : ✅ Lengkap · "
                f"{progress['fulfilled_count']}/{progress['total_count']} terpenuhi"
            )

    sent_link = order.get('sent_link')
    if sent_link and sent_link != 'pending':
        text += f"\n\n🔗 <b>Link Terkirim</b>\n{esc(sent_link)}"
    return text

async def kirim_notif(bot, text: str, reply_markup=None):
    channel_id = await get_setting('notif_channel_id')
    target = int(channel_id) if channel_id else ADMIN_ID
    try:
        msg = await telegram_send_rate_retry(
            lambda: bot.send_message(chat_id=target, text=text, parse_mode="HTML", reply_markup=reply_markup,
                                     **_telegram_background_options(bot)),
            label=f"notif admin {target}",
        )
        return msg.message_id
    except Exception as e:
        logger.error(f"[NOTIF] Gagal kirim ke {target}: {e}")
        if target != ADMIN_ID:
            try:
                msg = await telegram_send_rate_retry(
                    lambda: bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", reply_markup=reply_markup,
                                             **_telegram_background_options(bot)),
                    label="notif admin fallback",
                )
                return msg.message_id
            except Exception as ex:
                logger.error(f"[NOTIF] Gagal kirim ke admin utama: {ex}")
    return None

async def hapus_notif_lama(bot, order_id):
    order = await get_order_by_id(order_id)
    if not order:
        return
    msg_id = order.get('admin_msg_id')
    if not msg_id:
        return
    channel_id = await get_setting('notif_channel_id')
    target = int(channel_id) if channel_id else ADMIN_ID
    try:
        await bot.delete_message(chat_id=target, message_id=int(msg_id))
    except Exception as e:
        logger.debug(f"[NOTIF] Gagal hapus notif lama di {target}: {e}")
        if target != ADMIN_ID:
            try:
                await bot.delete_message(chat_id=ADMIN_ID, message_id=int(msg_id))
            except Exception as ex:
                logger.debug(f"[NOTIF] Gagal hapus notif di admin utama: {ex}")

async def hapus_qris_buyer_lama(bot, order_id, user_id):
    order = await get_order_by_id(order_id)
    if order and order.get('buyer_msg_id'):
        try:
            await bot.delete_message(chat_id=int(user_id), message_id=int(order['buyer_msg_id']))
        except Exception as e:
            logger.debug(f"Gagal hapus pesan QRIS buyer lama: {e}")

# =================== MAIN MENU ===================

# Foto disertakan di file agar deployment cukup mengunggah satu file Python.
_MENU_PHOTO_BASE64 = "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"
_MENU_PHOTO_FILE_ID = None
_MENU_PHOTO_LOCK = None
_catalog_photo_messages = OrderedDict()
_MENU_PHOTO_SETTING = "main_menu_photo_" + hashlib.sha256(_MENU_PHOTO_BASE64.encode("ascii")).hexdigest()[:16]


def _catalog_update_boundary(update):
    """Only retain a reusable target while the user is in that catalog message."""
    chat = getattr(update,'effective_chat',None)
    uid = getattr(chat,'id',None)
    record = _catalog_photo_messages.get(uid)
    if record is None:
        return
    message = getattr(update,'message',None)
    if message is not None and re.fullmatch(r'/start(?:@\w+)?(?:\s.*)?',getattr(message,'text','') or ''):
        return
    query = getattr(update,'callback_query',None)
    data = str(getattr(query,'data','') or '')
    if (getattr(getattr(query,'message',None),'message_id',None) == record['message_id']
            and (data in {'buy','back_to_menu','buyer_how','buyer_history'} or data.startswith(('buy|','pilih_','buyer_desc_')))):
        return
    _catalog_photo_messages.pop(uid,None)


def _menu_photo_identity(message):
    photos = getattr(message,'photo',None)
    unique = getattr(photos[-1],'file_unique_id',None) if photos else None
    return hashlib.sha256(unique.encode()).hexdigest()[:16] if isinstance(unique,str) and unique else 'unknown'


def _invalid_menu_photo_id(error):
    text = str(error).lower()
    return any(part in text for part in ('wrong file identifier','wrong remote file identifier',
               'invalid file id','file_id not found','file reference expired','file_reference_expired'))

def _menu_setting_defaults():
    return {
        'link_testimoni': 'https://t.me/+7zsdSrwYIG8wOTg1',
        'link_admin': ADMIN_URL,
        'main_group_button_name': '👥 Grup Hyper Family',
        'main_group_button_link': '',
        'maintenance': 'off',
        _MENU_PHOTO_SETTING: '',
    }

_menu_render_cache = OrderedDict()

def _menu_render_key(query):
    message = getattr(query, 'message', None)
    mid = getattr(message, 'message_id', None)
    chat_id = getattr(message, 'chat_id', None) or getattr(getattr(query, 'from_user', None), 'id', None)
    return (chat_id, mid) if chat_id is not None and mid is not None else None

def _forget_menu_render(query):
    _menu_render_cache.pop(_menu_render_key(query), None)

async def _render_buyer_menu(query, text, reply_markup):
    """Edit caption untuk menu bergambar; tetap dukung pesan teks lama."""
    started = _time.monotonic()
    trace = _callback_trace_state.get()
    if trace is not None and not trace['closed']:
        trace.setdefault('render_ns', _time.perf_counter_ns())
    navigation = _navigation_timing.get()
    if navigation is not None and not navigation['rendered']:
        navigation['rendered'] = True
        _record_latency('navigation-build', started - navigation['started'])
    key = _menu_render_key(query)
    cached = _menu_render_cache.get(key)
    # Keep only a digest, not the full keyboard graph, in the bounded render cache.
    # Include API extension fields (style/custom emoji), which PTB equality ignores.
    markup_data = reply_markup.to_dict() if reply_markup is not None else None
    signature = hashlib.sha256(json.dumps(
        (text, markup_data), sort_keys=True, ensure_ascii=False, separators=(',', ':')
    ).encode('utf-8')).digest()
    cache_fresh = cached is not None and started < cached[1]
    if cache_fresh and cached[0] == signature:
        _record_latency('buyer-menu-deduplicated', 0)
        return query.message
    try:
        message = query.message
        current_html = getattr(message, 'caption_html', None) if getattr(message, 'photo', None) else getattr(message, 'text_html', None)
        if not cache_fresh and current_html == text and getattr(message, 'reply_markup', None) == reply_markup:
            return message
        if query.message and query.message.photo:
            result = await query.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            result = await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=reply_markup)
        if key is not None:
            _menu_render_cache[key] = (signature, _time.monotonic() + 2.5)
            _menu_render_cache.move_to_end(key)
            while len(_menu_render_cache) > 4096:
                _menu_render_cache.popitem(last=False)
        return result
    except telegram.error.BadRequest as exc:
        if 'message is not modified' not in str(exc).lower():
            raise
    finally:
        _record_latency('buyer-menu-render', _time.monotonic() - started)

async def _send_main_menu_photo(context, user_id, text, markup, *, reuse_recent=False, photo_id=None):
    global _MENU_PHOTO_FILE_ID, _MENU_PHOTO_LOCK
    async with _get_user_notice_lock(user_id):
        record = _catalog_photo_messages.get(user_id)
        previous_home_id = context.user_data.get('_home_photo_id') or (record or {}).get('message_id')
        if reuse_recent and record and record['media_key'] == _MENU_PHOTO_SETTING:
            previous_id = record['message_id']
            try:
                msg = await context.bot.edit_message_caption(chat_id=user_id, message_id=previous_id,
                                                      caption=text, parse_mode='HTML', reply_markup=markup)
                _menu_render_cache.pop((user_id,previous_id),None)
                _catalog_photo_messages.move_to_end(user_id)
                logger.info('[CATALOG MEDIA] action=reuse source=existing_message identity=%s',record['identity'])
                return msg
            except telegram.error.BadRequest as exc:
                if 'message is not modified' in str(exc).lower():
                    _menu_render_cache.pop((user_id,previous_id),None)
                    logger.info('[CATALOG MEDIA] action=reuse_unchanged source=existing_message identity=%s',record['identity'])
                    return
                if not any(part in str(exc).lower() for part in ('message to edit not found',"message can't be edited",'message can not be edited')):
                    raise
                _catalog_photo_messages.pop(user_id,None)
        if _MENU_PHOTO_LOCK is None:
            _MENU_PHOTO_LOCK = asyncio.Lock()
        persist_id = None

        async def upload():
            global _MENU_PHOTO_FILE_ID
            nonlocal persist_id
            message = await context.bot.send_photo(chat_id=user_id,photo=BytesIO(base64.b64decode(_MENU_PHOTO_BASE64)),
                                                   caption=text,parse_mode='HTML',reply_markup=markup)
            if message.photo:
                _MENU_PHOTO_FILE_ID = message.photo[-1].file_id
                persist_id = _MENU_PHOTO_FILE_ID
            return message

        msg = None
        source = 'file_id'
        if not _MENU_PHOTO_FILE_ID:
            async with _MENU_PHOTO_LOCK:
                if not _MENU_PHOTO_FILE_ID:
                    _MENU_PHOTO_FILE_ID = (photo_id if photo_id is not None else await get_setting(_MENU_PHOTO_SETTING,'')) or None
                if not _MENU_PHOTO_FILE_ID:
                    msg = await upload()
                    source = 'upload_missing_id'
        if msg is None:
            used_id = _MENU_PHOTO_FILE_ID
            try:
                msg = await context.bot.send_photo(chat_id=user_id,photo=used_id,caption=text,
                                                   parse_mode='HTML',reply_markup=markup)
            except telegram.error.BadRequest as exc:
                if not _invalid_menu_photo_id(exc):
                    raise
                async with _MENU_PHOTO_LOCK:
                    if not _MENU_PHOTO_FILE_ID or _MENU_PHOTO_FILE_ID == used_id:
                        _MENU_PHOTO_FILE_ID = None
                        msg = await upload()
                        source = 'upload_invalid_id'
                    else:
                        used_id = _MENU_PHOTO_FILE_ID
                if msg is None:
                    msg = await context.bot.send_photo(chat_id=user_id,photo=used_id,caption=text,
                                                       parse_mode='HTML',reply_markup=markup)
        # Keep the existing valid file_id; a different returned alias is not a new asset.
        _catalog_photo_messages[user_id] = {'message_id':msg.message_id,'media_key':_MENU_PHOTO_SETTING,
                                            'identity':_menu_photo_identity(msg)}
        _catalog_photo_messages.move_to_end(user_id)
        while len(_catalog_photo_messages)>4096:
            _catalog_photo_messages.popitem(last=False)
        logger.info('[CATALOG MEDIA] action=send source=%s identity=%s',source,_menu_photo_identity(msg))
        # Replace only our last known catalog after the new photo succeeds.
        # Never delete invoices, access links, or the last catalog on a failed send.
        context.user_data['_home_photo_id'] = msg.message_id
        context.user_data['_home_photo_at'] = _time.monotonic()
        if previous_home_id and previous_home_id != msg.message_id:
            spawn_background(
                _safe_delete_chat_message(context.bot, user_id, previous_home_id, 'katalog lama'),
                name=f'cleanup-catalog-{user_id}-{previous_home_id}',
            )
        await hapus_msg_user_lama(context, user_id, keep_last=0)
        if persist_id:
            try:
                await set_setting(_MENU_PHOTO_SETTING,persist_id)
            except Exception as exc:
                logger.warning('[CATALOG MEDIA] file_id persistence failed: %s',type(exc).__name__)
        return msg

async def build_main_menu_parts(name=None, settings=None):
    if settings is None:
        settings = await get_settings(_menu_setting_defaults())
    link_testi = settings['link_testimoni']
    link_cs = settings['link_admin']
    group_button_name = settings['main_group_button_name']
    group_button_link = settings['main_group_button_link']
    greeting = f"<b>👋 Halo, {esc(name)}!</b>\n" if name else "<b>👋 Halo!</b>\n"
    text = (greeting + "━━━━━━━━━━━━━━━━━━\n"
        "<b>🛍️ Selamat datang di Hyper Store!</b>\n\n"
        "✨ <b>Pilihan tepat untukmu</b>\n"
        "📦 Berbagai pilihan paket tersedia\n"
        "💎 Akses sesuai paket yang dipilih\n"
        "💸 Harga terjangkau dan order praktis\n"
        "🔄 Update konten sesuai paket\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📞 <b>Butuh bantuan?</b>\nTekan <b>Hubungi Admin</b> di bawah.")
    keyboard = [
        [styled_button("🛒 Daftar Paket", callback_data="buy", style="success", icon_custom_emoji_id=os.environ.get("BTN_EMOJI_BUY_ID"))],
        [styled_button("📖 Cara Order", callback_data="buyer_how"),
         styled_button("⭐ Testimoni", url=link_testi, icon_custom_emoji_id=os.environ.get("BTN_EMOJI_TESTIMONI_ID"))],
        [styled_button("📜 Riwayat", callback_data="buyer_history"),
         styled_button("💬 Hubungi Admin", url=link_cs, icon_custom_emoji_id=os.environ.get("BTN_EMOJI_ADMIN_ID"))],
    ]
    group_button_link = normalize_button_url(group_button_link)
    if group_button_link:
        keyboard.append([styled_button(str(group_button_name or '👥 Grup Hyper Family').strip()[:64],
            url=group_button_link, style="success", icon_custom_emoji_id=os.environ.get("BTN_EMOJI_GROUP_ID"))])
    return text, keyboard

async def build_main_menu_text():
    text, _ = await build_main_menu_parts()
    return text

async def build_main_menu_keyboard():
    _, keyboard = await build_main_menu_parts()
    return keyboard

def simpan_admin_msg(context, user_id, message_id):
    context.bot_data.setdefault('admin_messages', {})
    context.bot_data['admin_messages'].setdefault(user_id, [])
    bucket = context.bot_data['admin_messages'][user_id]
    bucket.append(message_id)
    if len(bucket) > 50:
        del bucket[:-50]

async def hapus_admin_msg(context, user_id):
    msg_ids = context.bot_data.get('admin_messages', {}).pop(user_id, [])
    if not msg_ids:
        return

    async def _cleanup():
        await asyncio.gather(*[
            _safe_delete_chat_message(context.bot, user_id, msg_id, "pesan admin lama")
            for msg_id in msg_ids
        ], return_exceptions=True)

    spawn_background(_cleanup(), name=f"cleanup-admin-{user_id}")

def simpan_msg_user(context, user_id, message_id):
    context.bot_data.setdefault('user_messages', {})
    store = context.bot_data['user_messages']
    if user_id not in store and len(store) >= 20000:
        store.pop(next(iter(store)), None)
    store.setdefault(user_id, [])
    bucket = store[user_id]
    bucket.append(message_id)
    if len(bucket) > 50:
        del bucket[:-50]

async def hapus_msg_user_lama(context, user_id, keep_last=1):
    msgs = context.bot_data.get('user_messages', {}).get(user_id, [])
    if len(msgs) <= keep_last:
        return
    to_delete = msgs[:-keep_last] if keep_last else list(msgs)
    if keep_last:
        context.bot_data['user_messages'][user_id] = msgs[-keep_last:]
    else:
        context.bot_data.get('user_messages', {}).pop(user_id, None)
    record = _catalog_photo_messages.get(user_id)
    if record is not None and record['message_id'] in to_delete:
        _catalog_photo_messages.pop(user_id,None)

    async def _cleanup():
        await asyncio.gather(*[
            _safe_delete_chat_message(context.bot, user_id, msg_id, "pesan user lama")
            for msg_id in to_delete
        ], return_exceptions=True)

    spawn_background(_cleanup(), name=f"cleanup-user-{user_id}")


async def _send_user_message_replace(context: ContextTypes.DEFAULT_TYPE, user_id: int,
                                     text: str, parse_mode: str = None,
                                     reply_markup=None, keep_last: int = 1):
    """Kirim pesan status secara serial lalu bersihkan status lama milik buyer."""
    async with _get_user_notice_lock(user_id):
        msg = await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=keep_last)
        return msg

# =================== COOLDOWN MESSAGE ANTI-SPAM ===================
COOLDOWN_NOTICE_DEBOUNCE_SECONDS = 5

async def _safe_delete_chat_message(bot, chat_id, message_id, label="pesan"):
    if not chat_id or not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=int(message_id), **_telegram_background_options(bot))
    except Exception as e:
        logger.debug(f"Gagal hapus {label}: {e}")


async def _clear_user_notice(context, user_id: int, key: str, label: str):
    """Hapus pesan status lama yang disimpan per user, jika masih ada."""
    message_id = context.user_data.pop(key, None)
    if message_id:
        await _safe_delete_chat_message(context.bot, user_id, message_id, label)


async def _send_single_maintenance_notice(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Tampilkan satu notice maintenance; /start berulang hanya mengganti notice lama."""
    chat_id = update.effective_chat.id
    async with _get_user_notice_lock(user_id):
        if update.message:
            await _safe_delete_chat_message(
                context.bot, chat_id, update.message.message_id, "pesan /start user"
            )

        last_msg_id = context.user_data.get('_maintenance_notice_msg_id')
        if last_msg_id:
            await _safe_delete_chat_message(
                context.bot, chat_id, last_msg_id, "pesan maintenance lama"
            )

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚙️ <b>BOT SEDANG MAINTENANCE</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Bot sedang diperbaiki sementara.\n"
                "Invoice baru ditutup sampai maintenance selesai.\n"
                "Invoice yang sudah dibuat tetap diproses.\n\n"
                f"Hubungi admin: {ADMIN_USERNAME}"
            ),
            parse_mode="HTML",
        )
        context.user_data['_maintenance_notice_msg_id'] = msg.message_id
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=1)


async def _send_single_cooldown_notice(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, sisa: int):
    """Kirim satu pesan cooldown saja agar chat tidak numpuk saat user spam /start."""
    chat_id = update.effective_chat.id

    async with _get_user_notice_lock(user_id):
        # Bersihkan command /start dari user jika Telegram mengizinkan.
        if update.message:
            await _safe_delete_chat_message(
                context.bot, chat_id, update.message.message_id, "pesan /start user"
            )

        now_mono = _time.monotonic()
        last_ts = context.user_data.get('_cooldown_notice_ts', 0)
        last_msg_id = context.user_data.get('_cooldown_notice_msg_id')

        # Debounce ringan: kalau user spam sangat cepat, cukup hapus /start-nya tanpa kirim ulang.
        if last_msg_id and (now_mono - last_ts) < COOLDOWN_NOTICE_DEBOUNCE_SECONDS:
            return

        # Replace pesan cooldown lama supaya yang tersisa hanya pesan terbaru.
        if last_msg_id:
            await _safe_delete_chat_message(context.bot, chat_id, last_msg_id, "pesan cooldown lama")

        end_time = (now_wib() + timedelta(minutes=sisa)).strftime('%H:%M')
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏳ <b>Cooldown Aktif</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Kamu baru saja membatalkan pesanan.\n\n"
                f"⏱ Sisa waktu: <b>{sisa} menit</b>\n"
                f"⏰ Berakhir: <b>{end_time} WIB</b>"
            ),
            parse_mode="HTML"
        )
        context.user_data['_cooldown_notice_msg_id'] = msg.message_id
        context.user_data['_cooldown_notice_ts'] = now_mono
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=1)

async def kirim_link_ke_buyer(context, user_id, paket, order_id, amount):
    group_link = await generate_group_link(context.bot, paket, order_id)
    link = group_link or (paket.get("link") or DEFAULT_LINK)
    link_section = _build_link_section(group_link, link)

    msg = await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"<b>✅ PEMBAYARAN BERHASIL</b>\n"
            f"========================\n\n"
            f"📦 <b>Detail Pesanan</b>\n"
            f"- Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
            f"- Order ID: <code>{esc(order_id)}</code>\n"
            f"- Total: {format_harga(amount)}\n\n"
            f"========================\n"
            f"{link_section}\n\n"
            f"Terima kasih telah berbelanja di Hyper Family Store! 🙏"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [styled_button("💬 Chat Admin", style="primary", url=await get_setting('link_admin', ADMIN_URL))]
        ])
    )
    simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=2)
    return link

# =================== AUTOGOPAY WEBHOOK SERVER ===================

async def _process_autogopay_settlement(order: dict, paid_amount: int):
    """Proses delivery di luar response webhook agar callback dibalas cepat."""
    try:
        # Poller berhenti setelah membaca status completed. Jangan membatalkan
        # task berdasarkan user_id di sini: buyer mungkin sudah punya order lain.
        paket = await get_product(order["paket_id"])
        await _handle_payment_success(
            _current_bot,
            order["order_id"],
            order["paket_id"],
            order["user_id"],
            order.get("user_name", "User"),
            paid_amount,
            {
                "amount": paid_amount,
                "status": "completed",
                "provider_status": "settlement",
            },
            paket=paket,
        )
        logger.info(f"[AUTOGOPAY WEBHOOK] Settlement diproses: {order['order_id']}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(
            f"[AUTOGOPAY WEBHOOK] Gagal memproses settlement {order.get('order_id')}: {e}",
            exc_info=True,
        )


async def autogopay_webhook_handler(request: aio_web.Request) -> aio_web.Response:
    logger.info("[AUTOGOPAY WEBHOOK] Callback diterima")
    if not AUTOGOPAY_API_KEY:
        logger.error("[AUTOGOPAY WEBHOOK] API key belum dikonfigurasi.")
        return aio_web.Response(status=503, text="webhook not configured")

    try:
        body = await request.read()
    except Exception:
        return aio_web.Response(status=400, text="invalid request")

    received_signature = (request.headers.get("X-Signature") or "").strip()
    if received_signature.lower().startswith("sha256="):
        received_signature = received_signature.split("=", 1)[1].strip()
    expected_signature = hmac.new(
        AUTOGOPAY_API_KEY.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not received_signature or not hmac.compare_digest(received_signature, expected_signature):
        logger.warning("[AUTOGOPAY WEBHOOK] Signature tidak valid.")
        return aio_web.Response(status=401, text="invalid signature")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return aio_web.Response(status=400, text="invalid json")
    if not isinstance(data, dict):
        return aio_web.Response(status=400, text="invalid payload")

    callback_event = str(
        data.get("event") or request.headers.get("X-Callback-Event") or ""
    ).strip().lower()
    if callback_event != "transaction.received":
        return aio_web.json_response({"success": True, "ignored": True})

    transaction = data.get("transaction")
    if not isinstance(transaction, dict):
        return aio_web.Response(status=400, text="missing transaction")

    payment_method = str(transaction.get("payment_method") or "QRIS").strip().upper()
    expected_provider = {"QRIS": "gopay", "QRIS_SHOPEEPAY": "shopeepay"}.get(payment_method)
    if expected_provider is None:
        return aio_web.json_response({"success": True, "ignored": True})
    transaction_ids = list(dict.fromkeys(
        str(transaction[key]) for key in ("order_sn", "transaction_id", "id", "order_id")
        if transaction.get(key)
    ))
    provider_status = str(transaction.get("status") or "").strip().lower()
    if payment_method == "QRIS_SHOPEEPAY":
        # Sebagian payload ShopeePay mengirim paid/order_status, bukan
        # transaction status settlement seperti GoPay.
        if transaction.get("paid") is True or _safe_int(transaction.get("order_status"), 0) == 1:
            provider_status = "success"
    paid_amount = _safe_int(transaction.get("amount"), -1)

    if not transaction_ids:
        return aio_web.Response(status=400, text="missing transaction id")

    order = None
    for transaction_id in transaction_ids:
        candidate = await get_order_by_provider_transaction_id(transaction_id)
        if candidate:
            stored_provider = str(candidate.get("payment_provider") or "autogopay").lower()
            if stored_provider == "autogopay":
                stored_provider = "gopay"  # Order GoPay lama tetap kompatibel.
            if stored_provider == expected_provider:
                order = candidate
                break
    if not order:
        logger.warning(
            f"[AUTOGOPAY WEBHOOK] Transaction ID/provider tidak cocok: {transaction_ids}"
        )
        return aio_web.Response(status=404, text="order not found")

    # Event non-settlement cukup dicatat. Polling tetap menjadi fallback status.
    is_paid = (
        provider_status in {"settlement", "success", "paid"}
        or transaction.get("paid") is True
        or _safe_int(transaction.get("order_status"), 0) == 1
    )
    if expected_provider == "shopeepay":
        if "paid" in transaction and transaction.get("paid") is not True:
            is_paid = False
        if "order_status" in transaction and _safe_int(transaction.get("order_status"), 0) != 1:
            is_paid = False
    if not is_paid:
        await update_order_provider_status(order["order_id"], provider_status)
        return aio_web.json_response({"success": True, "ignored": True})

    expected_amount = _safe_int(order.get("harga_dibayar"), 0)
    if paid_amount < 0 or expected_amount <= 0 or paid_amount != expected_amount:
        logger.error(
            f"[AUTOGOPAY WEBHOOK] Nominal tidak cocok untuk {order['order_id']}: "
            f"expected={expected_amount}, received={paid_amount}"
        )
        return aio_web.Response(status=400, text="amount mismatch")

    await update_order_provider_status(order["order_id"], provider_status)

    if order.get("status") != "waiting":
        if order.get("status") != "completed":
            logger.error("[PAYMENT] Dana masuk pada order non-aktif: %s", order["order_id"])
            if _current_bot and normalize_autogopay_status(order.get("provider_status")) != "completed":
                spawn_background(kirim_notif(
                    _current_bot,
                    "⚠️ <b>DANA MASUK PADA ORDER NON-AKTIF</b>\n"
                    f"Order: <code>{esc(order['order_id'])}</code>\n"
                    f"Provider: {esc(expected_provider)}\n"
                    f"Nominal: {format_harga(paid_amount)}\n"
                    "Periksa pembayaran ini secara manual; link tidak dikirim otomatis."
                ), name=f"late-payment-{order['order_id']}")
        return aio_web.json_response({"success": True, "already_processed": True})

    if not _current_bot:
        logger.error(f"[AUTOGOPAY WEBHOOK] Bot belum siap: {order['order_id']}")
        return aio_web.Response(status=503, text="bot not ready")

    # AutoGoPay meminta HTTP 200 maksimal 10 detik. Delivery Telegram dilakukan async.
    spawn_background(_process_autogopay_settlement(order, paid_amount), name=f"settlement-{order.get('order_id')}")
    return aio_web.json_response({"success": True})


_webhook_runner = None

# =================== WEB SERVER MANAGEMENT ===================

def _db_ping_sync():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT 1 AS ok")
            row = c.fetchone()
            return bool(row and row.get('ok') == 1)

async def _readiness_handler(request):
    try:
        if _async_db_pool is not None:
            value = await asyncio.wait_for(_async_db_pool.fetchval("SELECT 1"), timeout=2.0)
            db_ok = value == 1
        else:
            db_ok = await asyncio.wait_for(run_db(_db_ping_sync), timeout=2.0)
    except Exception:
        db_ok = False
    payload = {
        "ok": bool(db_ok and _current_bot is not None),
        "release": BOT_RELEASE,
        "db": db_ok,
        "bot_ready": _current_bot is not None,
        "async_db": _async_db_pool is not None,
        "payment_tasks": sum(1 for t in _payment_tasks.values() if not t.done()) if '_payment_tasks' in globals() else 0,
        "provider_circuit_open": _autogopay_circuit_is_open(),
    }
    return aio_web.json_response(payload, status=200 if payload["ok"] else 503)

async def _start_webhook_server():
    global _webhook_runner
    webhook_app = aio_web.Application()

    # /health tetap sederhana untuk platform; /ready memberi readiness internal lebih lengkap.
    webhook_app.router.add_post('/webhook/autogopay', autogopay_webhook_handler)
    webhook_app.router.add_get('/health', lambda r: aio_web.Response(text='ok'))
    webhook_app.router.add_get('/ready', _readiness_handler)
    webhook_app.router.add_get('/', lambda r: aio_web.Response(text='Hyper Family Store Bot - OK'))

    runner = aio_web.AppRunner(webhook_app)
    await runner.setup()
    _webhook_runner = runner
    port = int(os.environ.get('PORT', 8080))
    site = aio_web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"[WEBHOOK] Server webhook berjalan di port {port}")

# =================== POST INIT ===================


async def _event_loop_lag_monitor():
    """Mendeteksi CPU starvation/blocking I/O pada hosting tanpa mengganggu bot."""
    interval = 0.05
    last_warning = 0.0
    expected = _time.monotonic() + interval
    while True:
        await asyncio.sleep(interval)
        now_mono = _time.monotonic()
        lag = max(0.0, now_mono - expected)
        expected = now_mono + interval
        if lag >= 0.001:
            _loop_stalls.append((_time.perf_counter_ns(), lag*1000))
        if lag >= EVENT_LOOP_LAG_WARNING_SECONDS and now_mono-last_warning >= 5:
            last_warning = now_mono
            logger.warning(f"[EVENT LOOP LAG] Event loop tertahan {lag:.2f}s")


async def _prewarm_hot_settings():
    defaults = _menu_setting_defaults()
    defaults.update(notif_channel_id='', testimoni_channel_id='')
    settings = await get_settings(defaults)
    target = str(settings.get('testimoni_channel_id') or '').strip()
    if not target:
        status = 'not_configured'
    else:
        try:
            target_id = int(target)
            status = 'configured' if target_id < 0 else 'invalid_destination'
            if target_id == ADMIN_ID or str(target_id) == str(settings.get('notif_channel_id') or '').strip():
                status = 'conflicts_with_private_admin'
        except ValueError:
            status = 'invalid_destination'
    logger.info('[PUBLIC TESTI] destination=%s', status)

async def _post_init_warmup(application: Application):
    """Warmup non-kritis dipisahkan agar polling bisa menerima /start lebih cepat."""
    waiting_orders, all_products, _, _ = await asyncio.gather(
        get_all_waiting(force=True),
        get_all_products(),
        _prewarm_hot_settings(),
        _get_admin_ids_cached(),
    )

    await asyncio.gather(
        application.bot.set_my_commands(
            [
                BotCommand("start",   "Buka toko"),
                BotCommand("help",    "Bantuan penggunaan bot"),
                BotCommand("riwayat", "Lihat riwayat ordermu"),
                BotCommand("resend", "Kirim ulang link produk yang dibeli"),
            ],
            scope=BotCommandScopeDefault()
        ),
        application.bot.set_my_commands(
            [
                BotCommand("start", "Buka toko"),
                BotCommand("help", "Bantuan"),
                BotCommand("admin", "Panel admin"),
                BotCommand("emojiid", "Ambil ID emoji Premium"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID)
        ),
    )

    products_by_id = {p['paket_id']: p for p in all_products}
    if waiting_orders:
        logger.info(f"[POST_INIT] Menemukan {len(waiting_orders)} order aktif, memulihkan monitoring pembayaran...")
        for order in waiting_orders:
            paket = products_by_id.get(order['paket_id'])
            if not paket:
                continue
            recovery_amount = order.get('harga_dibayar') or paket['harga']
            fallback_timeout = AUTOGOPAY_DEFAULT_TIMEOUT_SECONDS
            expiry_dt = parse_provider_datetime(order.get('payment_expires_at'))
            if expiry_dt is not None:
                recovery_timeout = max(1, int((expiry_dt - now_wib()).total_seconds()))
            elif order.get('created_at'):
                try:
                    created_at = order['created_at']
                    if isinstance(created_at, str):
                        created_at = parse_provider_datetime(created_at)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    elapsed = int((now_wib() - created_at.astimezone(WIB)).total_seconds())
                    recovery_timeout = max(1, fallback_timeout - elapsed)
                except Exception as e:
                    logger.debug(f"Gagal menghitung selisih durasi order untuk recovery: {e}")
                    recovery_timeout = 60
            else:
                recovery_timeout = 60
            _start_payment_task(
                application.bot,
                order_id=order['order_id'],
                paket_id=order['paket_id'],
                user_id=order['user_id'],
                user_name=order.get('user_name', 'User'),
                amount=recovery_amount,
                timeout_seconds=recovery_timeout,
                paket=paket,
            )
            logger.debug(f"[POST_INIT] Monitoring dipulihkan: {order['order_id']}")


async def post_init(application: Application):
    global _current_bot
    _current_bot = application.bot

    logger.info("[RELEASE] %s", BOT_RELEASE)
    logger.info('[CALLBACK TRACE] enabled=%s max=%s', CALLBACK_TRACE_ENABLED, CALLBACK_TRACE_MAX)
    logger.info('[TELEGRAM TRANSPORT] keepalive_s=%.1f pool=%s', TELEGRAM_KEEPALIVE_SECONDS, TELEGRAM_CONNECTION_POOL_SIZE)
    logger.info(
        f"[PERFORMANCE] Executor aktif: buyer_db={DB_THREAD_WORKERS}, "
        f"admin_db={DB_ADMIN_THREAD_WORKERS}, cpu={CPU_THREAD_WORKERS}"
    )
    # Resource wajib siap sebelum polling; warmup, command setup, dan recovery
    # tidak lagi menahan update pertama setelah bot hidup.
    await asyncio.gather(get_http_session(), init_async_db_pool())
    spawn_background(_post_init_warmup(application), name="post-init-warmup")

    spawn_background(_auto_backup_loop(), name="auto-backup-loop")
    spawn_background(_buyer_reminder_loop(_current_bot), name="buyer-reminder-loop")
    spawn_background(_cleanup_cooldowns_loop(), name="cooldown-cleanup-loop")
    spawn_background(_event_loop_lag_monitor(), name="event-loop-lag-monitor")
    spawn_background(_latency_reporter_loop(), name="latency-reporter-loop")
    spawn_background(_start_webhook_server(), name="webhook-server")
    if TELEGRAM_TRANSPORT_PROBE:
        spawn_background(_probe_telegram_transport(application.bot), name='telegram-transport-probe')
    logger.info("[POST_INIT] Background task aktif dan bot siap menerima update.")

# =================== GRACEFUL SHUTDOWN ===================

async def _stop_blast_before_shutdown(application):
    tasks = [task for task in list(_blast_tasks.values()) if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def post_shutdown(application: Application):
    global _http_session, _pool, _webhook_runner, _async_db_pool
    logger.info("[SHUTDOWN] Memulai graceful shutdown...")

    # Hentikan task yang memakai HTTP/DB sebelum resource ditutup.
    payment_tasks = [task for task in list(_payment_tasks.values()) if task and not task.done()]
    for task in payment_tasks:
        task.cancel()
    if payment_tasks:
        await asyncio.gather(*payment_tasks, return_exceptions=True)
    _payment_tasks.clear()
    _payment_task_orders.clear()

    blast_map = globals().get('_blast_tasks', {})
    blast_tasks = [task for task in list(blast_map.values()) if task and not task.done()]
    for task in blast_tasks:
        task.cancel()
    if blast_tasks:
        await asyncio.gather(*blast_tasks, return_exceptions=True)
    blast_map.clear()

    pending_background = [task for task in list(_background_tasks) if not task.done()]
    for task in pending_background:
        task.cancel()
    if pending_background:
        await asyncio.gather(*pending_background, return_exceptions=True)

    if _webhook_runner:
        await _webhook_runner.cleanup()
        _webhook_runner = None
        logger.info("[SHUTDOWN] Web server dibersihkan.")
    if _http_session and not _http_session.closed:
        await _http_session.close()
        logger.info("[SHUTDOWN] HTTP session ditutup.")
    if _async_db_pool is not None:
        await _async_db_pool.close()
        _async_db_pool = None
        logger.info("[SHUTDOWN] Async PostgreSQL pool ditutup.")

    try:
        _db_executor.shutdown(wait=True, cancel_futures=True)
        _db_admin_executor.shutdown(wait=True, cancel_futures=True)
        _cpu_executor.shutdown(wait=True, cancel_futures=True)
    except TypeError:
        _db_executor.shutdown(wait=True)
        _db_admin_executor.shutdown(wait=True)
        _cpu_executor.shutdown(wait=True)
    logger.info("[SHUTDOWN] Executor dihentikan.")
    if _pool:
        _pool.closeall()
        logger.info("[SHUTDOWN] Psycopg2 pool ditutup.")
    logger.info("[SHUTDOWN] Shutdown selesai.")

# =================== USER HANDLERS ===================

async def _refresh_active_order_status(bot, active: dict, paket: dict, user_name: str):
    try:
        amount = active.get("harga_dibayar") or paket.get("harga", 0)
        trans = await get_transaction_detail(active["order_id"], amount)
        if not trans or trans.get("status") != "completed":
            return
        await _handle_payment_success(bot, active["order_id"], active["paket_id"],
                                      active["user_id"], user_name, amount, trans, paket=paket)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[PAYMENT REFRESH] Gagal refresh order %s: %s", active.get('order_id'), type(exc).__name__)

async def _start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, trace=None):
    user_id = update.effective_user.id

    # Admin/maintenance sudah memakai cache; tiga pemeriksaan buyer digabung
    # menjadi satu snapshot DB agar cache miss tidak membuat tiga round-trip.
    snapshot, settings = await asyncio.gather(
        get_start_snapshot(user_id),
        get_settings(_menu_setting_defaults()),
    )
    maint = settings['maintenance'] == '1'
    banned = snapshot['banned']
    sisa = snapshot['cooldown_minutes']
    active = snapshot['active']
    if trace:
        trace.mark("buyer-checks")

    if maint and not active and not await is_admin(user_id, context):
        await _send_single_maintenance_notice(update, context, user_id)
        return

    # Setelah maintenance selesai, atau saat ada invoice lama yang harus
    # dilanjutkan, bersihkan notice maintenance yang tersimpan.
    notice_id = context.user_data.pop('_maintenance_notice_msg_id', None)
    if notice_id:
        spawn_background(_safe_delete_chat_message(context.bot, user_id, notice_id, "pesan maintenance lama"),
                         name=f"start-clean-notice-{user_id}")

    if banned:
        await update.message.reply_text(
            "🚫 <b>Akun kamu diblokir</b>\n"
            "========================\n\n"
            "Kamu tidak bisa menggunakan bot ini.\n"
            "Hubungi admin jika ada pertanyaan.",
            parse_mode="HTML"
        )
        return

    if sisa > 0:
        await _send_single_cooldown_notice(update, context, user_id, sisa)
        return
    if active:
        paket = await get_product(active["paket_id"])
        if not paket:
            paket = {"emoji": "📦", "nama": "Produk", "harga": 0, "link": DEFAULT_LINK}

        await _ensure_payment_task_for_order(
            context.bot,
            active,
            paket,
            active.get('user_name', update.effective_user.full_name)
        )
        # Membantu kasus webhook terlambat/gagal tanpa menahan balasan /start.
        spawn_background(
            _refresh_active_order_status(
                context.bot,
                active,
                paket,
                active.get('user_name', update.effective_user.full_name),
            ),
            name=f"start-refresh-{active['order_id']}",
        )

        total = active.get("harga_dibayar") or paket["harga"]
        text = (
            f"<b>⏳ ORDER AKTIF</b>\n"
            f"========================\n\n"
            f"Kamu masih punya pesanan yang belum dibayar:\n\n"
            f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
            f"💰 Total: {format_harga(total)}\n"
            f"📝 Order ID: <code>{esc(active['order_id'])}</code>\n\n"
            f"<i>Silakan selesaikan pembayaran atau batalkan pesanan dulu.</i>"
        )
        keyboard = [[styled_button("❌ Batalkan Pesanan", style="danger", callback_data="cancel_order")]]
        await _send_user_message_replace(
            context,
            user_id,
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
            keep_last=2,
        )
        if trace:
            trace.mark("active-order-send")
        return

    menu_text, menu_keyboard = await build_main_menu_parts(update.effective_user.full_name, settings=settings)
    if trace:
        trace.mark("menu-built")
    # An explicit /start must place a visible reply after the user's command.
    # Reuse Telegram's file_id, not an older message that may be off-screen.
    await _send_main_menu_photo(context, user_id, menu_text, InlineKeyboardMarkup(menu_keyboard),
                               photo_id=settings[_MENU_PHOTO_SETTING])
    if trace:
        trace.mark("menu-send")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wrapper tracing untuk mengukur delay /start tanpa mengubah alur bisnis."""
    trace = PerfTrace(
        "start",
        user_id=getattr(getattr(update, "effective_user", None), "id", "-"),
    )
    try:
        return await _start_handler(update, context, trace=trace)
    finally:
        trace.finish()


async def cmd_emojiid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan custom_emoji_id dari emoji pada command atau pesan yang dibalas."""
    if not await is_admin(update.effective_user.id, context):
        return

    messages = [update.effective_message]
    if update.effective_message.reply_to_message:
        messages.append(update.effective_message.reply_to_message)

    emoji_ids = []
    for message in messages:
        entities = list(message.entities or []) + list(message.caption_entities or [])
        for entity in entities:
            if str(entity.type) in {'custom_emoji', 'MessageEntityType.CUSTOM_EMOJI'}:
                emoji_id = getattr(entity, 'custom_emoji_id', None)
                if emoji_id and emoji_id not in emoji_ids:
                    emoji_ids.append(emoji_id)

    if not emoji_ids:
        await update.effective_message.reply_text(
            "Kirim <code>/emojiid</code> diikuti emoji Premium, atau balas pesan "
            "yang berisi emoji Premium dengan <code>/emojiid</code>.",
            parse_mode="HTML"
        )
        return

    lines = "\n".join(f"<code>{esc(emoji_id)}</code>" for emoji_id in emoji_ids)
    await update.effective_message.reply_text(
        f"✅ <b>Custom Emoji ID</b>\n\n{lines}\n\n"
        "Masukkan ID tersebut ke variabel tombol yang diinginkan.",
        parse_mode="HTML"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /help - menampilkan bantuan penggunaan bot."""
    text = (
        "<b>📖 BANTUAN PENGGUNAAN BOT</b>\n"
        "========================\n\n"
        "<b>🛒 CARA BELI:</b>\n"
        "1. Ketik /start untuk membuka toko\n"
        "2. Pilih paket yang diinginkan\n"
        "3. Scan QRIS untuk membayar\n"
        "4. Link produk dikirim otomatis setelah bayar\n\n"
        "<b>📋 COMMAND TERSEDIA:</b>\n"
        "/start - Buka toko & mulai belanja\n"
        "/help - Tampilkan bantuan ini\n"
        "/riwayat - Lihat riwayat order kamu\n"
        "/resend - Pilih produk untuk kirim ulang link terbaru\n\n"
        "<b>❓ PERTANYAAN UMUM:</b>\n\n"
        "<b>Q: Bagaimana cara membayar?</b>\n"
        "A: Scan QRIS yang muncul dengan e-wallet (GoPay, OVO, Dana, dll).\n\n"
        "<b>Q: Berapa lama prosesnya?</b>\n"
        "A: Biasanya terverifikasi dalam beberapa detik; saat provider sibuk bisa lebih lama.\n\n"
        "<b>Q: Link tidak masuk?</b>\n"
        "A: Ketik /start untuk cek status, atau hubungi admin.\n\n"
        "<b>Q: Bisa ganti paket?</b>\n"
        "A: Bisa, maksimal 1x ganti paket sebelum bayar.\n\n"
        "========================\n"
        "💬 Butuh bantuan? Hubungi admin:"
    )
    link_admin = await get_setting('link_admin', ADMIN_URL)
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Hubungi Admin", url=link_admin)]
        ])
    )

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)
    try:
        page = max(0, min(100000, int(query.data.split('|', 1)[1]))) if '|' in query.data else 0
    except ValueError:
        page = 0
    page, pages, shown = await _get_buyer_products_page(page)
    context.user_data['_buyer_package_page'] = page
    text = "📦 <b>DAFTAR PAKET</b>\n\nSilakan pilih paket yang tersedia di bawah ini."
    keyboard = []
    for product in shown:
        callback = f"pilih_{product['paket_id']}|{page}"
        if len(callback.encode('utf-8')) > 64:
            callback = f"pilih_{product['paket_id']}"
        keyboard.append([styled_button(
            f"{product.get('emoji') or '📦'} {str(product.get('nama') or 'Produk')[:50]}",
            callback_data=callback)])
    if not shown:
        text = "📦 <b>DAFTAR PAKET</b>\n\nBelum ada paket yang tersedia."
    if pages > 1:
        text += f"\n\nHalaman {page + 1}/{pages}"
    if page + 1 < pages:
        keyboard.append([styled_button("Berikutnya ➡️", callback_data=f"buy|{page + 1}", style="primary")])
    back = f"buy|{page - 1}" if page else "back_to_menu"
    keyboard.append([styled_button("⬅️ Kembali", callback_data=back)])
    await _render_buyer_menu(query, text, InlineKeyboardMarkup(keyboard))

async def pilih_paket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)

    selection = query.data[len("pilih_"):]
    if '|' in selection:
        paket_id, page_text = selection.rsplit('|', 1)
        try:
            page = max(0, min(100000, int(page_text)))
        except ValueError:
            page = 0
    else:
        paket_id = selection
        page = context.user_data.get('_buyer_package_page', 0)
    back_callback = f"buy|{page}" if page else "buy"
    # get_product memakai index paket_id di memory; pengecekan pembelian tetap
    # dilakukan ulang saat tombol "Lanjut Bayar" ditekan.
    paket = await get_product(paket_id)
    if not paket:
        await _callback_feedback(query, context, "❌ Produk tidak ditemukan.")
        return

    status_text = "Tersedia" if paket.get('aktif', True) else "Tidak tersedia"
    detail_desc = str(paket.get('deskripsi') or '-')
    if len(detail_desc) > 300:
        detail_desc = detail_desc[:297] + '...'
    text = (
        f"📦 <b>DETAIL PAKET</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<blockquote>"
        f"{esc(paket.get('emoji') or '📦')} Paket: <b>{esc(str(paket.get('nama') or 'Produk')[:60])}</b>\n"
        f"🎬 Isi: {esc(detail_desc)}\n"
        f"💰 Harga: <b>{format_harga(paket.get('harga', 0))}</b>\n"
        f"♾️ Status: {status_text}"
        f"</blockquote>\n"
    )

    if paket.get('aktif', True):
        text += "Lanjut buat order dan bayar via QRIS?"
        keyboard = [
            [styled_button("✅ Lanjut Bayar", style="success", callback_data=f"confirm_buy_{paket_id}")],
            [styled_button("⬅️ Kembali", callback_data=back_callback)]
        ]
    else:
        text += "Paket ini sedang tidak bisa dibeli. Silakan pilih paket lain."
        keyboard = [[styled_button("⬅️ Kembali", callback_data=back_callback)]]

    if len(str(paket.get('deskripsi') or '-')) > 300:
        keyboard.insert(0, [styled_button("📄 Deskripsi lengkap", style="primary", callback_data=f"buyer_desc_{paket_id}")])
    await _render_buyer_menu(query, text, InlineKeyboardMarkup(keyboard))

async def buyer_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)
    paket = await get_product(query.data[len("buyer_desc_"):])
    if not paket:
        await _callback_feedback(query, context, "❌ Produk tidak ditemukan.")
        return
    desc = str(paket.get('deskripsi') or '-')
    for start in range(0, len(desc), 1500):
        await query.message.reply_text(esc(desc[start:start + 1500]), parse_mode="HTML")

async def confirm_buy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user_name = query.from_user.full_name
    paket_id = query.data.replace("confirm_buy_", "")
    if not claim_invoice_creation(user_id):
        await _fast_callback_ack(query, "⏳ Invoice sedang dibuat. Tunggu sebentar.")
        return

    # Jangan biarkan spinner Telegram menunggu query DB/cooldown.
    trace = PerfTrace("confirm-buy", user_id=user_id, paket_id=paket_id)
    try:
        await _fast_callback_ack(query, "⏳ Memproses...")
        trace.mark("callback-answer")
        # Pengecekan yang tidak saling bergantung dijalankan bersamaan.
        banned, paket, active, sisa = await asyncio.gather(
            is_banned(user_id),
            get_product(paket_id),
            get_active_order(user_id),
            get_cooldown_sisa_db(user_id),
        )
        trace.mark("buyer-checks")

        if banned:
            await _callback_feedback(query, context, "🚫 Akun kamu diblokir. Hubungi admin.")
            return

        if not paket:
            await _callback_feedback(query, context, "❌ Produk tidak ditemukan.")
            return
        if not paket.get('aktif', True):
            await _callback_feedback(query, context, "❌ Paket ini sedang tidak tersedia.")
            return

        # Prioritas perilaku tetap sama: invoice aktif dicek sebelum cooldown.
        if active:
            paket_active = await get_product(active["paket_id"]) or {
                "emoji": "📦", "nama": "Produk", "harga": 0
            }
            await _ensure_payment_task_for_order(
                context.bot,
                active,
                paket_active,
                active.get('user_name', user_name)
            )
            total = active.get("harga_dibayar") or paket_active["harga"]
            caption = (
                f"<b>⏳ ORDER AKTIF</b>\n"
                f"========================\n\n"
                f"📦 Paket: {esc(paket_active['emoji'])} {esc(paket_active['nama'])}\n"
                f"💰 Total: {format_harga(total)}\n"
                f"📝 Order ID: <code>{esc(active['order_id'])}</code>\n\n"
                f"⚠️ Selesaikan pembayaran atau batalkan dulu."
            )
            keyboard = [[InlineKeyboardButton("❌ Batalkan", callback_data="cancel_order")]]
            await telegram_retry(
                lambda: _render_buyer_menu(query, caption, InlineKeyboardMarkup(keyboard)),
                label="tampilkan invoice aktif",
            )
            trace.mark("active-order")
            return

        if await _reject_new_invoice_during_maintenance(query, context, user_id):
            return

        if sisa > 0:
            await _show_cooldown_alert(query, sisa)
            return

        await _buat_order_baru(
            update, context, query, user_id, user_name, paket,
            order_changes=0, perf_trace=trace
        )
    finally:
        release_invoice_creation(user_id)
        trace.finish()

async def prereq_buy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user_name = query.from_user.full_name

    parts = query.data.split("|")
    prereq_pid = parts[0].replace("prereq_buy_", "")
    parent_order_id = parts[1] if len(parts) > 1 else None
    if not claim_invoice_creation(user_id):
        await _fast_callback_ack(query, "⏳ Invoice sedang dibuat. Tunggu sebentar.")
        return

    trace = PerfTrace("prereq-buy", user_id=user_id, paket_id=prereq_pid)
    try:
        await _fast_callback_ack(query, "⏳ Memproses...")
        trace.mark("callback-answer")
        banned, paket, active, sisa = await asyncio.gather(
            is_banned(user_id),
            get_product(prereq_pid),
            get_active_order(user_id),
            get_cooldown_sisa_db(user_id),
        )
        trace.mark("buyer-checks")

        if banned:
            await _callback_feedback(query, context, "🚫 Akun kamu diblokir. Hubungi admin.")
            return

        if not paket:
            await _callback_feedback(query, context, "❌ Produk tidak ditemukan.")
            return

        if not paket.get('aktif', True):
            await _callback_feedback(query, context, "❌ Paket ini sedang tidak tersedia.")
            return

        if active:
            await _callback_feedback(query, context, "⏳ Kamu sudah punya invoice aktif! Batalkan dulu.")
            return

        if await _reject_new_invoice_during_maintenance(query, context, user_id):
            return

        if sisa > 0:
            await _show_cooldown_alert(query, sisa)
            return

        if parent_order_id:
            parent_order = await get_order_by_id(parent_order_id)
            if parent_order and int(parent_order['user_id']) != user_id:
                await _callback_feedback(query, context, "❌ Pesanan bukan milik kamu.")
                return
            if parent_order:
                context.user_data['prereq_ctx'] = {
                    'parent_order_id': parent_order_id,
                    'parent_paket_id': parent_order['paket_id'],
                }
        trace.mark("prereq-context")

        await _buat_order_baru(
            update, context, query, user_id, user_name, paket,
            order_changes=0, perf_trace=trace
        )
    finally:
        release_invoice_creation(user_id)
        trace.finish()

async def _post_invoice_created_tasks(bot, context, user_id: int, order_id: str,
                                      user_name: str, paket: dict,
                                      total_payment: int, expire: str):
    """Pekerjaan non-kritis setelah QR terkirim agar handler user cepat selesai."""
    try:
        await hapus_msg_user_lama(context, user_id, keep_last=1)
        msg_id = await kirim_notif(
            bot,
            _format_order_notif(
                "📢 <b>ORDER BARU MASUK</b>",
                user_name, user_id, paket, order_id,
                amount=total_payment,
                extra=f"⏳ Menunggu pembayaran · berlaku sampai {expire} WIB"
            )
        )
        if msg_id:
            await set_admin_msg_id(order_id, msg_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[ORDER] Gagal menjalankan tugas setelah invoice {order_id}: {e}", exc_info=True)


async def _buat_order_baru(update, context, query, user_id, user_name, paket, order_changes=0, perf_trace=None):
    # Guard terakhir untuk callback lama/race saat admin mengaktifkan maintenance
    # tepat ketika buyer menekan tombol konfirmasi.
    if await _reject_new_invoice_during_maintenance(query, context, user_id):
        return False

    # A new invoice may edit this message outside the menu renderer.
    if query is not None:
        _forget_menu_render(query)
    # Gunakan pesan callback sebagai indikator loading agar tidak menambah request Telegram.
    loading_msg = None
    if query and not (query.message and query.message.photo):
        try:
            await telegram_retry(
                lambda: query.edit_message_text("⏳ Membuat invoice..."),
                label="tampilkan loading invoice",
            )
            loading_msg = query.message
        except Exception as exc:
            logger.warning(f"[ORDER] Gagal edit pesan loading, memakai pesan baru: {exc}")
            loading_msg = None
    if loading_msg is None:
        loading_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⏳ Membuat invoice...",
        )
    if perf_trace:
        perf_trace.mark("loading-ui")

    rand_suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    order_id = f"HFB-{user_id}-{now_wib().strftime('%Y%m%d%H%M%S')}-{rand_suffix}"

    trans_data = await create_transaction_qris(
        order_id=order_id,
        amount=paket["harga"],
        description=f"Hyper Family Buy - {paket['nama']}"
    )
    if perf_trace:
        perf_trace.mark("autogopay-create")

    if not trans_data:
        try:
            await telegram_retry(
                lambda: loading_msg.edit_text(
                    "❌ Gagal membuat invoice. Silakan coba lagi.\nKetik /start untuk memulai ulang."
                ),
                label="tampilkan gagal membuat invoice",
            )
            msg = loading_msg
        except Exception:
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Gagal membuat invoice. Silakan coba lagi.\nKetik /start untuk memulai ulang.",
            )
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        return

    qris_string = trans_data.get('qr_string') or trans_data.get('payment_number', '')
    if not qris_string:
        try:
            await telegram_retry(
                lambda: loading_msg.edit_text(
                    "❌ Gagal membuat QRIS. Silakan coba lagi.\nKetik /start untuk memulai ulang."
                ),
                label="tampilkan gagal membuat QRIS",
            )
            msg = loading_msg
        except Exception:
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Gagal membuat QRIS. Silakan coba lagi.\nKetik /start untuk memulai ulang.",
            )
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        return

    amount = trans_data.get('amount', paket["harga"])
    fee = trans_data.get('fee', 0)
    total_payment = amount + fee

    payment_provider = str(trans_data.get("payment_provider") or "gopay").lower()
    expiry_dt = parse_provider_datetime(trans_data.get('expiry_time'))
    if expiry_dt is None:
        expiry_dt = now_wib() + timedelta(seconds=AUTOGOPAY_DEFAULT_TIMEOUT_SECONDS)
    timeout_secs = max(1, int((expiry_dt - now_wib()).total_seconds()))
    expire = expiry_dt.strftime("%H:%M")

    try:
        await save_order(
            user_id, user_name, paket['paket_id'], order_id,
            harga_dibayar=total_payment,
            order_changes=order_changes,
            payment_provider=payment_provider,
            provider_transaction_id=trans_data.get('provider_transaction_id'),
            provider_order_id=trans_data.get('provider_order_id'),
            provider_status=trans_data.get('provider_status'),
            payment_expires_at=expiry_dt,
            checkout_url=trans_data.get('checkout_url'),
            qr_url=trans_data.get('qr_url'),
        )
        if perf_trace:
            perf_trace.mark("save-order")
    except Exception as e:
        logger.error(f"[PAYMENT] Gagal menyimpan order {order_id}: {e}", exc_info=True)
        if payment_provider != "shopeepay":
            await _autogopay_cancel_transaction(trans_data.get('provider_transaction_id'))
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Gagal menyimpan invoice. Silakan coba lagi.\nKetik /start untuk memulai ulang.",
        )
        simpan_msg_user(context, user_id, msg.message_id)
        spawn_background(
            _safe_delete_chat_message(
                context.bot, update.effective_chat.id, loading_msg.message_id, "pesan loading gagal"
            ),
            name=f"delete-failed-loading-{order_id}"
        )
        return

    try:
        qr_buffer = await run_cpu(generate_qr_image, qris_string)
        if perf_trace:
            perf_trace.mark("generate-qr")

        sisa_ganti = 1 - order_changes

        prereq_ctx = context.user_data.get('prereq_ctx') if context else None
        prereq_note = ""
        if prereq_ctx:
            parent_pid = prereq_ctx.get('parent_paket_id')
            parent_paket = await get_product(parent_pid) if parent_pid else None
            if parent_paket:
                prereq_note = (
                    f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>Tujuan:</b> Syarat untuk {esc(parent_paket['emoji'])} {esc(parent_paket['nama'])}\n"
                    f"Setelah lunas, link akan <b>otomatis dikirim</b>. 🚀"
                )

        caption = (
            f"<b>{esc(paket['emoji'])} {esc(paket['nama']).upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 <b>Detail Pembayaran</b>\n"
            f"- Harga: {format_harga(amount)}\n"
            f"- Fee: {format_harga(fee)}\n"
            f"- <b>Total: {format_harga(total_payment)}</b>\n\n"
            f"📝 Order ID: <code>{esc(order_id)}</code>\n"
            f"⏰ Berlaku hingga: {expire} WIB\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Scan QRIS di atas untuk membayar</b>\n\n"
            f"✅ Nominal sudah termasuk fee\n"
            f"✅ Pembayaran otomatis terverifikasi\n"
            f"✅ Link produk dikirim otomatis setelah bayar"
            f"{prereq_note}\n\n"
            f"⏳ Menunggu pembayaran..."
        )

        kb = []
        checkout_url = str(trans_data.get('checkout_url') or '').strip()
        parsed_checkout = urllib.parse.urlparse(checkout_url) if checkout_url else None
        if parsed_checkout and parsed_checkout.scheme == 'https' and parsed_checkout.netloc:
            kb.append([InlineKeyboardButton("🌐 Buka Halaman Pembayaran", url=checkout_url)])
        if sisa_ganti > 0:
            kb.append([InlineKeyboardButton(f"🔄 Ganti Paket (sisa {sisa_ganti}x)", callback_data="ganti_paket_list")])
        kb.append([styled_button("❌ Batalkan Pesanan", style="danger", callback_data="cancel_order")])

        msg = await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=qr_buffer,
            caption=caption,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        if perf_trace:
            perf_trace.mark("telegram-send-qr")

        await set_buyer_msg_id(order_id, msg.message_id)
        simpan_msg_user(context, user_id, msg.message_id)
        spawn_background(
            _safe_delete_chat_message(
                context.bot, update.effective_chat.id, loading_msg.message_id, "pesan loading invoice"
            ),
            name=f"delete-loading-{order_id}"
        )

    finally:
        # The database row is durable; Telegram failure must not disable payment recovery.
        _start_payment_task(context.bot, order_id, paket['paket_id'], user_id, user_name,
                            total_payment, timeout_seconds=timeout_secs, paket=paket)
    spawn_background(
        _post_invoice_created_tasks(
            context.bot, context, user_id, order_id, user_name,
            paket, total_payment, expire
        ),
        name=f"invoice-post-{order_id}"
    )
    if perf_trace:
        perf_trace.mark("finalize-invoice")

# =================== CANCEL ORDER ===================

async def cancel_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    active = await get_active_order(user_id)
    if active:
        paket = await get_product(active["paket_id"])
        amount = active.get("harga_dibayar") or (paket["harga"] if paket else 0)
        cancelled_order_id = active["order_id"]
        cancelled_user_name = query.from_user.full_name
        # Status lokal diubah segera agar /start berikutnya tidak tertahan oleh
        # HTTP cancel provider. Cleanup provider/notifikasi berjalan di background.
        if await update_order_status(cancelled_order_id, "cancelled") is None:
            await _callback_feedback(query, context, "Status pesanan sudah berubah. Buka /start untuk memeriksa.")
            return
        await _stop_payment_task(user_id, cancelled_order_id)

        async def _finalize_cancel():
            try:
                if amount:
                    await cancel_transaction(cancelled_order_id, amount)
                await hapus_qris_buyer_lama(context.bot, cancelled_order_id, user_id)
                await hapus_notif_lama(context.bot, cancelled_order_id)
                paket_notif = paket or {
                    "emoji": "📦",
                    "nama": active["paket_id"],
                }
                msg_id = await kirim_notif(
                    context.bot,
                    _format_order_notif(
                        "❌ <b>DIBATALKAN BUYER</b>",
                        cancelled_user_name,
                        user_id,
                        paket_notif,
                        cancelled_order_id,
                    )
                )
                if msg_id:
                    await set_admin_msg_id(cancelled_order_id, msg_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[CANCEL] Cleanup order {cancelled_order_id} gagal: {exc}")

        spawn_background(
            _finalize_cancel(),
            name=f"cancel-finalize-{cancelled_order_id}",
        )

    await set_cooldown_db(user_id)

    prereq_ctx = context.user_data.pop('prereq_ctx', None)
    context.user_data.clear()

    try:
        await query.message.delete()
    except Exception as e:
        logger.debug(f"Gagal menghapus pesan order yang dibatalkan: {e}")

    if prereq_ctx:
        parent_order_id = prereq_ctx.get('parent_order_id')
        parent_paket_id = prereq_ctx.get('parent_paket_id')
        parent_order = await get_order_by_id(parent_order_id) if parent_order_id else None
        parent_paket = await get_product(parent_paket_id) if parent_paket_id else None
        if parent_order and parent_paket:
            req_str = parent_paket.get("requires_paket_ids") or ""
            all_req_ids = [p.strip() for p in req_str.split(",") if p.strip()]
            missing = await check_prerequisites_sync(user_id, req_str)
            missing_set = set(missing)
            fulfilled_count = len(all_req_ids) - len(missing_set)
            total_count = len(all_req_ids)

            prereq_lines = []
            buy_buttons = []
            for pid in all_req_ids:
                p_obj = await get_product(pid)
                label = f"{p_obj['emoji']} {p_obj['nama']}" if p_obj else pid
                if pid in missing_set:
                    prereq_lines.append(f"  ❌ {label}")
                    buy_buttons.append([InlineKeyboardButton(
                        f"🛒 Beli {p_obj['nama'] if p_obj else pid}",
                        callback_data=f"prereq_buy_{pid}|{parent_order_id}"
                    )])
                else:
                    prereq_lines.append(f"  ✅ {label}")

            prereq_status = "\n".join(prereq_lines)
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"<b>⏸️ Pembelian dibatalkan</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📦 Paket: {esc(parent_paket['emoji'])} {esc(parent_paket['nama'])}\n"
                    f"🔖 Order ID: <code>{esc(parent_order_id)}</code>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📋 Progress syarat <b>({fulfilled_count}/{total_count} terpenuhi)</b>:\n"
                    f"{prereq_status}\n\n"
                    f"Beli paket ❌ di bawah untuk melanjutkan. 👇"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buy_buttons)
            )
            simpan_msg_user(context, user_id, msg.message_id)
            await hapus_msg_user_lama(context, user_id, keep_last=1)
            return

    menu_text, menu_keyboard = await build_main_menu_parts(update.effective_user.full_name)
    await _send_main_menu_photo(context, user_id, menu_text, InlineKeyboardMarkup(menu_keyboard))

# =================== BACK TO MENU ===================

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)
    text, keyboard = await build_main_menu_parts(update.effective_user.full_name)
    if query.message and query.message.photo:
        await _render_buyer_menu(query, text, InlineKeyboardMarkup(keyboard))
    else:
        await _send_main_menu_photo(context, update.effective_user.id, text, InlineKeyboardMarkup(keyboard))

async def buyer_how(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)
    text = ("📖 <b>CARA ORDER</b>\n\n"
            "1. Tekan Daftar Paket dan pilih paket.\n"
            "2. Tekan Lanjut Bayar dan selesaikan pembayaran QRIS.\n"
            "3. Ikuti petunjuk bot untuk mendapatkan akses.\n\n"
            "Jika ada kendala, hubungi admin.")
    await _render_buyer_menu(query, text, InlineKeyboardMarkup([[styled_button("⬅️ Kembali", callback_data="back_to_menu", style="primary")]]))

async def buyer_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)
    orders = await get_buyer_history_with_products(update.effective_user.id)
    labels = {'completed':'✅ Selesai', 'waiting':'⏳ Menunggu Bayar', 'pending':'🔄 Diproses', 'cancelled':'❌ Dibatalkan', 'expired':'⏰ Kedaluwarsa', 'rejected':'🚫 Ditolak'}
    text = "📜 <b>RIWAYAT ORDER</b>\n\n"
    for o in orders[:5]:
        text += f"{esc(o.get('paket_emoji') or '📦')} <b>{esc(str(o.get('paket_nama') or o['paket_id'])[:60])}</b>\n{esc(labels.get(o['status'], o['status']))} • {format_harga(o.get('harga_dibayar') or o.get('paket_harga') or 0)}\n\n"
    if not orders:
        text += "Kamu belum pernah melakukan pembelian."
    else:
        text += "Ketik /riwayat untuk melihat 10 transaksi terakhir."
    keyboard = []
    if orders:
        keyboard.append([styled_button("🔄 Kirim Ulang Link", callback_data="resend_menu", style="success")])
    keyboard.append([styled_button("⬅️ Kembali", callback_data="back_to_menu", style="primary")])
    await _render_buyer_menu(query, text, InlineKeyboardMarkup(keyboard))

# =================== GANTI PAKET ===================

async def ganti_paket_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await _fast_callback_ack(query)

    if await _reject_new_invoice_during_maintenance(query, context, user_id):
        return

    active = await get_active_order(user_id)
    if not active:
        await _callback_feedback(query, context, "❌ Tidak ada pesanan aktif.")
        return

    changes_used = active.get('order_changes', 0)
    if changes_used >= 1:
        await _callback_feedback(query, context, "⛔ Batas ganti paket sudah tercapai (1x).")
        return

    current_paket_id = active['paket_id']
    products = await get_all_products()
    products = [p for p in products if p.get('aktif', True)]

    sisa = 1 - changes_used
    text = (
        f"<b>🔄 GANTI PAKET</b>\n"
        f"========================\n\n"
        f"Sisa kesempatan ganti: <b>{sisa}x</b>\n\n"
        f"Pilih paket baru:\n"
        f"<i>(Pesanan lama akan otomatis dibatalkan)</i>"
    )

    keyboard = []
    for p in products:
        if p['paket_id'] == current_paket_id:
            continue
        keyboard.append([InlineKeyboardButton(
            f"{p['emoji']} {p['nama']} - {format_harga(p['harga'])}",
            callback_data=f"ganti_paket_konfirm|{p['paket_id']}"
        )])
    keyboard.append([InlineKeyboardButton("⬅️ Batal", callback_data="ganti_paket_batal")])

    if len(keyboard) == 1:
        await _callback_feedback(query, context, "❌ Tidak ada paket lain yang tersedia.")
        return

    try:
        await query.message.edit_caption(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def ganti_paket_konfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await _fast_callback_ack(query)

    if await _reject_new_invoice_during_maintenance(query, context, user_id):
        return

    new_paket_id = query.data.split("|", 1)[1]
    new_paket = await get_product(new_paket_id)
    if not new_paket:
        await _callback_feedback(query, context, "❌ Produk tidak ditemukan.")
        return

    active = await get_active_order(user_id)
    if not active:
        await _callback_feedback(query, context, "❌ Pesanan aktif tidak ditemukan.")
        return

    changes_used = active.get('order_changes', 0)
    sisa = 2 - changes_used

    text = (
        f"<b>⚠️ KONFIRMASI GANTI PAKET</b>\n"
        f"========================\n\n"
        f"Kamu akan beralih ke:\n"
        f"{esc(new_paket['emoji'])} <b>{esc(new_paket['nama'])}</b>\n"
        f"💰 {format_harga(new_paket['harga'])}\n\n"
        f"📌 Pesanan QRIS lama akan <b>dibatalkan</b> dan invoice baru akan dibuat.\n\n"
        f"Sisa kesempatan ganti setelah ini: <b>{sisa - 1}x</b>\n\n"
        f"Lanjutkan?"
    )

    keyboard = [
        [
            styled_button("✅ Ya, Ganti", style="success", callback_data=f"ganti_paket_exec|{new_paket_id}"),
            styled_button("❌ Batal", style="danger", callback_data="ganti_paket_batal"),
        ]
    ]
    try:
        await query.message.edit_caption(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def ganti_paket_exec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user_name = query.from_user.full_name
    await _fast_callback_ack(query, "⏳ Memproses pergantian paket...")

    if await _reject_new_invoice_during_maintenance(query, context, user_id):
        return

    active = await get_active_order(user_id)
    if not active:
        await _callback_feedback(query, context, "❌ Pesanan aktif tidak ditemukan.")
        return

    changes_used = active.get('order_changes', 0)
    if changes_used >= 1:
        await _callback_feedback(query, context, "⛔ Batas ganti paket sudah tercapai (1x).")
        return

    new_paket_id = query.data.split("|", 1)[1]
    new_paket = await get_product(new_paket_id)
    if not new_paket:
        await _callback_feedback(query, context, "❌ Produk tidak ditemukan.")
        return

    old_order_id = active["order_id"]
    old_paket = await get_product(active["paket_id"])
    amount_old = active.get("harga_dibayar") or (old_paket["harga"] if old_paket else 0)
    if await update_order_status(old_order_id, "cancelled") is None:
        await _callback_feedback(query, context, "Status pesanan sudah berubah. Buka /start untuk memeriksa.")
        return
    await _stop_payment_task(user_id, old_order_id)
    if amount_old:
        await cancel_transaction(old_order_id, amount_old)
    await hapus_qris_buyer_lama(context.bot, old_order_id, user_id)
    await hapus_notif_lama(context.bot, old_order_id)

    try:
        await query.message.delete()
    except Exception as e:
        logger.debug(f"Gagal menghapus pesan QRIS lama saat ganti paket: {e}")

    await set_cooldown_db(user_id)
    await _buat_order_baru(update, context, None, user_id, user_name,
                           new_paket, order_changes=changes_used + 1)

async def ganti_paket_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await _fast_callback_ack(query)

    active = await get_active_order(user_id)
    if not active:
        await _callback_feedback(query, context, "❌ Pesanan tidak ditemukan.")
        return

    paket = await get_product(active['paket_id'])
    if not paket:
        await _callback_feedback(query, context, "❌ Produk tidak ditemukan.")
        return

    changes_used = active.get('order_changes', 0)
    sisa_ganti = 1 - changes_used

    caption_back = (
        f"<b>{esc(paket['emoji'])} {esc(paket['nama']).upper()}</b>\n"
        f"========================\n\n"
        f"📝 Order ID: <code>{esc(active['order_id'])}</code>\n\n"
        f"⏳ Menunggu pembayaran..."
    )
    if sisa_ganti > 0:
        kb = [
            [InlineKeyboardButton(f"🔄 Ganti Paket (sisa {sisa_ganti}x)", callback_data="ganti_paket_list")],
            [styled_button("❌ Batalkan Pesanan", style="danger", callback_data="cancel_order")],
        ]
    else:
        kb = [[styled_button("❌ Batalkan Pesanan", style="danger", callback_data="cancel_order")]]

    try:
        await query.message.edit_caption(caption_back, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        logger.debug(f"Gagal update tampilan pembatalan ganti paket: {e}")

# =================== ASYNCIO MONITORING PAYMENT TASKS ===================

_payment_tasks: dict = {}
_payment_task_orders: dict = {}
_payment_tasks_lock = None
_current_bot = None

def _get_payment_tasks_lock() -> asyncio.Lock:
    global _payment_tasks_lock
    if _payment_tasks_lock is None:
        _payment_tasks_lock = asyncio.Lock()
    return _payment_tasks_lock

async def _stop_payment_task(user_id: int, order_id: str = None):
    async with _get_payment_tasks_lock():
        if order_id is not None and _payment_task_orders.get(user_id) != order_id:
            return
        task = _payment_tasks.pop(user_id, None)
        _payment_task_orders.pop(user_id, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
    if task and task is not asyncio.current_task() and not task.done():
        await asyncio.gather(task, return_exceptions=True)

def _start_payment_task(bot, order_id: str, paket_id: str, user_id: int,
                        user_name: str, amount: int, timeout_seconds: int = 1800,
                        paket: dict = None):
    spawn_background(
        _start_payment_task_async(
            bot, order_id, paket_id, user_id, user_name, amount, timeout_seconds, paket
        ),
        name=f"payment-bootstrap-{order_id}"
    )

async def _start_payment_task_async(bot, order_id: str, paket_id: str, user_id: int,
                                    user_name: str, amount: int, timeout_seconds: int = 1800,
                                    paket: dict = None):
    old_task = None
    async with _get_payment_tasks_lock():
        existing = _payment_tasks.get(user_id)
        if existing and not existing.done() and _payment_task_orders.get(user_id) == order_id:
            return
        old_task = _payment_tasks.pop(user_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
        # Replace/create/store dilakukan dalam satu lock agar bootstrap ganda tidak membuat task yatim.
        task = asyncio.create_task(
            _payment_poll_loop(
                bot, order_id, paket_id, user_id, user_name, amount, timeout_seconds, paket
            ),
            name=f"payment-poll-{order_id}"
        )
        _payment_tasks[user_id] = task
        _payment_task_orders[user_id] = order_id
    if old_task and old_task is not asyncio.current_task() and not old_task.done():
        await asyncio.gather(old_task, return_exceptions=True)


async def _ensure_payment_task_for_order(bot, order: dict, paket: dict, user_name: str):
    """Pastikan monitoring aktif tanpa membuat request status duplikat saat /start ditekan."""
    user_id = order.get('user_id')
    if not user_id:
        return
    async with _get_payment_tasks_lock():
        existing = _payment_tasks.get(user_id)
        if existing and not existing.done() and _payment_task_orders.get(user_id) == order['order_id']:
            return

    expiry_dt = parse_provider_datetime(order.get('payment_expires_at'))
    if expiry_dt is not None:
        timeout_seconds = max(1, int((expiry_dt - now_wib()).total_seconds()))
    else:
        timeout_seconds = AUTOGOPAY_DEFAULT_TIMEOUT_SECONDS
    amount = order.get('harga_dibayar') or paket.get('harga', 0)
    _start_payment_task(
        bot,
        order_id=order['order_id'],
        paket_id=order['paket_id'],
        user_id=user_id,
        user_name=user_name,
        amount=amount,
        timeout_seconds=timeout_seconds,
        paket=paket,
    )


def _check_order_status_sync(order_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT status FROM orders WHERE order_id=%s", (order_id,))
            row = c.fetchone()
            return dict(row) if row else None

async def _check_order_status(order_id):
    row, used_async = await _asyncpg_fetchrow("SELECT status FROM orders WHERE order_id=$1", str(order_id))
    if used_async:
        return row
    return await run_db(_check_order_status_sync, order_id)

async def _payment_poll_loop(bot, order_id: str, paket_id: str, user_id: int,
                             user_name: str, amount: int, timeout_seconds: int,
                             paket: dict = None):
    started_at = _time.monotonic()
    elapsed = 0
    try:
        order = await get_order_by_id(order_id)
        transaction_id = (order or {}).get("provider_transaction_id")
        payment_provider = str((order or {}).get("payment_provider") or "gopay").lower()
        last_provider_status = str((order or {}).get("provider_status") or "").lower()
        if not transaction_id:
            logger.error(f"[PAYMENT] transaction_id AutoGoPay tidak ditemukan untuk {order_id}")
            return

        last_db_check_elapsed = -30
        while True:
            elapsed = _time.monotonic() - started_at
            if elapsed >= timeout_seconds:
                break
            # Webhook-first: polling cepat hanya 30 detik pertama, lalu makin jarang.
            if elapsed < 30:
                interval = AUTOGOPAY_POLL_FAST_SECONDS
            elif elapsed < 120:
                interval = AUTOGOPAY_POLL_MEDIUM_SECONDS
            elif elapsed < 300:
                interval = AUTOGOPAY_POLL_SLOW_SECONDS
            else:
                interval = AUTOGOPAY_POLL_VERY_SLOW_SECONDS

            sleep_for = min(interval, timeout_seconds - elapsed)
            await asyncio.sleep(sleep_for)
            elapsed = _time.monotonic() - started_at
            if elapsed >= timeout_seconds:
                break

            # Cek status lokal periodik agar task berhenti jika order diubah dari jalur lain.
            if elapsed - last_db_check_elapsed >= 30:
                row = await _check_order_status(order_id)
                last_db_check_elapsed = elapsed
                if not row or row['status'] != 'waiting':
                    return

            if payment_provider == "shopeepay":
                trans = await _autogopay_get_shopeepay_status(transaction_id, amount)
            else:
                trans = await _autogopay_get_status(transaction_id, amount)
            if not trans:
                continue
            current_provider_status = str(trans.get("provider_status") or "").lower()
            if current_provider_status and current_provider_status != last_provider_status:
                await update_order_provider_status(order_id, current_provider_status)
                last_provider_status = current_provider_status

            if trans.get('status') == 'completed':
                logger.info(f"[PAYMENT POLL] Settlement terdeteksi: {order_id}")
                spawn_background(_handle_payment_success(
                    bot, order_id, paket_id, user_id, user_name, amount, trans, paket=paket
                ), name=f"poll-settlement-{order_id}")
                return

        row = await _check_order_status(order_id)
        if not row or row['status'] != 'waiting':
            return

        if payment_provider == "shopeepay":
            trans = await _autogopay_get_shopeepay_status(transaction_id, amount)
        else:
            trans = await _autogopay_get_status(transaction_id, amount)
        if trans:
            current_provider_status = str(trans.get("provider_status") or "").lower()
            if current_provider_status and current_provider_status != last_provider_status:
                await update_order_provider_status(order_id, current_provider_status)
        if trans and trans.get('status') == 'completed':
            spawn_background(_handle_payment_success(
                bot, order_id, paket_id, user_id, user_name, amount, trans, paket=paket
            ), name=f"poll-settlement-{order_id}")
            return

        if await update_order_status(order_id, 'expired') is None:
            return
        if amount:
            await cancel_transaction(order_id, amount)
        spawn_background(
            hapus_qris_buyer_lama(bot, order_id, user_id),
            name=f"cleanup-expired-qr-{order_id}"
        )
        await set_cooldown_db(user_id)
        cooldown_sisa = await get_cooldown_sisa_db(user_id)

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "<b>⏰ SESI BERAKHIR</b>\n"
                    "========================\n\n"
                    "Pesanan telah dibatalkan otomatis.\n\n"
                    f"Alasan: Pembayaran tidak diterima dalam waktu yang ditentukan.\n\n"
                    f"⏳ Kamu bisa membuat pesanan baru dalam <b>{cooldown_sisa} menit</b>.\n\n"
                    "Ketik /start untuk membuat pesanan baru."
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.debug(f"Gagal kirim notif expired ke buyer: {e}")

        async def _expired_admin_finalize():
            await hapus_notif_lama(bot, order_id)
            paket_exp = paket or await get_product(paket_id) or {"emoji": "📦", "nama": paket_id}
            msg_id = await kirim_notif(
                bot,
                _format_order_notif(
                    "⏰ <b>ORDER EXPIRED</b>",
                    user_name, user_id, paket_exp, order_id,
                    extra="ℹ️ Buyer tidak bayar sampai waktu habis"
                )
            )
            if msg_id:
                await set_admin_msg_id(order_id, msg_id)

        spawn_background(_expired_admin_finalize(), name=f"expired-admin-{order_id}")

    except asyncio.CancelledError:
        pass
    finally:
        current_task = asyncio.current_task()
        async with _get_payment_tasks_lock():
            if _payment_tasks.get(user_id) is current_task:
                _payment_tasks.pop(user_id, None)
                _payment_task_orders.pop(user_id, None)

def _split_required_ids(requires_str: str) -> list:
    return [pid.strip() for pid in (requires_str or '').split(',') if pid.strip()]


async def _build_prereq_progress(user_id: int, requires_str: str, parent_order_id: str = None) -> dict:
    all_req_ids = _split_required_ids(requires_str)
    missing = await check_prerequisites_sync(user_id, requires_str)
    missing_set = set(missing)

    lines = []
    buttons = []
    products_by_id = {p['paket_id']: p for p in await get_all_products()}
    for pid in all_req_ids:
        p_obj = products_by_id.get(pid)
        label = f"{p_obj['emoji']} {p_obj['nama']}" if p_obj else pid
        if pid in missing_set:
            lines.append(f"❌ {esc(label)}")
            callback_data = (
                f"prereq_buy_{pid}|{parent_order_id}"
                if parent_order_id
                else f"prereq_buy_{pid}"
            )
            buttons.append([InlineKeyboardButton(
                f"🛒 Beli {p_obj['nama'] if p_obj else pid}",
                callback_data=callback_data
            )])
        else:
            lines.append(f"✅ {esc(label)}")

    return {
        'all_ids': all_req_ids,
        'missing': missing,
        'missing_set': missing_set,
        'fulfilled_count': len(all_req_ids) - len(missing_set),
        'total_count': len(all_req_ids),
        'lines': lines,
        'text': "\n".join(lines),
        'buttons': buttons,
    }


def _admin_prereq_action_keyboard(order_id: str):
    """Keyboard khusus admin/channel untuk order bersyarat.

    Penting: jangan memakai progress['buttons'] di notif admin/channel karena
    tombol itu adalah tombol beli syarat untuk buyer. Di admin/channel cukup
    tampilkan tombol aksi admin saja agar tidak muncul tombol "Beli GB ...".
    """
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📤 Kirim Link Manual",
        callback_data=f"admin_kirim_link_prereq|{order_id}"
    )]])

async def _replace_admin_order_status(bot, order_id: str, text: str, reply_markup=None):
    """Replace status order di admin/channel: hapus pesan lama lalu kirim status terbaru."""
    await hapus_notif_lama(bot, order_id)
    msg_id = await kirim_notif(bot, text, reply_markup=reply_markup)
    if msg_id:
        await set_admin_msg_id(order_id, msg_id)
    return msg_id

async def _send_delivery_failed_alert(bot, order_id: str, user_id: int, user_name: str, paket: dict, error: Exception):
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🚨 <b>ALERT: GAGAL KIRIM LINK</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 Buyer : {tg_user_link(user_id, user_name)}\n"
                f"ID      : <code>{esc(user_id)}</code>\n"
                f"📦 Paket : {esc(paket.get('emoji','📦'))} {esc(paket.get('nama','Produk'))}\n"
                f"🔖 Order : <code>{esc(order_id)}</code>\n\n"
                "Order sudah lunas, tapi bot gagal mengirim link. Kirim manual secepatnya."
            ),
            parse_mode="HTML"
        )
    except Exception as ex:
        logger.error(f"Gagal kirim emergency alert ke admin: {ex}")

async def _send_buyer_product_link(bot, user_id: int, order_id: str, paket: dict, paid_amount: int, title: str) -> tuple:
    group_link = await generate_group_link(bot, paket, order_id)
    link = group_link or (paket.get("link") or DEFAULT_LINK)
    link_section = _build_link_section(group_link, link)

    msg = await telegram_send_rate_retry(
        lambda: bot.send_message(
            chat_id=user_id,
            text=(
                f"{title}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>Produk</b>\n"
            f"Paket : {esc(paket.get('emoji','📦'))} {esc(paket.get('nama','Produk'))}\n"
            f"Total : {format_harga(paid_amount)}\n\n"
            f"🔖 <b>Order ID</b>\n"
            f"<code>{esc(order_id)}</code>\n\n"
            f"{link_section}\n\n"
            f"Terima kasih telah berbelanja di Hyper Family Store! 🙏"
        ),
        parse_mode="HTML",
            reply_markup=None
        ),
        label=f"kirim link buyer {order_id}",
    )
    if msg and getattr(msg, 'message_id', None):
        try:
            await set_buyer_msg_id(order_id, msg.message_id)
        except Exception as exc:
            # ID hanya dipakai untuk cleanup/dedup; jangan menganggap delivery gagal
            # jika link sudah berhasil diterima buyer.
            logger.warning(f"[DELIVERY] Gagal menyimpan buyer_msg_id {order_id}: {exc}")
    return link, bool(group_link)

async def _send_prereq_hold_message(bot, user_id: int, order_id: str, paket: dict, paid_amount: int, progress: dict):
    await mark_delivery_held(order_id, 'Syarat belum terpenuhi')
    product_name = f"{paket.get('emoji','📦')} {paket.get('nama','Produk')}"
    prerequisite_text = "\n".join(progress['lines'])
    msg = await bot.send_message(
        chat_id=user_id,
        text=(
            f"✅ <b>PEMBAYARAN BERHASIL</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>{esc(product_name)}</b>\n"
            f"Pembayaran diterima. Lengkapi syarat berikut untuk membuka akses.\n\n"
            f"🔒 <b>PAKET SYARAT</b>\n"
            f"{prerequisite_text}\n\n"
            f"📊 Progress: <b>{progress['fulfilled_count']}/{progress['total_count']}</b> paket selesai\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Pilih tombol ❌ untuk membeli paket yang belum lengkap.\n"
            f"✅ Akses dikirim otomatis setelah seluruh syarat terpenuhi.\n"
            f"<i>Paket ini sudah lunas, tidak perlu dibayar ulang.</i>"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(progress['buttons']) if progress['buttons'] else None
    )
    # Simpan ID pesan hold agar update berikutnya mengedit pesan yang sama,
    # bukan mengirim pesan baru yang menumpuk di chat buyer.
    try:
        await set_buyer_msg_id(order_id, msg.message_id)
    except Exception as exc:
        logger.warning(f"[PREREQ] Gagal menyimpan buyer_msg_id {order_id}: {exc}")


def _format_buyer_prereq_update(parent_label: str, parent_name: str, progress: dict) -> str:
    missing_count = len(progress['missing'])
    return (
        "📋 <b>UPDATE AKSES</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>{esc(parent_label)}</b>\n"
        "✅ Pembayaran diterima\n"
        f"📊 <b>{progress['fulfilled_count']}/{progress['total_count']} paket selesai</b>\n\n"
        f"{progress['text']}\n"
        f"⏳ Tersisa <b>{missing_count} paket</b> syarat.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Pilih tombol paket di bawah untuk melengkapi syarat.\n"
        f"Akses {esc(parent_name)} dikirim otomatis setelah lengkap."
    )


def _format_buyer_prereq_complete(parent_label: str, parent_name: str, progress: dict) -> str:
    return (
        "✅ <b>SEMUA SYARAT SUDAH LENGKAP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Target: <b>{esc(parent_label)}</b>\n"
        f"📊 Progress: <b>{progress['fulfilled_count']}/{progress['total_count']}</b>\n\n"
        f"Link {esc(parent_name)} sedang dikirim otomatis."
    )


async def _edit_or_send_buyer_prereq_update(bot, user_id: int, parent_order: dict,
                                            text: str, reply_markup=None) -> bool:
    """Edit pesan hold/update lama; fallback kirim satu pesan jika pesan sudah hilang."""
    old_message_id = parent_order.get('buyer_msg_id')
    if old_message_id:
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=int(old_message_id),
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return True
        except Exception as exc:
            logger.debug(f"[PREREQ PROGRESS] Gagal edit pesan buyer {old_message_id}: {exc}")

    try:
        msg = await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        try:
            await set_buyer_msg_id(parent_order['order_id'], msg.message_id)
        except Exception as exc:
            logger.warning(
                f"[PREREQ PROGRESS] Gagal menyimpan buyer_msg_id {parent_order['order_id']}: {exc}"
            )
        return True
    except Exception as exc:
        logger.error(f"[PREREQ PROGRESS] Gagal kirim update buyer {user_id}: {exc}")
        return False

async def _notify_parent_prereq_progress(bot, user_id: int, user_name: str, parent_order: dict,
                                          parent_paket: dict, progress: dict, purchased_paket_id: str):
    if purchased_paket_id not in progress['all_ids']:
        return

    parent_name = parent_paket.get('nama', 'Produk')
    parent_label = f"{parent_paket.get('emoji','📦')} {parent_name}"

    if progress['missing']:
        await _edit_or_send_buyer_prereq_update(
            bot,
            user_id,
            parent_order,
            _format_buyer_prereq_update(parent_label, parent_name, progress),
            InlineKeyboardMarkup(progress['buttons']) if progress['buttons'] else None,
        )

        extra = (
            f"🎯 Target  : {esc(parent_label)}\n"
            f"📊 Progress: {progress['fulfilled_count']}/{progress['total_count']} terpenuhi\n\n"
            f"{progress['text']}\n\n"
            f"🔐 Link {esc(parent_name)} masih ditahan"
        )
        # Admin/channel memakai sistem replace message agar status parent (GB VIP/produk bersyarat)
        # tidak dobel. Tombol beli syarat sengaja tidak dikirim ke admin/channel;
        # tombol itu hanya untuk private chat buyer.
        msg_id = await _replace_admin_order_status(
            bot,
            parent_order['order_id'],
            _format_order_notif(
                "📋 <b>UPDATE SYARAT</b>",
                user_name, user_id, parent_paket, parent_order['order_id'],
                amount=parent_order.get('harga_dibayar', 0),
                extra=extra
            ),
            reply_markup=_admin_prereq_action_keyboard(parent_order['order_id'])
        )
        return

    await _edit_or_send_buyer_prereq_update(
        bot,
        user_id,
        parent_order,
        _format_buyer_prereq_complete(parent_label, parent_name, progress),
    )

async def _retain_delivery_result(order_id, link):
    # Retrying this UPDATE is idempotent; sending the Telegram message again is not.
    for attempt in range(3):
        try:
            await set_sent_link(order_id, link)
            return
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if attempt == 2:
                raise
            await asyncio.sleep(0.15 * (2 ** attempt))


async def _record_delivery_failure(order_id, error, *, sent=False):
    if not sent and isinstance(error, (telegram.error.BadRequest, telegram.error.Forbidden)):
        await reset_delivery_claim(order_id, type(error).__name__)
    else:
        # Timeout or post-send DB error: leave the claim locked for manual reconciliation.
        await set_delivery_status(order_id, 'pending',
                                  'Hasil pengiriman belum pasti; periksa manual: ' + type(error).__name__)


async def _process_completed_order_delivery(bot, order_id: str, paket_id: str, user_id: int,
                                            user_name: str, paid_amount: int,
                                            source_title: str = "✅ <b>PEMBAYARAN BERHASIL</b>",
                                            paket: dict = None):
    # Publish on verified payment, including held prerequisite orders. The SQL
    # claim still checks completed and keeps publication at most once per order.
    spawn_background(_publish_public_testimonial(bot, order_id), name=f'public-testi-{order_id}')
    # Cleanup QR tidak boleh menahan pengiriman akses buyer.
    spawn_background(
        hapus_qris_buyer_lama(bot, order_id, user_id),
        name=f"cleanup-paid-qr-{order_id}"
    )
    paket = copy.deepcopy(paket) if paket else await get_product(paket_id)
    paket = paket or {
        "emoji": "📦", "nama": "Produk", "harga": paid_amount, "link": DEFAULT_LINK,
        "requires_paket_ids": ""
    }
    paid_amount = paid_amount or paket.get('harga', 0)

    requires_str = paket.get("requires_paket_ids") or ""
    if requires_str:
        progress = await _build_prereq_progress(user_id, requires_str, parent_order_id=order_id)
        if progress['missing']:
            try:
                await _send_prereq_hold_message(bot, user_id, order_id, paket, paid_amount, progress)
            except Exception as e:
                logger.error(f"[PAYMENT] Gagal kirim notif prereq ke buyer {user_id}: {e}")
                await set_delivery_status(order_id, 'failed', str(e)[:500])

            extra_prereq = (
                f"🔐 Link {esc(paket.get('nama','Produk'))} ditahan\n"
                f"📋 Syarat  : {progress['fulfilled_count']}/{progress['total_count']} terpenuhi\n\n"
                f"{progress['text']}"
            )
            spawn_background(
                _replace_admin_order_status(
                    bot,
                    order_id,
                    _format_order_notif(
                        source_title, user_name, user_id, paket, order_id,
                        amount=paid_amount, extra=extra_prereq
                    ),
                    reply_markup=_admin_prereq_action_keyboard(order_id)
                ),
                name=f"paid-prereq-admin-{order_id}"
            )
            return

    claimed = await atomic_claim_for_delivery(order_id)
    if not claimed:
        logger.debug(f"[DELIVERY] Order {order_id} sudah diklaim/terkirim, skip delivery duplikat.")
        return

    link = None
    message_sent = False
    try:
        # Ini critical path: akses buyer dikirim sebelum cleanup/notif admin.
        link, _ = await _send_buyer_product_link(bot, user_id, order_id, paket, paid_amount, source_title)
        message_sent = True
        await _retain_delivery_result(order_id, link)
        kirim_berhasil = True
    except Exception as e:
        kirim_berhasil = False
        logger.error(f"[PAYMENT] Gagal kirim link ke buyer {user_id}: {e}")
        await _record_delivery_failure(order_id, e, sent=message_sent)
        spawn_background(
            _send_delivery_failed_alert(bot, order_id, user_id, user_name, paket, e),
            name=f"delivery-failed-alert-{order_id}"
        )

    async def _finalize_after_buyer():
        await hapus_notif_lama(bot, order_id)
        extra_paid = "🔗 Link produk berhasil dikirim ✅" if kirim_berhasil else "⚠️ Link gagal dikirim — cek manual"
        msg_id = await kirim_notif(
            bot,
            _format_order_notif(
                source_title, user_name, user_id, paket, order_id,
                amount=paid_amount, extra=extra_paid
            )
        )
        if msg_id:
            await set_admin_msg_id(order_id, msg_id)
        if kirim_berhasil:
            await _auto_deliver_pending_prereq_orders(
                bot, user_id, user_name,
                purchased_paket_id=paket_id,
                exclude_order_id=order_id
            )

    # Semua pekerjaan admin/prerequisite lanjutan tidak menahan critical path buyer.
    spawn_background(_finalize_after_buyer(), name=f"paid-finalize-{order_id}")

async def _handle_payment_success(bot, order_id: str, paket_id: str, user_id: int,
                                  user_name: str, amount: int, trans: dict, paket: dict = None):
    paid_amount = _safe_int(trans.get('amount'), -1)
    if _safe_int(amount) <= 0 or paid_amount != _safe_int(amount):
        logger.error("[PAYMENT] Nominal settlement ditolak untuk %s", order_id)
        return
    success = await mark_order_completed(order_id)
    if not success:
        logger.debug(f"[PAYMENT] Order ID {order_id} sudah diproses sebelumnya, skip duplikasi.")
        return

    await _process_completed_order_delivery(
        bot, order_id, paket_id, user_id, user_name, paid_amount,
        source_title="✅ <b>PEMBAYARAN BERHASIL</b>",
        paket=paket,
    )

# =================== AUTO-DELIVER PENDING PREREQ ORDERS ===================

async def _auto_deliver_pending_prereq_orders(bot, user_id: int, user_name: str,
                                              purchased_paket_id: str = None,
                                              exclude_order_id: str = None):
    pending = await get_completed_no_link_orders(user_id)
    if not pending:
        return

    for order in pending:
        pid = order['paket_id']
        oid = order['order_id']
        old_notice_id = order.get('buyer_msg_id')
        if exclude_order_id and oid == exclude_order_id:
            continue

        paket = await get_product(pid)
        if not paket:
            continue

        req_str = paket.get("requires_paket_ids") or ""
        if not req_str:
            continue

        progress = await _build_prereq_progress(user_id, req_str, parent_order_id=oid)
        if progress['missing']:
            if purchased_paket_id:
                await _notify_parent_prereq_progress(bot, user_id, user_name, order, paket, progress, purchased_paket_id)
            continue

        if purchased_paket_id:
            await _notify_parent_prereq_progress(bot, user_id, user_name, order, paket, progress, purchased_paket_id)

        claimed = await atomic_claim_for_delivery(oid)
        if not claimed:
            continue

        link = None
        try:
            link, _ = await _send_buyer_product_link(
                bot, user_id, oid, paket,
                order.get('harga_dibayar', 0) or paket.get('harga', 0),
                "🔓 <b>AKSES PRODUK TERBUKA</b>"
            )
        except Exception as e:
            logger.error(f"[AUTO-PREREQ] Gagal kirim link ke buyer {user_id}: {e}")
            await _record_delivery_failure(oid, e)
            spawn_background(
                _send_delivery_failed_alert(bot, oid, user_id, user_name, paket, e),
                name=f"auto-prereq-failed-{oid}",
            )
            continue

        await _retain_delivery_result(oid, link)
        spawn_background(_publish_public_testimonial(bot, oid), name=f'public-testi-{oid}')
        if old_notice_id:
            await _safe_delete_chat_message(
                bot, user_id, old_notice_id, "pesan update syarat lama"
            )

        async def _finalize_auto_prereq(order_id=oid, order_data=order, paket_data=paket):
            await hapus_notif_lama(bot, order_id)
            msg_id = await kirim_notif(
                bot,
                _format_order_notif(
                    "✅ <b>LINK AUTO-TERKIRIM</b>",
                    user_name, user_id, paket_data, order_id,
                    amount=order_data.get('harga_dibayar', 0),
                    extra="🔓 Link otomatis dikirim karena semua syarat terpenuhi ✅"
                )
            )
            if msg_id:
                await set_admin_msg_id(order_id, msg_id)

        spawn_background(_finalize_auto_prereq(), name=f"auto-prereq-finalize-{oid}")

# =================== ADMIN: KIRIM LINK PREREQ MANUAL ===================

@admin_callback
async def admin_kirim_link_prereq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)

    if not await is_admin(query.from_user.id, context):
        await _callback_feedback(query, context, "⛔ Akses ditolak.")
        return

    order_id = query.data.split("|")[1]
    order = await get_order_by_id(order_id)
    if not order:
        await query.edit_message_text("❌ Order tidak ditemukan.")
        return

    user_id = order['user_id']
    paket_id = order['paket_id']
    paket = await get_product(paket_id) or {"emoji": "📦", "nama": "Produk", "harga": 0, "link": DEFAULT_LINK}
    old_notice_id = order.get('buyer_msg_id')

    if not await atomic_claim_for_delivery(order_id):
        await _callback_feedback(query, context, "Pesanan belum lunas, sudah dikirim, atau sedang diproses.")
        return
    group_link = await generate_group_link(context.bot, paket, order_id)
    link = group_link or (paket.get("link") or DEFAULT_LINK)
    link_section = _build_link_section(group_link, link)

    kirim_berhasil = False
    try:
        msg = await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"<b>✅ LINK PRODUK KAMU SUDAH SIAP!</b>\n"
                f"========================\n\n"
                f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
                f"🔖 Order ID: <code>{esc(order_id)}</code>\n\n"
                f"========================\n"
                f"{link_section}\n\n"
                f"Terima kasih telah memenuhi syarat! Nikmati paketmu. 🙏"
            ),
            parse_mode="HTML",
            reply_markup=None
        )
        kirim_berhasil = True
    except Exception as e:
        delivery_error = e
        logger.error(f"[PREREQ] Gagal kirim link manual ke buyer {user_id}: {type(e).__name__}")

    if kirim_berhasil:
        await _retain_delivery_result(order_id, link)
        spawn_background(_publish_public_testimonial(context.bot, order_id), name=f'public-testi-{order_id}')
        if msg and getattr(msg, 'message_id', None):
            try:
                await set_buyer_msg_id(order_id, msg.message_id)
            except Exception as exc:
                logger.warning(f"[PREREQ] Gagal menyimpan buyer_msg_id {order_id}: {exc}")
        if old_notice_id:
            await _safe_delete_chat_message(
                context.bot, user_id, old_notice_id, "pesan hold syarat lama"
            )
    else:
        await _record_delivery_failure(order_id, delivery_error)

    if kirim_berhasil:
        await query.edit_message_text(
            query.message.text + f"\n\n✅ <b>Link sudah dikirim ke buyer oleh {esc(query.from_user.full_name)}.</b>",
            parse_mode="HTML"
        )
    else:
        await query.answer("❌ Gagal kirim link ke buyer. Cek bot tidak diblokir buyer.", show_alert=True)

# =================== AUTO-APPROVE & REVOKE JOIN REQUEST ===================

def _check_join_request_sync(user_id, chat_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT o.id FROM orders o
                JOIN products p ON o.paket_id = p.paket_id
                WHERE o.user_id = %s AND o.status = 'completed'
                AND p.group_chat_id = %s
                ORDER BY o.id DESC LIMIT 1
            """, (user_id, chat_id))
            return c.fetchone()

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    join_req = update.chat_join_request
    if not join_req:
        return

    user_id = join_req.from_user.id
    chat_id = str(join_req.chat.id)

    row = await run_db(_check_join_request_sync, user_id, chat_id)

    if row:
        try:
            await join_req.approve()
            logger.info(f"[JOIN] ✅ Approved {user_id} ke chat {chat_id}")
            if join_req.invite_link and join_req.invite_link.invite_link:
                try:
                    await context.bot.revoke_chat_invite_link(
                        chat_id=join_req.chat.id,
                        invite_link=join_req.invite_link.invite_link
                    )
                except Exception as rev_err:
                    logger.debug(f"[JOIN] Gagal revoke link: {rev_err}")
        except Exception as e:
            logger.error(f"[JOIN] Gagal approve {user_id} ke {chat_id}: {e}")
    else:
        try:
            await join_req.decline()
            logger.info(f"[JOIN] ❌ Declined {user_id} ke chat {chat_id}")
        except Exception as e:
            logger.error(f"[JOIN] Gagal decline {user_id} ke {chat_id}: {e}")

# =================== USER: TESTIMONI HANDLERS ===================

async def _legacy_review_notice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('awaiting_review_text', None)
    context.user_data.pop('temp_rating', None)
    await _fast_callback_ack(update.callback_query)
    await _callback_feedback(update.callback_query, context,
        'Testimoni transaksi kini dipublikasikan otomatis setelah akses berhasil dikirim. '
        'Kamu tidak perlu mengisi rating atau ulasan.')

async def handle_rate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _legacy_review_notice(update, context)

async def handle_rate_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _legacy_review_notice(update, context)

async def handle_rate_text_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _legacy_review_notice(update, context)

async def handle_rate_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('temp_rating', None)
    context.user_data.pop('awaiting_review_text', None)
    await query.edit_message_text("🙏 Terima kasih! Anda selalu dapat memberikan ulasan nanti di riwayat order.")

async def handle_rate_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    order_id = query.data.split("|")[1]

    context.user_data.pop('awaiting_review_text', None)
    context.user_data.pop('temp_rating', None)

    order = await get_completed_order_for_user(order_id, query.from_user.id)
    if not order:
        await query.edit_message_text("⚠️ Data pesanan tidak ditemukan atau bukan milik kamu.")
        return

    paket = await get_product(order['paket_id']) or {"emoji": "📦", "nama": "Produk", "harga": 0}
    sent_link = order.get('sent_link') or DEFAULT_LINK

    is_group_link = sent_link and "t.me/+" in sent_link
    if is_group_link:
        link_section = (
            f"🔗 <b>Link Bergabung (Khusus Kamu)</b>\n"
            f"{sent_link}\n\n"
            f"📋 <b>Cara gabung:</b>\n"
            f"1. Klik link di atas\n"
            f"2. Pencet <b>\"Minta Bergabung\"</b>\n"
            f"3. Bot langsung approve otomatis ✅\n\n"
            f"⚠️ <i>Link ini sekali pakai, jangan dishare!</i>"
        )
    else:
        link_section = (
            f"🔗 <b>Link Produk</b>\n"
            f"{sent_link}\n\n"
            f"💾 <i>Simpan link ini. Produk dapat diakses kapan saja.</i>"
        )

    await query.edit_message_text(
        f"<b>✅ PEMBAYARAN BERHASIL</b>\n"
        f"========================\n\n"
        f"📦 <b>Detail Pesanan</b>\n"
        f"- Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
        f"- Order ID: <code>{esc(order_id)}</code>\n\n"
        f"========================\n"
        f"{link_section}\n\n"
        f"Terima kasih telah berbelanja! 🙏",
        parse_mode="HTML",
        reply_markup=None
    )

async def handle_rate_back_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _legacy_review_notice(update, context)

# =================== ADMIN: TESTIMONI MODERASI HANDLERS ===================

@admin_callback
async def admin_testi_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)

    if not await is_admin(query.from_user.id, context):
        await _callback_feedback(query, context, "⛔ Akses ditolak.")
        return


    order_id = query.data.split("|")[1]
    result = await _publish_public_testimonial(context.bot, order_id, manual=True)
    messages = {
        'sent': '✅ Bukti transaksi berhasil dipublikasikan tanpa nama atau username buyer.',
        'already': 'ℹ️ Transaksi sudah dipublikasikan atau sedang dicatat. Tidak dikirim ulang.',
        'disabled': '⚠️ Set channel testimoni publik yang berbeda dari notifikasi admin di Pengaturan.',
        'not_ready': '⚠️ Bukti publik hanya untuk order lunas yang aksesnya sudah berhasil dikirim.',
        'uncertain': '⚠️ Hasil kirim belum pasti. Periksa channel secara manual; jangan kirim ulang.',
        'failed': '⚠️ Publikasi gagal. Periksa izin channel dan log; transaksi buyer tidak diubah.',
    }
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")]])
    await query.edit_message_text(messages[result], reply_markup=back_kb)

@admin_callback
async def admin_testi_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)

    if not await is_admin(query.from_user.id, context):
        await _callback_feedback(query, context, "⛔ Akses ditolak.")
        return


    order_id = query.data.split("|")[1]
    testi = await get_testimonial_by_order(order_id)
    if not testi or testi.get('status') != 'pending':
        await _callback_feedback(query, context, "Publikasi sudah diproses; statusnya tidak diubah.")
        return
    await update_testimonial_status(order_id, 'rejected')
    await query.edit_message_text(
        "❌ <b>Testimoni Ditolak</b>\n\nUlasan dihapus dari antrean moderasi.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")]])
    )

# =================== ADMIN: KELOLA PRODUK ===================

async def cmd_produk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    await _send_produk_menu(context, chat_id=update.message.from_user.id, message=update.message)

async def _send_produk_menu(context, chat_id, message=None, query=None):
    products = await get_all_products()

    text = "<b>📦 MANAJEMEN PRODUK</b>\n========================\n\n"
    if products:
        for p in products:
            aktif_str = "✅" if p.get('aktif', True) else "🔴 Nonaktif"
            text += f"{esc(p['emoji'])} <b>{esc(p['nama'])}</b> - {format_harga(p['harga'])} {aktif_str}\n"
    else:
        text += "<i>Belum ada produk.</i>\n"

    text += "\nPilih produk untuk diedit, atau tambah produk baru:"

    keyboard = [
        [InlineKeyboardButton(f"{p['emoji']} {p['nama']}", callback_data=f"pd_detail_{p['paket_id']}")]
        for p in products
    ]
    keyboard.append([InlineKeyboardButton("➕ Tambah Produk Baru", callback_data="pd_tambah")])
    keyboard.append([InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")])

    markup = InlineKeyboardMarkup(keyboard)

    if query:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
    elif message:
        await message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)

@admin_callback
async def produk_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, paket_id: str = None):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    paket_id = paket_id or query.data.replace("pd_detail_", "")
    p = await get_product(paket_id)
    if not p:
        await query.edit_message_text("⚠️ Produk tidak ditemukan.")
        return

    grp_info = (
        f"🏢 Group ID: <code>{esc(p.get('group_chat_id', ''))}</code>\n"
        if p.get('group_chat_id') else
        "🏢 Group ID: <i>Tidak di-set (pakai link biasa)</i>\n"
    )
    aktif_str = "✅ Aktif" if p.get('aktif', True) else "🔴 Nonaktif"
    req_str = p.get('requires_paket_ids') or ""
    req_info = (
        f"🔒 Syarat: <code>{esc(req_str)}</code>\n"
        if req_str else
        "🔒 Syarat: <i>Tidak ada (bebas beli)</i>\n"
    )

    text = (
        f"<b>{esc(p['emoji'])} {esc(p['nama'])}</b>\n"
        f"========================\n\n"
        f"💰 Harga: {format_harga(p['harga'])}\n"
        f"📝 Deskripsi: {esc(p['deskripsi'])}\n"
        f"🔗 Link: <code>{esc(p['link'])}</code>\n"
        f"{grp_info}"
        f"{req_info}"
        f"📊 Status: {aktif_str}\n\n"
        f"Pilih field yang mau diubah:"
    )

    keyboard = [
        [
            InlineKeyboardButton("✍️ Nama",       callback_data=f"pd_edit_{paket_id}_nama"),
            InlineKeyboardButton("😀 Emoji",      callback_data=f"pd_edit_{paket_id}_emoji"),
        ],
        [
            InlineKeyboardButton("💰 Harga",      callback_data=f"pd_edit_{paket_id}_harga"),
            InlineKeyboardButton("📝 Deskripsi",  callback_data=f"pd_edit_{paket_id}_deskripsi"),
        ],
        [
            InlineKeyboardButton("🔗 Link",       callback_data=f"pd_edit_{paket_id}_link"),
            InlineKeyboardButton("🏢 Group ID",   callback_data=f"pd_edit_{paket_id}_group_chat_id"),
        ],
        [InlineKeyboardButton("🔒 Syarat (Prerequisite)", callback_data=f"pd_edit_{paket_id}_requires_paket_ids")],
        [InlineKeyboardButton(
            "🔴 Nonaktifkan" if p.get('aktif', True) else "✅ Aktifkan",
            callback_data=f"pd_toggle_{paket_id}"
        )],
        [InlineKeyboardButton("🗑️ Hapus Produk", callback_data=f"pd_hapus_{paket_id}")],
        [styled_button("⬅️ Kembali", style="primary",       callback_data="pd_back")],
    ]

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

@admin_callback
async def produk_toggle_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    paket_id = query.data.replace("pd_toggle_", "")
    p = await get_product(paket_id)
    if not p:
        await query.answer("Produk tidak ditemukan.", show_alert=True)
        return

    new_aktif = not p.get('aktif', True)
    await update_product_field(paket_id, 'aktif', new_aktif)

    status_str = "diaktifkan" if new_aktif else "dinonaktifkan"
    await query.answer(f"✅ Produk berhasil {status_str}.", show_alert=True)
    p['aktif'] = new_aktif
    await produk_detail(update, context)

async def _show_prereq_selector(query, context, paket_id: str, p: dict):
    all_products = await get_all_products()
    current_str = p.get('requires_paket_ids') or ""
    current_ids = [x.strip() for x in current_str.split(",") if x.strip()]

    if 'editing_prereq' not in context.user_data or context.user_data['editing_prereq'].get('paket_id') != paket_id:
        context.user_data['editing_prereq'] = {'paket_id': paket_id, 'selected': list(current_ids)}

    selected = context.user_data['editing_prereq']['selected']

    buttons = []
    for prod in all_products:
        if prod['paket_id'] == paket_id:
            continue
        is_selected = prod['paket_id'] in selected
        mark = "✅ " if is_selected else "⬜ "
        label = f"{mark}{prod.get('emoji', '')} {prod['nama']}"
        buttons.append([InlineKeyboardButton(
            label,
            callback_data=f"pd_req_toggle|{paket_id}|{prod['paket_id']}"
        )])

    if not buttons:
        buttons.append([InlineKeyboardButton("❌ Belum ada produk lain", callback_data=f"pd_detail_{paket_id}")])

    selected_label = ", ".join(selected) if selected else "Tidak ada"
    buttons.append([
        InlineKeyboardButton("💾 Simpan", callback_data=f"pd_req_save|{paket_id}"),
        InlineKeyboardButton("🗑️ Kosongkan", callback_data=f"pd_req_clear|{paket_id}"),
    ])
    buttons.append([InlineKeyboardButton("⬅️ Batal", callback_data=f"pd_detail_{paket_id}")])

    await query.edit_message_text(
        f"<b>🔒 Atur Syarat (Prerequisite)</b>\n"
        f"========================\n\n"
        f"Produk: <b>{esc(p.get('emoji', ''))} {esc(p['nama'])}</b>\n\n"
        f"Centang produk yang harus sudah dibeli user sebelum bisa membeli produk ini.\n\n"
        f"<b>Dipilih:</b> <code>{esc(selected_label)}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@admin_callback
async def pd_req_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    paket_id = parts[1]
    req_id = parts[2]

    if 'editing_prereq' not in context.user_data or context.user_data['editing_prereq'].get('paket_id') != paket_id:
        context.user_data['editing_prereq'] = {'paket_id': paket_id, 'selected': []}

    selected = context.user_data['editing_prereq']['selected']
    if req_id in selected:
        selected.remove(req_id)
    else:
        selected.append(req_id)

    p = await get_product(paket_id)
    if not p:
        await query.answer("Produk tidak ditemukan.", show_alert=True)
        return
    await _show_prereq_selector(query, context, paket_id, p)

@admin_callback
async def pd_req_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🗑️ Syarat dikosongkan")
    parts = query.data.split("|")
    paket_id = parts[1]
    context.user_data['editing_prereq'] = {'paket_id': paket_id, 'selected': []}
    p = await get_product(paket_id)
    if not p:
        await query.answer("Produk tidak ditemukan.", show_alert=True)
        return
    await _show_prereq_selector(query, context, paket_id, p)

@admin_callback
async def pd_req_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    paket_id = parts[1]

    selected = context.user_data.get('editing_prereq', {}).get('selected', [])
    context.user_data.pop('editing_prereq', None)

    new_val = ",".join(selected) if selected else None
    await update_product_field(paket_id, 'requires_paket_ids', new_val)

    await produk_detail(update, context, paket_id=paket_id)

@admin_callback
async def produk_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    raw = query.data.replace("pd_edit_", "")
    FIELDS = ["nama", "emoji", "harga", "deskripsi", "link", "group_chat_id", "requires_paket_ids"]
    field = None
    paket_id = None
    for f in FIELDS:
        if raw.endswith(f"_{f}"):
            field = f
            paket_id = raw[: -(len(f) + 1)]
            break

    if not field or not paket_id:
        await query.answer("Format tidak valid.", show_alert=True)
        return

    p = await get_product(paket_id)
    if not p:
        await query.answer("Produk tidak ditemukan.", show_alert=True)
        return

    if field == 'requires_paket_ids':
        await _show_prereq_selector(query, context, paket_id, p)
        return

    label_map = {
        "nama": "nama produk",
        "emoji": "emoji",
        "harga": "harga (angka saja, contoh: 15000)",
        "deskripsi": "deskripsi",
        "link": "link produk",
        "group_chat_id": "ID grup private (contoh: -1001234567890, ketik 'hapus' untuk kosongkan)",
    }

    context.user_data['editing_product'] = {'paket_id': paket_id, 'field': field}

    await query.edit_message_text(
        f"<b>✍️ Edit {field.upper()} - {esc(p['emoji'])} {esc(p['nama'])}</b>\n"
        f"========================\n\n"
        f"Nilai saat ini: <code>{esc(str(p.get(field) or '-'))}</code>\n\n"
        f"<i>Kirim {esc(label_map.get(field, field))} baru sekarang:</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Batal", callback_data=f"pd_detail_{paket_id}")]
        ])
    )

@admin_callback
async def produk_hapus_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    paket_id = query.data.replace("pd_hapus_", "")
    p = await get_product(paket_id)
    if not p:
        await query.answer("Produk tidak ditemukan.", show_alert=True)
        return

    await query.edit_message_text(
        f"<b>⚠️ Hapus Produk?</b>\n"
        f"========================\n\n"
        f"Kamu yakin mau hapus <b>{esc(p['emoji'])} {esc(p['nama'])}</b>?\n"
        f"Tindakan ini tidak bisa dibatalkan.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Ya, Hapus", callback_data=f"pd_hapus_ok_{paket_id}"),
                styled_button("❌ Batal", style="danger",     callback_data=f"pd_detail_{paket_id}"),
            ]
        ])
    )

@admin_callback
async def produk_hapus_exec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    paket_id = query.data.replace("pd_hapus_ok_", "")
    p = await get_product(paket_id)
    if not p:
        await query.answer("Produk tidak ditemukan.", show_alert=True)
        return

    await delete_product(paket_id)
    await query.edit_message_text(
        f"✅ Produk <b>{esc(p['emoji'])} {esc(p['nama'])}</b> berhasil dihapus.",
        parse_mode="HTML"
    )
    await _send_produk_menu(context, chat_id=query.from_user.id)

@admin_callback
async def produk_tambah_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data['adding_product'] = {'step': 'nama'}

    await query.edit_message_text(
        "<b>➕ TAMBAH PRODUK BARU</b>\n"
        "========================\n\n"
        "Langkah 1/4\n\n"
        "<i>Kirim <b>nama</b> produk baru:</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Batal", callback_data="pd_tambah_batal")]
        ])
    )

@admin_callback
async def produk_tambah_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('adding_product', None)
    await _send_produk_menu(context, chat_id=query.from_user.id, query=query)

@admin_callback
async def pd_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('adding_product', None)
    context.user_data.pop('editing_product', None)
    await _send_produk_menu(context, chat_id=query.from_user.id, query=query)

# =================== ADMIN: ORDER AKTIF ===================

def _callback_page(data: str, prefix: str) -> int:
    if not data or not data.startswith(prefix + "|"):
        return 0
    try:
        return max(0, int(data.split("|", 1)[1]))
    except (TypeError, ValueError, IndexError):
        return 0


def _pagination_row(prefix: str, page: int, total: int, page_size: int):
    total_pages = max(1, (total + page_size - 1) // page_size)
    if total_pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}|{page - 1}"))
    row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data=f"{prefix}|{page}"))
    if page + 1 < total_pages:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}|{page + 1}"))
    return row


async def _build_active_orders_text_and_keyboard(page: int = 0):
    """Panel order aktif memakai LIMIT/OFFSET agar payload dan query tetap kecil."""
    orders, total = await get_admin_order_page("waiting", page, ADMIN_ORDER_PAGE_SIZE)
    if not orders:
        return None, None

    total_pages = max(1, (total + ADMIN_ORDER_PAGE_SIZE - 1) // ADMIN_ORDER_PAGE_SIZE)
    page = min(page, total_pages - 1)
    text = f"<b>⏳ ORDER MENUNGGU BAYAR ({total})</b>\n========================\n📄 Halaman {page + 1}/{total_pages}\n\n"
    keyboard = []
    for o in orders:
        paket_nama = o.get('paket_nama') or o['paket_id']
        paket_emoji = o.get('paket_emoji') or '📦'
        paket_harga = o.get('paket_harga') or 0
        durasi = hitung_durasi(o["waktu"])
        text += (
            f"- {esc(paket_emoji)} <b>{esc(o['user_name'])}</b>\n"
            f"  Paket: {esc(paket_nama)} - {format_harga(paket_harga)}\n"
            f"  Dibuat: {durasi}\n"
            f"  ID: <code>{esc(o['order_id'])}</code>\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(
                f"✅ Konfirmasi: {o['user_name']}",
                callback_data=f"adm_konfirm|{o['user_id']}|{o['order_id']}"
            ),
            InlineKeyboardButton(
                f"❌ Cancel: {o['user_name']}",
                callback_data=f"adm_cancel|{o['user_id']}|{o['order_id']}"
            )
        ])
    nav = _pagination_row("admpanel_orders_aktif", page, total, ADMIN_ORDER_PAGE_SIZE)
    if nav:
        keyboard.append(nav)
    return text, keyboard


async def _build_pending_orders_text_and_keyboard(page: int = 0, back_callback: str = "admpanel_orders"):
    orders, total = await get_admin_order_page("pending", page, ADMIN_ORDER_PAGE_SIZE)
    if not orders:
        return None, None
    total_pages = max(1, (total + ADMIN_ORDER_PAGE_SIZE - 1) // ADMIN_ORDER_PAGE_SIZE)
    page = min(page, total_pages - 1)
    text = f"<b>📋 ORDER PENDING ({total})</b>\n========================\n📄 Halaman {page + 1}/{total_pages}\n\n"
    keyboard = []
    for o in orders:
        paket_emoji = o.get('paket_emoji') or '📦'
        paket_nama = o.get('paket_nama') or o['paket_id']
        durasi = hitung_durasi(o["waktu"])
        text += f"- {esc(paket_emoji)} {esc(o['user_name'])} - {esc(paket_nama)} - {durasi}\n"
        keyboard.append([InlineKeyboardButton(f"👤 Proses: {o['user_name']}", callback_data=f"proses_{o['user_id']}")])
    nav = _pagination_row("admpanel_orders_pending", page, total, ADMIN_ORDER_PAGE_SIZE)
    if nav:
        keyboard.append(nav)
    keyboard.append([styled_button("⬅️ Kembali", style="primary", callback_data=back_callback)])
    return text, keyboard

async def cmd_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return

    text, keyboard = await _build_active_orders_text_and_keyboard()
    if not text:
        await update.message.reply_text(
            "<b>✅ TIDAK ADA ORDER AKTIF</b>\n"
            "========================\n\n"
            "Tidak ada buyer yang sedang menunggu membayar saat ini.",
            parse_mode="HTML"
        )
        return

    keyboard.append([styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_orders")])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

def _check_waiting_order_sync(order_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE order_id=%s AND status='waiting'", (order_id,))
            return c.fetchone()

@admin_callback
async def admin_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)

    if not await is_admin(query.from_user.id, context):
        await _callback_feedback(query, context, "⛔ Akses ditolak.")
        return

    parts = query.data.split("|")
    if len(parts) != 3:
        await _callback_feedback(query, context, "Format tidak valid.")
        return

    target_user_id = int(parts[1])
    order_id = parts[2]

    order = await run_db(_check_waiting_order_sync, order_id)
    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah selesai/dibatalkan.")
        return

    order = dict(order)
    if target_user_id != int(order['user_id']):
        await _callback_feedback(query, context, "User pesanan tidak cocok.")
        return
    paket = await get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0}

    cancel_amount = order.get('harga_dibayar') or paket.get('harga', 0)
    if await update_order_status(order_id, 'cancelled') is None:
        await _callback_feedback(query, context, "Status pesanan sudah berubah; pembatalan dihentikan.")
        return
    await _stop_payment_task(target_user_id, order_id)
    if cancel_amount:
        await cancel_transaction(order_id, cancel_amount)
    await hapus_qris_buyer_lama(context.bot, order_id, target_user_id)
    await hapus_notif_lama(context.bot, order_id)

    await set_cooldown_db(target_user_id)
    cooldown_sisa = await get_cooldown_sisa_db(target_user_id)

    msg_id = await kirim_notif(
        context.bot,
        _format_order_notif(
            "❌ <b>DIBATALKAN ADMIN</b>",
            order.get('user_name', '-'), target_user_id, paket, order_id
        )
    )
    if msg_id:
        await set_admin_msg_id(order_id, msg_id)

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                "<b>❌ PESANAN DIBATALKAN</b>\n"
                "========================\n\n"
                "Pesanan kamu telah dibatalkan oleh admin.\n\n"
                f"⏳ Kamu bisa membuat order baru dalam <b>{cooldown_sisa} menit</b>.\n\n"
                "Hubungi admin jika ada pertanyaan.\n"
                "Ketik /start untuk membuat pesanan baru."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.debug(f"Gagal kirim notif pembatalan admin ke buyer: {e}")

    await query.edit_message_text(
        f"✅ <b>Order Dibatalkan</b>\n\n"
        f"👤 Buyer: {esc(order.get('user_name', '-'))}\n"
        f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
        f"📝 Order ID: <code>{esc(order_id)}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Orders", callback_data="admpanel_orders")]])
    )

@admin_callback
async def admin_manual_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id, context):
        return

    parts = query.data.split("|")
    if len(parts) != 3:
        await query.answer("Format tidak valid.", show_alert=True)
        return

    target_user_id = int(parts[1])
    order_id = parts[2]

    order = await run_db(_check_waiting_order_sync, order_id)
    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah selesai/dibatalkan.")
        return

    order = dict(order)
    if target_user_id != int(order['user_id']):
        await _callback_feedback(query, context, "User pesanan tidak cocok.")
        return
    paket = await get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0, "link": DEFAULT_LINK}

    ok = await mark_order_completed(order_id)
    if not ok:
        await query.edit_message_text("⚠️ Order sudah diproses sebelumnya (mungkin oleh webhook).")
        return

    await _stop_payment_task(target_user_id, order_id)
    await _process_completed_order_delivery(
        context.bot,
        order_id=order_id,
        paket_id=order['paket_id'],
        user_id=target_user_id,
        user_name=order.get('user_name', '-'),
        paid_amount=order.get('harga_dibayar') or paket.get('harga', 0),
        source_title="✅ <b>DIKONFIRMASI MANUAL</b>",
        paket=paket,
    )

    refreshed = await get_order_by_id(order_id)
    delivery_status = (refreshed or {}).get('delivery_status', 'processed')
    delivery_label = {
        'sent': '✅ Link terkirim',
        'held': '⏸️ Link ditahan karena syarat belum terpenuhi',
        'failed': '⚠️ Link gagal dikirim, cek manual',
        'pending': '⏳ Pengiriman sedang diproses',
    }.get(delivery_status, '✅ Diproses')

    await query.edit_message_text(
        f"✅ <b>Pembayaran Dikonfirmasi Manual</b>\n\n"
        f"👤 Buyer: {tg_user_link(target_user_id, order.get('user_name', '-'))}\n"
        f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
        f"📝 Order ID: <code>{esc(order_id)}</code>\n"
        f"🔗 Status: {delivery_label}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Orders", callback_data="admpanel_orders")]])
    )

# =================== ADMIN: STATISTIK ===================

def _format_short(harga: int) -> str:
    if harga >= 1_000_000:
        return f"Rp {harga/1_000_000:.1f}Jt"
    elif harga >= 1_000:
        return f"Rp {harga//1_000}rb"
    return format_harga(harga)

def _build_stats_text(s: dict, now: datetime) -> str:
    def growth(new_val, old_val, is_money=False):
        if old_val == 0:
            return ""
        diff = new_val - old_val
        pct_change = round(abs(diff) / old_val * 100)
        if diff > 0:
            label = _format_short(diff) if is_money else str(diff)
            return f"  <i>↑ +{label} (+{pct_change}%)</i>"
        elif diff < 0:
            label = _format_short(abs(diff)) if is_money else str(abs(diff))
            return f"  <i>↓ -{label} (-{pct_change}%)</i>"
        return ""

    total_all = (s['total_orders'] + s['active_count'] + s['cancelled_count'] + s['expired_count'] + s.get('rejected_count', 0)) or 1
    def pct(n): return f"{round(n/total_all*100)}%"

    total_closed = (s['total_orders'] + s['cancelled_count'] + s['expired_count'] + s.get('rejected_count', 0)) or 1
    conv_rate = f"{round(s['total_orders'] / total_closed * 100)}%"

    if s['avg_rating'] > 0:
        full_stars = int(s['avg_rating'])
        half = "✨" if s['avg_rating'] - full_stars >= 0.5 else ""
        rating_str = f"<b>{s['avg_rating']}/5</b> " + "⭐" * full_stars + half
    else:
        rating_str = "<i>Belum ada penilaian</i>"

    trend_lines = ""
    days_id = ['Sen','Sel','Rab','Kam','Jum','Sab','Min']
    date_map = {r['hari']: r for r in s.get('trend_7d', [])}
    filled_trend = []
    for i in range(6, -1, -1):
        d = now.date() - timedelta(days=i)
        filled_trend.append(date_map.get(d, {'hari': d, 'cnt': 0, 'rev': 0}))
    max_cnt = max((r['cnt'] for r in filled_trend), default=1) or 1
    if any(r['cnt'] > 0 for r in filled_trend):
        for r in filled_trend:
            day_name = days_id[r['hari'].weekday()]
            bars = round((r['cnt'] / max_cnt) * 6)
            bar = '█' * bars + '░' * (6 - bars)
            trend_lines += f"  <code>{day_name} {bar}</code>  {r['cnt']}x · {_format_short(r['rev'])}\n"
    else:
        trend_lines = "  <i>Belum ada data</i>\n"

    peak_str = ""
    if s.get('peak_hour') is not None:
        jam_end = (s['peak_hour'] + 1) % 24
        peak_str = (
            f"\n⏰ <b>JAM TERSIBUK</b>\n"
            f"- {s['peak_hour']:02d}:00 – {jam_end:02d}:00 WIB  "
            f"<b>({s['peak_count']} order)</b>\n"
        )

    prod_lines = ""
    if s['products_breakdown']:
        max_cnt_pb = s['products_breakdown'][0]['cnt'] or 1
        total_rev = s['total_revenue'] or 1
        for p in s['products_breakdown']:
            emoji = p.get('emoji') or '📦'
            nama = esc(p.get('nama') or p['paket_id'])
            bars = round((p['cnt'] / max_cnt_pb) * 7)
            bar = '█' * bars + '░' * (7 - bars)
            pct_rev = round(p['total'] / total_rev * 100)
            prod_lines += (
                f"- {esc(emoji)} <b>{nama}</b>\n"
                f"│  <code>{bar}</code>  {p['cnt']}x · {_format_short(p['total'])}  <i>({pct_rev}%)</i>\n"
            )
    else:
        prod_lines = "- <i>Belum ada transaksi produk.</i>\n"

    prod_month_lines = ""
    if s.get('products_month'):
        for pm in s['products_month']:
            emoji = pm.get('emoji') or '📦'
            nama = esc(pm.get('nama') or pm['paket_id'])
            prod_month_lines += f"  - {esc(emoji)} {nama}: <b>{pm['cnt']}x</b> · {_format_short(pm['total'])}\n"
    else:
        prod_month_lines = "  <i>Belum ada penjualan bulan ini.</i>\n"

    top_buyer_lines = ""
    for i, b in enumerate(s.get('top_buyers_month', []), 1):
        name = esc(b.get('user_name') or str(b['user_id']))
        top_buyer_lines += f"  {i}. {name}: <b>{b['cnt']}x</b> ({_format_short(b['total'])})\n"
    if not top_buyer_lines:
        top_buyer_lines = "  <i>Belum ada data.</i>\n"

    days_elapsed = s.get('days_elapsed', 1)
    est_str = ""
    if days_elapsed > 0 and s['month_revenue'] > 0:
        estimated = round(s['month_revenue'] / days_elapsed * 30)
        est_str = f"- Estimasi Akhir Bulan  :  <b>~{_format_short(estimated)}</b>  <i>(pace {days_elapsed}hr)</i>"

    repeat_pct = round(s['repeat_buyers'] / s['total_buyers'] * 100) if s['total_buyers'] > 0 else 0

    pend = s.get('pending_testi', 0)
    pend_str = f"<b>{pend}</b> ⚠️" if pend > 0 else "<b>0</b>"

    lines = [
        "📊 <b>LAPORAN STATISTIK TOKO</b>",
        "========================",
        "",
        "📅 <b>HARI INI</b>",
        f"- Order Selesai  :  <b>{s['today_completed']}</b>{growth(s['today_completed'], s['yesterday_completed'])}",
        f"- Omzet          :  <b>{_format_short(s['today_revenue'])}</b>{growth(s['today_revenue'], s['yesterday_revenue'], True)}",
        "",
        "📆 <b>MINGGU INI</b>",
        f"- Order Selesai  :  <b>{s.get('week_completed', 0)}</b>{growth(s.get('week_completed', 0), s.get('last_week_completed', 0))}",
        f"- Omzet          :  <b>{_format_short(s.get('week_revenue', 0))}</b>{growth(s.get('week_revenue', 0), s.get('last_week_revenue', 0), True)}",
        "",
        "🗓️ <b>BULAN INI</b>",
        f"- Order Selesai  :  <b>{s['month_completed']}</b>{growth(s['month_completed'], s['last_month_completed'])}",
        f"- Omzet          :  <b>{_format_short(s['month_revenue'])}</b>{growth(s['month_revenue'], s['last_month_revenue'], True)}",
        est_str if est_str else None,
        "",
        "🏅 <b>PRODUK LARIS BULAN INI</b>",
        prod_month_lines.rstrip('\n'),
        "",
        "👑 <b>TOP 3 BUYER BULAN INI</b>",
        top_buyer_lines.rstrip('\n'),
        "",
        "========================",
        "📈 <b>TREN 7 HARI TERAKHIR</b>",
        trend_lines.rstrip('\n'),
        peak_str.rstrip('\n') if peak_str else None,
        "",
        "========================",
        "🏆 <b>ALL TIME</b>",
        f"- Total Omzet          :  <b>{_format_short(s['total_revenue'])}</b>",
        f"- Rata-rata/hari       :  <b>{_format_short(s['avg_daily_revenue'])}</b>  <i>(30 hari)</i>",
        f"- Rata-rata/transaksi  :  <b>{_format_short(s['aov'])}</b>",
        f"- Pembeli Unik         :  <b>{s['total_buyers']}</b> orang",
        f"- Beli 2x+             :  <b>{s['repeat_buyers']}</b> orang  <i>({repeat_pct}% dari buyer)</i>",
        f"- Baru Bulan Ini       :  <b>{s['new_buyers_month']}</b> orang",
        "",
        "========================",
        "📊 <b>STATUS TRANSAKSI</b>",
        f"- ✅ Selesai     :  <b>{s['total_orders']}</b>  <i>({pct(s['total_orders'])})</i>",
        f"- ⏳ Aktif       :  <b>{s['active_count']}</b>",
        f"- ❌ Dibatalkan  :  <b>{s['cancelled_count']}</b>  <i>({pct(s['cancelled_count'])})</i>",
        f"- ⏰ Kadaluarsa  :  <b>{s['expired_count']}</b>  <i>({pct(s['expired_count'])})</i>",
        f"- 🚫 Ditolak     :  <b>{s.get('rejected_count', 0)}</b>  <i>({pct(s.get('rejected_count', 0))})</i>",
        f"- 📈 Konversi    :  <b>{conv_rate}</b>  <i>(selesai / total tertutup)</i>",
        "",
        "========================",
        "📦 <b>PENJUALAN PER PRODUK (ALL TIME)</b>",
        prod_lines.rstrip('\n'),
        "",
        "⭐ <b>ULASAN &amp; TESTIMONI</b>",
        f"- Rating     :  {rating_str}",
        f"- Disetujui  :  <b>{s['total_testi']}</b> ulasan",
        f"- Pending    :  {pend_str}",
        "",
        "========================",
        f"<i>Update: {now.strftime('%H:%M, %d/%m/%Y')} WIB</i>",
    ]
    return "\n".join(l for l in lines if l is not None)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    now = now_wib()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    s = await get_order_stats_cached(today_start, month_start)
    text = _build_stats_text(s, now)
    await update.message.reply_text(text, parse_mode="HTML")

# =================== USER: RIWAYAT ORDER ===================

async def cmd_riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    orders = await get_buyer_history_with_products(user_id)

    if not orders:
        await update.message.reply_text(
            "<b>📋 RIWAYAT ORDER</b>\n"
            "========================\n\n"
            "Kamu belum pernah melakukan pembelian.\n\n"
            "Ketik /start untuk mulai belanja.",
            parse_mode="HTML"
        )
        return

    STATUS_LABEL = {
        'completed': '✅ Selesai',
        'waiting':   '⏳ Menunggu Bayar',
        'pending':   '🔄 Diproses',
        'cancelled': '❌ Dibatalkan',
        'expired':   '⏰ Kedaluwarsa',
        'rejected':  '🚫 Ditolak',
    }

    text = "<b>📋 RIWAYAT ORDER (10 terakhir)</b>\n========================\n\n"
    for o in orders:
        paket_emoji = o.get('paket_emoji') or '📦'
        paket_nama = o.get('paket_nama') or o['paket_id']
        status = STATUS_LABEL.get(o['status'], o['status'])
        harga = o.get('harga_dibayar') or o.get('paket_harga') or 0
        text += (
            f"{esc(paket_emoji)} <b>{esc(paket_nama)}</b>\n"
            f"- Status: {status}\n"
            f"- Harga: {format_harga(harga)}\n"
            f"- {o.get('waktu', '-')}\n\n"
        )

    await update.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            styled_button("🔄 Kirim Ulang Link", style="success", callback_data="resend_menu")
        ]])
    )

# =================== BACKUP & EXPORT ===================

def _generate_full_export_sync():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM products ORDER BY harga ASC")
            products = [dict(r) for r in c.fetchall()]
            c.execute("SELECT * FROM orders ORDER BY id ASC")
            orders = [dict(r) for r in c.fetchall()]
            c.execute("SELECT * FROM banned_users ORDER BY banned_at DESC")
            banned = [dict(r) for r in c.fetchall()]
            c.execute("SELECT * FROM testimonials ORDER BY id ASC")
            testimonials = [dict(r) for r in c.fetchall()]
            c.execute("SELECT * FROM settings")
            settings = [dict(r) for r in c.fetchall()]
            c.execute("SELECT * FROM admins ORDER BY added_at ASC")
            admins = [dict(r) for r in c.fetchall()]
    return products, orders, banned, testimonials, settings, admins

async def _generate_json_export():
    products, orders, banned, testimonials, settings, admins = await run_db_admin(_generate_full_export_sync)
    payload = {
        "meta": {
            "app": "Hyper Family Store",
            "exported_at": now_wib().strftime("%H:%M, %d/%m/%Y"),
            "version": "3.0",
            "counts": {
                "products": len(products),
                "orders": len(orders),
                "banned_users": len(banned),
                "testimonials": len(testimonials),
                "settings": len(settings),
                "admins": len(admins),
            }
        },
        "products": products,
        "orders": orders,
        "banned_users": banned,
        "testimonials": testimonials,
        "settings": settings,
        "admins": admins,
    }
    payload_text = await run_cpu(
        json.dumps, payload, ensure_ascii=False, indent=2, default=str
    )
    return payload_text, len(products), len(orders), len(banned)

async def _kirim_backup(bot):
    ts = now_wib().strftime('%Y%m%d_%H%M%S')
    zip_name = f"backup_{ts}.zip"
    try:
        products, orders, banned, testimonials, settings, admins = await run_db_admin(_generate_full_export_sync)

        def _build_backup_zip():
            payload = {
                "export_time": now_wib().strftime('%H:%M, %d/%m/%Y'),
                "version": "3.0",
                "products": products,
                "orders": orders,
                "banned_users": banned,
                "testimonials": testimonials,
                "settings": settings,
                "admins": admins,
            }
            json_bytes = json.dumps(
                payload, ensure_ascii=False, indent=2, default=str
            ).encode("utf-8")

            import io as _io

            def _rows_to_csv_bytes(rows: list) -> bytes:
                if not rows:
                    return b""
                text_buf = _io.StringIO()
                writer = csv.DictWriter(
                    text_buf, fieldnames=list(rows[0].keys()), extrasaction='ignore'
                )
                writer.writeheader()
                writer.writerows([
                    {k: str(v) if v is not None else '' for k, v in row.items()}
                    for row in rows
                ])
                return text_buf.getvalue().encode("utf-8")

            buffer = BytesIO()
            with zipfile.ZipFile(buffer, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("backup.json", json_bytes)
                zf.writestr("orders.csv", _rows_to_csv_bytes(orders))
                zf.writestr("products.csv", _rows_to_csv_bytes(products))
                zf.writestr("banned_users.csv", _rows_to_csv_bytes(banned))
                zf.writestr("testimonials.csv", _rows_to_csv_bytes(testimonials))
                zf.writestr("admins.csv", _rows_to_csv_bytes(admins))
            buffer.seek(0)
            buffer.name = zip_name
            return buffer

        zip_buf = await run_cpu(_build_backup_zip)

        await bot.send_document(
            chat_id=ADMIN_ID,
            document=zip_buf,
            filename=zip_name,
            caption=(
                f"📦 <b>Backup Database (ZIP)</b>\n"
                f"🕒 {now_wib().strftime('%H:%M, %d/%m/%Y')}\n"
                f"📋 {len(orders)} orders | {len(products)} produk | {len(banned)} banned | "
                f"{len(testimonials)} testimoni | {len(admins)} admin\n\n"
                f"<i>Isi ZIP: backup.json · orders.csv · products.csv · banned_users.csv · "
                f"testimonials.csv · admins.csv</i>"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error backup: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Gagal backup database: {esc(str(e))}", parse_mode="HTML")
        except Exception as ex:
            logger.error(f"Gagal kirim pesan kegagalan backup ke admin: {ex}")

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    await update.message.reply_text("⏳ Membuat backup database...")
    await _kirim_backup(context.bot)

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    await update.message.reply_text("⏳ Menyiapkan export JSON...")
    try:
        json_content, n_products, n_orders, n_banned = await _generate_json_export()
        filename = f"export_{now_wib().strftime('%Y%m%d_%H%M%S')}.json"
        buf = BytesIO(json_content.encode("utf-8"))
        buf.name = filename
        await update.message.reply_document(
            document=buf,
            filename=filename,
            caption=(
                f"✅ <b>Export Berhasil</b>\n"
                f"========================\n\n"
                f"📦 Products: {n_products}\n"
                f"📋 Orders: {n_orders}\n"
                f"🚫 Banned users: {n_banned}\n"
                f"🕒 {now_wib().strftime('%H:%M, %d/%m/%Y')}\n\n"
                f"Kirim file ini ke bot dengan /import_json untuk restore."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal export: {esc(str(e))}", parse_mode="HTML")

async def cmd_import_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    context.user_data['awaiting_json_import'] = True
    await update.message.reply_text(
        "<b>📥 IMPORT DATA JSON</b>\n"
        "========================\n\n"
        "Kirim file <code>.json</code> yang didapat dari /export.\n\n"
        "⚠️ Data yang sudah ada <b>tidak akan dihapus</b> - hanya ditambah/diperbarui.\n"
        "<i>Kirim file sekarang...</i>",
        parse_mode="HTML"
    )

def _import_json_data_sync(data: dict):
    products   = data.get("products",    [])
    orders     = data.get("orders",      [])
    banned     = data.get("banned_users",[])
    testimonials = data.get("testimonials", [])
    settings   = data.get("settings",   [])
    admins     = data.get("admins",      [])

    ok_p = ok_o = ok_b = ok_t = ok_s = ok_a = 0
    fail_p = fail_o = fail_b = fail_t = fail_s = fail_a = 0

    with db_session_safe() as conn:
        with conn.cursor() as c:
            if products:
                try:
                    c.executemany(
                        """INSERT INTO products (paket_id, nama, emoji, deskripsi, harga, link, group_chat_id, aktif)
                           VALUES (%(paket_id)s, %(nama)s, %(emoji)s, %(deskripsi)s, %(harga)s, %(link)s, %(group_chat_id)s, %(aktif)s)
                           ON CONFLICT (paket_id) DO UPDATE SET
                               nama=EXCLUDED.nama, emoji=EXCLUDED.emoji,
                               deskripsi=EXCLUDED.deskripsi, harga=EXCLUDED.harga,
                               link=EXCLUDED.link, group_chat_id=EXCLUDED.group_chat_id,
                               aktif=EXCLUDED.aktif""",
                        [{
                            "paket_id":      p.get("paket_id"),
                            "nama":          p.get("nama"),
                            "emoji":         p.get("emoji", "📦"),
                            "deskripsi":     p.get("deskripsi", ""),
                            "harga":         int(p.get("harga", 0)),
                            "link":          p.get("link", DEFAULT_LINK),
                            "group_chat_id": p.get("group_chat_id"),
                            "aktif":         p.get("aktif", True),
                        } for p in products]
                    )
                    ok_p = len(products)
                except Exception as e:
                    logger.error(f"[IMPORT] products batch gagal: {e}")
                    fail_p = len(products)

            for o in orders:
                try:
                    c.execute(
                        """INSERT INTO orders
                           (user_id, user_name, paket_id, order_id, status, waktu, harga_dibayar, sent_link, order_changes)
                           VALUES (%(user_id)s, %(user_name)s, %(paket_id)s, %(order_id)s,
                                   %(status)s, %(waktu)s, %(harga_dibayar)s, %(sent_link)s, %(order_changes)s)
                           ON CONFLICT (order_id) DO NOTHING""",
                        {
                            "user_id":       o.get("user_id"),
                            "user_name":     o.get("user_name"),
                            "paket_id":      o.get("paket_id"),
                            "order_id":      o.get("order_id"),
                            "status":        o.get("status", "completed"),
                            "waktu":         o.get("waktu", ""),
                            "harga_dibayar": int(o.get("harga_dibayar") or 0),
                            "sent_link":     o.get("sent_link"),
                            "order_changes": int(o.get("order_changes") or 0),
                        }
                    )
                    ok_o += 1
                except Exception as e:
                    logger.error(f"[IMPORT] order gagal: {e}")
                    fail_o += 1

            if banned:
                try:
                    c.executemany(
                        """INSERT INTO banned_users (user_id, reason, banned_at)
                           VALUES (%(user_id)s, %(reason)s, %(banned_at)s)
                           ON CONFLICT (user_id) DO NOTHING""",
                        [{
                            "user_id":   b.get("user_id"),
                            "reason":    b.get("reason", ""),
                            "banned_at": b.get("banned_at", now_wib().strftime("%H:%M - %d/%m/%Y")),
                        } for b in banned]
                    )
                    ok_b = len(banned)
                except Exception as e:
                    logger.error(f"[IMPORT] banned batch gagal: {e}")
                    fail_b = len(banned)

            for t in testimonials:
                try:
                    c.execute(
                        """INSERT INTO testimonials (user_id, user_name, paket_id, order_id, rating, review, status)
                           VALUES (%(user_id)s, %(user_name)s, %(paket_id)s, %(order_id)s, %(rating)s, %(review)s, %(status)s)
                           ON CONFLICT (order_id) DO NOTHING""",
                        {
                            "user_id":   t.get("user_id"),
                            "user_name": t.get("user_name"),
                            "paket_id":  t.get("paket_id"),
                            "order_id":  t.get("order_id"),
                            "rating":    t.get("rating", 5),
                            "review":    t.get("review", ""),
                            "status":    t.get("status", "approved"),
                        }
                    )
                    ok_t += 1
                except Exception as e:
                    logger.error(f"[IMPORT] testimonial gagal: {e}")
                    fail_t += 1

            for s in settings:
                if s.get('key') in ('managed_groups',):
                    continue
                try:
                    c.execute(
                        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                        (s.get('key'), s.get('value'))
                    )
                    ok_s += 1
                except Exception as e:
                    logger.error(f"[IMPORT] setting gagal: {e}")
                    fail_s += 1

            for a in admins:
                try:
                    c.execute(
                        """INSERT INTO admins (user_id, nama, added_by, added_at)
                           VALUES (%(user_id)s, %(nama)s, %(added_by)s, %(added_at)s)
                           ON CONFLICT (user_id) DO NOTHING""",
                        {
                            "user_id":  a.get("user_id"),
                            "nama":     a.get("nama", ""),
                            "added_by": a.get("added_by", 0),
                            "added_at": a.get("added_at", ""),
                        }
                    )
                    ok_a += 1
                except Exception as e:
                    logger.error(f"[IMPORT] admin gagal: {e}")
                    fail_a += 1

    return ok_p, fail_p, ok_o, fail_o, ok_b, fail_b, ok_t, fail_t, ok_s, fail_s, ok_a, fail_a

async def handle_json_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    if not context.user_data.get('awaiting_json_import'):
        return

    doc = update.message.document
    if not doc or not doc.file_name.endswith('.json'):
        await update.message.reply_text("❌ File harus berformat <code>.json</code>. Coba lagi dengan /import_json.", parse_mode="HTML")
        return

    context.user_data.pop('awaiting_json_import', None)
    status_msg = await update.message.reply_text("⏳ Membaca file JSON...")

    try:
        file = await context.bot.get_file(doc.file_id)
        raw = await file.download_as_bytearray()
        decoded = raw.decode("utf-8")
        data = await run_cpu(json.loads, decoded)
    except Exception as e:
        await status_msg.edit_text(f"❌ Gagal membaca file JSON: {esc(str(e))}", parse_mode="HTML")
        return

    if "products" not in data and "orders" not in data:
        await status_msg.edit_text("❌ Format JSON tidak valid. Pastikan file berasal dari /export.")
        return

    result = await run_db_admin(_import_json_data_sync, data)
    _invalidate_products_cache()
    _settings_cache.clear()
    _banned_cache.clear()
    _invalidate_order_caches(None)
    ok_p, fail_p, ok_o, fail_o, ok_b, fail_b, ok_t, fail_t, ok_s, fail_s, ok_a, fail_a = result

    await status_msg.edit_text(
        f"✅ <b>Import JSON Selesai</b>\n"
        f"========================\n\n"
        f"📦 Products: {ok_p} berhasil, {fail_p} gagal\n"
        f"📋 Orders: {ok_o} berhasil, {fail_o} gagal\n"
        f"🚫 Banned: {ok_b} berhasil, {fail_b} gagal\n"
        f"⭐ Testimoni: {ok_t} berhasil, {fail_t} gagal\n"
        f"⚙️ Settings: {ok_s} berhasil, {fail_s} gagal\n"
        f"👥 Admins: {ok_a} berhasil, {fail_a} gagal\n\n"
        f"<i>Semua data sudah ter-restore.</i>",
        parse_mode="HTML"
    )

# =================== KIRIM ULANG LINK ===================

# All resend lookups bypass the product cache. Keep paket_id when replacing a group.
RESEND_PAGE_SIZE = 8
RESEND_COOLDOWN_SECONDS = 10


@async_wrap
def get_resend_product_page(user_id, page=0):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT recent.id, recent.paket_id, p.nama, p.emoji
                FROM (
                    SELECT DISTINCT ON (paket_id) id, paket_id
                    FROM orders
                    WHERE user_id=%s AND status='completed'
                    ORDER BY paket_id, id DESC
                ) recent
                LEFT JOIN products p ON p.paket_id=recent.paket_id
                ORDER BY recent.id DESC
                LIMIT %s OFFSET %s
            """, (user_id, RESEND_PAGE_SIZE + 1, page * RESEND_PAGE_SIZE))
            return [dict(row) for row in c.fetchall()]


@async_wrap
def get_resend_order(user_id, row_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT * FROM orders
                WHERE id=%s AND user_id=%s AND status='completed'
            """, (row_id, user_id))
            row = c.fetchone()
            return dict(row) if row else None


@async_wrap
def get_resend_product(paket_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM products WHERE paket_id=%s", (paket_id,))
            row = c.fetchone()
            return dict(row) if row else None


def _resend_menu_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Pilih Produk Lain", callback_data="resend_menu")
    ]])


async def _resend_access_allowed(update, context):
    if not update.effective_chat or update.effective_chat.type != 'private':
        if update.callback_query:
            await _fast_callback_ack(update.callback_query, "Buka chat pribadi bot untuk kirim ulang link.")
        elif update.effective_message:
            await update.effective_message.reply_text("Buka chat pribadi bot lalu ketik /resend.")
        return False
    if await is_banned(update.effective_user.id):
        if update.callback_query:
            await _fast_callback_ack(update.callback_query, "Akses dibatasi. Hubungi admin.")
        else:
            await update.effective_message.reply_text("Akses dibatasi. Hubungi admin.")
        return False
    return True


async def show_resend_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """New and legacy resend buttons first open the buyer's product selector."""
    if not await _resend_access_allowed(update, context):
        return
    query = update.callback_query
    page = 0
    if query:
        await _fast_callback_ack(query)
        if query.data.startswith('resend_menu|'):
            try:
                page = min(100000, max(0, int(query.data.split('|', 1)[1])))
            except (ValueError, IndexError):
                page = 0
    try:
        rows = await get_resend_product_page(update.effective_user.id, page)
        if not rows and page:
            page = 0
            rows = await get_resend_product_page(update.effective_user.id, page)
        keyboard = []
        for row in rows[:RESEND_PAGE_SIZE]:
            label = f"{row.get('emoji') or '📦'} {row.get('nama') or row['paket_id']}"
            keyboard.append([InlineKeyboardButton(
                label[:80], callback_data=f"resend_select|{row['id']}"
            )])
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"resend_menu|{page - 1}"))
        if len(rows) > RESEND_PAGE_SIZE:
            navigation.append(InlineKeyboardButton("Berikutnya ➡️", callback_data=f"resend_menu|{page + 1}"))
        if navigation:
            keyboard.append(navigation)
        keyboard.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="back_to_menu")])
        text = (
            "🔄 <b>KIRIM ULANG LINK</b>\n\n"
            "Pilih produk yang mau dikirim ulang link-nya.\n"
            "Link akan dibuat memakai data produk terbaru.\n\n"
            f"Halaman {page + 1}"
        ) if rows else "📦 Belum ada pembelian lunas yang bisa dikirim ulang link-nya."
        markup = InlineKeyboardMarkup(keyboard)
        if query and query.data.startswith('resend_menu|'):
            try:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            except telegram.error.BadRequest as exc:
                if 'message is not modified' not in str(exc).lower():
                    raise
        else:
            # Preserve the original payment/access message when opening the selector.
            await context.bot.send_message(
                chat_id=update.effective_user.id, text=text,
                parse_mode="HTML", reply_markup=markup
            )
    except Exception:
        logger.exception("[RESEND] Gagal menampilkan pilihan produk")
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="⚠️ Daftar produk belum bisa dimuat. Coba /resend lagi sebentar."
        )


async def resend_group_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _resend_access_allowed(update, context):
        return
    # No await between checking and claiming: concurrent callbacks cannot double-send.
    if context.user_data.get('_resend_busy'):
        await _fast_callback_ack(query, "⏳ Link sedang diproses. Tunggu sebentar.")
        return
    last_sent = context.user_data.get('_resend_last_sent')
    if last_sent is not None and _time.monotonic() - last_sent < RESEND_COOLDOWN_SECONDS:
        await _fast_callback_ack(query, "⏳ Tunggu 10 detik sebelum kirim ulang lagi.")
        return
    context.user_data['_resend_busy'] = True
    try:
        # Guard is already held. ACK has no data dependency on entitlement/invite work.
        # Keep validation, invite creation, delivery and persistence in their original order.
        _schedule_navigation_ack(query, "⏳ Mengambil akses terbaru...")
        parts = query.data.split('|')
        if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 18:
            await _callback_feedback(query, context, "⚠️ Pilihan tidak valid. Buka /resend lagi.")
            return
        user_id = query.from_user.id
        order = await get_resend_order(user_id, int(parts[1]))
        if not order:
            await _callback_feedback(query, context, "⚠️ Pembelian tidak ditemukan atau belum lunas.")
            return
        paket = await get_resend_product(order['paket_id'])
        if not paket:
            await _callback_feedback(query, context, "⚠️ Produk sudah tidak tersedia. Hubungi admin untuk akses pengganti.")
            return
        if order.get('delivery_status') == 'pending':
            await _callback_feedback(query, context, "⏳ Pengiriman pesanan sedang diproses. Coba lagi sebentar.")
            return
        # Previously delivered access (including manual admin releases) remains valid.
        # Undelivered purchases must satisfy the same prerequisites as initial delivery.
        if order.get('delivery_status') != 'sent':
            missing = await check_prerequisites_sync(user_id, paket.get('requires_paket_ids') or '')
            if missing:
                await _callback_feedback(query, context, "🔒 Syarat paket belum lengkap. Selesaikan syarat pembelian atau hubungi admin.")
                return
        group_link = None
        if paket.get('group_chat_id'):
            group_link = await generate_group_link(context.bot, paket, order['order_id'])
            if not group_link:
                # The fallback may still point to a deleted group when only Group ID changed.
                await _callback_feedback(query, context, "⚠️ Link grup terbaru belum bisa dibuat. Hubungi admin agar Group ID dan izin bot di grup pengganti diperiksa.")
                return
            link = group_link
        else:
            link = (paket.get('link') or '').strip()
            if not link or link == DEFAULT_LINK:
                await _callback_feedback(query, context, "⚠️ Akses produk belum tersedia. Hubungi admin.")
                return
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ <b>LINK PRODUK TERBARU</b>\n\n"
                f"📦 {esc(paket.get('emoji') or '📦')} <b>{esc(paket.get('nama') or order['paket_id'])}</b>\n"
                f"🔖 Order: <code>{esc(order['order_id'])}</code>\n\n"
                f"{_build_link_section(esc(group_link) if group_link else None, esc(link))}"
            ),
            parse_mode="HTML", reply_markup=_resend_menu_keyboard()
        )
        context.user_data['_resend_last_sent'] = _time.monotonic()
        try:
            await set_sent_link(order['order_id'], link)
        except Exception:
            # The buyer already received access; a database error must not claim delivery failed.
            logger.exception("[RESEND] Link terkirim tetapi gagal menyimpan order %s", order['order_id'])
    except Exception:
        logger.exception("[RESEND] Gagal mengirim ulang link")
        await _callback_feedback(query, context, "⚠️ Link belum berhasil dikirim. Coba /resend lagi sebentar atau hubungi admin.")
    finally:
        context.user_data.pop('_resend_busy', None)


# =================== BACKGROUND LOOPS ===================
REMINDER_HARI = 7

def _get_buyers_for_reminder_sync(hari: int):
    target_day_start = (now_wib() - timedelta(days=hari)).replace(hour=0, minute=0, second=0, microsecond=0)
    target_day_end = target_day_start + timedelta(days=1)
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT DISTINCT user_id, user_name FROM orders WHERE status='completed' AND created_at >= %s AND created_at < %s",
                (target_day_start, target_day_end)
            )
            return [dict(r) for r in c.fetchall()]

async def _send_buyer_reminders(bot):
    buyers = await run_db_admin(_get_buyers_for_reminder_sync, REMINDER_HARI)
    if not buyers:
        return
    logger.info(f"[REMINDER] Mengirim reminder ke {len(buyers)} buyer...")
    for buyer in buyers:
        try:
            buyer_name = esc(buyer.get('user_name') or 'Kak')
            await bot.send_message(
                chat_id=buyer['user_id'],
                text=(
                    f"👋 Halo, <b>{buyer_name}</b>!\n\n"
                    f"Terima kasih sudah berbelanja di\n"
                    f"<b>Hyper Family Store</b> 🛒✨\n\n"
                    f"Semoga paket yang kamu beli sesuai\n"
                    f"dan memuaskan, ya 😊\n\n"
                    f"Mau lihat pilihan paket lainnya?\n\n"
                    f"Tekan tombol di bawah untuk\n"
                    f"membuka katalog 👇"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛍️ Lihat Katalog", callback_data="back_to_menu")]
                ])
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"[REMINDER] Gagal kirim ke {buyer['user_id']}: {e}")

async def _buyer_reminder_loop(bot):
    while True:
        try:
            now = now_wib()
            target = now.replace(hour=10, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_secs = (target - now).total_seconds()
            await asyncio.sleep(wait_secs)
            await _send_buyer_reminders(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[REMINDER] Error di reminder loop: {e}")
            await asyncio.sleep(3600)

async def _auto_backup_loop():
    while True:
        try:
            now = now_wib()
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_secs = (next_midnight - now).total_seconds()
            await asyncio.sleep(wait_secs)
            logger.info("[AUTO_BACKUP] Menjalankan backup harian otomatis...")
            await _kirim_backup(_current_bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[AUTO_BACKUP] Error di backup loop: {e}")
            try:
                if _current_bot:
                    await _current_bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f"⚠️ <b>AUTO BACKUP GAGAL</b>\n"
                            f"========================\n\n"
                            f"Error: <code>{esc(str(e))}</code>\n"
                            f"🕒 {now_wib().strftime('%H:%M, %d/%m/%Y')}\n\n"
                            f"<i>Jalankan /backup secara manual.</i>"
                        ),
                        parse_mode="HTML"
                    )
            except Exception as ex:
                logger.error(f"Gagal mengirim status error backup otomatis ke admin: {ex}")
            await asyncio.sleep(3600)

async def _cleanup_cooldowns_loop():
    while True:
        try:
            await asyncio.sleep(3600)
            await cleanup_expired_cooldowns()
            logger.debug("[CLEANUP] Expired cooldowns dibersihkan.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[CLEANUP] Error di cleanup cooldown loop: {e}")

# =================== ADMIN: DURABLE BROADCAST ===================

BLAST_RATE = _env_float('BLAST_MESSAGES_PER_SECOND', 18.0, 1.0, 22.0)
BLAST_WORKERS = _env_int('BLAST_WORKERS', 6, 1, 10)
BLAST_SEND_TIMEOUT = 25
BLAST_LEASE_SECONDS = 120
_blast_tasks = {}
_blast_tasks_lock = None
_blast_schema_ready = False
_blast_schema_lock = None


def _get_blast_tasks_lock():
    global _blast_tasks_lock
    if _blast_tasks_lock is None:
        _blast_tasks_lock = asyncio.Lock()
    return _blast_tasks_lock


@contextmanager
def _blast_transaction():
    # db_session_safe defaults to autocommit: claims require an explicit transaction.
    with db_session_safe() as conn:
        conn.autocommit = False
        with conn.cursor() as cursor:
            yield cursor
        conn.commit()


def _blast_schema_sync():
    with _blast_transaction() as c:
        c.execute('SELECT pg_advisory_xact_lock(72419031)')
        c.execute("""CREATE TABLE IF NOT EXISTS broadcast_jobs (
            id BIGSERIAL PRIMARY KEY, admin_id BIGINT NOT NULL,
            nonce TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'paused', message_id BIGINT,
            lease_token TEXT, lease_until TIMESTAMPTZ,
            retry_until TIMESTAMPTZ, error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS broadcast_one_active
            ON broadcast_jobs ((1)) WHERE status IN ('running','paused','cancelling')""")
        c.execute("""CREATE TABLE IF NOT EXISTS broadcast_recipients (
            job_id BIGINT NOT NULL REFERENCES broadcast_jobs(id),
            user_id BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, message_id BIGINT,
            error TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (job_id, user_id)
        )""")
        c.execute("""CREATE INDEX IF NOT EXISTS broadcast_recipient_queue
            ON broadcast_recipients(job_id, status, user_id)""")


async def _ensure_blast_schema():
    global _blast_schema_ready, _blast_schema_lock
    if _blast_schema_ready:
        return
    if _blast_schema_lock is None:
        _blast_schema_lock = asyncio.Lock()
    async with _blast_schema_lock:
        if not _blast_schema_ready:
            await run_db_admin(_blast_schema_sync)
            _blast_schema_ready = True


def _blast_recover_locked(c, job):
    # Only called with the job row locked, after the old runner's lease expired.
    if job and job.get('lease_token') and not job['lease_live']:
        c.execute("""UPDATE broadcast_recipients SET status='uncertain',
            error='Proses terputus; hasil Telegram belum pasti', updated_at=NOW()
            WHERE job_id=%s AND status='sending'""", (job['id'],))
        c.execute("""UPDATE broadcast_jobs SET lease_token=NULL, lease_until=NULL,
            status=CASE WHEN status='cancelling' THEN 'cancelled'
                        WHEN status='running' THEN 'paused' ELSE status END,
            error='Proses terputus. Periksa progres lalu lanjutkan sisa antrean.',
            updated_at=NOW() WHERE id=%s""", (job['id'],))


def _blast_snapshot_sync(job_id=None):
    with _blast_transaction() as c:
        if job_id is None:
            c.execute("""SELECT *, COALESCE(lease_until>NOW(),FALSE) AS lease_live
                FROM broadcast_jobs ORDER BY
                (status IN ('running','paused','cancelling')) DESC, id DESC LIMIT 1 FOR UPDATE""")
        else:
            c.execute("""SELECT *, COALESCE(lease_until>NOW(),FALSE) AS lease_live
                FROM broadcast_jobs WHERE id=%s FOR UPDATE""", (job_id,))
        job = c.fetchone()
        if not job:
            return None
        job = dict(job)
        _blast_recover_locked(c, job)
        c.execute("""SELECT *, COALESCE(lease_until>NOW(),FALSE) AS lease_live,
            GREATEST(0, EXTRACT(EPOCH FROM retry_until-NOW())) AS wait_seconds
            FROM broadcast_jobs WHERE id=%s""", (job['id'],))
        job = dict(c.fetchone())
        c.execute("""SELECT status, COUNT(*) AS n FROM broadcast_recipients
            WHERE job_id=%s GROUP BY status""", (job['id'],))
        job['counts'] = {r['status']: int(r['n']) for r in c.fetchall()}
        return job


def _blast_create_sync(admin_id, body, nonce):
    with _blast_transaction() as c:
        c.execute('SELECT pg_advisory_xact_lock(72419031)')
        c.execute('SELECT id FROM broadcast_jobs WHERE nonce=%s', (nonce,))
        old = c.fetchone()
        if old:
            return old['id'], False
        c.execute("SELECT id FROM broadcast_jobs WHERE status IN ('running','paused','cancelling') LIMIT 1")
        active = c.fetchone()
        if active:
            return active['id'], False
        c.execute("INSERT INTO broadcast_jobs(admin_id,body,nonce) VALUES (%s,%s,%s) RETURNING id",
                  (admin_id, body, nonce))
        job_id = c.fetchone()['id']
        # Frozen, unique audience. No reachability probe; keep the original all-order audience.
        c.execute("""INSERT INTO broadcast_recipients(job_id,user_id)
            SELECT %s, o.user_id FROM orders o
            WHERE o.user_id>0 AND NOT EXISTS
                (SELECT 1 FROM banned_users b WHERE b.user_id=o.user_id)
            GROUP BY o.user_id ON CONFLICT DO NOTHING""", (job_id,))
        if c.rowcount == 0:
            c.execute("UPDATE broadcast_jobs SET status='completed' WHERE id=%s", (job_id,))
        return job_id, True


def _blast_acquire_sync(job_id, token):
    with _blast_transaction() as c:
        c.execute("""SELECT *, COALESCE(lease_until>NOW(),FALSE) AS lease_live
            FROM broadcast_jobs WHERE id=%s FOR UPDATE""", (job_id,))
        job = c.fetchone()
        if not job or job['status'] not in ('paused','running') or job['lease_live']:
            return False
        _blast_recover_locked(c, job)
        c.execute("""UPDATE broadcast_jobs SET status='running', lease_token=%s,
            lease_until=NOW()+(%s * INTERVAL '1 second'), error=NULL, updated_at=NOW()
            WHERE id=%s""", (token, BLAST_LEASE_SECONDS, job_id))
        return True


def _blast_heartbeat_sync(job_id, token):
    with _blast_transaction() as c:
        c.execute("""UPDATE broadcast_jobs SET lease_until=NOW()+(%s * INTERVAL '1 second'),
            updated_at=NOW() WHERE id=%s AND lease_token=%s AND lease_until>NOW()
            RETURNING status, body, GREATEST(0,EXTRACT(EPOCH FROM retry_until-NOW())) AS wait_seconds""",
                  (BLAST_LEASE_SECONDS, job_id, token))
        row = c.fetchone()
        return dict(row) if row else None


def _blast_claim_sync(job_id, token):
    with _blast_transaction() as c:
        # Serializes claim with pause/cancel. A claim is made just before sending.
        c.execute("""SELECT id FROM broadcast_jobs WHERE id=%s AND lease_token=%s
            AND lease_until>NOW() AND status='running'
            AND (retry_until IS NULL OR retry_until<=NOW()) FOR UPDATE""", (job_id, token))
        if not c.fetchone():
            return None
        c.execute("""SELECT user_id, attempts FROM broadcast_recipients
            WHERE job_id=%s AND status='pending' ORDER BY user_id LIMIT 1 FOR UPDATE""", (job_id,))
        row = c.fetchone()
        if not row:
            return None
        c.execute("""UPDATE broadcast_recipients SET status='sending', attempts=attempts+1,
            updated_at=NOW() WHERE job_id=%s AND user_id=%s""", (job_id, row['user_id']))
        return {'user_id': row['user_id'], 'attempts': row['attempts'] + 1}


def _blast_result_sync(job_id, token, user_id, status, error=None, message_id=None, delay=0):
    with _blast_transaction() as c:
        c.execute('SELECT id FROM broadcast_jobs WHERE id=%s AND lease_token=%s FOR UPDATE', (job_id, token))
        if not c.fetchone():
            raise RuntimeError('Broadcast lease lost while saving result')
        c.execute("""UPDATE broadcast_recipients SET status=%s,error=%s,message_id=%s,
            updated_at=NOW() WHERE job_id=%s AND user_id=%s AND status='sending'""",
                  (status, (error or '')[:500], message_id, job_id, user_id))
        if delay:
            c.execute("""UPDATE broadcast_jobs SET retry_until=GREATEST(COALESCE(retry_until,NOW()),
                NOW()+(%s * INTERVAL '1 second')) WHERE id=%s""", (delay, job_id))


def _blast_control_sync(job_id, action):
    with _blast_transaction() as c:
        c.execute("""SELECT *, COALESCE(lease_until>NOW(),FALSE) AS lease_live
            FROM broadcast_jobs WHERE id=%s FOR UPDATE""", (job_id,))
        job = c.fetchone()
        if not job:
            return
        _blast_recover_locked(c, job)
        if action == 'pause':
            c.execute("UPDATE broadcast_jobs SET status='paused',updated_at=NOW() WHERE id=%s AND status='running'", (job_id,))
        elif action == 'cancel':
            c.execute("""UPDATE broadcast_jobs SET status=CASE
                WHEN lease_token IS NOT NULL AND lease_until>NOW() THEN 'cancelling' ELSE 'cancelled' END,
                updated_at=NOW() WHERE id=%s AND status IN ('running','paused','cancelling')""", (job_id,))


def _blast_release_sync(job_id, token, error=None):
    with _blast_transaction() as c:
        c.execute('SELECT status FROM broadcast_jobs WHERE id=%s AND lease_token=%s FOR UPDATE', (job_id, token))
        job = c.fetchone()
        if not job:
            return
        c.execute("""UPDATE broadcast_recipients SET status='uncertain',
            error='Proses terputus; jangan kirim ulang otomatis', updated_at=NOW()
            WHERE job_id=%s AND status='sending'""", (job_id,))
        c.execute("SELECT COUNT(*) AS n FROM broadcast_recipients WHERE job_id=%s AND status='pending'", (job_id,))
        remaining = c.fetchone()['n']
        state = 'cancelled' if job['status']=='cancelling' else ('completed' if not remaining else 'paused')
        c.execute("""UPDATE broadcast_jobs SET status=%s,lease_token=NULL,lease_until=NULL,
            error=%s,updated_at=NOW() WHERE id=%s AND lease_token=%s""", (state, error, job_id, token))


def _blast_message_sync(job_id, message_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute('UPDATE broadcast_jobs SET message_id=%s WHERE id=%s', (message_id, job_id))


def _blast_panel(job):
    counts = job['counts']
    total = sum(counts.values())
    processed = sum(counts.get(s,0) for s in ('sent','failed','skipped','uncertain'))
    labels = {'running':'🟢 Berjalan','paused':'⏸ Dijeda','cancelling':'⏳ Membatalkan',
              'cancelled':'⛔ Dibatalkan','completed':'✅ Selesai'}
    status = labels.get(job['status'],job['status'])
    wait = math.ceil(float(job.get('wait_seconds') or 0))
    if job['status']=='running' and wait:
        status = f'⏳ Menunggu batas Telegram ({wait} detik)'
    elif job['status']=='paused' and job.get('lease_live'):
        status = '⏸ Menjeda — menunggu pengiriman yang sedang berjalan'
    text = (
        f"📢 <b>BLAST #{job['id']}</b>\n{status}\n\n"
        f"👥 Target unik: <b>{total}</b>\n"
        f"📊 Diproses: <b>{processed}/{total}</b> ({processed*100//max(total,1)}%)\n"
        f"✅ Berhasil: <b>{counts.get('sent',0)}</b>\n"
        f"❌ Gagal: <b>{counts.get('failed',0)}</b>\n"
        f"⏩ Dilewati / tidak dapat diakses: <b>{counts.get('skipped',0)}</b>\n"
        f"❔ Belum pasti: <b>{counts.get('uncertain',0)}</b>\n"
        f"📤 Sedang dikirim: <b>{counts.get('sending',0)}</b>\n"
        f"📋 Belum diproses: <b>{counts.get('pending',0)}</b>\n"
    )
    if job.get('created_at') and job.get('updated_at'):
        endpoint = datetime.now(timezone.utc) if job['status'] in ('running','paused','cancelling') else job['updated_at']
        duration = max(0, int((endpoint-job['created_at']).total_seconds()))
        text += f"⏳ Durasi (termasuk jeda): {duration//60} menit {duration%60} detik\n"
    runtime = _blast_tasks.get(job['id'])
    if runtime and not runtime.done():
        # Session metrics are live estimates, not a promise about Telegram delivery time.
        metric = getattr(runtime, 'blast_metric', None)
        if metric:
            elapsed = max(.1, _time.monotonic()-metric['started'])
            speed = max(0, processed-metric['baseline'])/elapsed
            text += f"\n⚡ Kecepatan sesi: {speed:.1f} user/detik"
            if speed and job['status']=='running' and not wait:
                text += f"\n⏱ Estimasi sisa: {math.ceil((total-processed)/speed)} detik"
    text += f"\n🕒 Update: {now_wib().strftime('%H:%M:%S WIB')}"
    if job.get('error'):
        text += f"\n⚠️ {esc(job['error'])}"
    if counts.get('uncertain'):
        text += '\n\nHasil belum pasti tidak dikirim ulang otomatis untuk mencegah pesan dobel.'
    buttons = []
    if job['status']=='running':
        buttons.append(styled_button('⏸ Jeda',style='primary',callback_data=f"blastctl|pause|{job['id']}"))
    if job['status']=='paused' and not job.get('lease_live'):
        buttons.append(styled_button('▶️ Lanjutkan',style='success',callback_data=f"blastctl|resume|{job['id']}"))
    if job['status'] in ('running','paused'):
        buttons.append(styled_button('⛔ Batalkan',style='danger',callback_data=f"blastctl|cancel|{job['id']}"))
    keyboard = [buttons] if buttons else []
    keyboard.append([styled_button('🔄 Refresh Status',style='primary',callback_data=f"blastctl|refresh|{job['id']}")])
    if job['status'] in ('completed','cancelled'):
        keyboard.append([InlineKeyboardButton('📢 Blast Baru', callback_data='admpanel_blast')])
    keyboard.append([InlineKeyboardButton('⬅️ Panel Admin',callback_data='admpanel_back')])
    return text, InlineKeyboardMarkup(keyboard)


async def _blast_publish(bot, job, force_new=False, destination=None):
    text, keyboard = _blast_panel(job)
    chat_id = destination or job['admin_id']
    if job.get('message_id') and not force_new and chat_id==job['admin_id']:
        try:
            await asyncio.wait_for(bot.edit_message_text(
                chat_id=chat_id,message_id=job['message_id'],text=text,
                parse_mode='HTML',reply_markup=keyboard), timeout=8)
        except telegram.error.BadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return
            if 'message to edit not found' not in str(exc).lower():
                raise
        else:
            return
    msg = await asyncio.wait_for(bot.send_message(chat_id=chat_id,text=text,
        parse_mode='HTML',reply_markup=keyboard), timeout=8)
    if chat_id==job['admin_id']:
        await run_db_admin(_blast_message_sync, job['id'], msg.message_id)


class _BlastPacer:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_at = 0.0
        self.blocked_until = 0.0

    async def wait(self, state):
        async with self.lock:
            while not state['stop']:
                delay = max(self.next_at,self.blocked_until)-_time.monotonic()
                if delay<=0:
                    self.next_at = _time.monotonic()+1.0/BLAST_RATE
                    return True
                await asyncio.sleep(min(delay,.25))
        return False


def _blast_send_options(bot):
    options = dict(read_timeout=15,write_timeout=15,connect_timeout=10,pool_timeout=5)
    if getattr(bot,'rate_limiter',None) is not None:
        # AIORateLimiter uses an integer override; handle 429 ourselves, once.
        options['rate_limit_args'] = 0
    return options


async def _blast_worker_inner(bot, job_id, token, body, state, pacer):
    while not state['stop']:
        if not await pacer.wait(state):
            return
        row = await run_db_admin(_blast_claim_sync,job_id,token)
        if not row:
            await asyncio.sleep(.25)
            continue
        uid = row['user_id']
        status, error, message_id, delay = 'uncertain', None, None, 0
        try:
            msg = await asyncio.wait_for(bot.send_message(chat_id=uid,text=body,
                parse_mode='HTML',**_blast_send_options(bot)),timeout=BLAST_SEND_TIMEOUT)
            status, message_id = 'sent', msg.message_id
        except telegram.error.RetryAfter as exc:
            delay = exc.retry_after.total_seconds() if isinstance(exc.retry_after,timedelta) else float(exc.retry_after)
            delay = max(1,delay)+.5
            pacer.blocked_until = max(pacer.blocked_until,_time.monotonic()+delay)
            status = 'pending' if row['attempts']<3 else 'failed'
            error = 'Batas Telegram; retry terjadwal' if status=='pending' else 'Batas Telegram berulang (3 percobaan)'
        except telegram.error.Forbidden:
            status, error = 'skipped', 'Bot diblokir atau chat tidak dapat diakses'
        except telegram.error.BadRequest as exc:
            status, error = 'failed', str(exc)
        except (asyncio.TimeoutError, telegram.error.NetworkError) as exc:
            error = f'Hasil belum pasti: {type(exc).__name__}'
        except asyncio.CancelledError:
            # Leave durable sending marker; coordinator/recovery changes it to uncertain.
            raise
        except Exception as exc:
            error = f'Hasil belum pasti: {type(exc).__name__}'
        # A DB error here stops the job; never blindly resend an unrecorded success.
        await run_db_admin(_blast_result_sync,job_id,token,uid,status,error,message_id,delay)
        if status == 'uncertain':
            state['network_errors'] = state.get('network_errors', 0) + 1
            if state['network_errors'] >= 5:
                state['problem'] = 'Gangguan koneksi berulang. Blast dijeda untuk mencegah hasil tidak pasti bertambah.'
                state['stop'] = True
        elif status == 'sent':
            state['network_errors'] = 0


async def _blast_worker(bot, job_id, token, body, state, pacer):
    try:
        await _blast_worker_inner(bot, job_id, token, body, state, pacer)
    except asyncio.CancelledError:
        raise
    except Exception:
        state['stop'] = True
        state['problem'] = 'Gangguan database/proses. Periksa koneksi lalu lanjutkan sisa antrean.'
        raise


async def _run_broadcast(bot, job_id, token):
    state = {'stop':False}
    workers = []
    problem = None
    try:
        job = await run_db_admin(_blast_snapshot_sync,job_id)
        if not job or job['lease_token']!=token:
            return
        counts=job['counts']
        asyncio.current_task().blast_metric = dict(started=_time.monotonic(),
            baseline=sum(counts.get(s,0) for s in ('sent','failed','skipped','uncertain')))
        pacer = _BlastPacer()
        pacer.blocked_until = _time.monotonic()+float(job.get('wait_seconds') or 0)
        workers = [asyncio.create_task(_blast_worker(bot,job_id,token,job['body'],state,pacer))
                   for _ in range(BLAST_WORKERS)]
        next_report=0
        while True:
            if state['stop']:
                problem = state.get('problem')
                break
            current = await run_db_admin(_blast_heartbeat_sync,job_id,token)
            if not current or current['status']!='running':
                break
            for task in workers:
                if task.done():
                    task.result()  # Propagate DB/worker failures to the coordinator.
                    raise RuntimeError('Worker berhenti sebelum antrean selesai')
            job = await run_db_admin(_blast_snapshot_sync,job_id)
            if not job['counts'].get('pending') and not job['counts'].get('sending'):
                break
            if _time.monotonic()>=next_report:
                next_report=_time.monotonic()+4
                try:
                    await _blast_publish(bot,job)
                except Exception:
                    logger.warning('[BLAST] Panel gagal diperbarui; /blast_status tetap tersedia',exc_info=True)
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        problem='Bot berhenti. Lanjutkan sisa antrean dari panel blast.'
        raise
    except Exception:
        problem='Gangguan proses/database. Blast dijeda; buka /blast_status.'
        logger.exception('[BLAST] Runner gagal; hentikan klaim baru')
    finally:
        state['stop']=True
        # Allow requests already in flight to finish and persist their outcomes.
        if workers:
            try:
                await asyncio.wait_for(asyncio.gather(*workers,return_exceptions=True),timeout=BLAST_SEND_TIMEOUT+20)
            except (asyncio.TimeoutError,asyncio.CancelledError):
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers,return_exceptions=True)
        try:
            await run_db_admin(_blast_release_sync,job_id,token,problem)
            job = await run_db_admin(_blast_snapshot_sync,job_id)
            if job:
                await _blast_publish(bot,job)
        except Exception:
            logger.exception('[BLAST] Finalisasi gagal; lease recovery akan menjaga antrean')
        finally:
            async with _get_blast_tasks_lock():
                if _blast_tasks.get(job_id) is asyncio.current_task():
                    _blast_tasks.pop(job_id,None)


async def _start_blast_runner(bot,job_id):
    async with _get_blast_tasks_lock():
        existing=_blast_tasks.get(job_id)
        if existing and not existing.done():
            return False
        token=uuid.uuid4().hex
        if not await run_db_admin(_blast_acquire_sync,job_id,token):
            return False
        _blast_tasks[job_id]=asyncio.create_task(_run_broadcast(bot,job_id,token),name=f'blast-{job_id}')
        return True


async def _blast_admin_allowed(update,context):
    if not update.effective_chat or update.effective_chat.type!='private':
        if update.callback_query:
            await _fast_callback_ack(update.callback_query,'Buka panel lewat chat pribadi bot.')
        return False
    if not await is_admin(update.effective_user.id,context):
        if update.callback_query:
            await _fast_callback_ack(update.callback_query,'Khusus admin.')
        return False
    return True


@admin_callback
async def cmd_blast_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _blast_admin_allowed(update,context):
        return
    if update.callback_query:
        await _fast_callback_ack(update.callback_query)
    await _ensure_blast_schema()
    job=await run_db_admin(_blast_snapshot_sync)
    if not job:
        await update.effective_message.reply_text('Belum ada riwayat blast. Ketik /blast untuk mulai.')
        return
    await _blast_publish(context.bot,job,force_new=True,destination=update.effective_user.id)


async def cmd_blast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _blast_admin_allowed(update,context):
        return
    if update.callback_query:
        await _fast_callback_ack(update.callback_query)
    await _ensure_blast_schema()
    job=await run_db_admin(_blast_snapshot_sync)
    if job and job['status'] in ('running','paused','cancelling'):
        await _blast_publish(context.bot,job,force_new=True,destination=update.effective_user.id)
        return
    context.user_data['blast_state']={'step':'typing'}
    await update.effective_message.reply_text(
        '📢 <b>BROADCAST PESAN</b>\n\nKirim teks yang mau di-blast. Mendukung HTML.\n'
        'Penerima: semua user unik yang pernah membuat order, kecuali yang diban admin.\n\n'
        'Pesan akan ditampilkan untuk preview sebelum dikirim.',parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Batal',callback_data='blast_batal')]]))


@admin_callback
async def blast_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _blast_admin_allowed(update,context):
        return
    await _fast_callback_ack(update.callback_query)
    context.user_data.pop('blast_state',None)
    await update.callback_query.edit_message_text('✅ Draft blast dibatalkan. Blast yang sudah berjalan dapat dikontrol lewat /blast_status.')


async def _blast_preview(update,context,text):
    # Validate the actual message with Telegram in admin chat only; never probe recipients.
    if not await _blast_admin_allowed(update,context):
        return
    if len(text)>4096:
        await update.effective_message.reply_text('⚠️ Pesan terlalu panjang. Maksimal 4096 karakter, termasuk kode HTML pada editor ini.')
        return
    try:
        await update.effective_message.reply_text(text,parse_mode='HTML')
    except telegram.error.BadRequest:
        await update.effective_message.reply_text('⚠️ Format HTML tidak valid atau pesan terlalu panjang. Perbaiki lalu kirim lagi.')
        return
    nonce=uuid.uuid4().hex
    context.user_data['blast_state']={'step':'preview','text':text,'nonce':nonce}
    await update.effective_message.reply_text('📋 Pesan di atas adalah preview. Kirim ke seluruh target blast?',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('✅ Kirim Sekarang',callback_data=f'blast_confirm|{nonce}'),
             InlineKeyboardButton('✍️ Ubah Pesan',callback_data='blast_retype')],
            [InlineKeyboardButton('❌ Batalkan',callback_data='blast_batal')]]))


@admin_callback
async def blast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _blast_admin_allowed(update,context):
        return
    query=update.callback_query
    await _fast_callback_ack(query,'⏳ Menyiapkan antrean...')
    draft=context.user_data.get('blast_state') or {}
    nonce=query.data.partition('|')[2]
    if draft.get('step')!='preview' or not nonce or nonce!=draft.get('nonce'):
        await _callback_feedback(query,context,'Preview sudah tidak aktif. Buka /blast atau /blast_status.')
        return
    await _ensure_blast_schema()
    job_id,created=await run_db_admin(_blast_create_sync,query.from_user.id,draft['text'],nonce)
    context.user_data.pop('blast_state',None)
    job=await run_db_admin(_blast_snapshot_sync,job_id)
    # Save the initial panel before starting the reporter to avoid duplicate status bubbles.
    await _blast_publish(context.bot,job,force_new=True,destination=query.from_user.id)
    if created and job['status']=='paused':
        await _start_blast_runner(context.bot,job_id)


@admin_callback
async def blast_control(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _blast_admin_allowed(update,context):
        return
    query=update.callback_query
    await _fast_callback_ack(query)
    parts=query.data.split('|')
    if len(parts)!=3 or parts[1] not in ('pause','resume','cancel','refresh') or not parts[2].isdigit():
        return
    job_id=int(parts[2])
    await _ensure_blast_schema()
    if parts[1]=='resume':
        await _start_blast_runner(context.bot,job_id)
    elif parts[1] in ('pause','cancel'):
        await run_db_admin(_blast_control_sync,job_id,parts[1])
    job=await run_db_admin(_blast_snapshot_sync,job_id)
    if job:
        text,markup=_blast_panel(job)
        try:
            await query.edit_message_text(text,parse_mode='HTML',reply_markup=markup)
        except telegram.error.BadRequest as exc:
            if 'message is not modified' not in str(exc).lower():
                raise


@admin_callback
async def blast_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Old buttons have no job identity: never let one cancel an unrelated new blast.
    if not await _blast_admin_allowed(update,context):
        return
    await _fast_callback_ack(update.callback_query)
    await _callback_feedback(update.callback_query,context,'Tombol lama sudah tidak berlaku. Buka /blast_status untuk mengontrol blast aktif.')


# =================== GENERAL MESSAGE HANDLER ===================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    text = update.message.text.strip() if update.message.text else ""

    if context.user_data.get('awaiting_review_text'):
        context.user_data.pop('awaiting_review_text', None)
        context.user_data.pop('temp_rating', None)
        await update.message.reply_text(
            "Testimoni transaksi kini dipublikasikan otomatis setelah akses berhasil dikirim. "
            "Kamu tidak perlu mengisi rating atau ulasan.")
        return

    if await is_admin(user_id, context):

        blast_state = context.user_data.get('blast_state')
        if blast_state and blast_state.get('step') == 'typing':
            if not text:
                await update.message.reply_text("❌ Pesan tidak boleh kosong.")
                return
            await _blast_preview(update, context, text)
            return

        if context.user_data.get('awaiting_cari'):
            context.user_data.pop('awaiting_cari', None)
            order_id = text.strip()
            back_orders_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Orders", callback_data="admpanel_orders")]])
            try:
                order = await get_order_by_id(order_id)
            except Exception as e:
                await update.message.reply_text(f"❌ Gagal mengambil data order: {esc(str(e))}", parse_mode="HTML", reply_markup=back_orders_kb)
                return
            if not order:
                await update.message.reply_text(f"❌ Order tidak ditemukan.\n\nID yang dicari: <code>{esc(order_id)}</code>", parse_mode="HTML", reply_markup=back_orders_kb)
                return
            paket = await get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0}
            detail_text = await build_order_detail_text(order, paket)
            await update.message.reply_text(
                detail_text,
                parse_mode="HTML",
                reply_markup=back_orders_kb
            )
            return

        if context.user_data.get('awaiting_ban'):
            context.user_data.pop('awaiting_ban', None)
            parts = text.split(None, 1)
            try:
                target_id = int(parts[0])
            except (ValueError, IndexError):
                await update.message.reply_text("❌ Format salah. Contoh: <code>123456789 spam</code>", parse_mode="HTML")
                return
            if await is_admin(target_id, context):
                await update.message.reply_text("❌ Tidak bisa ban sesama admin.")
                return
            reason = parts[1] if len(parts) > 1 else "Tidak ada alasan"
            await ban_user(target_id, reason)
            await update.message.reply_text(
                f"🚫 <b>User Berhasil Dibanned</b>\n\n"
                f"👤 User ID: <code>{target_id}</code>\n"
                f"📝 Alasan: {esc(reason)}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Kelola User", callback_data="admpanel_user")]])
            )
            return

        if context.user_data.get('awaiting_unban'):
            context.user_data.pop('awaiting_unban', None)
            try:
                target_id = int(text.strip())
            except ValueError:
                await update.message.reply_text("❌ User ID harus berupa angka.", parse_mode="HTML")
                return
            if not await is_banned(target_id):
                await update.message.reply_text(f"⚠️ User <code>{target_id}</code> tidak ada dalam daftar ban.", parse_mode="HTML")
                return
            await unban_user(target_id)
            await update.message.reply_text(
                f"✅ <b>User Berhasil Di-unban</b>\n\n👤 User ID: <code>{target_id}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Kelola User", callback_data="admpanel_user")]])
            )
            return

        if context.user_data.get('awaiting_kick_search'):
            context.user_data.pop('awaiting_kick_search', None)
            query_str = text.strip()
            results = await search_buyers_sync(query_str)
            if not results:
                await update.message.reply_text(
                    f"❌ Tidak ditemukan buyer dengan nama/ID <b>{esc(query_str)}</b>.\n\n"
                    "<i>Coba cari ulang atau periksa ejaan.</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Cari Lagi", callback_data="kick_cek_user"),
                                                        styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_kick")]])
                )
                return
            buttons = []
            for r in results:
                nama = r.get('user_name') or str(r['user_id'])
                buttons.append([InlineKeyboardButton(
                    f"👤 {nama} ({r['user_id']})",
                    callback_data=f"kick_select|{r['user_id']}"
                )])
            buttons.append([InlineKeyboardButton("🔄 Cari Lagi", callback_data="kick_cek_user"),
                            styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_kick")])
            await update.message.reply_text(
                f"🔍 <b>Hasil Pencarian: \"{esc(query_str)}\"</b>\n"
                f"========================\n\n"
                f"Ditemukan <b>{len(results)}</b> buyer. Pilih salah satu:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return

        if context.user_data.get('awaiting_add_group'):
            context.user_data.pop('awaiting_add_group', None)
            gid_str = text.strip()
            if not gid_str.lstrip('-').isdigit():
                await update.message.reply_text("❌ Chat ID tidak valid. Harus berupa angka, contoh: <code>-1001234567890</code>", parse_mode="HTML")
                return
            managed_groups = await get_managed_groups()
            if gid_str in [str(g) for g in managed_groups]:
                await update.message.reply_text(f"⚠️ Grup <code>{gid_str}</code> sudah ada dalam daftar.", parse_mode="HTML")
                return
            managed_groups.append(int(gid_str))
            await set_managed_groups(managed_groups)
            await update.message.reply_text(
                f"✅ Grup <code>{gid_str}</code> berhasil ditambahkan.\n"
                f"Total grup terdaftar: <b>{len(managed_groups)}</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Menu Kick", callback_data="admpanel_kick")]])
            )
            return

        if context.user_data.get('awaiting_channel_id'):
            context.user_data.pop('awaiting_channel_id', None)
            val = text.strip()
            back_setting_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Pengaturan", callback_data="admpanel_setting")]])
            if val.lower() == 'hapus':
                await set_setting('notif_channel_id', None)
                await update.message.reply_text("✅ Channel notifikasi berhasil <b>dinonaktifkan</b>.", parse_mode="HTML", reply_markup=back_setting_kb)
            else:
                if not val.lstrip('-').isdigit():
                    await update.message.reply_text(
                        "❌ Format channel ID tidak valid.\n"
                        "Contoh: <code>-1001234567890</code>\n"
                        "Ketik <code>hapus</code> untuk menonaktifkan.",
                        parse_mode="HTML"
                    )
                    return
                await set_setting('notif_channel_id', val)
                try:
                    await context.bot.send_message(
                        chat_id=int(val),
                        text=(
                            f"✅ <b>Channel notifikasi aktif!</b>\n\n"
                            f"Bot Hyper Family Store akan mengirim update status order ke channel ini.\n"
                            f"🕒 {now_wib().strftime('%H:%M, %d/%m/%Y')}"
                        ),
                        parse_mode="HTML"
                    )
                    await update.message.reply_text(
                        f"✅ <b>Channel ID berhasil disimpan!</b>\n\nID: <code>{esc(val)}</code>\n\nPesan test sudah dikirim ke channel.",
                        parse_mode="HTML",
                        reply_markup=back_setting_kb
                    )
                except Exception as e:
                    await update.message.reply_text(
                        f"⚠️ <b>Channel ID disimpan, tapi gagal kirim pesan test.</b>\n\n"
                        f"Error: <code>{esc(str(e))}</code>\n\n"
                        f"Pastikan bot sudah dijadikan <b>admin</b> di channel tersebut.",
                        parse_mode="HTML",
                        reply_markup=back_setting_kb
                    )
            return

        if context.user_data.get('awaiting_testi_channel_id'):
            context.user_data.pop('awaiting_testi_channel_id', None)
            val = text.strip()
            back_setting_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Pengaturan", callback_data="admpanel_setting")]])
            if val.lower() == 'hapus':
                await set_setting('testimoni_channel_id', None)
                await update.message.reply_text("✅ Channel testimoni berhasil <b>dinonaktifkan</b>.", parse_mode="HTML", reply_markup=back_setting_kb)
            else:
                if not val.lstrip('-').isdigit():
                    await update.message.reply_text("❌ Format ID channel tidak valid. Contoh: <code>-1001234567890</code>.\nKetik <code>hapus</code> untuk menonaktifkan.", parse_mode="HTML")
                    return
                try:
                    await context.bot.send_message(
                        chat_id=int(val),
                        text="💠 <b>Channel Testimoni Transaksi</b>\n\nKoneksi berhasil. Bukti transaksi dipublikasikan otomatis setelah pembayaran valid dan akses berhasil dikirim, tanpa nama atau username buyer.",
                        parse_mode="HTML"
                    )
                    await set_setting('testimoni_channel_id', val)
                    await update.message.reply_text(
                        f"✅ <b>Channel Testimoni berhasil disimpan & diverifikasi!</b>\n\nID: <code>{esc(val)}</code>\n\nPesan uji coba telah terkirim.",
                        parse_mode="HTML",
                        reply_markup=back_setting_kb
                    )
                except Exception as e:
                    await update.message.reply_text(
                        f"⚠️ <b>Gagal menghubungkan channel.</b>\n\n"
                        f"Error: <code>{esc(str(e))}</code>\n\n"
                        f"Pastikan bot sudah ditambahkan sebagai <b>Administrator</b> di channel tersebut.",
                        parse_mode="HTML",
                        reply_markup=back_setting_kb
                    )
            return

        if context.user_data.get('awaiting_add_admin'):
            context.user_data.pop('awaiting_add_admin', None)
            if not is_super_admin(user_id):
                return
            fwd = update.message.forward_from
            if fwd:
                new_id = fwd.id
                new_name = fwd.full_name or str(new_id)
            else:
                try:
                    new_id = int(text.strip())
                    new_name = str(new_id)
                except ValueError:
                    await update.message.reply_text(
                        "❌ Forward pesan dari user yang ingin dijadikan admin, atau kirim <b>User ID</b> (angka).",
                        parse_mode="HTML"
                    )
                    return
            if is_super_admin(new_id):
                await update.message.reply_text("ℹ️ Itu adalah akun super admin.")
                return
            await add_admin(new_id, new_name, added_by=user_id)
            invalidate_admin_cache(context, new_id)
            await update.message.reply_text(
                f"✅ <b>Admin berhasil ditambahkan!</b>\n\n👤 {esc(new_name)} (<code>{new_id}</code>)",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Kelola Admin", callback_data="admpanel_admins")]])
            )
            return

        if context.user_data.get('awaiting_link_testi'):
            context.user_data.pop('awaiting_link_testi', None)
            val = text.strip()
            await set_setting('link_testimoni', val)
            await update.message.reply_text(
                f"✅ Link Testimoni berhasil diperbarui:\n{esc(val)}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Pengaturan", callback_data="admpanel_setting")]])
            )
            return

        if context.user_data.get('awaiting_link_admin'):
            context.user_data.pop('awaiting_link_admin', None)
            val = text.strip()
            await set_setting('link_admin', val)
            await update.message.reply_text(
                f"✅ Link Admin/CS berhasil diperbarui:\n{esc(val)}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Pengaturan", callback_data="admpanel_setting")]])
            )
            return

        if context.user_data.get('awaiting_main_group_name'):
            context.user_data.pop('awaiting_main_group_name', None)
            val = text.strip()
            if not val or len(val) > 64 or any(ord(ch) < 32 for ch in val):
                await update.message.reply_text(
                    "❌ Nama tombol harus 1–64 karakter dan tidak boleh mengandung baris baru."
                )
                return
            await set_setting('main_group_button_name', val)
            await update.message.reply_text(
                f"✅ Nama tombol grup diperbarui menjadi:\n<b>{esc(val)}</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[styled_button(
                    "⬅️ Kembali ke Pengaturan", style="primary", callback_data="admpanel_setting"
                )]])
            )
            return

        if context.user_data.get('awaiting_main_group_link'):
            context.user_data.pop('awaiting_main_group_link', None)
            val = text.strip()
            if val.lower() == 'hapus':
                await set_setting('main_group_button_link', '')
                await update.message.reply_text(
                    "✅ Tombol grup disembunyikan dari menu utama.",
                    reply_markup=InlineKeyboardMarkup([[styled_button(
                        "⬅️ Kembali ke Pengaturan", style="primary", callback_data="admpanel_setting"
                    )]])
                )
                return
            val = normalize_button_url(val)
            if not val:
                await update.message.reply_text(
                    "❌ Link tidak valid. Gunakan <code>https://t.me/...</code>, "
                    "<code>tg://...</code>, atau <code>@username</code>.",
                    parse_mode="HTML"
                )
                return
            await set_setting('main_group_button_link', val)
            await update.message.reply_text(
                f"✅ Link tombol grup diperbarui:\n<code>{esc(val)}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[styled_button(
                    "⬅️ Kembali ke Pengaturan", style="primary", callback_data="admpanel_setting"
                )]])
            )
            return

        adding = context.user_data.get('adding_product')
        if adding:
            step = adding.get('step')

            if step == 'nama':
                adding['nama'] = text
                adding['step'] = 'emoji'
                await update.message.reply_text(
                    "<b>➕ TAMBAH PRODUK BARU</b>\n"
                    "========================\n\n"
                    "Langkah 2/4\n\n"
                    f"Nama: <b>{esc(text)}</b>\n\n"
                    "<i>Kirim <b>emoji</b> untuk produk ini (contoh: 🔥):</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Batal", callback_data="pd_tambah_batal")]
                    ])
                )
                return

            if step == 'emoji':
                adding['emoji'] = text
                adding['step'] = 'deskripsi'
                await update.message.reply_text(
                    "<b>➕ TAMBAH PRODUK BARU</b>\n"
                    "========================\n\n"
                    "Langkah 3/4\n\n"
                    f"Nama: <b>{esc(adding['nama'])}</b>\n"
                    f"Emoji: {esc(text)}\n\n"
                    "<i>Kirim <b>deskripsi</b> produk (contoh: 500+ Video Premium):</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Batal", callback_data="pd_tambah_batal")]
                    ])
                )
                return

            if step == 'deskripsi':
                adding['deskripsi'] = text
                adding['step'] = 'harga'
                await update.message.reply_text(
                    "<b>➕ TAMBAH PRODUK BARU</b>\n"
                    "========================\n\n"
                    "Langkah 4/4\n\n"
                    f"Nama: <b>{esc(adding['nama'])}</b>\n"
                    f"Emoji: {esc(adding['emoji'])}\n"
                    f"Deskripsi: {esc(text)}\n\n"
                    "<i>Kirim <b>harga</b> (angka saja, contoh: 15000):</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Batal", callback_data="pd_tambah_batal")]
                    ])
                )
                return

            if step == 'harga':
                if not text.isdigit():
                    await update.message.reply_text("❌ Harga harus berupa angka. Coba lagi:")
                    return

                harga = int(text)
                nama = adding['nama']
                emoji = adding['emoji']
                deskripsi = adding['deskripsi']
                paket_id = make_paket_id(nama)

                existing = await get_product(paket_id)
                if existing:
                    paket_id = f"{paket_id}_{int(now_wib().timestamp())}"

                await add_product(paket_id, nama, emoji, deskripsi, harga)
                context.user_data.pop('adding_product', None)

                await update.message.reply_text(
                    f"✅ Produk berhasil ditambahkan!\n\n"
                    f"{esc(emoji)} <b>{esc(nama)}</b>\n"
                    f"📝 {esc(deskripsi)}\n"
                    f"💰 {format_harga(harga)}\n\n"
                    f"Jangan lupa set link-nya lewat /produk.",
                    parse_mode="HTML"
                )
                await _send_produk_menu(context, chat_id=user_id)
                return

        editing = context.user_data.get('editing_product')
        if editing:
            paket_id = editing['paket_id']
            field = editing['field']

            if field == 'harga' and not text.isdigit():
                await update.message.reply_text("❌ Harga harus berupa angka. Coba lagi:")
                return

            if field in ('group_chat_id', 'requires_paket_ids'):
                value = text.strip() if text.strip() and text.strip().lower() != 'hapus' else None
            else:
                value = int(text) if field == 'harga' else text

            await update_product_field(paket_id, field, value)
            context.user_data.pop('editing_product', None)

            await update.message.reply_text(
                f"✅ <b>{esc(field.capitalize())}</b> berhasil diupdate!\nNilai baru: <code>{esc(str(value) if value else '(kosong)')}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")
                ]])
            )
            p = await get_product(paket_id)
            if p:
                await _send_produk_menu(context, chat_id=user_id)
            return

# =================== ADMIN: LINK ===================

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    products = await get_all_products()
    text = "<b>🔗 LINK PRODUK SAAT INI</b>\n========================\n\n"
    for p in products:
        grp = p.get('group_chat_id')
        if grp:
            text += f"{esc(p['emoji'])} <b>{esc(p['nama'])}</b>\n- 🏢 Group: <code>{esc(grp)}</code>\n- 🔗 Fallback: <code>{esc(p['link'])}</code>\n\n"
        else:
            text += f"{esc(p['emoji'])} <b>{esc(p['nama'])}</b>\n- <code>{esc(p['link'])}</code>\n\n"
    text += "<i>Ketik /produk untuk mengubah link atau Group ID.</i>"
    await update.message.reply_text(text, parse_mode="HTML")

# =================== ADMIN: PENDING ORDERS ===================


async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return

    text, keyboard = await _build_pending_orders_text_and_keyboard(0, back_callback="admpanel_orders")
    if not text:
        await update.message.reply_text(
            "<b>✅ TIDAK ADA ORDER PENDING</b>\n========================",
            parse_mode="HTML"
        )
        return

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

def _get_pending_order_sync(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE user_id=%s AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,))
            return c.fetchone()

@admin_callback
async def admin_proses_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.replace("proses_", ""))

    order = await run_db(_get_pending_order_sync, user_id)
    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah diproses.")
        return

    order = dict(order)
    paket = await get_product(order["paket_id"]) or {"emoji": "📦", "nama": order["paket_id"], "harga": 0, "deskripsi": "-"}
    trans = await get_transaction_detail(order["order_id"], paket["harga"]) if order["order_id"] else None
    durasi = hitung_durasi(order["waktu"])

    caption = (
        f"<b>{esc(paket['emoji'])} {esc(paket['nama']).upper()}</b>\n"
        f"========================\n\n"
        f"👤 Pembeli: {esc(order['user_name'])} (<code>{order['user_id']}</code>)\n"
        f"📦 Konten: {esc(paket['deskripsi'])}\n"
        f"💰 Total: {format_harga(paket['harga'])}\n"
        f"🕒 Dibuat: {durasi}\n"
    )
    if trans:
        caption += f"\n📝 Order ID: <code>{esc(order['order_id'])}</code>\n"
        caption += f"💳 Status: {'✅ Lunas' if trans['status'] == 'completed' else '⏳ Belum Bayar'}\n"
        if trans['status'] == 'completed':
            caption += f"💵 Dibayar: {format_harga(trans.get('amount', paket['harga']))}\n"

    keyboard = [
        [
            InlineKeyboardButton("✅ Konfirmasi", callback_data=f"confirm_{user_id}"),
            styled_button("❌ Tolak", style="danger",      callback_data=f"reject_{user_id}"),
        ],
        [styled_button("⬅️ Kembali", style="primary", callback_data="back_orders")]
    ]

    # Satu edit lebih cepat daripada delete + send (dua round-trip Telegram).
    await telegram_retry(
        lambda: query.edit_message_text(
            caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
        ),
        label="admin detail pending",
    )

@admin_callback
async def back_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)

    text, keyboard = await _build_pending_orders_text_and_keyboard(0, back_callback="admpanel_back")
    if not text:
        await query.edit_message_text(
            "✅ Tidak ada order pending saat ini.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")]])
        )
        return

    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await context.bot.send_message(
            chat_id=query.from_user.id, text=text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

@admin_callback
async def admin_konfirmasi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action = parts[0]
    user_id = int(parts[1])

    order = await run_db(_get_pending_order_sync, user_id)
    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah diproses.")
        return

    order = dict(order)
    paket = await get_product(order["paket_id"]) or {"emoji": "📦", "nama": order["paket_id"], "harga": 0, "link": DEFAULT_LINK}

    if action == "confirm":
        ok = await mark_order_completed(order["order_id"], expected_status="pending")
        if not ok:
            await query.edit_message_text("⚠️ Order sudah diproses sebelumnya (mungkin oleh webhook).")
            return

        await _process_completed_order_delivery(
            context.bot,
            order_id=order["order_id"],
            paket_id=order["paket_id"],
            user_id=user_id,
            user_name=order.get('user_name', '-'),
            paid_amount=order.get('harga_dibayar') or paket.get('harga', 0),
            source_title="✅ <b>ORDER SELESAI (KONFIRMASI MANUAL)</b>",
            paket=paket,
        )

        refreshed = await get_order_by_id(order["order_id"])
        delivery_status = (refreshed or {}).get('delivery_status', 'processed')
        delivery_label = {
            'sent': '✅ Link terkirim',
            'held': '⏸️ Link ditahan karena syarat belum terpenuhi',
            'failed': '⚠️ Link gagal dikirim, cek manual',
            'pending': '⏳ Pengiriman sedang diproses',
        }.get(delivery_status, '✅ Diproses')

        await query.edit_message_text(
            f"✅ <b>Dikonfirmasi</b>\n"
            f"========================\n\n"
            f"👤 Pembeli: {tg_user_link(user_id, order.get('user_name', '-'))}\n"
            f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n\n"
            f"🔗 Status: {delivery_label}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")]])
        )

    elif action == "reject":
        if await update_order_status(order["order_id"], 'rejected') is None:
            await _callback_feedback(query, context, "Status pesanan sudah berubah; penolakan dihentikan.")
            return
        await hapus_qris_buyer_lama(context.bot, order["order_id"], user_id)
        await hapus_notif_lama(context.bot, order["order_id"])

        # === SET COOLDOWN SAAT ADMIN REJECT ORDER ===
        # [DESKRIPSI: Reject order juga set cooldown ke buyer, konsisten dengan cancel]
        await set_cooldown_db(user_id)

        msg_id = await kirim_notif(
            context.bot,
            _format_order_notif(
                "❌ <b>ORDER DITOLAK</b>",
                order['user_name'], user_id, paket, order['order_id']
            )
        )
        if msg_id:
            await set_admin_msg_id(order["order_id"], msg_id)

        await query.edit_message_text(
            f"❌ <b>Ditolak</b>\n"
            f"========================\n\n"
            f"👤 Pembeli: {esc(order['user_name'])}\n"
            f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")]])
        )
        msg = await context.bot.send_message(
            chat_id=user_id,
            text=(
                "<b>❌ PESANAN DITOLAK</b>\n"
                "========================\n\n"
                "Maaf, pesanan Anda tidak dapat diproses.\n\n"
                "Kemungkinan penyebab:\n"
                "- Pembayaran tidak valid\n"
                "- Bukti transfer tidak sesuai\n"
                "- Produk tidak tersedia\n\n"
                f"⏳ Kamu bisa membuat order baru dalam <b>{COOLDOWN_MENIT} menit</b>.\n\n"
                "Ketik /start untuk mencoba lagi atau hubungi admin."
            ),
            parse_mode="HTML"
        )
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        await hapus_admin_msg(context, user_id)
        
        # =================== ADMIN: PANEL UTAMA ===================

def build_admin_panel_keyboard(user_id: int = None):
    rows = [
        [
            InlineKeyboardButton("📦 Produk",        callback_data="admpanel_produk"),
            InlineKeyboardButton("📋 Orders",         callback_data="admpanel_orders"),
        ],
        [
            InlineKeyboardButton("📊 Statistik",      callback_data="admpanel_stats"),
            InlineKeyboardButton("📢 Broadcast",      callback_data="admpanel_blast"),
            InlineKeyboardButton("📊 Status Blast", callback_data="admpanel_blast_status"),
        ],
        [
            InlineKeyboardButton("💾 Data & Backup",  callback_data="admpanel_data"),
            InlineKeyboardButton("🚫 Kelola User",    callback_data="admpanel_user"),
        ],
        [
            InlineKeyboardButton("👢 Cek & Kick Grup", callback_data="admpanel_kick"),
            InlineKeyboardButton("⚙️ Pengaturan",     callback_data="admpanel_setting"),
        ],
    ]
    if user_id and is_super_admin(user_id):
        rows.append([InlineKeyboardButton("👥 Kelola Admin", callback_data="admpanel_admins")])
    return InlineKeyboardMarkup(rows)

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not await is_admin(uid, context):
        return
    await update.message.reply_text(
        "<b>⚙️ PANEL ADMIN</b>\n"
        "========================\n\n"
        "Pilih menu yang ingin dibuka:",
        parse_mode="HTML",
        reply_markup=build_admin_panel_keyboard(uid)
    )

@admin_callback
async def admpanel_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "<b>⚙️ PANEL ADMIN</b>\n"
        "========================\n\n"
        "Pilih menu yang ingin dibuka:",
        parse_mode="HTML",
        reply_markup=build_admin_panel_keyboard(query.from_user.id)
    )

@admin_callback
async def admpanel_produk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _send_produk_menu(context, chat_id=query.from_user.id, query=query)

@admin_callback
async def admpanel_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏳ Order Aktif",   callback_data="admpanel_orders_aktif"),
            InlineKeyboardButton("🔄 Order Pending", callback_data="admpanel_orders_pending"),
        ],
        [InlineKeyboardButton("🔍 Cari Order",    callback_data="admpanel_orders_cari")],
        [styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(
        "<b>📋 ORDERS</b>\n"
        "========================\n\n"
        "Pilih jenis order:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

@admin_callback
async def admpanel_orders_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)
    page = _callback_page(query.data, "admpanel_orders_aktif")

    text, keyboard = await _build_active_orders_text_and_keyboard(page)
    if not text:
        await query.edit_message_text(
            "<b>✅ TIDAK ADA ORDER AKTIF</b>\n========================\n\nTidak ada buyer yang sedang menunggu membayar.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_orders")]])
        )
        return

    keyboard.append([styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_orders")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

@admin_callback
async def admpanel_orders_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)
    page = _callback_page(query.data, "admpanel_orders_pending")

    text, keyboard = await _build_pending_orders_text_and_keyboard(page, back_callback="admpanel_orders")
    if not text:
        await query.edit_message_text(
            "<b>✅ TIDAK ADA ORDER PENDING</b>\n========================",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_orders")]])
        )
        return

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

@admin_callback
async def admpanel_orders_cari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_cari'] = True
    await query.edit_message_text(
        "<b>🔍 CARI ORDER</b>\n"
        "========================\n\n"
        "Kirim <b>Order ID</b> yang ingin dicari.\n"
        "Contoh: <code>HFB-123456789-20240101120000-ABCD</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[styled_button("❌ Batal", style="danger", callback_data="admpanel_orders")]])
    )

@admin_callback
async def admpanel_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    now = now_wib()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    s = await get_order_stats_cached(today_start, month_start)
    text = _build_stats_text(s, now)
    try:
        await query.edit_message_text(text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_back")]]))
    except Exception as e:
        logger.debug(f"Gagal mengedit stats di panel: {e}")
        await context.bot.send_message(chat_id=query.from_user.id, text=text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_back")]]))

@admin_callback
async def admpanel_blast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_blast(update, context)


@admin_callback
async def blast_retype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _blast_admin_allowed(update, context):
        return
    await _fast_callback_ack(update.callback_query)
    context.user_data['blast_state'] = {'step': 'typing'}
    await update.callback_query.edit_message_text(
        '✍️ Kirim teks blast yang baru. Akan ada preview sebelum pengiriman.',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton('❌ Batal', callback_data='blast_batal')
        ]])
    )


@admin_callback
async def admpanel_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Backup (.txt)", callback_data="admpanel_data_backup"),
            InlineKeyboardButton("📤 Export (.json)", callback_data="admpanel_data_export"),
        ],
        [InlineKeyboardButton("📥 Import JSON",    callback_data="admpanel_data_import")],
        [InlineKeyboardButton("🔗 Info Link Produk", callback_data="admpanel_data_link")],
        [styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(
        "<b>💾 DATA &amp; BACKUP</b>\n"
        "========================\n\n"
        "Pilih tindakan:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

@admin_callback
async def admpanel_data_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ Membuat backup...")
    await _kirim_backup(context.bot)
    await query.edit_message_text(
        "✅ Backup berhasil dikirim ke DM kamu.",
        reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_data")]])
    )

@admin_callback
async def admpanel_data_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ Membuat export JSON...")
    try:
        json_content, n_products, n_orders, n_banned = await _generate_json_export()
        filename = f"export_{now_wib().strftime('%Y%m%d_%H%M%S')}.json"
        buf = BytesIO(json_content.encode("utf-8"))
        buf.name = filename
        await context.bot.send_document(
            chat_id=query.from_user.id,
            document=buf,
            filename=filename,
            caption=(
                f"✅ <b>Export Berhasil</b>\n"
                f"📦 Products: {n_products}\n"
                f"📋 Orders: {n_orders}\n"
                f"🚫 Banned: {n_banned}\n"
                f"🕒 {now_wib().strftime('%H:%M, %d/%m/%Y')}"
            ),
            parse_mode="HTML"
        )
        await query.edit_message_text(
            "✅ File export berhasil dikirim.",
            reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_data")]])
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ Gagal export: {esc(str(e))}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_data")]])
        )

@admin_callback
async def admpanel_data_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_json_import'] = True
    await query.edit_message_text(
        "<b>📥 IMPORT DATA JSON</b>\n"
        "========================\n\n"
        "Kirim file <code>.json</code> yang didapat dari Export.\n\n"
        "⚠️ Data yang sudah ada <b>tidak akan dihapus</b>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[styled_button("❌ Batal", style="danger", callback_data="admpanel_data")]])
    )

@admin_callback
async def admpanel_data_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = await get_all_products()
    text = "<b>🔗 LINK PRODUK SAAT INI</b>\n========================\n\n"
    for p in products:
        grp = p.get('group_chat_id')
        if grp:
            text += f"{esc(p['emoji'])} <b>{esc(p['nama'])}</b>\n- 🏢 Group: <code>{esc(grp)}</code>\n\n"
        else:
            text += f"{esc(p['emoji'])} <b>{esc(p['nama'])}</b>\n- <code>{esc(p['link'])}</code>\n\n"
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_data")]])
    )

@admin_callback
async def admpanel_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚫 Ban User",      callback_data="admpanel_user_ban"),
            InlineKeyboardButton("✅ Unban User",    callback_data="admpanel_user_unban"),
        ],
        [InlineKeyboardButton("📋 Daftar Ban",    callback_data="admpanel_user_daftar")],
        [styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(
        "<b>🚫 KELOLA USER</b>\n"
        "========================\n\n"
        "Pilih aksi:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

@admin_callback
async def admpanel_user_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "<b>🚫 BAN USER</b>\n"
        "========================\n\n"
        "Ketik User ID dan alasan (opsional):\n"
        "Format: <code>user_id alasan</code>\n"
        "Contoh: <code>123456789 spam</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Batal", callback_data="admpanel_user")]])
    )
    context.user_data['awaiting_ban'] = True

@admin_callback
async def admpanel_user_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "<b>✅ UNBAN USER</b>\n"
        "========================\n\n"
        "Ketik User ID yang mau di-unban:\n"
        "Contoh: <code>123456789</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Batal", callback_data="admpanel_user")]])
    )
    context.user_data['awaiting_unban'] = True

@admin_callback
async def admpanel_user_daftar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query)
    page = _callback_page(query.data, "admpanel_user_daftar")
    banned, total = await get_banned_page(page, ADMIN_LIST_PAGE_SIZE)
    if not banned:
        await query.edit_message_text(
            "<b>🚫 DAFTAR BAN</b>\n========================\n\nBelum ada user yang dibanned.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_user")]])
        )
        return
    total_pages = max(1, (total + ADMIN_LIST_PAGE_SIZE - 1) // ADMIN_LIST_PAGE_SIZE)
    text = f"<b>🚫 DAFTAR BAN ({total} user)</b>\n========================\n📄 Halaman {page + 1}/{total_pages}\n\n"
    for b in banned:
        text += (
            f"👤 ID: <code>{b['user_id']}</code>\n"
            f"📝 Alasan: {esc(b['reason'] or '-')}\n"
            f"🕒 Dibanned: {b['banned_at']}\n\n"
        )
    keyboard = []
    nav = _pagination_row("admpanel_user_daftar", page, total, ADMIN_LIST_PAGE_SIZE)
    if nav:
        keyboard.append(nav)
    keyboard.append([styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_user")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# =================== ADMIN: CEK & KICK USER DI GRUP ===================

@admin_callback
async def admpanel_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await is_admin(query.from_user.id, context):
        await query.answer("⛔ Akses ditolak.", show_alert=True)
        return

    managed_groups = await get_managed_groups()

    group_lines = ""
    if managed_groups:
        async def _resolve_group_line(index, gid):
            try:
                chat = await context.bot.get_chat(int(gid))
                nama_grup = esc(chat.title or str(gid))
            except Exception as e:
                logger.debug(f"Gagal mengambil judul chat {gid}: {e}")
                nama_grup = "⚠️ Tidak dapat diakses"
            return f"  {index}. <b>{nama_grup}</b>\n     <code>{gid}</code>\n"

        group_lines = "".join(await asyncio.gather(*(
            _resolve_group_line(i, gid) for i, gid in enumerate(managed_groups, 1)
        )))
    else:
        group_lines = "  <i>Belum ada grup terdaftar.</i>\n"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Cek User di Grup", callback_data="kick_cek_user"),
            InlineKeyboardButton("➕ Tambah Grup",      callback_data="kick_add_group"),
        ],
        [InlineKeyboardButton("🗑️ Hapus Grup",          callback_data="kick_del_group")],
        [styled_button("⬅️ Kembali", style="primary",              callback_data="admpanel_back")],
    ])

    await query.edit_message_text(
        f"<b>👢 CEK &amp; KICK USER DI GRUP</b>\n"
        f"========================\n\n"
        f"Fitur ini memungkinkan kamu mengecek keberadaan user di grup dan mengeluarkannya.\n\n"
        f"<b>Grup Terdaftar ({len(managed_groups)}):</b>\n"
        f"{group_lines}\n"
        f"<i>Pastikan bot sudah menjadi admin di semua grup tersebut.</i>",
        parse_mode="HTML",
        reply_markup=keyboard
    )

@admin_callback
async def kick_cek_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_kick_search'] = True
    await query.edit_message_text(
        "<b>🔍 CEK USER DI GRUP</b>\n"
        "========================\n\n"
        "Kirim <b>nama</b> atau <b>User ID</b> yang ingin dicek.\n\n"
        "<i>Contoh nama: Budi  ·  Contoh ID: 123456789</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[styled_button("❌ Batal", style="danger", callback_data="admpanel_kick")]])
    )

@admin_callback
async def kick_select_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.debug(f"Gagal answer callback query kick_select: {e}")
    if not await is_admin(query.from_user.id, context):
        return
    target_id = int(query.data.split("|", 1)[1])
    managed_groups = await get_managed_groups()
    if not managed_groups:
        await query.answer("Tidak ada grup terdaftar.", show_alert=True)
        return

    lines = []
    group_names = {}
    for gid in managed_groups:
        try:
            chat = await context.bot.get_chat(int(gid))
            group_names[str(gid)] = chat.title or str(gid)
        except Exception as e:
            logger.debug(f"Gagal mengambil metadata chat {gid}: {e}")
            group_names[str(gid)] = str(gid)

    buttons = []
    for gid in managed_groups:
        try:
            member = await context.bot.get_chat_member(chat_id=int(gid), user_id=target_id)
            status = member.status
            if status in ('member', 'administrator', 'creator', 'restricted'):
                icon = "✅"
                status_label = status
            else:
                icon = "❌"
                status_label = status
        except Exception as e:
            logger.debug(f"Gagal mengambil data keanggotaan {target_id} di chat {gid}: {e}")
            icon = "⚠️"
            status_label = "error"
        nama_grup = esc(group_names.get(str(gid), str(gid)))
        lines.append(f"  {icon} <b>{nama_grup}</b>  <i>({status_label})</i>")
        if icon == "✅":
            buttons.append([InlineKeyboardButton(
                f"👢 Kick dari {group_names.get(str(gid), str(gid))}",
                callback_data=f"kick_one|{target_id}|{gid}"
            )])

    result_text = (
        f"🔍 <b>Status User</b> <code>{target_id}</code>\n"
        f"========================\n\n"
        + "\n".join(lines)
        + "\n\n<i>Pilih aksi di bawah:</i>"
    )
    buttons.append([InlineKeyboardButton(f"👢 Kick dari Semua Grup + Ban", callback_data=f"kick_do_kick|{target_id}")])
    buttons.append([styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_kick")])
    await query.edit_message_text(result_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

@admin_callback
async def kick_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _fast_callback_ack(query, "⏳ Memproses kick...")
    if not await is_admin(query.from_user.id, context):
        await _callback_feedback(query, context, "⛔ Akses ditolak.")
        return
    parts = query.data.split("|")
    target_id = int(parts[1])
    gid = int(parts[2])
    try:
        await context.bot.ban_chat_member(chat_id=gid, user_id=target_id)
        await context.bot.unban_chat_member(chat_id=gid, user_id=target_id, only_if_banned=True)
        try:
            chat = await context.bot.get_chat(gid)
            nama_grup = chat.title or str(gid)
        except Exception as e:
            logger.debug(f"Gagal mengambil detail chat {gid}: {e}")
            nama_grup = str(gid)
    except Exception as e:
        await _callback_feedback(query, context, f"❌ Gagal: {str(e)[:80]}")
    query.data = f"kick_select|{target_id}"
    await kick_select_user(update, context)

@admin_callback
async def kick_add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_add_group'] = True
    await query.edit_message_text(
        "<b>➕ TAMBAH GRUP</b>\n"
        "========================\n\n"
        "Kirim <b>Chat ID grup</b> yang ingin ditambahkan.\n"
        "Contoh: <code>-1001234567890</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[styled_button("❌ Batal", style="danger", callback_data="admpanel_kick")]])
    )

@admin_callback
async def kick_del_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    managed_groups = await get_managed_groups()
    if not managed_groups:
        await query.answer("Belum ada grup terdaftar.", show_alert=True)
        return
    buttons = []
    for gid in managed_groups:
        try:
            chat = await context.bot.get_chat(int(gid))
            label = f"🗑️ {chat.title or gid}  ({gid})"
        except Exception as e:
            logger.debug(f"Gagal mengambil info chat {gid}: {e}")
            label = f"🗑️ {gid}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"kick_del_confirm|{gid}")])
    buttons.append([styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_kick")])
    await query.edit_message_text(
        "<b>🗑️ HAPUS GRUP</b>\n========================\n\nPilih grup yang ingin dihapus:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@admin_callback
async def kick_del_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    gid_str = query.data.split("|", 1)[1]
    managed_groups = await get_managed_groups()
    new_groups = [g for g in managed_groups if str(g) != gid_str]
    await set_managed_groups(new_groups)
    await query.answer(f"✅ Grup {gid_str} dihapus.", show_alert=True)
    await admpanel_kick(update, context)

@admin_callback
async def kick_do_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await is_admin(query.from_user.id, context):
        return
    target_id = int(query.data.split("|", 1)[1])
    managed_groups = await get_managed_groups()
    if not managed_groups:
        await query.answer("Tidak ada grup terdaftar.", show_alert=True)
        return
    kicked = []
    failed = []
    for gid in managed_groups:
        try:
            chat = await context.bot.get_chat(int(gid))
            nama_grup = chat.title or str(gid)
        except Exception as e:
            logger.debug(f"Gagal memuat judul chat {gid}: {e}")
            nama_grup = str(gid)
        try:
            await context.bot.ban_chat_member(chat_id=int(gid), user_id=target_id)
            await context.bot.unban_chat_member(chat_id=int(gid), user_id=target_id, only_if_banned=True)
            kicked.append(esc(nama_grup))
        except Exception as e:
            failed.append(f"{esc(nama_grup)}: {esc(str(e)[:60])}")

    await ban_user(target_id, "Dikick dari grup oleh admin")

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "🚫 <b>Akses Dinonaktifkan</b>\n\n"
                "Akses kamu ke <b>Hyper Family Store</b> telah dinonaktifkan oleh admin.\n\n"
                "<i>Jika merasa ini kesalahan, hubungi admin untuk informasi lebih lanjut.</i>"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.debug(f"Gagal mengirim notif pencabutan akses ke user {target_id}: {e}")

    result_text = (
        f"✅ <b>User <code>{target_id}</code> berhasil dikick + di-ban</b>\n"
        f"========================\n\n"
        f"🗑️ Dikick dari grup:\n"
        + "\n".join(f"  - {g}" for g in kicked)
    )
    if failed:
        result_text += "\n\n❌ Gagal di:\n" + "\n".join(f"  - {f}" for f in failed)
    result_text += "\n\n🚫 User sudah di-ban dari bot dan menerima notifikasi."
    await query.edit_message_text(
        result_text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_kick")]])
    )

# =================== ADMIN: PENGATURAN ===================

@admin_callback
async def admpanel_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    clear_user_state(context)

    channel_id, testi_channel_id, maint_on, link_testi, link_admin, group_name, group_link = await asyncio.gather(
        get_setting('notif_channel_id'),
        get_setting('testimoni_channel_id'),
        is_maintenance(),
        get_setting('link_testimoni'),
        get_setting('link_admin'),
        get_setting('main_group_button_name', '👥 Grup Hyper Family'),
        get_setting('main_group_button_link', ''),
    )
    ch_status = f"✅ ID: <code>{esc(channel_id)}</code>" if channel_id else "🔕 Nonaktif"
    testi_ch_status = f"✅ ID: <code>{esc(testi_channel_id)}</code>" if testi_channel_id else "🔕 Nonaktif"
    maint_status = "⚙️ ON - bot maintenance" if maint_on else "✅ OFF - bot normal"
    maint_btn_label = "🟢 Matikan Maintenance" if maint_on else "⚙️ Aktifkan Maintenance"
    link_testi = link_testi or '-'
    link_admin = link_admin or '-'
    group_name = group_name or '👥 Grup Hyper Family'
    group_link = group_link or '- (tombol disembunyikan)'

    text = (
        "<b>⚙️ PENGATURAN</b>\n"
        "========================\n\n"
        "<b>📢 Channel Notifikasi Order</b>\n"
        f"{ch_status}\n\n"
        "<b>💠 Channel Testimoni Transaksi Publik</b>\n"
        f"{testi_ch_status}\n\n"
        "<b>⚙️ Maintenance Mode</b>\n"
        f"{maint_status}\n\n"
        "<b>⭐ Link Button Testimoni</b>\n"
        f"<code>{esc(link_testi)}</code>\n\n"
        "<b>💬 Link Admin/CS</b>\n"
        f"<code>{esc(link_admin)}</code>\n\n"
        "<b>👥 Tombol Grup Menu Utama</b>\n"
        f"Nama: <b>{esc(group_name)}</b>\n"
        f"Link: <code>{esc(group_link)}</code>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Set Channel Notif", callback_data="admpanel_setting_channel_set"),
            InlineKeyboardButton("🔕 Matikan Notif",    callback_data="admpanel_setting_channel_off"),
        ],
        [
            InlineKeyboardButton("⭐ Set Channel Testi", callback_data="admpanel_setting_testich_set"),
            InlineKeyboardButton("🔕 Matikan Testi",    callback_data="admpanel_setting_testich_off"),
        ],
        [InlineKeyboardButton("📨 Test Notifikasi Order", callback_data="admpanel_setting_channel_test")],
        [InlineKeyboardButton(maint_btn_label,            callback_data="admpanel_setting_maintenance")],
        [
            InlineKeyboardButton("⭐ Ubah Link Testimoni", callback_data="admpanel_setting_link_testi"),
            InlineKeyboardButton("💬 Ubah Link Admin/CS",  callback_data="admpanel_setting_link_admin"),
        ],
        [
            InlineKeyboardButton("✏️ Nama Tombol Grup", callback_data="admpanel_setting_group_name"),
            InlineKeyboardButton("🔗 Link Tombol Grup", callback_data="admpanel_setting_group_link"),
        ],
        [styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

@admin_callback
async def admpanel_setting_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    was_on = await is_maintenance()
    await set_setting('maintenance', '0' if was_on else '1')
    if was_on:
        msg = "✅ <b>Maintenance mode dinonaktifkan.</b>\n\nBot kembali normal - buyer bisa membuat invoice baru."
    else:
        msg = (
            "⚙️ <b>Maintenance mode diaktifkan.</b>\n\n"
            "Invoice baru ditutup. Invoice yang sudah dibuat tetap diproses."
        )
    await query.edit_message_text(
        msg, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Pengaturan", callback_data="admpanel_setting")]])
    )

@admin_callback
async def admpanel_setting_channel_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_channel_id'] = True
    await query.edit_message_text(
        "<b>✍️ SET CHANNEL ID</b>\n"
        "========================\n\n"
        "Ketik Channel ID tujuan notifikasi:\n"
        "Contoh: <code>-1001234567890</code>\n\n"
        "Ketik <code>hapus</code> untuk menonaktifkan channel.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Batal", callback_data="admpanel_setting")]])
    )

@admin_callback
async def admpanel_setting_channel_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await set_setting('notif_channel_id', None)
    await query.edit_message_text(
        "🔕 <b>Channel notifikasi dinonaktifkan.</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Pengaturan", callback_data="admpanel_setting")]])
    )

@admin_callback
async def admpanel_setting_testich_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_testi_channel_id'] = True
    await query.edit_message_text(
        "<b>✍️ SET CHANNEL TESTIMONI</b>\n"
        "========================\n\n"
        "Ketik Channel ID tujuan bukti transaksi publik (berbeda dari channel notifikasi admin):\n"
        "Contoh: <code>-1001234567890</code>\n\n"
        "Ketik <code>hapus</code> untuk menonaktifkan channel.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Batal", callback_data="admpanel_setting")]])
    )

@admin_callback
async def admpanel_setting_testich_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await set_setting('testimoni_channel_id', None)
    await query.edit_message_text(
        "🔕 <b>Channel testimoni otomatis dinonaktifkan.</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Pengaturan", callback_data="admpanel_setting")]])
    )

@admin_callback
async def admpanel_setting_channel_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel_id = await get_setting('notif_channel_id')
    if not channel_id:
        await query.answer("❌ Channel ID belum diset!", show_alert=True)
        return
    try:
        await context.bot.send_message(
            chat_id=int(channel_id),
            text=(
                f"📨 <b>Test Notifikasi</b>\n"
                f"========================\n\n"
                f"✅ Bot berhasil mengirim pesan ke channel ini.\n"
                f"🕒 {now_wib().strftime('%H:%M, %d/%m/%Y')}"
            ),
            parse_mode="HTML"
        )
        await query.answer("✅ Pesan test berhasil dikirim!", show_alert=True)
    except Exception as e:
        await query.answer(f"❌ Gagal: {str(e)[:100]}", show_alert=True)

@admin_callback
async def admpanel_setting_link_testi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_link_testi'] = True
    current = await get_setting('link_testimoni') or '-'
    await query.edit_message_text(
        f"<b>⭐ UBAH LINK TESTIMONI</b>\n"
        f"========================\n\n"
        f"Link saat ini:\n<code>{esc(current)}</code>\n\n"
        f"Kirim link baru:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Batal", callback_data="admpanel_setting")]])
    )

@admin_callback
async def admpanel_setting_link_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_link_admin'] = True
    current = await get_setting('link_admin') or '-'
    await query.edit_message_text(
        f"<b>💬 UBAH LINK ADMIN/CS</b>\n"
        f"========================\n\n"
        f"Link saat ini:\n<code>{esc(current)}</code>\n\n"
        f"Kirim link baru:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Batal", callback_data="admpanel_setting")]])
    )


@admin_callback
async def admpanel_setting_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    clear_user_state(context)
    context.user_data['awaiting_main_group_name'] = True
    current = await get_setting('main_group_button_name', '👥 Grup Hyper Family')
    await query.edit_message_text(
        "<b>✏️ UBAH NAMA TOMBOL GRUP</b>\n"
        "========================\n\n"
        f"Nama saat ini:\n<b>{esc(current)}</b>\n\n"
        "Kirim nama baru (maksimal 64 karakter):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[styled_button(
            "⬅️ Batal", style="danger", callback_data="admpanel_setting"
        )]])
    )


@admin_callback
async def admpanel_setting_group_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    clear_user_state(context)
    context.user_data['awaiting_main_group_link'] = True
    current = await get_setting('main_group_button_link', '') or '-'
    await query.edit_message_text(
        "<b>🔗 UBAH LINK TOMBOL GRUP</b>\n"
        "========================\n\n"
        f"Link saat ini:\n<code>{esc(current)}</code>\n\n"
        "Kirim link grup baru. Bisa memakai <code>https://t.me/...</code> atau "
        "<code>@username</code>.\nKetik <code>hapus</code> untuk menyembunyikan tombol.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[styled_button(
            "⬅️ Batal", style="danger", callback_data="admpanel_setting"
        )]])
    )

# =================== ADMIN: BAN / UNBAN COMMANDS ===================

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Cara pakai: <code>/ban &lt;user_id&gt; [alasan]</code>\n\nContoh: <code>/ban 123456789 spam</code>",
            parse_mode="HTML"
        )
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID harus berupa angka.", parse_mode="HTML")
        return
    if await is_admin(target_id, context):
        await update.message.reply_text("❌ Tidak bisa ban sesama admin.")
        return
    reason = " ".join(args[1:]) if len(args) > 1 else "Tidak ada alasan"
    await ban_user(target_id, reason)
    await update.message.reply_text(
        f"🚫 <b>User Berhasil Dibanned</b>\n\n"
        f"👤 User ID: <code>{target_id}</code>\n"
        f"📝 Alasan: {esc(reason)}",
        parse_mode="HTML"
    )

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Cara pakai: <code>/unban &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID harus berupa angka.", parse_mode="HTML")
        return
    if not await is_banned(target_id):
        await update.message.reply_text(f"⚠️ User <code>{target_id}</code> tidak ada dalam daftar ban.", parse_mode="HTML")
        return
    await unban_user(target_id)
    await update.message.reply_text(
        f"✅ <b>User Berhasil Di-unban</b>\n\n👤 User ID: <code>{target_id}</code>",
        parse_mode="HTML"
    )

async def cmd_daftar_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    banned = await get_all_banned()
    if not banned:
        await update.message.reply_text("✅ Tidak ada user yang dibanned saat ini.")
        return
    text = f"<b>🚫 DAFTAR BAN ({len(banned)} user)</b>\n========================\n\n"
    for b in banned:
        text += f"- ID: <code>{b['user_id']}</code> | Alasan: {esc(b['reason'] or '-')}\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_cari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Cara pakai: <code>/cari &lt;order_id&gt;</code>", parse_mode="HTML")
        return
    order_id = args[0].strip()
    order = await get_order_by_id(order_id)
    if not order:
        await update.message.reply_text(f"❌ Order tidak ditemukan: <code>{esc(order_id)}</code>", parse_mode="HTML")
        return
    paket = await get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0}
    detail_text = await build_order_detail_text(order, paket)
    await update.message.reply_text(detail_text, parse_mode="HTML")

# =================== ADMIN: KELOLA ADMIN (SUPER ADMIN ONLY) ===================

@admin_callback
async def admpanel_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_super_admin(query.from_user.id):
        await query.answer("❌ Hanya super admin.", show_alert=True)
        return

    admins = await get_all_admins()
    lines = []
    for a in admins:
        lines.append(f"- {esc(a['nama'])} (<code>{a['user_id']}</code>)")
    admin_list = "\n".join(lines) if lines else "<i>Belum ada admin tambahan.</i>"

    text = (
        "<b>👥 KELOLA ADMIN</b>\n"
        "========================\n\n"
        "<b>Daftar Admin Saat Ini:</b>\n"
        f"{admin_list}\n\n"
        "<i>Admin tambahan bisa akses semua fitur panel kecuali menu Kelola Admin.</i>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Admin", callback_data="admpanel_admin_add")],
        [InlineKeyboardButton("➖ Hapus Admin",  callback_data="admpanel_admin_remove")],
        [styled_button("⬅️ Kembali", style="primary",       callback_data="admpanel_back")],
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

@admin_callback
async def admpanel_admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_super_admin(query.from_user.id):
        await query.answer("❌ Hanya super admin.", show_alert=True)
        return
    context.user_data['awaiting_add_admin'] = True
    await query.edit_message_text(
        "<b>➕ TAMBAH ADMIN</b>\n"
        "========================\n\n"
        "Forward pesan dari user yang ingin dijadikan admin,\n"
        "atau ketik <b>User ID</b>-nya langsung.\n\n"
        "<i>Pastikan user sudah pernah start bot.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Batal", callback_data="admpanel_admins")]])
    )

@admin_callback
async def admpanel_admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_super_admin(query.from_user.id):
        await query.answer("❌ Hanya super admin.", show_alert=True)
        return

    admins = await get_all_admins()
    if not admins:
        await query.edit_message_text(
            "ℹ️ Belum ada admin tambahan yang bisa dihapus.",
            reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_admins")]])
        )
        return

    buttons = []
    for a in admins:
        buttons.append([InlineKeyboardButton(
            f"🗑️ {a['nama']} ({a['user_id']})",
            callback_data=f"admpanel_admin_del_{a['user_id']}"
        )])
    buttons.append([styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_admins")])
    await query.edit_message_text(
        "<b>➖ HAPUS ADMIN</b>\n========================\n\nPilih admin yang ingin dihapus:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@admin_callback
async def admpanel_admin_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_super_admin(query.from_user.id):
        await query.answer("❌ Hanya super admin.", show_alert=True)
        return

    try:
        target_id = int(query.data.split("admpanel_admin_del_")[1])
    except (IndexError, ValueError):
        await query.answer("❌ ID tidak valid.", show_alert=True)
        return

    await remove_admin(target_id)
    invalidate_admin_cache(context, target_id)
    await query.edit_message_text(
        f"✅ Admin <code>{target_id}</code> berhasil dihapus.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[styled_button("⬅️ Kembali", style="primary", callback_data="admpanel_admins")]])
    )

# =================== GLOBAL ERROR HANDLER ===================

async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    user = getattr(update, 'effective_user', None) if update is not None else None
    chat = getattr(update, 'effective_chat', None) if update is not None else None
    update_id = getattr(update, 'update_id', '-') if update is not None else '-'
    logger.error(
        f"[UNHANDLED] update_id={update_id} user_id={getattr(user, 'id', '-')} "
        f"chat_id={getattr(chat, 'id', '-')} error={error}",
        exc_info=(type(error), error, error.__traceback__) if error else True,
    )

def _install_uvloop_if_available():
    if uvloop is None or os.name == 'nt' or os.environ.get('DISABLE_UVLOOP', '').strip() == '1':
        return False
    try:
        # PTB menjalankan event loop sendiri; install policy sebelum main() membuat run_polling memakai uvloop.
        uvloop.install()
        logger.info("[PERFORMANCE] uvloop aktif.")
        return True
    except Exception as exc:
        logger.warning(f"[PERFORMANCE] uvloop tidak dapat diaktifkan, fallback asyncio: {exc}")
        return False

# =================== MAIN ENTRYPOINT ===================

def main():
    init_pool()
    if RUN_DB_MIGRATIONS_ON_STARTUP:
        init_db()
    else:
        logger.info("[DB] Startup migrations dilewati (RUN_DB_MIGRATIONS_ON_STARTUP=0).")

    builder = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .update_queue(CallbackTimingQueue())
        .get_updates_request(PollingHTTPXRequest())
        .request(TelegramHTTPXRequest(connection_pool_size=TELEGRAM_CONNECTION_POOL_SIZE,
                                    pool_timeout=TELEGRAM_POOL_TIMEOUT_SECONDS))
    )
    if hasattr(builder, 'post_stop'):
        builder = builder.post_stop(_stop_blast_before_shutdown)
    if BuyerPriorityRateLimiter is not None:
        builder = builder.rate_limiter(BuyerPriorityRateLimiter())
        logger.info('[TELEGRAM] Limiter aktif: buyer priority, background dibatasi.')
    elif AIORateLimiter is not None:
        try:
            builder = builder.rate_limiter(
                AIORateLimiter(overall_max_rate=28, overall_time_period=1, max_retries=1)
            )
            logger.info("[TELEGRAM] AIORateLimiter aktif.")
        except Exception as exc:
            logger.info(f"[TELEGRAM] Rate limiter opsional tidak aktif: {exc}")
    if PerChatUpdateProcessor is not None:
        builder = builder.concurrent_updates(PerChatUpdateProcessor(BOT_CONCURRENT_UPDATES))
    else:
        # PTB 20.0-20.3: tetap paralel, tetapi tanpa serializer per-chat bawaan kustom.
        builder = builder.concurrent_updates(BOT_CONCURRENT_UPDATES)
    app = builder.build()
    app.add_error_handler(global_error_handler)

    # User Commands
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("riwayat", cmd_riwayat))
    app.add_handler(CommandHandler("resend", show_resend_menu))
    app.add_handler(CommandHandler("blast_status", cmd_blast_status))
    app.add_handler(CommandHandler("emojiid", cmd_emojiid))
    app.add_handler(CallbackQueryHandler(cmd_blast_status, pattern="^admpanel_blast_status$"))

    # Admin Commands
    app.add_handler(CommandHandler("admin",       cmd_admin))
    app.add_handler(CommandHandler("produk",      cmd_produk))
    app.add_handler(CommandHandler("pending",     admin_pending))
    app.add_handler(CommandHandler("aktif",       cmd_aktif))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("blast",       cmd_blast))
    app.add_handler(CommandHandler("backup",      cmd_backup))
    app.add_handler(CommandHandler("export",      cmd_export))
    app.add_handler(CommandHandler("import_json", cmd_import_json))
    app.add_handler(CommandHandler("link",        cmd_link))
    app.add_handler(CommandHandler("ban",         cmd_ban))
    app.add_handler(CommandHandler("unban",       cmd_unban))
    app.add_handler(CommandHandler("daftar_ban",  cmd_daftar_ban))
    app.add_handler(CommandHandler("cari",        cmd_cari))

    # User Callback Query Handlers
    app.add_handler(CallbackQueryHandler(buyer_how, pattern="^buyer_how$"))
    app.add_handler(CallbackQueryHandler(buyer_history, pattern="^buyer_history$"))
    app.add_handler(CallbackQueryHandler(buyer_description, pattern="^buyer_desc_"))
    app.add_handler(CallbackQueryHandler(buy_callback,         pattern=r"^buy(?:\|\d+)?$"))
    app.add_handler(CallbackQueryHandler(pilih_paket,          pattern="^pilih_"))
    app.add_handler(CallbackQueryHandler(confirm_buy_handler,  pattern="^confirm_buy_"))
    app.add_handler(CallbackQueryHandler(prereq_buy_handler,   pattern="^prereq_buy_"))
    app.add_handler(CallbackQueryHandler(cancel_order_handler, pattern="^cancel_order$"))
    app.add_handler(CallbackQueryHandler(back_to_menu,         pattern="^back_to_menu$"))
    
    app.add_handler(CallbackQueryHandler(ganti_paket_list,     pattern="^ganti_paket_list$"))
    app.add_handler(CallbackQueryHandler(ganti_paket_konfirm,  pattern="^ganti_paket_konfirm\\|"))
    app.add_handler(CallbackQueryHandler(ganti_paket_exec,     pattern="^ganti_paket_exec\\|"))
    app.add_handler(CallbackQueryHandler(ganti_paket_batal,    pattern="^ganti_paket_batal$"))

    # Buyer Actions Handlers
    app.add_handler(CallbackQueryHandler(show_resend_menu, pattern=r"^(?:resend_menu(?:\|\d+)?|resendlink\|.*)$"))
    app.add_handler(CallbackQueryHandler(resend_group_link, pattern=r"^resend_select\|"))

    # Testimonial Flow Handlers
    app.add_handler(CallbackQueryHandler(handle_rate_start,      pattern="^rate_start\\|"))
    app.add_handler(CallbackQueryHandler(handle_rate_val,        pattern="^rate_val\\|"))
    app.add_handler(CallbackQueryHandler(handle_rate_text_skip,  pattern="^rate_text_skip\\|"))
    app.add_handler(CallbackQueryHandler(handle_rate_skip,       pattern="^rate_skip$"))
    app.add_handler(CallbackQueryHandler(handle_rate_back,       pattern="^rate_back\\|"))
    app.add_handler(CallbackQueryHandler(handle_rate_back_stars, pattern="^rate_back_stars\\|"))

    # Admin Prerequisite Manual Handler
    app.add_handler(CallbackQueryHandler(admin_kirim_link_prereq, pattern="^admin_kirim_link_prereq\\|"))

    # Admin Testimonial Verification
    app.add_handler(CallbackQueryHandler(admin_testi_approve, pattern="^adm_testi_approve\\|"))
    app.add_handler(CallbackQueryHandler(admin_testi_reject,  pattern="^adm_testi_reject\\|"))

    # Admin Product Management Flow
    app.add_handler(CallbackQueryHandler(produk_detail,        pattern="^pd_detail_"))
    app.add_handler(CallbackQueryHandler(produk_edit_field,    pattern="^pd_edit_"))
    app.add_handler(CallbackQueryHandler(produk_toggle_aktif,  pattern="^pd_toggle_"))
    app.add_handler(CallbackQueryHandler(produk_hapus_confirm, pattern="^pd_hapus_(?!ok_)"))
    app.add_handler(CallbackQueryHandler(produk_hapus_exec,    pattern="^pd_hapus_ok_"))
    app.add_handler(CallbackQueryHandler(produk_tambah_start,  pattern="^pd_tambah$"))
    app.add_handler(CallbackQueryHandler(produk_tambah_batal,  pattern="^pd_tambah_batal$"))
    app.add_handler(CallbackQueryHandler(pd_back,              pattern="^pd_back$"))

    # Admin Order Management Flow
    app.add_handler(CallbackQueryHandler(admin_proses_order,   pattern="^proses_"))
    app.add_handler(CallbackQueryHandler(admin_konfirmasi,     pattern="^(confirm|reject)_"))
    app.add_handler(CallbackQueryHandler(back_orders,          pattern="^back_orders$"))
    app.add_handler(CallbackQueryHandler(admin_cancel_order,   pattern="^adm_cancel\\|"))
    app.add_handler(CallbackQueryHandler(admin_manual_confirm, pattern="^adm_konfirm\\|"))

    # Admin Main Panel Navigation Flow
    app.add_handler(CallbackQueryHandler(admpanel_back,            pattern="^admpanel_back$"))
    app.add_handler(CallbackQueryHandler(admpanel_produk,          pattern="^admpanel_produk$"))
    app.add_handler(CallbackQueryHandler(admpanel_orders,          pattern="^admpanel_orders$"))
    app.add_handler(CallbackQueryHandler(admpanel_orders_aktif,    pattern=r"^admpanel_orders_aktif(?:\|\d+)?$"))
    app.add_handler(CallbackQueryHandler(admpanel_orders_pending,  pattern=r"^admpanel_orders_pending(?:\|\d+)?$"))
    app.add_handler(CallbackQueryHandler(admpanel_orders_cari,     pattern="^admpanel_orders_cari$"))
    app.add_handler(CallbackQueryHandler(admpanel_stats,           pattern="^admpanel_stats$"))
    app.add_handler(CallbackQueryHandler(admpanel_blast,           pattern="^admpanel_blast$"))
    app.add_handler(CallbackQueryHandler(admpanel_data,            pattern="^admpanel_data$"))
    app.add_handler(CallbackQueryHandler(admpanel_data_backup,     pattern="^admpanel_data_backup$"))
    app.add_handler(CallbackQueryHandler(admpanel_data_export,     pattern="^admpanel_data_export$"))
    app.add_handler(CallbackQueryHandler(admpanel_data_import,     pattern="^admpanel_data_import$"))
    app.add_handler(CallbackQueryHandler(admpanel_data_link,       pattern="^admpanel_data_link$"))
    app.add_handler(CallbackQueryHandler(admpanel_user,            pattern="^admpanel_user$"))
    app.add_handler(CallbackQueryHandler(admpanel_user_ban,        pattern="^admpanel_user_ban$"))
    app.add_handler(CallbackQueryHandler(admpanel_user_unban,      pattern="^admpanel_user_unban$"))
    app.add_handler(CallbackQueryHandler(admpanel_user_daftar,     pattern=r"^admpanel_user_daftar(?:\|\d+)?$"))

    # Admin Group Kick Feature Flow
    app.add_handler(CallbackQueryHandler(admpanel_kick,    pattern="^admpanel_kick$"))
    app.add_handler(CallbackQueryHandler(kick_cek_user,    pattern="^kick_cek_user$"))
    app.add_handler(CallbackQueryHandler(kick_select_user, pattern="^kick_select\\|"))
    app.add_handler(CallbackQueryHandler(kick_one,         pattern="^kick_one\\|"))
    app.add_handler(CallbackQueryHandler(pd_req_toggle,    pattern="^pd_req_toggle\\|"))
    app.add_handler(CallbackQueryHandler(pd_req_save,      pattern="^pd_req_save\\|"))
    app.add_handler(CallbackQueryHandler(pd_req_clear,     pattern="^pd_req_clear\\|"))
    app.add_handler(CallbackQueryHandler(kick_add_group,   pattern="^kick_add_group$"))
    app.add_handler(CallbackQueryHandler(kick_del_group,   pattern="^kick_del_group$"))
    app.add_handler(CallbackQueryHandler(kick_del_confirm, pattern="^kick_del_confirm\\|"))
    app.add_handler(CallbackQueryHandler(kick_do_kick,     pattern="^kick_do_kick\\|"))

    # Admin Settings Configuration Flow
    app.add_handler(CallbackQueryHandler(admpanel_setting,              pattern="^admpanel_setting$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_channel_set,  pattern="^admpanel_setting_channel_set$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_channel_off,  pattern="^admpanel_setting_channel_off$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_testich_set,  pattern="^admpanel_setting_testich_set$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_testich_off,  pattern="^admpanel_setting_testich_off$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_channel_test, pattern="^admpanel_setting_channel_test$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_maintenance,  pattern="^admpanel_setting_maintenance$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_link_testi,   pattern="^admpanel_setting_link_testi$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_link_admin,   pattern="^admpanel_setting_link_admin$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_group_name,   pattern="^admpanel_setting_group_name$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_group_link,   pattern="^admpanel_setting_group_link$"))

    # Super Admin Management Flow
    app.add_handler(CallbackQueryHandler(admpanel_admins,       pattern="^admpanel_admins$"))
    app.add_handler(CallbackQueryHandler(admpanel_admin_add,    pattern="^admpanel_admin_add$"))
    app.add_handler(CallbackQueryHandler(admpanel_admin_remove, pattern="^admpanel_admin_remove$"))
    app.add_handler(CallbackQueryHandler(admpanel_admin_del,    pattern="^admpanel_admin_del_"))

    app.add_handler(CallbackQueryHandler(blast_control, pattern=r"^blastctl\|"))

    # Blast Message Confirmation Flow
    app.add_handler(CallbackQueryHandler(blast_batal,   pattern="^blast_batal$"))
    app.add_handler(CallbackQueryHandler(blast_confirm, pattern=r"^blast_confirm(?:\|[a-f0-9]{32})?$"))
    app.add_handler(CallbackQueryHandler(blast_stop,    pattern="^blast_stop$"))
    app.add_handler(CallbackQueryHandler(blast_retype,  pattern="^blast_retype$"))

    # Auto-Approve Join Request Handler
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Backup & Import Document Handlers
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.PRIVATE,
        handle_json_document
    ))

    # General Fallback Message Handler
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND & filters.ChatType.PRIVATE,
        message_handler
    ))

    logger.info(
        f"Bot Hyper Family Store berjalan | concurrent={BOT_CONCURRENT_UPDATES} | "
        f"telegram_pool={TELEGRAM_CONNECTION_POOL_SIZE} | db_pool={DB_POOL_MAX}"
    )
    app.run_polling(allowed_updates=["message", "callback_query", "chat_join_request"])

if __name__ == "__main__":
    _install_uvloop_if_available()
    main()
