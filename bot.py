import os
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
from aiohttp import web as aio_web
import aiohttp
import qrcode
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from io import BytesIO
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import telegram.error
from telegram import (
    Update, BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
    InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ChatJoinRequestHandler, filters, ContextTypes
)

# =================== LOGGING SETUP ===================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("HyperFamilyBot")

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
PAKASIR_API_KEY = os.environ.get("PAKASIR_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
PAKASIR_WEBHOOK_SECRET = os.environ.get("PAKASIR_WEBHOOK_SECRET", "")
APP_ENV = os.environ.get("APP_ENV") or os.environ.get("ENV") or "development"

PAKASIR_SLUG = "atkikukkvd"
PAKASIR_BASE_URL = "https://app.pakasir.com"
DEFAULT_LINK = "https://t.me/Kikukkvd"

if not TOKEN:
    raise ValueError("BOT_TOKEN tidak di-set!")
if not _ADMIN_ID_RAW or not _ADMIN_ID_RAW.strip().isdigit():
    raise ValueError("ADMIN_ID tidak di-set atau bukan angka valid!")
ADMIN_ID = int(_ADMIN_ID_RAW.strip())
if not PAKASIR_API_KEY:
    raise ValueError("PAKASIR_API_KEY tidak di-set!")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL tidak di-set!")
if APP_ENV.lower() == "production" and not PAKASIR_WEBHOOK_SECRET:
    raise ValueError("PAKASIR_WEBHOOK_SECRET wajib di-set saat APP_ENV/ENV=production!")

# =================== SINGLETON HTTP SESSION ===================
_http_session: aiohttp.ClientSession = None

async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session

# =================== PAKASIR API ===================

async def create_transaction_qris(order_id, amount, description):
    payload = {
        "project": PAKASIR_SLUG,
        "order_id": order_id,
        "amount": amount,
        "api_key": PAKASIR_API_KEY,
    }
    try:
        session = await get_http_session()
        async with session.post(
            f'{PAKASIR_BASE_URL}/api/transactioncreate/qris',
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            result = await response.json()
            if 'payment' in result:
                return result['payment']
            logger.error(f"Pakasir error: {result}")
            return None
    except Exception as e:
        logger.error(f"Error saat membuat transaksi QRIS: {e}", exc_info=True)
        return None

async def cancel_transaction(order_id, amount):
    if not amount:
        return None
    payload = {
        "project": PAKASIR_SLUG,
        "order_id": order_id,
        "amount": amount,
        "api_key": PAKASIR_API_KEY,
    }
    try:
        session = await get_http_session()
        async with session.post(
            f'{PAKASIR_BASE_URL}/api/transactioncancel',
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            return await response.json()
    except Exception as e:
        logger.error(f"Error saat membatalkan transaksi: {e}", exc_info=True)
        return None

async def get_transaction_detail(order_id, amount):
    try:
        session = await get_http_session()
        async with session.get(
            f'{PAKASIR_BASE_URL}/api/transactiondetail',
            params={
                'project': PAKASIR_SLUG,
                'amount': amount,
                'order_id': order_id,
                'api_key': PAKASIR_API_KEY,
            },
            timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            result = await response.json()
            if 'transaction' in result:
                return result['transaction']
            return None
    except Exception as e:
        logger.error(f"Error saat mengambil detail transaksi: {e}", exc_info=True)
        return None

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

def init_pool():
    global _pool
    # Alokasi pool dinamis yang lebih tinggi untuk mengantisipasi concurrent request
    _pool = ThreadedConnectionPool(5, 30, DATABASE_URL, cursor_factory=RealDictCursor)
    logger.info("[DB] Threaded Connection pool berhasil diinisialisasi (min=5, max=30)")

@contextmanager
def db_session_safe(retries=3, delay=0.5):
    """
    Context manager aman dengan mekanisme retry eksponensial.
    Mencegah crash aplikasi akibat Pool Exhaustion pada beban konkurensi tinggi.
    """
    conn = None
    for attempt in range(retries):
        try:
            conn = _pool.getconn()
            break
        except psycopg2.pool.PoolError:
            if attempt == retries - 1:
                logger.error("[DB CRITICAL] Connection pool habis dan seluruh upaya retry gagal!")
                raise
            time_sleep = delay * (2 ** attempt)
            logger.warning(f"[DB WARN] Pool exhausted. Menunggu {time_sleep}s sebelum mencoba kembali (Percobaan ke-{attempt+1})...")
            _time.sleep(time_sleep)
    try:
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"[DB ERROR] Transaksi database gagal dan di-rollback: {e}", exc_info=True)
        raise
    finally:
        if conn and _pool:
            _pool.putconn(conn)

def async_wrap(func):
    @functools.wraps(func)
    async def run(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
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
                    link TEXT DEFAULT 'https://t.me/Kikukkvd',
                    group_chat_id TEXT DEFAULT NULL,
                    aktif BOOLEAN DEFAULT TRUE
                )
            """)
            c.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS link TEXT DEFAULT 'https://t.me/Kikukkvd'")
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
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_delivery_status ON orders(delivery_status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_testimonials_status ON testimonials(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cooldowns_user_id ON cooldowns(user_id)")

            for key, val in [
                ('link_testimoni', 'https://t.me/+7zsdSrwYIG8wOTg1'),
                ('link_admin', 'https://t.me/Kikukkvd'),
                ('testimoni_channel_id', ''),
            ]:
                c.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (key, val)
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
            c.execute("UPDATE testimonials SET status=%s WHERE order_id=%s", (status, order_id))

@async_wrap
def get_testimonial_by_order(order_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM testimonials WHERE order_id=%s", (order_id,))
            row = c.fetchone()
            return dict(row) if row else None

# =================== PRODUCT DB FUNCTIONS ===================

# In-memory TTL Cache
_settings_cache: dict = {}
_SETTINGS_TTL = 60  # detik
_products_cache: tuple = None
_PRODUCTS_TTL = 30  # detik

def _settings_cache_get(key):
    entry = _settings_cache.get(key)
    if entry and _time.monotonic() < entry[1]:
        return True, entry[0]
    return False, None

def _settings_cache_set(key, value):
    _settings_cache[key] = (value, _time.monotonic() + _SETTINGS_TTL)

def _settings_cache_del(key):
    _settings_cache.pop(key, None)

def _get_all_products_sync():
    global _products_cache
    cached = _products_cache
    if cached and _time.monotonic() < cached[1]:
        return cached[0]
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM products ORDER BY harga ASC")
            data = [dict(r) for r in c.fetchall()]
            _products_cache = (data, _time.monotonic() + _PRODUCTS_TTL)
            return data

@async_wrap
def get_all_products():
    return _get_all_products_sync()

def _invalidate_products_cache():
    global _products_cache
    _products_cache = None

def _get_product_sync(paket_id):
    # Pakai cache all-products jika masih valid agar callback/payment flow lebih smooth.
    global _products_cache
    cached = _products_cache
    if cached and _time.monotonic() < cached[1]:
        for item in cached[0]:
            if item.get('paket_id') == paket_id:
                return dict(item)
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM products WHERE paket_id=%s", (paket_id,))
            row = c.fetchone()
            return dict(row) if row else None

@async_wrap
def get_product(paket_id):
    return _get_product_sync(paket_id)

@async_wrap
def add_product(paket_id, nama, emoji, deskripsi, harga, link=None, group_chat_id=None):
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
    _invalidate_products_cache()

@async_wrap
def update_product_field(paket_id, field, value):
    _FIELD_MAP = {
        "nama": "nama", "emoji": "emoji", "deskripsi": "deskripsi",
        "harga": "harga", "link": "link", "group_chat_id": "group_chat_id",
        "aktif": "aktif", "requires_paket_ids": "requires_paket_ids"
    }
    safe_field = _FIELD_MAP.get(field)
    if not safe_field:
        logger.warning(f"[SECURITY WARNING] Field SQL tidak diizinkan: {field}")
        return
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(f"UPDATE products SET {safe_field}=%s WHERE paket_id=%s", (value, paket_id))
    _invalidate_products_cache()

@async_wrap
def delete_product(paket_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM products WHERE paket_id=%s", (paket_id,))
    _invalidate_products_cache()

def make_paket_id(nama):
    pid = re.sub(r'[^a-z0-9]+', '_', nama.lower().strip()).strip('_')
    return pid or "produk"

# =================== ORDER DB FUNCTIONS ===================

@async_wrap
def get_active_order(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT * FROM orders WHERE user_id=%s AND status IN ('waiting','pending') ORDER BY id DESC LIMIT 1",
                (user_id,)
            )
            row = c.fetchone()
            return dict(row) if row else None

@async_wrap
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

@async_wrap
def get_all_waiting():
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

@async_wrap
def get_all_buyers():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT o.user_id, o.user_name, MAX(o.id) as max_id
                FROM orders o
                LEFT JOIN banned_users b ON o.user_id = b.user_id
                WHERE b.user_id IS NULL
                GROUP BY o.user_id, o.user_name
                ORDER BY max_id DESC
            """)
            return [dict(r) for r in c.fetchall()]

@async_wrap
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
    return await asyncio.to_thread(_get_managed_groups_sync)

async def set_managed_groups(groups: list):
    return await asyncio.to_thread(_set_managed_groups_sync, groups)

@async_wrap
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
                SELECT COUNT(DISTINCT user_id) as cnt FROM orders
                WHERE status='completed' AND created_at >= %s
                AND user_id NOT IN (
                    SELECT DISTINCT user_id FROM orders
                    WHERE status='completed' AND created_at < %s
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

@async_wrap
def update_order_status(order_id, status):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE orders SET status=%s WHERE order_id=%s", (status, order_id))

def _mark_order_completed_sync(order_id) -> bool:
    """Atomic: mengubah status menjadi completed hanya jika masih 'waiting'."""
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                "UPDATE orders SET status='completed' WHERE order_id=%s AND status='waiting' RETURNING id",
                (order_id,)
            )
            updated = c.fetchone()
            return updated is not None

async def mark_order_completed(order_id) -> bool:
    return await asyncio.to_thread(_mark_order_completed_sync, order_id)

@async_wrap
def get_order_by_id(order_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE order_id=%s", (order_id,))
            row = c.fetchone()
            return dict(row) if row else None

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
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """SELECT DISTINCT paket_id FROM orders
                   WHERE user_id=%s AND status='completed' AND paket_id = ANY(%s)""",
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
    """Secara atomik mengklaim order untuk pengiriman guna mencegah race condition."""
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """UPDATE orders
                   SET sent_link='pending', delivery_status='pending', delivery_error=NULL
                   WHERE order_id=%s
                   AND (
                        delivery_status IS NULL
                        OR delivery_status IN ('not_sent', 'failed', 'held')
                        OR sent_link IS NULL
                        OR sent_link=''
                   )""",
                (order_id,)
            )
            return c.rowcount > 0

@async_wrap
def save_order(user_id, user_name, paket_id, order_id, harga_dibayar=0, order_changes=0):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO orders (user_id, user_name, paket_id, order_id, status, waktu, harga_dibayar, order_changes)
                   VALUES (%s, %s, %s, %s, 'waiting', %s, %s, %s)""",
                (user_id, user_name, paket_id, order_id,
                 now_wib().strftime("%H:%M - %d/%m/%Y"), harga_dibayar, order_changes)
            )

# =================== BAN DB FUNCTIONS ===================

@async_wrap
def is_banned(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM banned_users WHERE user_id=%s", (user_id,))
            return c.fetchone() is not None

@async_wrap
def ban_user(user_id, reason=""):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO banned_users (user_id, reason, banned_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET reason=EXCLUDED.reason, banned_at=EXCLUDED.banned_at""",
                (user_id, reason, now_wib().strftime("%H:%M - %d/%m/%Y"))
            )

@async_wrap
def unban_user(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM banned_users WHERE user_id=%s", (user_id,))

@async_wrap
def get_all_banned():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM banned_users ORDER BY banned_at DESC")
            return [dict(r) for r in c.fetchall()]

# =================== SETTINGS DB FUNCTIONS ===================

@async_wrap
def get_setting(key, default=None):
    hit, cached_val = _settings_cache_get(key)
    if hit:
        return cached_val if cached_val is not None else default
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = c.fetchone()
            val = row['value'] if row else None
            _settings_cache_set(key, val)
            return val if val is not None else default

@async_wrap
def set_setting(key, value):
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
    _settings_cache_del(key)

# =================== COOLDOWN DB + IN-MEMORY CACHE ===================
COOLDOWN_MENIT = 5
COOLDOWN_MENIT_MAX = 60
_cooldown_memory_cache: dict = {}
_cancel_count_cache: dict = {}

def _get_dynamic_cooldown_minutes(user_id: int) -> int:
    """Hitung cooldown dinamis berdasarkan jumlah cancel user."""
    cancel_count = _cancel_count_cache.get(user_id, 0)
    if cancel_count >= 5:
        return COOLDOWN_MENIT_MAX
    elif cancel_count >= 3:
        return 30
    elif cancel_count >= 2:
        return 15
    return COOLDOWN_MENIT

def _increment_cancel_count(user_id: int):
    """Increment cancel counter untuk user."""
    _cancel_count_cache[user_id] = _cancel_count_cache.get(user_id, 0) + 1

def _get_cooldown_memory(user_id: int) -> datetime:
    expires = _cooldown_memory_cache.get(user_id)
    if expires is None:
        return None
    if now_utc() >= expires:
        _cooldown_memory_cache.pop(user_id, None)
        return None
    return expires

def _set_cooldown_memory(user_id: int, expires_at: datetime):
    _cooldown_memory_cache[user_id] = expires_at

def _clear_cooldown_memory(user_id: int):
    _cooldown_memory_cache.pop(user_id, None)

@async_wrap
def set_cooldown_db(user_id):
    cooldown_minutes = _get_dynamic_cooldown_minutes(user_id)
    expires_at = now_utc() + timedelta(minutes=cooldown_minutes)
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO cooldowns (user_id, expires_at) VALUES (%s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET expires_at=EXCLUDED.expires_at""",
                (user_id, expires_at)
            )
    _set_cooldown_memory(user_id, expires_at)
    _increment_cancel_count(user_id)

@async_wrap
def get_cooldown_sisa_db(user_id):
    mem_expires = _get_cooldown_memory(user_id)
    if mem_expires is not None:
        sisa = (mem_expires - now_utc()).total_seconds()
        return math.ceil(sisa / 60) if sisa > 0 else 0
    
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT expires_at FROM cooldowns WHERE user_id=%s", (user_id,))
            row = c.fetchone()
            if not row:
                return 0
            try:
                until = row['expires_at']
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                
                until_utc = until.astimezone(timezone.utc)
                sisa = (until_utc - now_utc()).total_seconds()
                
                if sisa > 0:
                    _set_cooldown_memory(user_id, until_utc)
                
                return math.ceil(sisa / 60) if sisa > 0 else 0
            except Exception as e:
                logger.error(f"[COOLDOWN] Gagal menghitung cooldown: {e}")
                return 0

@async_wrap
def clear_cooldown_db(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM cooldowns WHERE user_id=%s", (user_id,))
    _clear_cooldown_memory(user_id)

@async_wrap
def cleanup_expired_cooldowns():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM cooldowns WHERE expires_at < NOW()")

# =================== ADMIN DB FUNCTIONS ===================

@async_wrap
def get_all_admins():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM admins ORDER BY added_at ASC")
            return [dict(r) for r in c.fetchall()]

@async_wrap
def add_admin(user_id, nama, added_by):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO admins (user_id, nama, added_by, added_at)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET nama=EXCLUDED.nama""",
                (user_id, str(nama), added_by, now_wib().strftime("%H:%M - %d/%m/%Y"))
            )

@async_wrap
def remove_admin(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM admins WHERE user_id=%s", (user_id,))

@async_wrap
def is_admin_in_db(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM admins WHERE user_id=%s", (user_id,))
            return c.fetchone() is not None

# =================== ADMIN CHECK WITH SESSION CACHE ===================

async def is_admin(user_id: int, context=None) -> bool:
    if user_id == ADMIN_ID:
        return True
    if context is not None:
        cache = context.bot_data.get('_admin_cache', {})
        if user_id in cache:
            return cache[user_id]
    result = await is_admin_in_db(user_id)
    if context is not None:
        context.bot_data.setdefault('_admin_cache', {})[user_id] = result
    return result

def invalidate_admin_cache(context, user_id: int = None):
    if user_id is not None:
        context.bot_data.get('_admin_cache', {}).pop(user_id, None)
    else:
        context.bot_data['_admin_cache'] = {}

def is_super_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

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

# =================== NOTIFIKASI ORDER ===================

def _format_order_notif(judul: str, user_name: str, user_id: int,
                         paket: dict, order_id: str,
                         amount: int = None, extra: str = None) -> str:
    """Format order status admin yang lebih terstruktur dan buyer clickable."""
    status_label = _infer_order_status_label(judul)
    lines = [
        judul,
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "👤 <b>Buyer</b>",
        f"Nama : {tg_user_link(user_id, user_name)}",
        f"ID   : <code>{esc(user_id)}</code>",
        "",
        "📦 <b>Produk</b>",
        f"Paket : {esc(paket.get('emoji','📦'))} {esc(paket.get('nama','?'))}",
    ]
    if amount is not None:
        lines.append(f"Total : {format_harga(amount)}")
    lines.extend([
        "",
        "📌 <b>Status Order</b>",
        f"Status : {status_label}",
    ])
    if extra:
        lines.extend(["", str(extra)])
    lines.extend([
        "",
        "🕒 <b>Waktu</b>",
        now_wib().strftime('%H:%M, %d/%m/%Y'),
        "",
        "🔖 <b>Order ID</b>",
        f"<code>{esc(order_id)}</code>",
    ])
    return "\n".join(lines)

async def build_order_detail_text(order: dict, paket: dict) -> str:
    STATUS_LABEL = {
        'completed': '✅ Completed', 'waiting': '⏳ Waiting',
        'pending': '🔄 Pending Manual', 'cancelled': '❌ Cancelled',
        'expired': '⏰ Expired', 'rejected': '🚫 Rejected',
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
    text = (
        f"<b>🧾 ORDER STATUS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>Buyer</b>\n"
        f"Nama : {tg_user_link(order.get('user_id'), order.get('user_name', '-'))}\n"
        f"ID   : <code>{esc(order.get('user_id', '-'))}</code>\n\n"
        f"📦 <b>Produk</b>\n"
        f"Paket : {esc(paket.get('emoji','📦'))} {esc(paket.get('nama','Produk'))}\n"
        f"Total : {format_harga(amount)}\n\n"
        f"📌 <b>Status Order</b>\n"
        f"Status : {STATUS_LABEL.get(order.get('status'), order.get('status', '-'))}\n"
        f"Link   : {DELIVERY_LABEL.get(order.get('delivery_status'), order.get('delivery_status') or '—')}\n"
    )

    requires_str = paket.get('requires_paket_ids') or ''
    if requires_str:
        progress = await _build_prereq_progress(order.get('user_id'), requires_str, parent_order_id=order.get('order_id'))
        if progress['missing']:
            akses_status = '⏸️ Link ditahan'
            alasan = 'Syarat belum terpenuhi'
        else:
            akses_status = '✅ Syarat terpenuhi'
            alasan = 'Semua syarat akses sudah lengkap'
        text += (
            f"\n🔐 <b>Akses Produk</b>\n"
            f"Status : {akses_status}\n"
            f"Alasan : {alasan}\n\n"
            f"📋 <b>Progress Syarat</b>\n"
            f"Progress : {progress['fulfilled_count']}/{progress['total_count']} terpenuhi\n\n"
            f"{progress['text']}\n"
        )

    sent_link = order.get('sent_link')
    if sent_link and sent_link != 'pending':
        text += f"\n🔗 <b>Link Terkirim</b>\n{esc(sent_link)}\n"

    text += (
        f"\n🕒 <b>Waktu</b>\n"
        f"{esc(order.get('waktu', '-'))}\n\n"
        f"🔖 <b>Order ID</b>\n"
        f"<code>{esc(order.get('order_id', '-'))}</code>"
    )
    return text

async def kirim_notif(bot, text: str, reply_markup=None):
    channel_id = await get_setting('notif_channel_id')
    target = int(channel_id) if channel_id else ADMIN_ID
    try:
        msg = await bot.send_message(chat_id=target, text=text, parse_mode="HTML", reply_markup=reply_markup)
        return msg.message_id
    except Exception as e:
        logger.error(f"[NOTIF] Gagal kirim ke {target}: {e}")
        if target != ADMIN_ID:
            try:
                msg = await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", reply_markup=reply_markup)
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

async def build_main_menu_text():
    products = await get_all_products()
    aktif_products = [p for p in products if p.get('aktif', True)]
    text = (
        "<b>🛒 HYPER FAMILY STORE</b>\n"
        "========================\n\n"
        "Selamat datang! Pilih paket yang tersedia:\n\n"
    )
    for p in aktif_products:
        text += (
            f"{esc(p['emoji'])} <b>{esc(p['nama']).upper()}</b>\n"
            f"- {esc(p['deskripsi'])}\n"
            f"- {format_harga(p['harga'])}\n\n"
        )
    text += (
        "========================\n"
        "💳 QRIS (All E-Wallet)  |  ⚡ 1-5 Menit  |  🕒 24 Jam"
    )
    return text

async def build_main_menu_keyboard():
    link_testi = await get_setting('link_testimoni', 'https://t.me/+7zsdSrwYIG8wOTg1')
    link_cs = await get_setting('link_admin', 'https://t.me/Kikukkvd')
    return [
        [InlineKeyboardButton("🛒 Beli Sekarang", callback_data="buy")],
        [
            InlineKeyboardButton("⭐ Testimoni", url=link_testi),
            InlineKeyboardButton("💬 Admin", url=link_cs)
        ]
    ]

def simpan_admin_msg(context, user_id, message_id):
    context.bot_data.setdefault('admin_messages', {})
    context.bot_data['admin_messages'].setdefault(user_id, [])
    context.bot_data['admin_messages'][user_id].append(message_id)

async def hapus_admin_msg(context, user_id):
    msg_ids = context.bot_data.get('admin_messages', {}).pop(user_id, [])
    for msg_id in msg_ids:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Gagal hapus pesan admin: {e}")

def simpan_msg_user(context, user_id, message_id):
    context.bot_data.setdefault('user_messages', {})
    context.bot_data['user_messages'].setdefault(user_id, [])
    context.bot_data['user_messages'][user_id].append(message_id)

async def hapus_msg_user_lama(context, user_id, keep_last=1):
    msgs = context.bot_data.get('user_messages', {}).get(user_id, [])
    if len(msgs) > keep_last:
        to_delete = msgs[:-keep_last]
        context.bot_data['user_messages'][user_id] = msgs[-keep_last:]
        for msg_id in to_delete:
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
            except Exception as e:
                logger.debug(f"Gagal hapus pesan user lama: {e}")

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
            f"Terima kasih telah berbelanja! 🙏\n\n"
            f"Bantu kami berkembang dengan memberikan ulasan di bawah ini:"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Beri Ulasan / Testimoni", callback_data=f"rate_start|{order_id}")],
            [
                InlineKeyboardButton("🔄 Kirim Ulang Link", callback_data=f"resendlink|{order_id}"),
                InlineKeyboardButton("💬 Chat Admin", url=await get_setting('link_admin', 'https://t.me/Kikukkvd'))
            ]
        ])
    )
    simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=2)
    return link

# =================== SECURITY: KRIPTOGRAFIS SIGNATURE VERIFIER ===================

def verify_telegram_webapp_signature(init_data: str, bot_token: str) -> bool:
    """Memverifikasi keaslian initData dari Telegram WebApp (Mini App)."""
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        if 'hash' not in parsed_data:
            return False
        
        received_hash = parsed_data.pop('hash')
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        return hmac.compare_digest(computed_hash, received_hash)
    except Exception as e:
        logger.error(f"[SECURITY] Gagal memverifikasi signature Telegram WebApp: {e}")
        return False

# =================== WEBHOOK SERVER (PAKASIR) ===================

async def pakasir_webhook_handler(request: aio_web.Request) -> aio_web.Response:
    if PAKASIR_WEBHOOK_SECRET:
        try:
            body = await request.read()
            sig = request.headers.get("X-Pakasir-Signature", "")
            expected = hmac.new(
                PAKASIR_WEBHOOK_SECRET.encode(),
                body,
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                logger.warning("[WEBHOOK] Signature tidak valid, request ditolak.")
                return aio_web.Response(status=401, text='invalid signature')
        except Exception as e:
            logger.error(f"[WEBHOOK] Error cek signature: {e}")
            return aio_web.Response(status=400, text='bad request')
    else:
        try:
            body = await request.read()
        except Exception:
            return aio_web.Response(status=400, text='invalid request')

    try:
        data = json.loads(body)
    except Exception:
        return aio_web.Response(status=400, text='invalid json')

    order_id = data.get('order_id')
    amount = data.get('amount')
    status = data.get('status')

    if status != 'completed':
        return aio_web.Response(text='ignored')

    if not order_id or amount is None:
        return aio_web.Response(status=400, text='missing fields')

    if not is_valid_order_id(order_id):
        logger.warning(f"[WEBHOOK] Order ID format tidak valid: {order_id}")
        return aio_web.Response(status=400, text='invalid order_id format')

    order = await get_order_by_id(order_id)
    if not order:
        return aio_web.Response(status=404, text='order not found')

    if order['status'] != 'waiting':
        return aio_web.Response(text='already processed')

    paket_id = order['paket_id']
    user_id = order['user_id']
    user_name = order.get('user_name', 'User')

    paket = await get_product(paket_id)
    if not paket:
        return aio_web.Response(status=404, text='product not found')

    incoming_amount = _safe_int(amount, -1)
    expected_amount = _safe_int(order.get('harga_dibayar'), 0) or _safe_int(paket.get('harga'), 0)
    if incoming_amount != expected_amount:
        logger.warning(
            f"[SECURITY ALERT] Amount webhook mismatch order={order_id} incoming={incoming_amount} expected={expected_amount}"
        )
        return aio_web.Response(status=400, text='amount mismatch')

    try:
        verified_detail = await get_transaction_detail(order_id, incoming_amount)
    except Exception as e:
        logger.error(f"[WEBHOOK] Error verifikasi transaksi: {e}")
        return aio_web.Response(status=502, text='verification service error')

    verified_amount = _safe_int((verified_detail or {}).get('amount'), incoming_amount)
    if (
        not verified_detail
        or verified_detail.get('status') != 'completed'
        or verified_amount != expected_amount
    ):
        logger.warning(f"[SECURITY ALERT] Percobaan webhook palsu diblokir! Order ID: {order_id}")
        return aio_web.Response(status=400, text='verification failed')

    if not _current_bot:
        logger.error(f"[WEBHOOK] Bot belum siap saat webhook masuk: {order_id}")
        return aio_web.Response(status=503, text='bot not ready')

    await _stop_payment_task(user_id)
    await _handle_payment_success(
        _current_bot, order_id, paket_id, user_id, user_name,
        expected_amount, {'amount': expected_amount, 'status': 'completed'}
    )

    logger.info(f"[WEBHOOK] ✅ Webhook berhasil diverifikasi & diproses: {order_id}")
    return aio_web.Response(text='ok')

_webhook_runner = None

# =================== REST API DASHBOARD ===================

from collections import defaultdict
import time as _time_module

DASHBOARD_API_KEY = os.environ.get('DASHBOARD_API_KEY')
DASHBOARD_URL = os.environ.get('DASHBOARD_URL', '')

# =================== RATE LIMITER ===================

_rate_limit_store: dict = defaultdict(list)
RATE_LIMIT_MAX_REQUESTS = 100
RATE_LIMIT_WINDOW_SECONDS = 60

def _check_rate_limit(client_id: str) -> bool:
    """Cek apakah client masih dalam batas rate limit. Return True jika diizinkan."""
    now = _time_module.time()
    _rate_limit_store[client_id] = [t for t in _rate_limit_store[client_id] if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(_rate_limit_store[client_id]) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    _rate_limit_store[client_id].append(now)
    return True

def _dashboard_html_path():
    base = os.path.dirname(os.path.abspath(__file__))
    for name in ('dashboard.html', 'premium_admin_dashboard.html'):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    for fname in os.listdir(base):
        if fname.startswith('premium_admin_dashboard') and fname.endswith('.html'):
            return os.path.join(base, fname)
    return None

def _allowed_cors_origin(request: aio_web.Request = None) -> str:
    """Batasi CORS ke DASHBOARD_URL jika diset; fallback '*' untuk dev lokal."""
    if not DASHBOARD_URL:
        return '*'
    allowed = {DASHBOARD_URL.rstrip('/')}
    origin = request.headers.get('Origin') if request else None
    if origin and origin.rstrip('/') in allowed:
        return origin
    return DASHBOARD_URL.rstrip('/')

def _cors_headers(request: aio_web.Request = None) -> dict:
    return {
        'Access-Control-Allow-Origin': _allowed_cors_origin(request),
        'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
        'Access-Control-Allow-Headers': 'Authorization, Content-Type',
        'Vary': 'Origin',
    }

async def _api_auth(request: aio_web.Request) -> bool:
    """Verifikasi API dashboard + pastikan Telegram WebApp user adalah admin."""
    client_id = request.remote or 'unknown'
    if not _check_rate_limit(client_id):
        logger.warning(f"[RATE LIMIT] Client {client_id} melebihi batas request")
        return False

    if not DASHBOARD_API_KEY:
        return False

    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return hmac.compare_digest(auth[7:], DASHBOARD_API_KEY)

    if auth.startswith('tma '):
        init_data_raw = auth[4:]
        if not verify_telegram_webapp_signature(init_data_raw, TOKEN):
            return False
        try:
            parsed = dict(urllib.parse.parse_qsl(init_data_raw))
            user_payload = json.loads(parsed.get('user', '{}'))
            user_id = int(user_payload.get('id', 0))
        except Exception as e:
            logger.warning(f"[API AUTH] initData valid tapi user payload tidak valid: {e}")
            return False
        return await is_admin(user_id)

    return False

def _json_resp(data, status=200, request: aio_web.Request = None):
    return aio_web.Response(
        status=status,
        content_type='application/json',
        headers=_cors_headers(request),
        text=json.dumps(data, default=str)
    )

def _err(msg, status=400, request: aio_web.Request = None):
    return _json_resp({'error': msg}, status, request=request)

async def _cors_handler(request: aio_web.Request) -> aio_web.Response:
    return aio_web.Response(headers=_cors_headers(request))

@aio_web.middleware
async def _api_error_middleware(request: aio_web.Request, handler):
    try:
        resp = await handler(request)
        resp.headers.update(_cors_headers(request))
        return resp
    except Exception as e:
        logger.error(f"[API ERROR] {request.method} {request.path}: {e}", exc_info=True)
        return aio_web.Response(
            status=500,
            content_type='application/json',
            headers=_cors_headers(request),
            text=json.dumps({'error': 'Internal server error'})
        )

async def api_serve_dashboard(request: aio_web.Request) -> aio_web.Response:
    path = _dashboard_html_path()
    if not path:
        return aio_web.Response(status=404, text='dashboard.html tidak ditemukan.')
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    return aio_web.Response(content_type='text/html', text=content)


def _validate_product_payload(data: dict, partial: bool = False):
    errors = []
    nama = str(data.get('nama', '')).strip()
    if not partial and not nama:
        errors.append('nama wajib diisi')

    if 'harga' in data or not partial:
        try:
            harga = int(data.get('harga', 0))
            if harga <= 0:
                errors.append('harga harus lebih dari 0')
        except (TypeError, ValueError):
            errors.append('harga harus angka')

    paket_id = str(data.get('paket_id') or make_paket_id(nama)).strip()
    if not partial and not re.match(r'^[a-z0-9_]{1,40}$', paket_id):
        errors.append('paket_id hanya boleh huruf kecil, angka, underscore, maksimal 40 karakter')

    for key in ('link',):
        val = data.get(key)
        if val and not (str(val).startswith('http://') or str(val).startswith('https://') or str(val).startswith('tg://')):
            errors.append(f'{key} harus berupa URL valid')

    if data.get('requires_paket_ids'):
        ids = _split_required_ids(data.get('requires_paket_ids'))
        if any(not re.match(r'^[a-z0-9_]{1,40}$', pid) for pid in ids):
            errors.append('requires_paket_ids berisi paket_id tidak valid')

    return errors

async def api_get_products(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    return _json_resp(await get_all_products())

async def api_post_product(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    try:
        data = await request.json()
    except Exception:
        return _err('Invalid JSON')
    errors = _validate_product_payload(data, partial=False)
    if errors:
        return _err('; '.join(errors))
    paket_id = data.get('paket_id') or make_paket_id(data.get('nama', ''))
    await add_product(
        paket_id=paket_id, nama=data.get('nama', ''), emoji=data.get('emoji', '📦'),
        deskripsi=data.get('deskripsi', ''), harga=int(data.get('harga', 0)),
        link=data.get('link'), group_chat_id=data.get('group_chat_id') or None
    )
    if data.get('requires_paket_ids'):
        await update_product_field(paket_id, 'requires_paket_ids', data['requires_paket_ids'])
    return _json_resp({'ok': True, 'paket_id': paket_id})

async def api_put_product(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    paket_id = request.match_info['paket_id']
    try:
        data = await request.json()
    except Exception:
        return _err('Invalid JSON')
    errors = _validate_product_payload(data, partial=True)
    if errors:
        return _err('; '.join(errors))
    allowed = {'nama', 'emoji', 'deskripsi', 'harga', 'link', 'group_chat_id', 'aktif', 'requires_paket_ids'}
    for field, value in data.items():
        if field in allowed:
            if field == 'harga':
                value = int(value)
            await update_product_field(paket_id, field, value)
    return _json_resp({'ok': True})

async def api_delete_product(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    await delete_product(request.match_info['paket_id'])
    return _json_resp({'ok': True})

@async_wrap
def _get_all_orders_api():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT o.*, p.nama as paket_nama, p.emoji as paket_emoji
                FROM orders o
                LEFT JOIN products p ON o.paket_id = p.paket_id
                ORDER BY o.id DESC LIMIT 500
            """)
            return [dict(r) for r in c.fetchall()]

async def api_get_orders(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    return _json_resp(await _get_all_orders_api())

async def api_confirm_order(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    order_id = request.match_info['order_id']
    order = await get_order_by_id(order_id)
    if not order:
        return _err('Order tidak ditemukan', 404)
    if order['status'] != 'waiting':
        return _err(f"Status order: {order['status']}, bukan waiting")
    if not _current_bot:
        return _err('Bot tidak tersedia', 503)

    ok = await mark_order_completed(order_id)
    if not ok:
        return _err('Gagal konfirmasi, mungkin sudah diproses')

    await _process_completed_order_delivery(
        _current_bot,
        order_id=order_id,
        paket_id=order['paket_id'],
        user_id=order['user_id'],
        user_name=order.get('user_name', 'User'),
        paid_amount=order.get('harga_dibayar') or 0,
        source_title="✅ <b>DIKONFIRMASI ADMIN DASHBOARD</b>"
    )
    return _json_resp({'ok': True, 'delivery_status': 'processed'})

async def api_cancel_order(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    order_id = request.match_info['order_id']
    order = await get_order_by_id(order_id)
    if not order:
        return _err('Order tidak ditemukan', 404)
    paket = await get_product(order['paket_id']) or {'harga': 0}
    cancel_amount = order.get('harga_dibayar') or paket.get('harga', 0)
    if cancel_amount:
        await cancel_transaction(order_id, cancel_amount)
    await update_order_status(order_id, 'cancelled')
    if _current_bot:
        try:
            await _current_bot.send_message(
                chat_id=order['user_id'],
                text=(
                    "<b>❌ PESANAN DIBATALKAN</b>\n========================\n\n"
                    "Pesanan kamu dibatalkan oleh admin.\n"
                    "Ketik /start untuk membuat pesanan baru."
                ),
                parse_mode='HTML'
            )
        except Exception as e:
            logger.debug(f"Gagal kirim pembatalan via API ke buyer: {e}")
    return _json_resp({'ok': True})

@async_wrap
def _get_all_testimonials_api():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM testimonials ORDER BY created_at DESC")
            return [dict(r) for r in c.fetchall()]

async def api_get_testimonials(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    return _json_resp(await _get_all_testimonials_api())

async def api_testimonial_action(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    order_id = request.match_info['order_id']
    try:
        data = await request.json()
    except Exception:
        return _err('Invalid JSON')
    action = data.get('action')
    if action not in ('approve', 'reject'):
        return _err('action harus approve atau reject')
    status = 'approved' if action == 'approve' else 'rejected'
    await update_testimonial_status(order_id, status)
    if action == 'approve' and _current_bot:
        channel_id = await get_setting('testimoni_channel_id')
        if channel_id:
            testi = await get_testimonial_by_order(order_id)
            if testi:
                stars = '★' * (testi['rating'] or 0) + '☆' * (5 - (testi['rating'] or 0))
                try:
                    await _current_bot.send_message(
                        chat_id=int(channel_id),
                        text=(
                            f"⭐ <b>Testimoni Pembeli</b>\n\n"
                            f"👤 {esc(samarkan_nama(testi.get('user_name', 'Anonim')))}\n"
                            f"📦 Paket: <code>{esc(testi.get('paket_id', '-'))}</code>\n"
                            f"⭐ {stars}\n\n"
                            f"💬 {esc(testi.get('review') or '-')}"
                        ),
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.error(f"[API TESTI] Gagal post ke channel: {e}")
    return _json_resp({'ok': True})

async def api_get_banned(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    return _json_resp(await get_all_banned())

async def api_post_ban(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    try:
        data = await request.json()
    except Exception:
        return _err('Invalid JSON')
    user_id = data.get('user_id')
    if not user_id:
        return _err('user_id diperlukan')
    await ban_user(int(user_id), data.get('reason', ''))
    return _json_resp({'ok': True})

async def api_delete_ban(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    await unban_user(int(request.match_info['user_id']))
    return _json_resp({'ok': True})

async def api_get_admins(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    return _json_resp(await get_all_admins())

async def api_post_admin(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    try:
        data = await request.json()
    except Exception:
        return _err('Invalid JSON')
    user_id = data.get('user_id')
    if not user_id:
        return _err('user_id diperlukan')
    await add_admin(int(user_id), data.get('nama', str(user_id)), ADMIN_ID)
    return _json_resp({'ok': True})

async def api_delete_admin(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    await remove_admin(int(request.match_info['user_id']))
    return _json_resp({'ok': True})

async def api_get_stats(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    today = now_wib().replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)
    stats = await get_order_stats(today, month)
    if 'trend_7d' in stats:
        formatted = []
        for row in stats['trend_7d']:
            d = row.get('hari')
            formatted.append({
                'date': d.strftime('%d/%m') if hasattr(d, 'strftime') else str(d),
                'day': d.strftime('%d/%m') if hasattr(d, 'strftime') else str(d),
                'rev': int(row.get('rev', 0)),
                'cnt': int(row.get('cnt', 0)),
            })
        stats['trend_7d'] = formatted
    return _json_resp(stats)

async def api_get_groups(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    return _json_resp(await get_managed_groups())

async def api_kick_check(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    user_id = int(request.match_info['user_id'])
    groups = await get_managed_groups()
    results = []
    for grp in groups:
        chat_id = grp.get('chat_id') or grp.get('id')
        title = grp.get('title', str(chat_id))
        is_member = False
        if _current_bot and chat_id:
            try:
                member = await _current_bot.get_chat_member(chat_id=int(chat_id), user_id=user_id)
                is_member = member.status not in ('left', 'kicked', 'banned')
            except Exception as e:
                logger.debug(f"Gagal verifikasi keanggotaan grup {chat_id}: {e}")
        results.append({'chat_id': chat_id, 'title': title, 'is_member': is_member})
    return _json_resp({'groups': results})

async def api_kick_execute(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    try:
        data = await request.json()
    except Exception:
        return _err('Invalid JSON')
    user_id = data.get('user_id')
    chat_id = data.get('chat_id')
    if not user_id or not chat_id:
        return _err('user_id dan chat_id diperlukan')
    if not _current_bot:
        return _err('Bot tidak tersedia')
    try:
        await _current_bot.ban_chat_member(chat_id=int(chat_id), user_id=int(user_id))
        await _current_bot.unban_chat_member(chat_id=int(chat_id), user_id=int(user_id))
        return _json_resp({'ok': True})
    except Exception as e:
        return _err(f'Gagal kick: {str(e)}')

async def api_broadcast(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    try:
        data = await request.json()
    except Exception:
        return _err('Invalid JSON')
    text = data.get('text', '').strip()
    if not text:
        return _err('text diperlukan')
    if not _current_bot:
        return _err('Bot tidak tersedia')
    buyers = await get_all_buyers()
    sent = 0
    failed = 0
    for b in buyers:
        try:
            await _current_bot.send_message(chat_id=b['user_id'], text=text, parse_mode='HTML')
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.debug(f"Broadcast gagal terkirim ke buyer {b['user_id']}: {e}")
            failed += 1
    return _json_resp({'ok': True, 'sent': sent, 'failed': failed, 'total': len(buyers)})

async def api_post_settings(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    try:
        data = await request.json()
    except Exception:
        return _err('Invalid JSON')
    allowed_keys = {'maintenance', 'link_testimoni', 'link_admin', 'notif_channel_id',
                    'testimoni_channel_id', 'webhook_secret'}
    for key, value in data.items():
        if key in allowed_keys:
            await set_setting(key, value if value else None)
    return _json_resp({'ok': True})

@async_wrap
def _get_backup_data():
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM products ORDER BY harga ASC")
            products = [dict(r) for r in c.fetchall()]
            c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1000")
            orders = [dict(r) for r in c.fetchall()]
            c.execute("SELECT * FROM testimonials ORDER BY created_at DESC")
            testimonials = [dict(r) for r in c.fetchall()]
            c.execute("SELECT * FROM banned_users")
            banned = [dict(r) for r in c.fetchall()]
            c.execute("SELECT * FROM settings")
            settings = [dict(r) for r in c.fetchall()]
        return {'products': products, 'orders': orders, 'testimonials': testimonials,
                'banned': banned, 'settings': settings}

async def api_backup(request: aio_web.Request) -> aio_web.Response:
    if not await _api_auth(request): return _err('Unauthorized', 401)
    data = await _get_backup_data()
    filename = f"backup_{now_wib().strftime('%Y%m%d_%H%M')}.json"
    return aio_web.Response(
        status=200,
        content_type='application/json',
        headers={
            'Access-Control-Allow-Origin': _allowed_cors_origin(request),
            'Vary': 'Origin',
            'Content-Disposition': f'attachment; filename="{filename}"',
        },
        text=json.dumps(data, default=str)
    )

# =================== WEB SERVER MANAGEMENT ===================

async def _start_webhook_server():
    global _webhook_runner
    webhook_app = aio_web.Application(middlewares=[_api_error_middleware])

    # Endpoints Webhook & Status
    webhook_app.router.add_post('/webhook/pakasir', pakasir_webhook_handler)
    webhook_app.router.add_get('/health', lambda r: aio_web.Response(text='ok'))
    webhook_app.router.add_get('/', lambda r: aio_web.Response(text='Hyper Family Store Bot - OK'))

    # Dashboard Panel
    webhook_app.router.add_get('/dashboard', api_serve_dashboard)

    # CORS Preflight
    webhook_app.router.add_options('/{path_info:.*}', _cors_handler)

    # REST APIs
    webhook_app.router.add_get('/api/products', api_get_products)
    webhook_app.router.add_post('/api/products', api_post_product)
    webhook_app.router.add_put('/api/products/{paket_id}', api_put_product)
    webhook_app.router.add_delete('/api/products/{paket_id}', api_delete_product)

    webhook_app.router.add_get('/api/orders', api_get_orders)
    webhook_app.router.add_post('/api/orders/{order_id}/confirm', api_confirm_order)
    webhook_app.router.add_post('/api/orders/{order_id}/cancel', api_cancel_order)

    webhook_app.router.add_get('/api/testimonials', api_get_testimonials)
    webhook_app.router.add_post('/api/testimonials/{order_id}/action', api_testimonial_action)

    webhook_app.router.add_get('/api/banned', api_get_banned)
    webhook_app.router.add_post('/api/banned', api_post_ban)
    webhook_app.router.add_delete('/api/banned/{user_id}', api_delete_ban)

    webhook_app.router.add_get('/api/admins', api_get_admins)
    webhook_app.router.add_post('/api/admins', api_post_admin)
    webhook_app.router.add_delete('/api/admins/{user_id}', api_delete_admin)

    webhook_app.router.add_get('/api/stats', api_get_stats)
    webhook_app.router.add_get('/api/groups', api_get_groups)
    webhook_app.router.add_get('/api/kick/check/{user_id}', api_kick_check)
    webhook_app.router.add_post('/api/kick/execute', api_kick_execute)

    webhook_app.router.add_post('/api/broadcast', api_broadcast)
    webhook_app.router.add_post('/api/settings', api_post_settings)
    webhook_app.router.add_get('/api/backup', api_backup)

    runner = aio_web.AppRunner(webhook_app)
    await runner.setup()
    _webhook_runner = runner
    port = int(os.environ.get('PORT', 8080))
    site = aio_web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"[WEBHOOK] Server API berhasil berjalan di port {port}")

# =================== POST INIT ===================

async def post_init(application: Application):
    global _current_bot, _http_session
    _current_bot = application.bot
    _http_session = aiohttp.ClientSession()

    await application.bot.set_my_commands(
        [
            BotCommand("start",   "Buka toko"),
            BotCommand("help",    "Bantuan penggunaan bot"),
            BotCommand("riwayat", "Lihat riwayat ordermu"),
        ],
        scope=BotCommandScopeDefault()
    )
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Buka toko"),
            BotCommand("help", "Bantuan"),
            BotCommand("admin", "Panel admin"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID)
    )

    waiting_orders = await get_all_waiting()
    if waiting_orders:
        logger.info(f"[POST_INIT] Menemukan {len(waiting_orders)} order aktif, memulihkan background task pembayaran...")
        for order in waiting_orders:
            paket = await get_product(order['paket_id'])
            if not paket:
                continue
            recovery_amount = order.get('harga_dibayar') or paket['harga']
            if order.get('created_at'):
                try:
                    elapsed = int((now_wib() - order['created_at']).total_seconds())
                    recovery_timeout = max(300, 1800 - elapsed)
                except Exception as e:
                    logger.debug(f"Gagal menghitung selisih durasi order untuk recovery: {e}")
                    recovery_timeout = 300
            else:
                recovery_timeout = 300
            _start_payment_task(
                application.bot,
                order_id=order['order_id'],
                paket_id=order['paket_id'],
                user_id=order['user_id'],
                user_name=order.get('user_name', 'User'),
                amount=recovery_amount,
                timeout_seconds=recovery_timeout
            )
            logger.info(f"[POST_INIT] Task monitoring diaktifkan kembali untuk Order ID: {order['order_id']}")

    asyncio.create_task(_auto_backup_loop())
    asyncio.create_task(_buyer_reminder_loop(_current_bot))
    asyncio.create_task(_cleanup_cooldowns_loop())
    asyncio.create_task(_start_webhook_server())
    logger.info("[POST_INIT] Seluruh background task berkala berhasil dijadwalkan.")

# =================== GRACEFUL SHUTDOWN ===================

async def post_shutdown(application: Application):
    global _http_session, _pool, _webhook_runner
    logger.info("[SHUTDOWN] Memulai siklus anggun mematikan sistem (Graceful Shutdown)...")
    if _http_session and not _http_session.closed:
        await _http_session.close()
        logger.info("[SHUTDOWN] Sesi client HTTP asinkron ditutup.")
    if _webhook_runner:
        await _webhook_runner.cleanup()
        logger.info("[SHUTDOWN] Web Server API berhasil dibersihkan.")
    if _pool:
        _pool.closeall()
        logger.info("[SHUTDOWN] Seluruh koneksi ke DB PostgreSQL ditutup dengan aman.")
    logger.info("[SHUTDOWN] Shutdown selesai.")

# =================== USER HANDLERS ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Jalankan semua pengecekan awal secara paralel untuk respons lebih cepat
    is_admin_flag, maint, banned, sisa, active = await asyncio.gather(
        is_admin(user_id, context),
        is_maintenance(),
        is_banned(user_id),
        get_cooldown_sisa_db(user_id),
        get_active_order(user_id),
    )

    if not is_admin_flag and maint:
        await update.message.reply_text(
            "⚙️ <b>BOT SEDANG MAINTENANCE</b>\n"
            "========================\n\n"
            "Bot sedang dalam perbaikan sementara.\n"
            "Silakan coba lagi nanti.\n\n"
            "Hubungi admin: @Kikukkvd",
            parse_mode="HTML"
        )
        return

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
        await update.message.reply_text(
            f"⏳ <b>Cooldown Aktif</b>\n"
            f"========================\n\n"
            f"Kamu baru saja membatalkan pesanan. Tunggu <b>{sisa} menit</b> lagi sebelum bisa membuat order baru.\n\n"
            f"⏰ Cooldown berakhir sekitar pukul <b>{(now_wib() + timedelta(minutes=sisa)).strftime('%H:%M')} WIB</b>",
            parse_mode="HTML"
        )
        return
    if active:
        paket = await get_product(active["paket_id"])
        if not paket:
            paket = {"emoji": "📦", "nama": "Produk", "harga": 0, "link": DEFAULT_LINK}

        trans = await get_transaction_detail(active["order_id"], active.get("harga_dibayar") or paket["harga"])

        if trans and trans.get("status") == "completed":
            success = await mark_order_completed(active["order_id"])
            if success:
                await _stop_payment_task(user_id)
                paid_amount = trans.get("amount", paket["harga"])
                await _process_completed_order_delivery(
                    context.bot,
                    order_id=active["order_id"],
                    paket_id=active["paket_id"],
                    user_id=user_id,
                    user_name=active.get('user_name', update.effective_user.full_name),
                    paid_amount=paid_amount,
                    source_title="✅ <b>ORDER BERHASIL</b>"
                )
            return

        total = (trans.get("amount", paket["harga"]) + trans.get("fee", 0)) if trans else (active.get("harga_dibayar") or paket["harga"])
        text = (
            f"<b>⏳ ORDER AKTIF</b>\n"
            f"========================\n\n"
            f"Kamu masih punya pesanan yang belum dibayar:\n\n"
            f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
            f"💰 Total: {format_harga(total)}\n"
            f"📝 Order ID: <code>{esc(active['order_id'])}</code>\n\n"
            f"<i>Silakan selesaikan pembayaran atau batalkan pesanan dulu.</i>"
        )
        keyboard = [[InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="cancel_order")]]
        msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        return

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=await build_main_menu_text(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(await build_main_menu_keyboard())
    )
    simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=1)

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
        "/riwayat - Lihat riwayat order kamu\n\n"
        "<b>❓ PERTANYAAN UMUM:</b>\n\n"
        "<b>Q: Bagaimana cara membayar?</b>\n"
        "A: Scan QRIS yang muncul dengan e-wallet (GoPay, OVO, Dana, dll).\n\n"
        "<b>Q: Berapa lama prosesnya?</b>\n"
        "A: Pembayaran otomatis terverifikasi dalam 1-5 menit.\n\n"
        "<b>Q: Link tidak masuk?</b>\n"
        "A: Ketik /start untuk cek status, atau hubungi admin.\n\n"
        "<b>Q: Bisa ganti paket?</b>\n"
        "A: Bisa, maksimal 1x ganti paket sebelum bayar.\n\n"
        "========================\n"
        "💬 Butuh bantuan? Hubungi admin:"
    )
    link_admin = await get_setting('link_admin', 'https://t.me/Kikukkvd')
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Hubungi Admin", url=link_admin)]
        ])
    )

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    # Jalankan semua pengecekan awal secara paralel
    is_admin_flag, maint, banned = await asyncio.gather(
        is_admin(user_id, context),
        is_maintenance(),
        is_banned(user_id),
    )

    if not is_admin_flag and maint:
        await query.answer("⚙️ Bot sedang maintenance. Coba lagi nanti.", show_alert=True)
        return

    if banned:
        await query.answer("🚫 Akun kamu diblokir. Hubungi admin.", show_alert=True)
        return

    await query.answer()

    products = await get_all_products()
    aktif = [p for p in products if p.get('aktif', True)]
    text = "<b>📦 PILIH PAKET</b>\n========================\n\n"
    for p in aktif:
        text += (
            f"{esc(p['emoji'])} <b>{esc(p['nama']).upper()}</b>\n"
            f"- {esc(p['deskripsi'])}\n"
            f"- Harga: {format_harga(p['harga'])}\n"
            f"- Status: Tersedia ✅\n\n"
        )
    text += "========================"

    keyboard = [
        [InlineKeyboardButton(f"{p['emoji']} {p['nama']} - {format_harga(p['harga'])}", callback_data=f"pilih_{p['paket_id']}")]
        for p in aktif
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Kembali", callback_data="back_to_menu")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def pilih_paket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user_name = query.from_user.full_name

    if await is_banned(user_id):
        await query.answer("🚫 Akun kamu diblokir. Hubungi admin.", show_alert=True)
        return

    paket_id = query.data.replace("pilih_", "")
    paket = await get_product(paket_id)
    if not paket:
        await query.answer("❌ Produk tidak ditemukan.", show_alert=True)
        return

    active = await get_active_order(user_id)
    if active:
        paket_active = await get_product(active["paket_id"]) or {"emoji": "📦", "nama": "Produk", "harga": 0}
        trans = await get_transaction_detail(active["order_id"], active.get("harga_dibayar") or paket_active["harga"])
        if trans and trans.get("status") == "completed":
            await query.answer("✅ Pembayaran sudah diterima!", show_alert=True)
            return
        await query.answer("⏳ Kamu sudah punya invoice aktif!", show_alert=True)
        total = (trans.get("amount", paket_active["harga"]) + trans.get("fee", 0)) if trans else (active.get("harga_dibayar") or paket_active["harga"])
        caption = (
            f"<b>⏳ ORDER AKTIF</b>\n"
            f"========================\n\n"
            f"📦 Paket: {esc(paket_active['emoji'])} {esc(paket_active['nama'])}\n"
            f"💰 Total: {format_harga(total)}\n"
            f"📝 Order ID: <code>{esc(active['order_id'])}</code>\n\n"
            f"⚠️ Selesaikan pembayaran atau batalkan dulu."
        )
        keyboard = [[InlineKeyboardButton("❌ Batalkan", callback_data="cancel_order")]]
        await query.edit_message_text(caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    sisa = await get_cooldown_sisa_db(user_id)
    if sisa > 0:
        await query.answer(
            f"⏳ Kamu baru saja membatalkan order. Coba lagi dalam {sisa} menit.",
            show_alert=True
        )
        return

    await query.answer()
    await _buat_order_baru(update, context, query, user_id, user_name, paket, order_changes=0)

async def prereq_buy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user_name = query.from_user.full_name

    if await is_banned(user_id):
        await query.answer("🚫 Akun kamu diblokir. Hubungi admin.", show_alert=True)
        return

    parts = query.data.split("|")
    prereq_pid = parts[0].replace("prereq_buy_", "")
    parent_order_id = parts[1] if len(parts) > 1 else None

    paket = await get_product(prereq_pid)
    if not paket:
        await query.answer("❌ Produk tidak ditemukan.", show_alert=True)
        return

    active = await get_active_order(user_id)
    if active:
        await query.answer("⏳ Kamu sudah punya invoice aktif! Batalkan dulu.", show_alert=True)
        return

    sisa = await get_cooldown_sisa_db(user_id)
    if sisa > 0:
        await query.answer(
            f"⏳ Kamu baru saja membatalkan order. Coba lagi dalam {sisa} menit.",
            show_alert=True
        )
        return

    if parent_order_id:
        parent_order = await get_order_by_id(parent_order_id)
        if parent_order:
            context.user_data['prereq_ctx'] = {
                'parent_order_id': parent_order_id,
                'parent_paket_id': parent_order['paket_id'],
            }

    await query.answer()
    await _buat_order_baru(update, context, query, user_id, user_name, paket, order_changes=0)

async def _buat_order_baru(update, context, query, user_id, user_name, paket, order_changes=0):
    # Kirim typing indicator agar user tahu bot sedang memproses
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    except Exception:
        pass

    loading_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ Membuat invoice...",
    )

    rand_suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    order_id = f"HFB-{user_id}-{now_wib().strftime('%Y%m%d%H%M%S')}-{rand_suffix}"

    trans_data = await create_transaction_qris(
        order_id=order_id,
        amount=paket["harga"],
        description=f"Hyper Family Buy - {paket['nama']}"
    )

    try:
        await loading_msg.delete()
    except Exception as e:
        logger.debug(f"Gagal menghapus pesan loading: {e}")

    if not trans_data:
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Gagal membuat invoice. Silakan coba lagi.\nKetik /start untuk memulai ulang.",
        )
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        return

    qris_string = trans_data.get('payment_number', '')
    if not qris_string:
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

    await save_order(user_id, user_name, paket['paket_id'], order_id,
                     harga_dibayar=total_payment, order_changes=order_changes)

    QRIS_TIMEOUT_MENIT = 30
    expire = (now_wib() + timedelta(minutes=QRIS_TIMEOUT_MENIT)).strftime("%H:%M")

    qr_buffer = await asyncio.to_thread(generate_qr_image, qris_string)

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

    if sisa_ganti > 0:
        kb = [
            [InlineKeyboardButton(f"🔄 Ganti Paket (sisa {sisa_ganti}x)", callback_data="ganti_paket_list")],
            [InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="cancel_order")],
        ]
    else:
        kb = [[InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="cancel_order")]]

    if query:
        try:
            await query.message.delete()
        except Exception as e:
            logger.debug(f"Gagal menghapus pesan sebelumnya: {e}")

    msg = await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=qr_buffer,
        caption=caption,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )

    await set_buyer_msg_id(order_id, msg.message_id)
    simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=1)

    msg_id = await kirim_notif(
        context.bot,
        _format_order_notif(
            "📢 <b>ORDER BARU MASUK</b>",
            user_name, user_id, paket, order_id,
            amount=total_payment,
            extra=f"⏰ Berlaku: {expire} WIB · ⏳ Menunggu pembayaran"
        )
    )
    if msg_id:
        await set_admin_msg_id(order_id, msg_id)

    timeout_secs = QRIS_TIMEOUT_MENIT * 60

    _start_payment_task(
        context.bot, order_id, paket['paket_id'], user_id, user_name,
        total_payment, timeout_seconds=timeout_secs
    )

# =================== CANCEL ORDER ===================

async def cancel_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    active = await get_active_order(user_id)
    if active:
        paket = await get_product(active["paket_id"])
        amount = active.get("harga_dibayar") or (paket["harga"] if paket else 0)
        if amount:
            await cancel_transaction(active["order_id"], amount)
        await update_order_status(active["order_id"], "cancelled")
        await _stop_payment_task(user_id)
        await hapus_qris_buyer_lama(context.bot, active["order_id"], user_id)

        cancelled_order_id = active["order_id"]
        await hapus_notif_lama(context.bot, cancelled_order_id)

        paket_notif = await get_product(active["paket_id"]) or {"emoji": "📦", "nama": active["paket_id"]}
        msg_id = await kirim_notif(
            context.bot,
            _format_order_notif(
                "❌ <b>DIBATALKAN BUYER</b>",
                query.from_user.full_name, user_id, paket_notif, cancelled_order_id
            )
        )
        if msg_id:
            await set_admin_msg_id(cancelled_order_id, msg_id)

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

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=await build_main_menu_text(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(await build_main_menu_keyboard())
    )
    simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=1)

# =================== BACK TO MENU ===================

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    try:
        await query.message.delete()
    except Exception as e:
        logger.debug(f"Gagal menghapus pesan kembali ke menu: {e}")

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=await build_main_menu_text(),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(await build_main_menu_keyboard())
    )
    simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=1)

# =================== GANTI PAKET ===================

async def ganti_paket_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    active = await get_active_order(user_id)
    if not active:
        await query.answer("❌ Tidak ada pesanan aktif.", show_alert=True)
        return

    changes_used = active.get('order_changes', 0)
    if changes_used >= 1:
        await query.answer("⛔ Batas ganti paket sudah tercapai (1x).", show_alert=True)
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
        await query.answer("❌ Tidak ada paket lain yang tersedia.", show_alert=True)
        return

    await query.answer()
    try:
        await query.message.edit_caption(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def ganti_paket_konfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    new_paket_id = query.data.split("|", 1)[1]
    new_paket = await get_product(new_paket_id)
    if not new_paket:
        await query.answer("❌ Produk tidak ditemukan.", show_alert=True)
        return

    active = await get_active_order(user_id)
    if not active:
        await query.answer("❌ Pesanan aktif tidak ditemukan.", show_alert=True)
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
            InlineKeyboardButton("✅ Ya, Ganti", callback_data=f"ganti_paket_exec|{new_paket_id}"),
            InlineKeyboardButton("❌ Batal", callback_data="ganti_paket_batal"),
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

    active = await get_active_order(user_id)
    if not active:
        await query.answer("❌ Pesanan aktif tidak ditemukan.", show_alert=True)
        return

    changes_used = active.get('order_changes', 0)
    if changes_used >= 1:
        await query.answer("⛔ Batas ganti paket sudah tercapai (1x).", show_alert=True)
        return

    new_paket_id = query.data.split("|", 1)[1]
    new_paket = await get_product(new_paket_id)
    if not new_paket:
        await query.answer("❌ Produk tidak ditemukan.", show_alert=True)
        return

    await query.answer("⏳ Memproses pergantian paket...")

    old_order_id = active["order_id"]
    old_paket = await get_product(active["paket_id"])
    amount_old = active.get("harga_dibayar") or (old_paket["harga"] if old_paket else 0)
    if amount_old:
        await cancel_transaction(old_order_id, amount_old)
    await update_order_status(old_order_id, "cancelled")
    await _stop_payment_task(user_id)
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

    active = await get_active_order(user_id)
    if not active:
        await query.answer("❌ Pesanan tidak ditemukan.", show_alert=True)
        return

    paket = await get_product(active['paket_id'])
    if not paket:
        await query.answer("❌ Produk tidak ditemukan.", show_alert=True)
        return

    await query.answer()
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
            [InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="cancel_order")],
        ]
    else:
        kb = [[InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="cancel_order")]]

    try:
        await query.message.edit_caption(caption_back, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        logger.debug(f"Gagal update tampilan pembatalan ganti paket: {e}")

# =================== ASYNCIO MONITORING PAYMENT TASKS ===================

_payment_tasks: dict = {}
_payment_tasks_lock = asyncio.Lock()
_current_bot = None

async def _stop_payment_task(user_id: int):
    async with _payment_tasks_lock:
        task = _payment_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()

def _start_payment_task(bot, order_id: str, paket_id: str, user_id: int,
                         user_name: str, amount: int, timeout_seconds: int = 1800):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    loop.create_task(
        _start_payment_task_async(bot, order_id, paket_id, user_id, user_name, amount, timeout_seconds)
    )

async def _start_payment_task_async(bot, order_id: str, paket_id: str, user_id: int,
                                     user_name: str, amount: int, timeout_seconds: int = 1800):
    await _stop_payment_task(user_id)
    task = asyncio.create_task(
        _payment_poll_loop(bot, order_id, paket_id, user_id, user_name, amount, timeout_seconds)
    )
    async with _payment_tasks_lock:
        _payment_tasks[user_id] = task

def _check_order_status_sync(order_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT status FROM orders WHERE order_id=%s", (order_id,))
            row = c.fetchone()
            return dict(row) if row else None

async def _check_order_status(order_id):
    return await asyncio.to_thread(_check_order_status_sync, order_id)

async def _payment_poll_loop(bot, order_id: str, paket_id: str, user_id: int,
                              user_name: str, amount: int, timeout_seconds: int):
    elapsed = 0
    try:
        while elapsed < timeout_seconds:
            await asyncio.sleep(30)
            elapsed += 30

            row = await _check_order_status(order_id)
            if not row or row['status'] != 'waiting':
                return

            trans = await get_transaction_detail(order_id, amount)
            if not trans:
                continue

            if trans.get('status') == 'completed':
                await _handle_payment_success(bot, order_id, paket_id, user_id, user_name, amount, trans)
                return

        row = await _check_order_status(order_id)
        if not row or row['status'] != 'waiting':
            return

        trans = await get_transaction_detail(order_id, amount)
        if trans and trans.get('status') == 'completed':
            await _handle_payment_success(bot, order_id, paket_id, user_id, user_name, amount, trans)
            return

        if amount:
            await cancel_transaction(order_id, amount)
        await update_order_status(order_id, 'expired')
        await hapus_qris_buyer_lama(bot, order_id, user_id)
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

        await hapus_notif_lama(bot, order_id)

        paket_exp = await get_product(paket_id) or {"emoji": "📦", "nama": paket_id}
        msg_id = await kirim_notif(
            bot,
            _format_order_notif(
                "⏰ <b>ORDER EXPIRED</b>",
                user_name, user_id, paket_exp, order_id,
                extra="Buyer tidak bayar sampai waktu habis"
            )
        )
        if msg_id:
            await set_admin_msg_id(order_id, msg_id)

    except asyncio.CancelledError:
        pass
    finally:
        current_task = asyncio.current_task()
        async with _payment_tasks_lock:
            if _payment_tasks.get(user_id) is current_task:
                _payment_tasks.pop(user_id, None)

def _split_required_ids(requires_str: str) -> list:
    return [pid.strip() for pid in (requires_str or '').split(',') if pid.strip()]

async def _build_prereq_progress(user_id: int, requires_str: str, parent_order_id: str = None) -> dict:
    all_req_ids = _split_required_ids(requires_str)
    missing = await check_prerequisites_sync(user_id, requires_str)
    missing_set = set(missing)

    lines = []
    buttons = []
    for pid in all_req_ids:
        p_obj = await get_product(pid)
        label = f"{p_obj['emoji']} {p_obj['nama']}" if p_obj else pid
        if pid in missing_set:
            lines.append(f"❌ {esc(label)}")
            if parent_order_id:
                buttons.append([InlineKeyboardButton(
                    f"🛒 Beli {p_obj['nama'] if p_obj else pid}",
                    callback_data=f"prereq_buy_{pid}|{parent_order_id}"
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

async def _send_delivery_failed_alert(bot, order_id: str, user_id: int, user_name: str, paket: dict, error: Exception):
    await reset_delivery_claim(order_id, str(error)[:500])
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

    await bot.send_message(
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
            f"Terima kasih telah berbelanja! 🙏\n\n"
            f"Bantu kami berkembang dengan memberikan ulasan di bawah ini:"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Beri Ulasan / Testimoni", callback_data=f"rate_start|{order_id}")]
        ])
    )
    return link, bool(group_link)

async def _send_prereq_hold_message(bot, user_id: int, order_id: str, paket: dict, paid_amount: int, progress: dict):
    await mark_delivery_held(order_id, 'Syarat belum terpenuhi')
    product_name = f"{paket.get('emoji','📦')} {paket.get('nama','Produk')}"
    await bot.send_message(
        chat_id=user_id,
        text=(
            f"✅ <b>ORDER BERHASIL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>Produk</b>\n"
            f"Paket : {esc(product_name)}\n"
            f"Total : {format_harga(paid_amount)}\n\n"
            f"🔐 <b>Akses Produk</b>\n"
            f"Status : ⏸️ Link {esc(paket.get('nama','Produk'))} ditahan sementara\n"
            f"Alasan : Kamu belum memenuhi semua syarat akses\n\n"
            f"📋 <b>Progress Syarat {esc(paket.get('nama','Produk'))}</b>\n"
            f"Progress : {progress['fulfilled_count']}/{progress['total_count']} terpenuhi\n\n"
            f"{progress['text']}\n\n"
            f"Silakan lengkapi produk yang masih bertanda ❌.\n"
            f"Setelah semua syarat terpenuhi, link {esc(paket.get('nama','Produk'))} akan otomatis dikirim.\n\n"
            f"🔖 <b>Order ID</b>\n"
            f"<code>{esc(order_id)}</code>"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(progress['buttons']) if progress['buttons'] else None
    )

async def _notify_parent_prereq_progress(bot, user_id: int, user_name: str, parent_order: dict,
                                          parent_paket: dict, progress: dict, purchased_paket_id: str):
    if purchased_paket_id not in progress['all_ids']:
        return
    if progress['missing']:
        missing_count = len(progress['missing'])
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"📋 <b>UPDATE PROGRESS {esc(parent_paket.get('nama','Produk'))}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Pesanan {esc(parent_paket.get('nama','Produk'))} kamu masih menunggu syarat terpenuhi.\n\n"
                    f"📦 <b>Produk Utama</b>\n"
                    f"Paket : {esc(parent_paket.get('emoji','📦'))} {esc(parent_paket.get('nama','Produk'))}\n\n"
                    f"📊 <b>Progress Saat Ini</b>\n"
                    f"Progress : {progress['fulfilled_count']}/{progress['total_count']} terpenuhi\n\n"
                    f"{progress['text']}\n\n"
                    f"⏸️ Link {esc(parent_paket.get('nama','Produk'))} masih ditahan.\n"
                    f"Silakan lengkapi {missing_count} produk lagi agar link otomatis dikirim."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(progress['buttons']) if progress['buttons'] else None
            )
        except Exception as e:
            logger.error(f"[PREREQ PROGRESS] Gagal kirim progress parent ke buyer {user_id}: {e}")

        extra = (
            f"📋 <b>Progress Produk Terkait</b>\n"
            f"Produk : {esc(parent_paket.get('emoji','📦'))} {esc(parent_paket.get('nama','Produk'))}\n"
            f"Progress : {progress['fulfilled_count']}/{progress['total_count']} terpenuhi\n\n"
            f"{progress['text']}"
        )
        msg_id = await kirim_notif(
            bot,
            _format_order_notif(
                "📋 <b>UPDATE PROGRESS SYARAT</b>",
                user_name, user_id, parent_paket, parent_order['order_id'],
                amount=parent_order.get('harga_dibayar', 0),
                extra=extra
            ),
            reply_markup=InlineKeyboardMarkup(progress['buttons']) if progress['buttons'] else None
        )
        if msg_id:
            await set_admin_msg_id(parent_order['order_id'], msg_id)
        return

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 <b>SYARAT {esc(parent_paket.get('nama','Produk'))} SUDAH LENGKAP</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 <b>Produk Utama</b>\n"
                f"Paket : {esc(parent_paket.get('emoji','📦'))} {esc(parent_paket.get('nama','Produk'))}\n\n"
                f"📊 <b>Progress Akhir</b>\n"
                f"Progress : {progress['fulfilled_count']}/{progress['total_count']} terpenuhi\n\n"
                f"{progress['text']}\n\n"
                f"✅ Semua syarat sudah terpenuhi.\n"
                f"Link {esc(parent_paket.get('nama','Produk'))} akan dikirim otomatis sekarang."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"[PREREQ PROGRESS] Gagal kirim final progress ke buyer {user_id}: {e}")

async def _process_completed_order_delivery(bot, order_id: str, paket_id: str, user_id: int,
                                            user_name: str, paid_amount: int,
                                            source_title: str = "✅ <b>PEMBAYARAN BERHASIL</b>"):
    await hapus_qris_buyer_lama(bot, order_id, user_id)
    paket = await get_product(paket_id) or {
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

            await hapus_notif_lama(bot, order_id)
            extra_prereq = (
                f"🔐 <b>Akses Produk</b>\n"
                f"Status : ⏸️ Link ditahan\n"
                f"Alasan : Syarat belum terpenuhi\n\n"
                f"📋 <b>Progress Syarat</b>\n"
                f"Progress : {progress['fulfilled_count']}/{progress['total_count']} terpenuhi\n\n"
                f"{progress['text']}"
            )
            msg_id = await kirim_notif(
                bot,
                _format_order_notif(
                    source_title,
                    user_name, user_id, paket, order_id,
                    amount=paid_amount,
                    extra=extra_prereq
                ),
                reply_markup=InlineKeyboardMarkup(progress['buttons'] + [[
                    InlineKeyboardButton("📤 Kirim Link Manual", callback_data=f"admin_kirim_link_prereq|{order_id}")
                ]])
            )
            if msg_id:
                await set_admin_msg_id(order_id, msg_id)
            return

    claimed = await atomic_claim_for_delivery(order_id)
    if not claimed:
        logger.info(f"[DELIVERY] Order {order_id} sudah diklaim/terkirim, skip delivery duplikat.")
        return

    kirim_berhasil = False
    link = None
    try:
        link, _ = await _send_buyer_product_link(bot, user_id, order_id, paket, paid_amount, source_title)
        kirim_berhasil = True
    except Exception as e:
        logger.error(f"[PAYMENT] Gagal kirim link ke buyer {user_id}: {e}")
        await _send_delivery_failed_alert(bot, order_id, user_id, user_name, paket, e)

    await hapus_notif_lama(bot, order_id)

    if kirim_berhasil:
        await set_sent_link(order_id, link)
        extra_paid = "🔗 <b>Pengiriman Link</b>\nStatus : ✅ Sudah dikirim"
    else:
        extra_paid = "🔗 <b>Pengiriman Link</b>\nStatus : ⚠️ Gagal dikirim - cek manual"

    msg_id = await kirim_notif(
        bot,
        _format_order_notif(
            source_title,
            user_name, user_id, paket, order_id,
            amount=paid_amount,
            extra=extra_paid
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

async def _handle_payment_success(bot, order_id: str, paket_id: str, user_id: int,
                                   user_name: str, amount: int, trans: dict):
    # Proteksi atomik ganda guna mengunci state transaksi selesai
    success = await mark_order_completed(order_id)
    if not success:
        logger.info(f"[PAYMENT] Order ID {order_id} sudah diproses sebelumnya, membatalkan duplikasi.")
        return

    paid_amount = _safe_int(trans.get('amount', amount), amount)
    await _process_completed_order_delivery(
        bot, order_id, paket_id, user_id, user_name, paid_amount,
        source_title="✅ <b>PEMBAYARAN BERHASIL</b>"
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
            await _send_delivery_failed_alert(bot, oid, user_id, user_name, paket, e)
            continue

        await set_sent_link(oid, link)
        await hapus_notif_lama(bot, oid)
        msg_id = await kirim_notif(
            bot,
            _format_order_notif(
                "✅ <b>LINK AUTO-TERKIRIM</b>",
                user_name, user_id, paket, oid,
                amount=order.get('harga_dibayar', 0),
                extra="🔗 <b>Pengiriman Link</b>\nStatus : ✅ Sudah dikirim otomatis karena semua syarat terpenuhi."
            )
        )
        if msg_id:
            await set_admin_msg_id(oid, msg_id)

# =================== ADMIN: KIRIM LINK PREREQ MANUAL ===================

async def admin_kirim_link_prereq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not await is_admin(query.from_user.id, context):
        await query.answer("⛔ Akses ditolak.", show_alert=True)
        return

    await query.answer()
    order_id = query.data.split("|")[1]
    order = await get_order_by_id(order_id)
    if not order:
        await query.edit_message_text("❌ Order tidak ditemukan.")
        return

    user_id = order['user_id']
    paket_id = order['paket_id']
    paket = await get_product(paket_id) or {"emoji": "📦", "nama": "Produk", "harga": 0, "link": DEFAULT_LINK}

    group_link = await generate_group_link(context.bot, paket, order_id)
    link = group_link or (paket.get("link") or DEFAULT_LINK)
    link_section = _build_link_section(group_link, link)

    kirim_berhasil = False
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"<b>✅ LINK PRODUK KAMU SUDAH SIAP!</b>\n"
                f"========================\n\n"
                f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
                f"🔖 Order ID: <code>{esc(order_id)}</code>\n\n"
                f"========================\n"
                f"{link_section}\n\n"
                f"Terima kasih telah memenuhi syarat! Nikmati paketmu. 🙏\n\n"
                f"Bantu kami berkembang dengan memberikan ulasan:"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Beri Ulasan / Testimoni", callback_data=f"rate_start|{order_id}")]
            ])
        )
        kirim_berhasil = True
    except Exception as e:
        logger.error(f"[PREREQ] Gagal kirim link manual ke buyer {user_id}: {e}")

    if kirim_berhasil:
        await set_sent_link(order_id, link)
    else:
        await set_delivery_status(order_id, 'failed', 'Manual prereq delivery gagal')

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

    row = await asyncio.to_thread(_check_join_request_sync, user_id, chat_id)

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

async def handle_rate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    order_id = query.data.split("|")[1]
    order = await get_completed_order_for_user(order_id, query.from_user.id)
    if not order:
        await query.edit_message_text("⚠️ Pesanan tidak ditemukan atau bukan milik kamu.")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ 1", callback_data=f"rate_val|1|{order_id}"),
            InlineKeyboardButton("⭐ 2", callback_data=f"rate_val|2|{order_id}"),
            InlineKeyboardButton("⭐ 3", callback_data=f"rate_val|3|{order_id}"),
            InlineKeyboardButton("⭐ 4", callback_data=f"rate_val|4|{order_id}"),
            InlineKeyboardButton("⭐ 5", callback_data=f"rate_val|5|{order_id}"),
        ],
        [InlineKeyboardButton("⬅️ Kembali (Lihat Link)", callback_data=f"rate_back|{order_id}")],
        [InlineKeyboardButton("❌ Lewati", callback_data="rate_skip")]
    ])

    await query.edit_message_text(
        "<b>⭐ PENILAIAN PELAYANAN TOKO</b>\n"
        "========================\n\n"
        "Masukan Anda membantu kami meningkatkan kualitas pelayanan.\n"
        "Silakan pilih bintang penilaian Anda:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

async def handle_rate_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    rating = int(parts[1])
    order_id = parts[2]

    order = await get_completed_order_for_user(order_id, query.from_user.id)
    if not order:
        await query.edit_message_text("⚠️ Pesanan tidak ditemukan atau bukan milik kamu.")
        return

    context.user_data['temp_rating'] = {'order_id': order_id, 'rating': rating}
    context.user_data['awaiting_review_text'] = True

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩ Kirim Rating Saja (Tanpa Teks)", callback_data=f"rate_text_skip|{order_id}")],
        [InlineKeyboardButton("⬅️ Kembali Pilih Bintang", callback_data=f"rate_back_stars|{order_id}")],
        [InlineKeyboardButton("🔗 Lihat Link Lagi", callback_data=f"rate_back|{order_id}")]
    ])

    await query.edit_message_text(
        f"Anda memilih rating: {'⭐' * rating}\n\n"
        "Silakan ketik dan kirimkan ulasan singkat Anda.\n"
        "Atau tekan tombol di bawah jika tidak ingin menulis teks:",
        reply_markup=keyboard
    )

async def handle_rate_text_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    order_id = query.data.split("|")[1]
    temp = context.user_data.pop('temp_rating', None)
    context.user_data.pop('awaiting_review_text', None)

    if not temp:
        await query.edit_message_text("⚠️ Terjadi kesalahan sesi. Silakan ulangi.")
        return

    rating = temp['rating']
    review_text = "Tidak ada ulasan tertulis."

    order = await get_completed_order_for_user(order_id, query.from_user.id)
    if not order:
        await query.edit_message_text("⚠️ Pesanan tidak ditemukan atau bukan milik kamu.")
        return
    paket_id_testi = order['paket_id']
    paket = await get_product(paket_id_testi)
    paket_nama = paket['nama'] if paket else "Produk"
    paket_emoji = paket['emoji'] if paket else "📦"

    await save_testimonial(query.from_user.id, query.from_user.full_name, paket_id_testi, order_id, rating, review_text)

    moderation_text = (
        f"📩 <b>MODERASI TESTIMONI BARU</b>\n"
        f"========================\n\n"
        f"👤 Buyer: {esc(query.from_user.full_name)} (<code>{query.from_user.id}</code>)\n"
        f"📦 Paket: {esc(paket_emoji)} {esc(paket_nama)}\n"
        f"📊 Rating: {'⭐' * rating}\n"
        f"💬 Ulasan: <i>\"{esc(review_text)}\"</i>\n\n"
        f"========================\n"
        f"Pilih tindakan untuk persetujuan publikasi:"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Setujui & Posting", callback_data=f"adm_testi_approve|{order_id}"),
            InlineKeyboardButton("❌ Tolak", callback_data=f"adm_testi_reject|{order_id}")
        ]
    ])

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=moderation_text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Gagal mengirim notif moderasi ke admin: {e}")

    await query.edit_message_text("🙏 Terima kasih banyak! Penilaian Anda telah dikirim dan menunggu peninjauan admin.")

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
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Beri Ulasan / Testimoni", callback_data=f"rate_start|{order_id}")]
        ])
    )

async def handle_rate_back_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    order_id = query.data.split("|")[1]

    context.user_data.pop('awaiting_review_text', None)
    context.user_data.pop('temp_rating', None)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ 1", callback_data=f"rate_val|1|{order_id}"),
            InlineKeyboardButton("⭐ 2", callback_data=f"rate_val|2|{order_id}"),
            InlineKeyboardButton("⭐ 3", callback_data=f"rate_val|3|{order_id}"),
            InlineKeyboardButton("⭐ 4", callback_data=f"rate_val|4|{order_id}"),
            InlineKeyboardButton("⭐ 5", callback_data=f"rate_val|5|{order_id}"),
        ],
        [InlineKeyboardButton("⬅️ Kembali (Lihat Link)", callback_data=f"rate_back|{order_id}")],
        [InlineKeyboardButton("❌ Lewati", callback_data="rate_skip")]
    ])

    await query.edit_message_text(
        "<b>⭐ PENILAIAN PELAYANAN TOKO</b>\n"
        "========================\n\n"
        "Masukan Anda membantu kami meningkatkan kualitas pelayanan.\n"
        "Silakan pilih bintang penilaian Anda:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

# =================== ADMIN: TESTIMONI MODERASI HANDLERS ===================

async def admin_testi_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not await is_admin(query.from_user.id, context):
        await query.answer("⛔ Akses ditolak.", show_alert=True)
        return

    await query.answer()

    order_id = query.data.split("|")[1]
    testi = await get_testimonial_by_order(order_id)
    if not testi:
        await query.edit_message_text("❌ Data ulasan tidak ditemukan di database.")
        return

    await update_testimonial_status(order_id, 'approved')

    channel_id = await get_setting('testimoni_channel_id')
    if not channel_id or channel_id.strip() == "":
        await query.edit_message_text(
            "✅ <b>Testimoni Disetujui</b>\n\n"
            "⚠️ ID Channel Testimoni belum diset. Ulasan tersimpan di DB tapi tidak dikirim ke channel publik.",
            parse_mode="HTML"
        )
        return

    nama_sensor = samarkan_nama(testi['user_name'])
    order = await get_order_by_id(order_id)
    paket = await get_product(order['paket_id']) if order else None
    paket_nama = paket['nama'] if paket else "Produk"
    paket_emoji = paket['emoji'] if paket else "📦"

    bintang_penuh = '⭐' * testi['rating']
    bintang_kosong = '☆' * (5 - testi['rating'])

    channel_msg = (
        f"⭐ <b>TESTIMONI PELANGGAN</b>\n"
        f"========================\n\n"
        f"📦 <b>Produk:</b> {esc(paket_emoji)} {esc(paket_nama)}\n"
        f"👤 <b>Pembeli:</b> {esc(nama_sensor)}\n"
        f"🌟 <b>Rating:</b> {bintang_penuh}{bintang_kosong}  <b>{testi['rating']}/5</b>\n\n"
        f"💬 <i>'{esc(testi['review'])}'</i>\n\n"
        f"========================\n"
        f"✅ <b>Transaksi Terverifikasi</b>\n"
        f"🛒 Order otomatis via @{context.bot.username}"
    )

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")]])
    try:
        await context.bot.send_message(chat_id=int(channel_id), text=channel_msg, parse_mode="HTML")
        await query.edit_message_text(
            "✅ <b>Testimoni Disetujui</b>\n\nUlasan berhasil dipublikasikan ke channel testimoni.",
            parse_mode="HTML",
            reply_markup=back_kb
        )
    except Exception as e:
        await query.edit_message_text(
            f"⚠️ <b>Ulasan Disetujui di DB, tapi GAGAL dikirim ke Channel.</b>\n\n"
            f"Error: <code>{esc(str(e))}</code>\n"
            f"Pastikan bot sudah dijadikan Administrator di channel <code>{esc(channel_id)}</code>.",
            parse_mode="HTML",
            reply_markup=back_kb
        )

async def admin_testi_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not await is_admin(query.from_user.id, context):
        await query.answer("⛔ Akses ditolak.", show_alert=True)
        return

    await query.answer()

    order_id = query.data.split("|")[1]
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

async def produk_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    paket_id = query.data.replace("pd_detail_", "")
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
        [InlineKeyboardButton("⬅️ Kembali",       callback_data="pd_back")],
    ]

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

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

async def pd_req_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    paket_id = parts[1]

    selected = context.user_data.get('editing_prereq', {}).get('selected', [])
    context.user_data.pop('editing_prereq', None)

    new_val = ",".join(selected) if selected else None
    await update_product_field(paket_id, 'requires_paket_ids', new_val)

    query.data = f"pd_detail_{paket_id}"
    await produk_detail(update, context)

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
                InlineKeyboardButton("❌ Batal",     callback_data=f"pd_detail_{paket_id}"),
            ]
        ])
    )

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

async def produk_tambah_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('adding_product', None)
    await _send_produk_menu(context, chat_id=query.from_user.id, query=query)

async def pd_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('adding_product', None)
    context.user_data.pop('editing_product', None)
    await _send_produk_menu(context, chat_id=query.from_user.id, query=query)

# =================== ADMIN: ORDER AKTIF ===================

async def _build_active_orders_text_and_keyboard():
    """Fungsi bersama untuk membangun text dan keyboard order aktif."""
    orders = await get_all_waiting()
    if not orders:
        return None, None

    text = f"<b>⏳ ORDER MENUNGGU BAYAR ({len(orders)})</b>\n========================\n\n"
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

    keyboard.append([InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_orders")])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

def _check_waiting_order_sync(order_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE order_id=%s AND status='waiting'", (order_id,))
            return c.fetchone()

async def admin_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not await is_admin(query.from_user.id, context):
        await query.answer("⛔ Akses ditolak.", show_alert=True)
        return

    parts = query.data.split("|")
    if len(parts) != 3:
        await query.answer("Format tidak valid.", show_alert=True)
        return

    await query.answer()
    target_user_id = int(parts[1])
    order_id = parts[2]

    order = await asyncio.to_thread(_check_waiting_order_sync, order_id)
    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah selesai/dibatalkan.")
        return

    order = dict(order)
    paket = await get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0}

    cancel_amount = order.get('harga_dibayar') or paket.get('harga', 0)
    if cancel_amount:
        await cancel_transaction(order_id, cancel_amount)

    await update_order_status(order_id, 'cancelled')
    await _stop_payment_task(target_user_id)
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

    order = await asyncio.to_thread(_check_waiting_order_sync, order_id)
    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah selesai/dibatalkan.")
        return

    order = dict(order)
    paket = await get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0, "link": DEFAULT_LINK}

    await _stop_payment_task(target_user_id)
    ok = await mark_order_completed(order_id)
    if not ok:
        await query.edit_message_text("⚠️ Order sudah diproses sebelumnya (mungkin oleh webhook).")
        return

    await _process_completed_order_delivery(
        context.bot,
        order_id=order_id,
        paket_id=order['paket_id'],
        user_id=target_user_id,
        user_name=order.get('user_name', '-'),
        paid_amount=order.get('harga_dibayar') or paket.get('harga', 0),
        source_title="✅ <b>DIKONFIRMASI MANUAL</b>"
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
    s = await get_order_stats(today_start, month_start)
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

    await update.message.reply_text(text, parse_mode="HTML")

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
    products, orders, banned, testimonials, settings, admins = await asyncio.to_thread(_generate_full_export_sync)
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
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        len(products), len(orders), len(banned)
    )

async def _kirim_backup(bot):
    ts = now_wib().strftime('%Y%m%d_%H%M%S')
    zip_name = f"backup_{ts}.zip"
    try:
        products, orders, banned, testimonials, settings, admins = await asyncio.to_thread(_generate_full_export_sync)

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
        json_bytes = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")

        import io as _io
        def _rows_to_csv_bytes(rows: list) -> bytes:
            if not rows:
                return b""
            text_buf = _io.StringIO()
            writer = csv.DictWriter(text_buf, fieldnames=list(rows[0].keys()), extrasaction='ignore')
            writer.writeheader()
            writer.writerows([{k: str(v) if v is not None else '' for k, v in row.items()} for row in rows])
            return text_buf.getvalue().encode("utf-8")

        zip_buf = BytesIO()
        with zipfile.ZipFile(zip_buf, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("backup.json",        json_bytes)
            zf.writestr("orders.csv",         _rows_to_csv_bytes(orders))
            zf.writestr("products.csv",       _rows_to_csv_bytes(products))
            zf.writestr("banned_users.csv",   _rows_to_csv_bytes(banned))
            zf.writestr("testimonials.csv",   _rows_to_csv_bytes(testimonials))
            zf.writestr("admins.csv",         _rows_to_csv_bytes(admins))
        zip_buf.seek(0)
        zip_buf.name = zip_name

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
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        await status_msg.edit_text(f"❌ Gagal membaca file JSON: {esc(str(e))}", parse_mode="HTML")
        return

    if "products" not in data and "orders" not in data:
        await status_msg.edit_text("❌ Format JSON tidak valid. Pastikan file berasal dari /export.")
        return

    result = await asyncio.to_thread(_import_json_data_sync, data)
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

def _get_completed_order_sync(order_id, user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT * FROM orders WHERE order_id=%s AND user_id=%s AND status='completed'",
                (order_id, user_id)
            )
            return c.fetchone()

async def resend_group_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    parts = query.data.split("|")
    order_id = parts[1] if len(parts) > 1 else None
    if not order_id:
        await query.edit_message_text("⚠️ Order ID tidak valid.")
        return

    order = await asyncio.to_thread(_get_completed_order_sync, order_id, user_id)
    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau belum lunas.")
        return

    order = dict(order)
    paket = await get_product(order['paket_id'])
    if not paket:
        await query.edit_message_text("⚠️ Produk tidak ditemukan.")
        return

    new_link = await generate_group_link(context.bot, paket, order_id)

    if new_link:
        await query.edit_message_text(
            f"✅ <b>Link Baru Berhasil Dibuat!</b>\n\n"
            f"🔗 {new_link}\n\n"
            f"📋 <b>Cara gabung:</b>\n"
            f"1. Klik link di atas\n"
            f"2. Pencet <b>\"Minta Bergabung\"</b>\n"
            f"3. Bot langsung approve otomatis ✅\n\n"
            f"<i>Segera join ya!</i>",
            parse_mode="HTML"
        )
    else:
        fallback_link = paket.get("link") or DEFAULT_LINK
        await query.edit_message_text(
            f"✅ <b>Link Produk</b>\n\n"
            f"🔗 {fallback_link}\n\n"
            f"<i>Produk ini pakai link biasa.</i>",
            parse_mode="HTML"
        )

# =================== BACKGROUND LOOPS ===================
REMINDER_HARI = 3

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
    buyers = await asyncio.to_thread(_get_buyers_for_reminder_sync, REMINDER_HARI)
    if not buyers:
        return
    logger.info(f"[REMINDER] Mengirim reminder ke {len(buyers)} buyer...")
    for buyer in buyers:
        try:
            await bot.send_message(
                chat_id=buyer['user_id'],
                text=(
                    f"👋 Halo <b>{esc(buyer['user_name'])}</b>!\n\n"
                    f"Sudah <b>{REMINDER_HARI} hari</b> sejak kamu belanja di <b>Hyper Family Store</b> 🛒\n\n"
                    f"Puas dengan produknya? Mau belanja lagi?\n"
                    f"Kami punya paket menarik yang siap dikirim langsung!\n\n"
                    f"Ketik /start untuk lihat katalog kami 😊"
                ),
                parse_mode="HTML"
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

# =================== ADMIN: BROADCAST ===================

_blast_tasks: dict = {}
_blast_tasks_lock = asyncio.Lock()

async def _run_broadcast(bot, admin_id: int, buyers: list, text_blast: str):
    total = len(buyers)
    progress_msg = await bot.send_message(
        chat_id=admin_id,
        text=f"📢 <b>Memulai Broadcast...</b>\nTarget: {total} buyer.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Hentikan Sekarang", callback_data="blast_stop")]])
    )

    sent = 0
    failed = 0
    skipped = 0

    try:
        for index, b in enumerate(buyers):
            if asyncio.current_task().cancelled():
                break

            target_id = b['user_id']
            try:
                await bot.send_message(
                    chat_id=target_id,
                    text=text_blast,
                    parse_mode="HTML"
                )
                sent += 1
            except telegram.error.Forbidden:
                skipped += 1
                logger.info(f"[BLAST] User {target_id} memblokir bot, dilewati.")
            except telegram.error.RetryAfter as e:
                logger.warning(f"[BLAST] Terkena rate limit Telegram, tidur {e.retry_after} detik.")
                await asyncio.sleep(e.retry_after)
                try:
                    await bot.send_message(chat_id=target_id, text=text_blast, parse_mode="HTML")
                    sent += 1
                except telegram.error.Forbidden:
                    skipped += 1
                except Exception:
                    failed += 1
            except Exception as e:
                logger.error(f"[BLAST] Gagal mengirim pesan ke {target_id}: {e}")
                failed += 1

            await asyncio.sleep(0.05)

            if (index + 1) % 20 == 0 or (index + 1) == total:
                percent = int(((index + 1) / total) * 100)
                try:
                    await bot.edit_message_text(
                        chat_id=admin_id,
                        message_id=progress_msg.message_id,
                        text=(
                            f"📢 <b>Progres Broadcast: {percent}%</b>\n"
                            f"========================\n"
                            f"👤 Diproses : {index + 1} / {total}\n"
                            f"✅ Sukses   : {sent}\n"
                            f"⏩ Dilewati (blokir bot) : {skipped}\n"
                            f"❌ Gagal Lainnya         : {failed}"
                        ),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Hentikan Sekarang", callback_data="blast_stop")]])
                    )
                except Exception as ex:
                    logger.debug(f"Gagal memperbarui progres broadcast admin: {ex}")

    except asyncio.CancelledError:
        logger.info(f"[BLAST] Broadcast dibatalkan oleh admin {admin_id}.")

    finally:
        async with _blast_tasks_lock:
            _blast_tasks.pop(admin_id, None)
        is_cancelled = sent + failed + skipped < total
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=(
                    f"{'⛔' if is_cancelled else '✅'} <b>BROADCAST SELESAI</b>\n"
                    f"========================\n\n"
                    f"✅ Sukses Terkirim          : <b>{sent}</b> buyer\n"
                    f"⏩ Dilewati (blokir bot)   : <b>{skipped}</b> buyer\n"
                    f"❌ Gagal Lainnya            : <b>{failed}</b>\n"
                    f"📊 Total Target             : <b>{total}</b>\n\n"
                    f"<i>Selesai pada: {now_wib().strftime('%H:%M, %d/%m/%Y WIB')}</i>"
                ),
                parse_mode="HTML"
            )
        except Exception as ex:
            logger.debug(f"Gagal mengirim laporan final broadcast: {ex}")

async def cmd_blast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return

    buyers = await get_all_buyers()
    jumlah = len(buyers)
    if jumlah == 0:
        await update.message.reply_text("❌ Belum ada buyer aktif terdaftar.")
        return

    context.user_data['blast_state'] = {'step': 'typing', 'buyers': buyers}
    await update.message.reply_text(
        f"<b>📢 BROADCAST PESAN</b>\n"
        f"========================\n\n"
        f"Total penerima: <b>{jumlah} buyer</b>\n\n"
        f"Kirim pesan yang mau di-blast sekarang.\n"
        f"<i>Mendukung HTML: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;, &lt;code&gt;code&lt;/code&gt;</i>\n\n"
        f"⚠️ Setelah kirim pesan, akan ada <b>preview dan konfirmasi</b> sebelum blast dikirim.\n"
        f"⚠️ User yang memblokir bot akan <b>dilewati</b> (tidak di-ban).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Batal", callback_data="blast_batal")]
        ])
    )

async def blast_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('blast_state', None)
    await query.edit_message_text(
        "✅ Broadcast dibatalkan.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")]])
    )

async def blast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ Memulai broadcast di background...")

    blast_state = context.user_data.pop('blast_state', None)
    if not blast_state or blast_state.get('step') != 'preview':
        await query.edit_message_text("⚠️ Sesi blast tidak ditemukan. Mulai ulang dengan /blast.")
        return

    buyers = blast_state.get('buyers', [])
    text_blast = blast_state.get('text', '')
    admin_id = query.from_user.id

    await query.edit_message_text(
        f"📢 <b>Broadcast dimulai di background!</b>\n"
        f"Target: <b>{len(buyers)}</b> buyer.\n\n"
        f"<i>Progres akan muncul di bawah ini...</i>",
        parse_mode="HTML"
    )

    task = asyncio.create_task(_run_broadcast(context.bot, admin_id, buyers, text_blast))
    async with _blast_tasks_lock:
        _blast_tasks[admin_id] = task

async def blast_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⛔ Menghentikan broadcast...")

    admin_id = query.from_user.id
    async with _blast_tasks_lock:
        task = _blast_tasks.pop(admin_id, None)
    if task and not task.done():
        task.cancel()
        await query.edit_message_text(
            "⛔ <b>Broadcast dihentikan oleh admin.</b>\n\n"
            f"<i>Laporan akhir akan muncul sebentar lagi.</i>",
            parse_mode="HTML"
        )
    else:
        await query.answer("ℹ️ Tidak ada broadcast yang berjalan.", show_alert=True)

# =================== GENERAL MESSAGE HANDLER ===================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    text = update.message.text.strip() if update.message.text else ""

    if context.user_data.get('awaiting_review_text'):
        context.user_data.pop('awaiting_review_text', None)
        temp = context.user_data.pop('temp_rating', None)

        if not temp:
            await update.message.reply_text("❌ Sesi pengisian ulasan kedaluwarsa. Silakan ulangi.")
            return

        rating = temp['rating']
        order_id = temp['order_id']
        order = await get_completed_order_for_user(order_id, user_id)
        if not order:
            await update.message.reply_text("❌ Pesanan tidak ditemukan atau bukan milik kamu.")
            return

        await save_testimonial(user_id, update.effective_user.full_name, order['paket_id'], order_id, rating, text)

        paket = await get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id']}
        moderation_text = (
            f"📩 <b>MODERASI TESTIMONI BARU</b>\n"
            f"========================\n\n"
            f"👤 Buyer: {esc(update.effective_user.full_name)} (<code>{user_id}</code>)\n"
            f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
            f"📊 Rating: {'⭐' * rating}\n"
            f"💬 Ulasan: <i>\"{esc(text)}\"</i>\n\n"
            f"========================\n"
            f"Pilih tindakan untuk persetujuan publikasi:"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Setujui & Posting", callback_data=f"adm_testi_approve|{order_id}"),
                InlineKeyboardButton("❌ Tolak", callback_data=f"adm_testi_reject|{order_id}")
            ]
        ])

        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=moderation_text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Gagal mengirim notif moderasi ke admin: {e}")

        await update.message.reply_text("🙏 Terima kasih banyak! Ulasan Anda telah berhasil dikirim dan saat ini sedang ditinjau oleh admin.")
        return

    if await is_admin(user_id, context):

        blast_state = context.user_data.get('blast_state')
        if blast_state and blast_state.get('step') == 'typing':
            if not text:
                await update.message.reply_text("❌ Pesan tidak boleh kosong.")
                return

            buyers = blast_state.get('buyers', [])
            context.user_data['blast_state'] = {
                'step': 'preview',
                'text': text,
                'buyers': buyers,
            }

            await update.message.reply_text(
                f"📋 <b>PREVIEW PESAN BLAST</b>\n"
                f"========================\n\n"
                f"{text}\n\n"
                f"========================\n"
                f"Target: <b>{len(buyers)} buyer</b>\n\n"
                f"Apakah pesan ini sudah benar?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Kirim Sekarang", callback_data="blast_confirm"),
                        InlineKeyboardButton("✍️ Ubah Pesan", callback_data="blast_retype"),
                    ],
                    [InlineKeyboardButton("❌ Batalkan", callback_data="blast_batal")],
                ])
            )
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
                                                        InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_kick")]])
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
                            InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_kick")])
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
                        text="⭐ <b>Uji Coba Channel Testimoni</b>\n\nKoneksi berhasil! Bot siap memposting ulasan dari pembeli ke channel ini.",
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

    orders = await get_all_pending()
    if not orders:
        await update.message.reply_text(
            "<b>✅ TIDAK ADA ORDER PENDING</b>\n========================",
            parse_mode="HTML"
        )
        return

    text = f"<b>📋 ORDER PENDING ({len(orders)})</b>\n========================\n\n"
    keyboard = []
    for o in orders:
        paket_emoji = o.get('paket_emoji') or '📦'
        paket_nama = o.get('paket_nama') or o['paket_id']
        durasi = hitung_durasi(o["waktu"])
        text += f"- {esc(paket_emoji)} {esc(o['user_name'])} - {esc(paket_nama)} - {durasi}\n"
        keyboard.append([InlineKeyboardButton(f"👤 Proses: {o['user_name']}", callback_data=f"proses_{o['user_id']}")])

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

def _get_pending_order_sync(user_id):
    with db_session_safe() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE user_id=%s AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,))
            return c.fetchone()

async def admin_proses_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.replace("proses_", ""))

    order = await asyncio.to_thread(_get_pending_order_sync, user_id)
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
            InlineKeyboardButton("❌ Tolak",      callback_data=f"reject_{user_id}"),
        ],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="back_orders")]
    ]

    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.from_user.id, text=caption, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def back_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    orders = await get_all_pending()
    if not orders:
        await query.edit_message_text(
            "✅ Tidak ada order pending saat ini.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")]])
        )
        return

    text = f"<b>📋 ORDER PENDING ({len(orders)})</b>\n========================\n\n"
    keyboard = []
    for o in orders:
        paket_emoji = o.get('paket_emoji') or '📦'
        paket_nama = o.get('paket_nama') or o['paket_id']
        durasi = hitung_durasi(o["waktu"])
        text += f"- {esc(paket_emoji)} {esc(o['user_name'])} - {esc(paket_nama)} - {durasi}\n"
        keyboard.append([InlineKeyboardButton(f"👤 Proses: {o['user_name']}", callback_data=f"proses_{o['user_id']}")])

    keyboard.append([InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")])
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await context.bot.send_message(
            chat_id=query.from_user.id, text=text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def admin_konfirmasi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action = parts[0]
    user_id = int(parts[1])

    order = await asyncio.to_thread(_get_pending_order_sync, user_id)
    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah diproses.")
        return

    order = dict(order)
    paket = await get_product(order["paket_id"]) or {"emoji": "📦", "nama": order["paket_id"], "harga": 0, "link": DEFAULT_LINK}

    if action == "confirm":
        ok = await mark_order_completed(order["order_id"])
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
            source_title="✅ <b>ORDER SELESAI (KONFIRMASI MANUAL)</b>"
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
        await update_order_status(order["order_id"], 'rejected')
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
    if DASHBOARD_URL:
        rows.append([InlineKeyboardButton("🖥️ Dashboard Web", web_app=WebAppInfo(url=DASHBOARD_URL))])
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

async def admpanel_produk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _send_produk_menu(context, chat_id=query.from_user.id, query=query)

async def admpanel_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏳ Order Aktif",   callback_data="admpanel_orders_aktif"),
            InlineKeyboardButton("🔄 Order Pending", callback_data="admpanel_orders_pending"),
        ],
        [InlineKeyboardButton("🔍 Cari Order",    callback_data="admpanel_orders_cari")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(
        "<b>📋 ORDERS</b>\n"
        "========================\n\n"
        "Pilih jenis order:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

async def admpanel_orders_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text, keyboard = await _build_active_orders_text_and_keyboard()
    if not text:
        await query.edit_message_text(
            "<b>✅ TIDAK ADA ORDER AKTIF</b>\n========================\n\nTidak ada buyer yang sedang menunggu membayar.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_orders")]])
        )
        return

    keyboard.append([InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_orders")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def admpanel_orders_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    orders = await get_all_pending()
    if not orders:
        await query.edit_message_text(
            "<b>✅ TIDAK ADA ORDER PENDING</b>\n========================",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_orders")]])
        )
        return

    text = f"<b>📋 ORDER PENDING ({len(orders)})</b>\n========================\n\n"
    keyboard = []
    for o in orders:
        paket_emoji = o.get('paket_emoji') or '📦'
        paket_nama = o.get('paket_nama') or o['paket_id']
        durasi = hitung_durasi(o["waktu"])
        text += f"- {esc(paket_emoji)} {esc(o['user_name'])} - {esc(paket_nama)} - {durasi}\n"
        keyboard.append([InlineKeyboardButton(f"👤 Proses: {o['user_name']}", callback_data=f"proses_{o['user_id']}")])
    keyboard.append([InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_orders")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="admpanel_orders")]])
    )

async def admpanel_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    now = now_wib()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    s = await get_order_stats(today_start, month_start)
    text = _build_stats_text(s, now)
    try:
        await query.edit_message_text(text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_back")]]))
    except Exception as e:
        logger.debug(f"Gagal mengedit stats di panel: {e}")
        await context.bot.send_message(chat_id=query.from_user.id, text=text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_back")]]))

async def admpanel_blast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    buyers = await get_all_buyers()
    jumlah = len(buyers)
    if jumlah == 0:
        await query.edit_message_text(
            "❌ Belum ada buyer aktif terdaftar.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_back")]])
        )
        return

    context.user_data['blast_state'] = {'step': 'typing', 'buyers': buyers}
    await query.edit_message_text(
        f"<b>📢 BROADCAST PESAN</b>\n"
        f"========================\n\n"
        f"Total penerima: <b>{jumlah} buyer</b>\n\n"
        f"Kirim pesan yang mau di-blast sekarang.\n"
        f"<i>Mendukung HTML: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;</i>\n\n"
        f"⚠️ Akan ada <b>preview + konfirmasi</b> sebelum dikirim.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="blast_batal")]])
    )

async def blast_retype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    blast_state = context.user_data.get('blast_state', {})
    buyers = blast_state.get('buyers', [])
    context.user_data['blast_state'] = {'step': 'typing', 'buyers': buyers}

    await query.edit_message_text(
        f"<b>📢 BROADCAST - UBAH PESAN</b>\n"
        f"========================\n\n"
        f"Kirim pesan baru:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="blast_batal")]])
    )

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
        [InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(
        "<b>💾 DATA &amp; BACKUP</b>\n"
        "========================\n\n"
        "Pilih tindakan:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

async def admpanel_data_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ Membuat backup...")
    await _kirim_backup(context.bot)
    await query.edit_message_text(
        "✅ Backup berhasil dikirim ke DM kamu.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_data")]])
    )

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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_data")]])
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ Gagal export: {esc(str(e))}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_data")]])
        )

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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="admpanel_data")]])
    )

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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_data")]])
    )

async def admpanel_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚫 Ban User",      callback_data="admpanel_user_ban"),
            InlineKeyboardButton("✅ Unban User",    callback_data="admpanel_user_unban"),
        ],
        [InlineKeyboardButton("📋 Daftar Ban",    callback_data="admpanel_user_daftar")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(
        "<b>🚫 KELOLA USER</b>\n"
        "========================\n\n"
        "Pilih aksi:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

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

async def admpanel_user_daftar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    banned = await get_all_banned()
    if not banned:
        await query.edit_message_text(
            "<b>🚫 DAFTAR BAN</b>\n========================\n\nBelum ada user yang dibanned.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_user")]])
        )
        return
    text = f"<b>🚫 DAFTAR BAN ({len(banned)} user)</b>\n========================\n\n"
    for b in banned:
        text += (
            f"👤 ID: <code>{b['user_id']}</code>\n"
            f"📝 Alasan: {esc(b['reason'] or '-')}\n"
            f"🕒 Dibanned: {b['banned_at']}\n\n"
        )
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_user")]])
    )

# =================== ADMIN: CEK & KICK USER DI GRUP ===================

async def admpanel_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await is_admin(query.from_user.id, context):
        await query.answer("⛔ Akses ditolak.", show_alert=True)
        return

    managed_groups = await get_managed_groups()

    group_lines = ""
    if managed_groups:
        for i, gid in enumerate(managed_groups, 1):
            try:
                chat = await context.bot.get_chat(int(gid))
                nama_grup = esc(chat.title or str(gid))
            except Exception as e:
                logger.debug(f"Gagal mengambil judul chat {gid}: {e}")
                nama_grup = "⚠️ Tidak dapat diakses"
            group_lines += f"  {i}. <b>{nama_grup}</b>\n     <code>{gid}</code>\n"
    else:
        group_lines = "  <i>Belum ada grup terdaftar.</i>\n"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Cek User di Grup", callback_data="kick_cek_user"),
            InlineKeyboardButton("➕ Tambah Grup",      callback_data="kick_add_group"),
        ],
        [InlineKeyboardButton("🗑️ Hapus Grup",          callback_data="kick_del_group")],
        [InlineKeyboardButton("⬅️ Kembali",              callback_data="admpanel_back")],
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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="admpanel_kick")]])
    )

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
    buttons.append([InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_kick")])
    await query.edit_message_text(result_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

async def kick_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await is_admin(query.from_user.id, context):
        await query.answer("⛔ Akses ditolak.", show_alert=True)
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
        await query.answer(f"✅ Berhasil kick dari {nama_grup}", show_alert=True)
    except Exception as e:
        await query.answer(f"❌ Gagal: {str(e)[:80]}", show_alert=True)
    query.data = f"kick_select|{target_id}"
    await kick_select_user(update, context)

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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="admpanel_kick")]])
    )

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
    buttons.append([InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_kick")])
    await query.edit_message_text(
        "<b>🗑️ HAPUS GRUP</b>\n========================\n\nPilih grup yang ingin dihapus:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def kick_del_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    gid_str = query.data.split("|", 1)[1]
    managed_groups = await get_managed_groups()
    new_groups = [g for g in managed_groups if str(g) != gid_str]
    await set_managed_groups(new_groups)
    await query.answer(f"✅ Grup {gid_str} dihapus.", show_alert=True)
    await admpanel_kick(update, context)

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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_kick")]])
    )

# =================== ADMIN: PENGATURAN ===================

async def admpanel_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    channel_id = await get_setting('notif_channel_id')
    ch_status = f"✅ ID: <code>{esc(channel_id)}</code>" if channel_id else "🔕 Nonaktif"

    testi_channel_id = await get_setting('testimoni_channel_id')
    testi_ch_status = f"✅ ID: <code>{esc(testi_channel_id)}</code>" if testi_channel_id else "🔕 Nonaktif"

    maint_on = await is_maintenance()
    maint_status = "⚙️ ON - bot maintenance" if maint_on else "✅ OFF - bot normal"
    maint_btn_label = "🟢 Matikan Maintenance" if maint_on else "⚙️ Aktifkan Maintenance"

    link_testi = await get_setting('link_testimoni') or '-'
    link_admin = await get_setting('link_admin')     or '-'

    text = (
        "<b>⚙️ PENGATURAN</b>\n"
        "========================\n\n"
        "<b>📢 Channel Notifikasi Order</b>\n"
        f"{ch_status}\n\n"
        "<b>⭐ Channel Testimoni Pembeli</b>\n"
        f"{testi_ch_status}\n\n"
        "<b>⚙️ Maintenance Mode</b>\n"
        f"{maint_status}\n\n"
        "<b>⭐ Link Button Testimoni</b>\n"
        f"<code>{esc(link_testi)}</code>\n\n"
        "<b>💬 Link Admin/CS</b>\n"
        f"<code>{esc(link_admin)}</code>"
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
        [InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

async def admpanel_setting_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    was_on = await is_maintenance()
    await set_setting('maintenance', '0' if was_on else '1')
    if was_on:
        msg = "✅ <b>Maintenance mode dinonaktifkan.</b>\n\nBot kembali normal - buyer bisa akses."
    else:
        msg = "⚙️ <b>Maintenance mode diaktifkan.</b>\n\nBuyer tidak bisa akses bot sampai maintenance dimatikan."
    await query.edit_message_text(
        msg, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Pengaturan", callback_data="admpanel_setting")]])
    )

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

async def admpanel_setting_channel_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await set_setting('notif_channel_id', None)
    await query.edit_message_text(
        "🔕 <b>Channel notifikasi dinonaktifkan.</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Pengaturan", callback_data="admpanel_setting")]])
    )

async def admpanel_setting_testich_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_testi_channel_id'] = True
    await query.edit_message_text(
        "<b>✍️ SET CHANNEL TESTIMONI</b>\n"
        "========================\n\n"
        "Ketik Channel ID tujuan ulasan testimoni:\n"
        "Contoh: <code>-1001234567890</code>\n\n"
        "Ketik <code>hapus</code> untuk menonaktifkan channel.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Batal", callback_data="admpanel_setting")]])
    )

async def admpanel_setting_testich_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await set_setting('testimoni_channel_id', None)
    await query.edit_message_text(
        "🔕 <b>Channel testimoni otomatis dinonaktifkan.</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Pengaturan", callback_data="admpanel_setting")]])
    )

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
        [InlineKeyboardButton("⬅️ Kembali",       callback_data="admpanel_back")],
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_admins")]])
        )
        return

    buttons = []
    for a in admins:
        buttons.append([InlineKeyboardButton(
            f"🗑️ {a['nama']} ({a['user_id']})",
            callback_data=f"admpanel_admin_del_{a['user_id']}"
        )])
    buttons.append([InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_admins")])
    await query.edit_message_text(
        "<b>➖ HAPUS ADMIN</b>\n========================\n\nPilih admin yang ingin dihapus:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

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
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_admins")]])
    )

# =================== MAIN ENTRYPOINT ===================

def main():
    init_pool()
    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # User Commands
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("riwayat", cmd_riwayat))

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
    app.add_handler(CallbackQueryHandler(buy_callback,         pattern="^buy$"))
    app.add_handler(CallbackQueryHandler(pilih_paket,          pattern="^pilih_"))
    app.add_handler(CallbackQueryHandler(prereq_buy_handler,   pattern="^prereq_buy_"))
    app.add_handler(CallbackQueryHandler(cancel_order_handler, pattern="^cancel_order$"))
    app.add_handler(CallbackQueryHandler(back_to_menu,         pattern="^back_to_menu$"))
    
    app.add_handler(CallbackQueryHandler(ganti_paket_list,     pattern="^ganti_paket_list$"))
    app.add_handler(CallbackQueryHandler(ganti_paket_konfirm,  pattern="^ganti_paket_konfirm\\|"))
    app.add_handler(CallbackQueryHandler(ganti_paket_exec,     pattern="^ganti_paket_exec\\|"))
    app.add_handler(CallbackQueryHandler(ganti_paket_batal,    pattern="^ganti_paket_batal$"))

    # Buyer Actions Handlers
    app.add_handler(CallbackQueryHandler(resend_group_link, pattern="^resendlink\\|"))

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
    app.add_handler(CallbackQueryHandler(admpanel_orders_aktif,    pattern="^admpanel_orders_aktif$"))
    app.add_handler(CallbackQueryHandler(admpanel_orders_pending,  pattern="^admpanel_orders_pending$"))
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
    app.add_handler(CallbackQueryHandler(admpanel_user_daftar,     pattern="^admpanel_user_daftar$"))

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

    # Super Admin Management Flow
    app.add_handler(CallbackQueryHandler(admpanel_admins,       pattern="^admpanel_admins$"))
    app.add_handler(CallbackQueryHandler(admpanel_admin_add,    pattern="^admpanel_admin_add$"))
    app.add_handler(CallbackQueryHandler(admpanel_admin_remove, pattern="^admpanel_admin_remove$"))
    app.add_handler(CallbackQueryHandler(admpanel_admin_del,    pattern="^admpanel_admin_del_"))

    # Blast Message Confirmation Flow
    app.add_handler(CallbackQueryHandler(blast_batal,   pattern="^blast_batal$"))
    app.add_handler(CallbackQueryHandler(blast_confirm, pattern="^blast_confirm$"))
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

    logger.info("Bot Hyper Family Store berhasil berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
