import os
import asyncio
import re
import json
import html as html_module
import hmac
import hashlib
import logging
import math
import random
import string
import functools
from aiohttp import web as aio_web
import aiohttp
import qrcode
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from io import BytesIO
from datetime import datetime, timedelta, timezone
import telegram.error
from telegram import (
    Update, BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
    InlineKeyboardButton, InlineKeyboardMarkup
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
    """Escape HTML special characters untuk parse_mode=HTML."""
    return html_module.escape(str(text))

# =================== TIMEZONE ===================
WIB = timezone(timedelta(hours=7))

def now_wib() -> datetime:
    return datetime.now(WIB)

# =================== KONFIGURASI ===================
TOKEN = os.environ.get("BOT_TOKEN")
_ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "")
PAKASIR_API_KEY = os.environ.get("PAKASIR_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
PAKASIR_WEBHOOK_SECRET = os.environ.get("PAKASIR_WEBHOOK_SECRET", "")

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
        logger.error(f"Error create transaction: {e}", exc_info=True)
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
        logger.error(f"Error cancel transaction: {e}", exc_info=True)
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
        logger.error(f"Error get detail: {e}", exc_info=True)
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

# =================== DATABASE POOL ===================

_pool: ThreadedConnectionPool = None

def init_pool():
    global _pool
    _pool = ThreadedConnectionPool(2, 20, DATABASE_URL, cursor_factory=RealDictCursor)
    logger.info("[DB] Threaded Connection pool diinisialisasi (min=2, max=20)")

def get_conn():
    return _pool.getconn()

def release_conn(conn):
    if _pool and conn:
        _pool.putconn(conn)

def async_wrap(func):
    @functools.wraps(func)
    async def run(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return run

def init_db():
    conn = get_conn()
    try:
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
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS harga_dibayar INTEGER DEFAULT 0")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_changes INTEGER DEFAULT 0")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP")
            c.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS link_locked BOOLEAN DEFAULT FALSE")

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

            conn.commit()
    finally:
        release_conn(conn)

# =================== TESTIMONIAL DB FUNCTIONS ===================

@async_wrap
def save_testimonial(user_id, user_name, paket_id, order_id, rating, review):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO testimonials (user_id, user_name, paket_id, order_id, rating, review, status)
                   VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                   ON CONFLICT (order_id) DO UPDATE SET rating=EXCLUDED.rating, review=EXCLUDED.review, status='pending'""",
                (user_id, user_name, paket_id, order_id, rating, review)
            )
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def update_testimonial_status(order_id, status):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE testimonials SET status=%s WHERE order_id=%s", (status, order_id))
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def get_testimonial_by_order(order_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT * FROM testimonials WHERE order_id=%s", (order_id,))
            row = c.fetchone()
            return dict(row) if row else None
    finally:
        release_conn(conn)

# =================== PRODUCT DB FUNCTIONS ===================

@async_wrap
def get_all_products():
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT * FROM products ORDER BY harga ASC")
            return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

def _get_product_sync(paket_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT * FROM products WHERE paket_id=%s", (paket_id,))
            row = c.fetchone()
            return dict(row) if row else None
    finally:
        release_conn(conn)

@async_wrap
def get_product(paket_id):
    return _get_product_sync(paket_id)

@async_wrap
def add_product(paket_id, nama, emoji, deskripsi, harga, link=None, group_chat_id=None):
    conn = get_conn()
    try:
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
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def update_product_field(paket_id, field, value):
    allowed = {"nama", "emoji", "deskripsi", "harga", "link", "group_chat_id", "aktif", "requires_paket_ids"}
    if field not in allowed:
        return
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(f"UPDATE products SET {field}=%s WHERE paket_id=%s", (value, paket_id))
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def delete_product(paket_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM products WHERE paket_id=%s", (paket_id,))
            conn.commit()
    finally:
        release_conn(conn)

def make_paket_id(nama):
    pid = re.sub(r'[^a-z0-9]+', '_', nama.lower().strip()).strip('_')
    return pid or "produk"

# =================== ORDER DB FUNCTIONS ===================

@async_wrap
def get_active_order(user_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT * FROM orders WHERE user_id=%s AND status IN ('waiting','pending') ORDER BY id DESC LIMIT 1",
                (user_id,)
            )
            row = c.fetchone()
            return dict(row) if row else None
    finally:
        release_conn(conn)

@async_wrap
def get_all_pending():
    conn = get_conn()
    try:
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
    finally:
        release_conn(conn)

@async_wrap
def get_all_waiting():
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT o.*, p.nama as paket_nama, p.emoji as paket_emoji, p.harga as paket_harga
                FROM orders o
                LEFT JOIN products p ON o.paket_id = p.paket_id
                WHERE o.status='waiting'
                ORDER BY o.id ASC
            """)
            return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

@async_wrap
def get_buyer_history_with_products(user_id, limit=10):
    conn = get_conn()
    try:
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
    finally:
        release_conn(conn)

@async_wrap
def get_all_buyers():
    conn = get_conn()
    try:
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
    finally:
        release_conn(conn)

def _get_managed_groups_sync() -> list:
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT value FROM settings WHERE key='managed_groups'")
            row = c.fetchone()
            if row and row['value']:
                return json.loads(row['value'])
            return []
    finally:
        release_conn(conn)

def _set_managed_groups_sync(groups: list):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES ('managed_groups', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (json.dumps(groups),)
            )
        conn.commit()
    finally:
        release_conn(conn)

async def get_managed_groups() -> list:
    return await asyncio.to_thread(_get_managed_groups_sync)

async def set_managed_groups(groups: list):
    return await asyncio.to_thread(_set_managed_groups_sync, groups)

@async_wrap
def get_order_stats(today_start: datetime, month_start: datetime):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            yesterday_start = today_start - timedelta(days=1)
            if month_start.month == 1:
                last_month_start = month_start.replace(year=month_start.year - 1, month=12, day=1)
            else:
                last_month_start = month_start.replace(month=month_start.month - 1, day=1)
            last_month_end = month_start

            c.execute("""
                SELECT COUNT(*) as cnt, COALESCE(SUM(harga_dibayar), 0) as rev
                FROM orders WHERE status='completed' AND created_at >= %s
            """, (today_start,))
            r = c.fetchone(); today_completed = r['cnt']; today_revenue = r['rev']

            c.execute("""
                SELECT COUNT(*) as cnt, COALESCE(SUM(harga_dibayar), 0) as rev
                FROM orders WHERE status='completed' AND created_at >= %s AND created_at < %s
            """, (yesterday_start, today_start))
            r = c.fetchone(); yesterday_completed = r['cnt']; yesterday_revenue = r['rev']

            c.execute("""
                SELECT COUNT(*) as cnt, COALESCE(SUM(harga_dibayar), 0) as rev
                FROM orders WHERE status='completed' AND created_at >= %s
            """, (month_start,))
            r = c.fetchone(); month_completed = r['cnt']; month_revenue = r['rev']

            c.execute("""
                SELECT COUNT(*) as cnt, COALESCE(SUM(harga_dibayar), 0) as rev
                FROM orders WHERE status='completed' AND created_at >= %s AND created_at < %s
            """, (last_month_start, last_month_end))
            r = c.fetchone(); last_month_completed = r['cnt']; last_month_revenue = r['rev']

            c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status='completed'")
            total_orders = c.fetchone()['cnt']

            c.execute("SELECT COUNT(*) as cnt FROM orders")
            total_generated = c.fetchone()['cnt'] or 1

            c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status IN ('waiting','pending')")
            active_count = c.fetchone()['cnt']

            c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status='cancelled'")
            cancelled_count = c.fetchone()['cnt']

            c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status='expired'")
            expired_count = c.fetchone()['cnt']

            c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status='rejected'")
            rejected_count = c.fetchone()['cnt']

            c.execute("SELECT COALESCE(SUM(harga_dibayar), 0) as total FROM orders WHERE status='completed'")
            total_revenue = c.fetchone()['total']

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
            trend_7d = [dict(r) for r in c.fetchall()]

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

            c.execute("SELECT COALESCE(AVG(rating), 0) as avg, COUNT(*) as cnt FROM testimonials WHERE status='approved'")
            testi_row = c.fetchone()
            avg_rating = round(float(testi_row['avg']), 1)
            total_testi = testi_row['cnt']

            c.execute("SELECT COUNT(*) as cnt FROM testimonials WHERE status='pending'")
            pending_testi = c.fetchone()['cnt']

            c.execute("""
                SELECT o.paket_id, p.nama, p.emoji, COUNT(*) as cnt, COALESCE(SUM(o.harga_dibayar), 0) as total
                FROM orders o
                LEFT JOIN products p ON o.paket_id = p.paket_id
                WHERE o.status='completed'
                GROUP BY o.paket_id, p.nama, p.emoji
                ORDER BY cnt DESC
            """)
            products_breakdown = [dict(r) for r in c.fetchall()]

            best_product = None
            if products_breakdown:
                b = products_breakdown[0]
                emoji = b.get('emoji') or '📦'
                nama = b.get('nama') or b['paket_id']
                best_product = f"{emoji} {nama} ({b['cnt']}x)"

    finally:
        release_conn(conn)

    return {
        'total_orders': total_orders,
        'today_completed': today_completed,
        'today_revenue': today_revenue,
        'yesterday_completed': yesterday_completed,
        'yesterday_revenue': yesterday_revenue,
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
    }

@async_wrap
def update_order_status(order_id, status):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE orders SET status=%s WHERE order_id=%s", (status, order_id))
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def set_order_link_locked(order_id: str, locked: bool):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE orders SET link_locked=%s WHERE order_id=%s", (locked, order_id))
            conn.commit()
    finally:
        release_conn(conn)

def _mark_order_completed_sync(order_id) -> bool:
    """Atomic: set completed hanya jika masih 'waiting'. Return True jika berhasil."""
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                "UPDATE orders SET status='completed' WHERE order_id=%s AND status='waiting' RETURNING id",
                (order_id,)
            )
            updated = c.fetchone()
            conn.commit()
            return updated is not None
    finally:
        release_conn(conn)

async def mark_order_completed(order_id) -> bool:
    return await asyncio.to_thread(_mark_order_completed_sync, order_id)

@async_wrap
def get_order_by_id(order_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE order_id=%s", (order_id,))
            row = c.fetchone()
            return dict(row) if row else None
    finally:
        release_conn(conn)

@async_wrap
def check_prerequisites_sync(user_id: int, requires_paket_ids_str: str) -> list:
    """
    Cek apakah user sudah pernah COMPLETED semua paket yang ada di requires_paket_ids_str.
    Mengembalikan list paket_id yang BELUM terpenuhi (kosong = semua sudah).
    requires_paket_ids_str: CSV string, contoh: "gb_biasa,gb_standar"
    """
    if not requires_paket_ids_str:
        return []
    required_ids = [p.strip() for p in requires_paket_ids_str.split(",") if p.strip()]
    if not required_ids:
        return []
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                """SELECT DISTINCT paket_id FROM orders
                   WHERE user_id=%s AND status='completed' AND link_locked=FALSE AND paket_id = ANY(%s)""",
                (user_id, required_ids)
            )
            done_ids = {row["paket_id"] for row in c.fetchall()}
            return [pid for pid in required_ids if pid not in done_ids]
    finally:
        release_conn(conn)

@async_wrap
def set_admin_msg_id(order_id, msg_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE orders SET admin_msg_id=%s WHERE order_id=%s", (msg_id, order_id))
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def set_buyer_msg_id(order_id, msg_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE orders SET buyer_msg_id=%s WHERE order_id=%s", (msg_id, order_id))
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def set_sent_link(order_id, link):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE orders SET sent_link=%s WHERE order_id=%s", (link, order_id))
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def save_order(user_id, user_name, paket_id, order_id, harga_dibayar=0, order_changes=0):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO orders (user_id, user_name, paket_id, order_id, status, waktu, harga_dibayar, order_changes)
                   VALUES (%s, %s, %s, %s, 'waiting', %s, %s, %s)""",
                (user_id, user_name, paket_id, order_id,
                 now_wib().strftime("%H:%M - %d/%m/%Y"), harga_dibayar, order_changes)
            )
            conn.commit()
    finally:
        release_conn(conn)

# =================== BAN DB FUNCTIONS ===================

@async_wrap
def is_banned(user_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM banned_users WHERE user_id=%s", (user_id,))
            return c.fetchone() is not None
    finally:
        release_conn(conn)

@async_wrap
def ban_user(user_id, reason=""):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO banned_users (user_id, reason, banned_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET reason=EXCLUDED.reason, banned_at=EXCLUDED.banned_at""",
                (user_id, reason, now_wib().strftime("%H:%M - %d/%m/%Y"))
            )
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def unban_user(user_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM banned_users WHERE user_id=%s", (user_id,))
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def get_all_banned():
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT * FROM banned_users ORDER BY banned_at DESC")
            return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

# =================== SETTINGS DB FUNCTIONS ===================

@async_wrap
def get_setting(key, default=None):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = c.fetchone()
            return row['value'] if row else default
    finally:
        release_conn(conn)

@async_wrap
def set_setting(key, value):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            if value is None:
                c.execute("DELETE FROM settings WHERE key=%s", (key,))
            else:
                c.execute(
                    """INSERT INTO settings (key, value) VALUES (%s, %s)
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""",
                    (key, str(value))
                )
            conn.commit()
    finally:
        release_conn(conn)

# =================== COOLDOWN DB ===================
COOLDOWN_MENIT = 5

@async_wrap
def set_cooldown_db(user_id):
    expires_at = now_wib() + timedelta(minutes=COOLDOWN_MENIT)
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO cooldowns (user_id, expires_at) VALUES (%s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET expires_at=EXCLUDED.expires_at""",
                (user_id, expires_at)
            )
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def get_cooldown_sisa_db(user_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT expires_at FROM cooldowns WHERE user_id=%s", (user_id,))
            row = c.fetchone()
            if not row:
                return 0
            try:
                until = row['expires_at']
                if until.tzinfo is None:
                    until = until.replace(tzinfo=WIB)
                sisa = (until - now_wib()).total_seconds()
                return math.ceil(sisa / 60) if sisa > 0 else 0
            except Exception as e:
                logger.error(f"[COOLDOWN] Gagal menghitung sisa cooldown: {e}")
                return 0
    finally:
        release_conn(conn)

@async_wrap
def clear_cooldown_db(user_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM cooldowns WHERE user_id=%s", (user_id,))
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def cleanup_expired_cooldowns():
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM cooldowns WHERE expires_at < NOW()")
            conn.commit()
    finally:
        release_conn(conn)

# =================== ADMIN DB FUNCTIONS ===================

@async_wrap
def get_all_admins():
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT * FROM admins ORDER BY added_at ASC")
            return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

@async_wrap
def add_admin(user_id, nama, added_by):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                """INSERT INTO admins (user_id, nama, added_by, added_at)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET nama=EXCLUDED.nama""",
                (user_id, str(nama), added_by, now_wib().strftime("%H:%M - %d/%m/%Y"))
            )
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def remove_admin(user_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM admins WHERE user_id=%s", (user_id,))
            conn.commit()
    finally:
        release_conn(conn)

@async_wrap
def is_admin_in_db(user_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM admins WHERE user_id=%s", (user_id,))
            return c.fetchone() is not None
    finally:
        release_conn(conn)

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
    except Exception:
        return waktu_str

def _build_link_section(group_link: str, fallback_link: str) -> str:
    """Helper untuk membangun blok teks link produk (group atau biasa)."""
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
    lines = [
        judul,
        "========================",
        f"👤 Pembeli  : {esc(user_name)} (<code>{user_id}</code>)",
        f"📦 Paket    : {esc(paket.get('emoji','📦'))} {esc(paket.get('nama','?'))}",
    ]
    if amount is not None:
        lines.append(f"💰 Total    : {format_harga(amount)}")
    lines.append(f"📝 Order ID : <code>{esc(order_id)}</code>")
    lines.append(f"🕒 Waktu    : {now_wib().strftime('%H:%M, %d/%m/%Y')}")
    if extra:
        lines.append(f"\nℹ️ {extra}")
    return "\n".join(lines)

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
            except Exception:
                pass
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
    except Exception:
        if target != ADMIN_ID:
            try:
                await bot.delete_message(chat_id=ADMIN_ID, message_id=int(msg_id))
            except Exception:
                pass

async def hapus_qris_buyer_lama(bot, order_id, user_id):
    order = await get_order_by_id(order_id)
    if order and order.get('buyer_msg_id'):
        try:
            await bot.delete_message(chat_id=int(user_id), message_id=int(order['buyer_msg_id']))
        except Exception:
            pass

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
        except Exception:
            pass

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
            except Exception:
                pass

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

# =================== AUTO-RELEASE SWEEP TASK ===================

async def release_order_tertahan(bot, user_id: int):
    """
    Fungsi penyapu otomatis. Berjalan di latar belakang mengecek order terkunci milik user 
    yang prasyaratnya sekarang sudah berhasil terlengkapi semuanya.
    """
    conn = get_conn()
    locked_orders = []
    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT * FROM orders WHERE user_id=%s AND status='completed' AND link_locked=TRUE",
                (user_id,)
            )
            locked_orders = [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

    if not locked_orders:
        return

    for order in locked_orders:
        paket = await get_product(order['paket_id'])
        if not paket:
            continue
        
        requires_str = paket.get("requires_paket_ids") or ""
        missing = await check_prerequisites_sync(user_id, requires_str)
        
        # Jika prasyarat sudah lengkap (kosong)
        if not missing:
            group_link = await generate_group_link(bot, paket, order['order_id'])
            link = group_link or (paket.get("link") or DEFAULT_LINK)
            link_section = _build_link_section(group_link, link)

            # Kirim akses ke buyer
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"<b>🎉 AKSES TERKUNCI TELAH DIBUKA!</b>\n"
                        f"========================\n\n"
                        f"Prasyarat untuk paket {esc(paket['emoji'])} <b>{esc(paket['nama'])}</b> telah terpenuhi! ✅\n\n"
                        f"{link_section}\n\n"
                        f"Terima kasih atas kesabaran Anda! 🙏"
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⭐ Beri Ulasan / Testimoni", callback_data=f"rate_start|{order['order_id']}")]
                    ])
                )
            except Exception as e:
                logger.error(f"[RELEASE] Gagal kirim link terbebas ke user {user_id}: {e}")

            # Update database kunci order tersebut dibuka
            conn_up = get_conn()
            try:
                with conn_up.cursor() as c:
                    c.execute("UPDATE orders SET link_locked=FALSE, sent_link=%s WHERE order_id=%s", (link, order['order_id']))
                    conn_up.commit()
            finally:
                release_conn(conn_up)

            # Kirim notifikasi ke Admin
            await kirim_notif(
                bot,
                f"🔓 <b>LINK DI-RELEASE OTOMATIS</b>\n"
                f"========================\n\n"
                f"👤 Buyer    : <code>{user_id}</code>\n"
                f"📦 Paket    : {esc(paket['nama'])}\n"
                f"📝 Order ID : <code>{esc(order['order_id'])}</code>\n\n"
                f"ℹ️ Prasyarat akhirnya lengkap, link akses sudah dikirim otomatis ke buyer."
            )

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

    order = await get_order_by_id(order_id)
    if not order:
        return aio_web.Response(status=404, text='order not found')

    if order['status'] != 'waiting':
        return aio_web.Response(text='already processed')

    verified_detail = await get_transaction_detail(order_id, amount)
    if not verified_detail or verified_detail.get('status') != 'completed':
        logger.warning(f"[SECURITY ALERT] Percobaan webhook palsu diblokir! Order ID: {order_id}")
        return aio_web.Response(status=400, text='verification failed')

    paket_id = order['paket_id']
    user_id = order['user_id']
    user_name = order.get('user_name', 'User')

    paket = await get_product(paket_id)
    if not paket:
        return aio_web.Response(status=404, text='product not found')

    _stop_payment_task(user_id)

    if _current_bot:
        asyncio.create_task(
            _handle_payment_success(
                _current_bot, order_id, paket_id, user_id, user_name,
                paket['harga'], {'amount': amount, 'status': 'completed'}
            )
        )

    logger.info(f"[WEBHOOK] ✅ Webhook sukses diverifikasi & diproses: {order_id}")
    return aio_web.Response(text='ok')

_webhook_runner = None

async def _start_webhook_server():
    global _webhook_runner
    webhook_app = aio_web.Application()
    webhook_app.router.add_post('/webhook/pakasir', pakasir_webhook_handler)
    webhook_app.router.add_get('/health', lambda r: aio_web.Response(text='ok'))
    webhook_app.router.add_get('/', lambda r: aio_web.Response(text='Hyper Family Store Bot - OK'))

    runner = aio_web.AppRunner(webhook_app)
    await runner.setup()
    _webhook_runner = runner
    port = int(os.environ.get('PORT', 8080))
    site = aio_web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"[WEBHOOK] Server berjalan di port {port}")

# =================== POST INIT ===================

async def post_init(application: Application):
    global _current_bot, _http_session
    _current_bot = application.bot

    _http_session = aiohttp.ClientSession()

    await application.bot.set_my_commands(
        [
            BotCommand("start",   "Buka toko"),
            BotCommand("riwayat", "Lihat riwayat ordermu"),
        ],
        scope=BotCommandScopeDefault()
    )
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Buka toko"),
            BotCommand("admin", "Panel admin"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID)
    )

    waiting_orders = await get_all_waiting()
    if waiting_orders:
        logger.info(f"[POST_INIT] Ditemukan {len(waiting_orders)} order aktif, membuat ulang payment tasks...")
        for order in waiting_orders:
            paket = await get_product(order['paket_id'])
            if not paket:
                continue
            recovery_amount = order.get('harga_dibayar') or paket['harga']
            if order.get('created_at'):
                try:
                    elapsed = int((now_wib() - order['created_at']).total_seconds())
                    recovery_timeout = max(300, 1800 - elapsed)
                except Exception:
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
            logger.info(f"[POST_INIT] Task dimulai ulang untuk order {order['order_id']}")

    asyncio.create_task(_auto_backup_loop())
    asyncio.create_task(_buyer_reminder_loop(_current_bot))
    asyncio.create_task(_cleanup_cooldowns_loop())
    asyncio.create_task(_start_webhook_server())
    logger.info("[POST_INIT] Semua task background dijadwalkan.")

# =================== GRACEFUL SHUTDOWN ===================

async def post_shutdown(application: Application):
    global _http_session, _pool, _webhook_runner
    logger.info("[SHUTDOWN] Memulai proses graceful shutdown...")
    if _http_session and not _http_session.closed:
        await _http_session.close()
        logger.info("[SHUTDOWN] Sesi HTTP aiohttp berhasil ditutup.")
    if _webhook_runner:
        await _webhook_runner.cleanup()
        logger.info("[SHUTDOWN] Webhook server runner berhasil dibersihkan.")
    if _pool:
        _pool.closeall()
        logger.info("[SHUTDOWN] Database connection pool berhasil ditutup.")
    logger.info("[SHUTDOWN] Graceful shutdown selesai.")

# =================== USER HANDLERS ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_admin(user_id, context) and await is_maintenance():
        await update.message.reply_text(
            "⚙️ <b>BOT SEDANG MAINTENANCE</b>\n"
            "========================\n\n"
            "Bot sedang dalam perbaikan sementara.\n"
            "Silakan coba lagi nanti.\n\n"
            "Hubungi admin: @Kikukkvd",
            parse_mode="HTML"
        )
        return

    if await is_banned(user_id):
        await update.message.reply_text(
            "🚫 <b>Akun kamu diblokir</b>\n"
            "========================\n\n"
            "Kamu tidak bisa menggunakan bot ini.\n"
            "Hubungi admin jika ada pertanyaan.",
            parse_mode="HTML"
        )
        return

    active = await get_active_order(user_id)
    if active:
        paket = await get_product(active["paket_id"])
        if not paket:
            paket = {"emoji": "📦", "nama": "Produk", "harga": 0, "link": DEFAULT_LINK}

        trans = await get_transaction_detail(active["order_id"], active.get("harga_dibayar") or paket["harga"])

        if trans and trans.get("status") == "completed":
            _stop_payment_task(user_id)
            await _handle_payment_success(context.bot, active["order_id"], active["paket_id"], user_id, active.get("user_name", "User"), active.get("harga_dibayar") or paket["harga"], trans)
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
        keyboard = [[InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="back_start")]]
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

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if not await is_admin(user_id, context) and await is_maintenance():
        await query.answer("⚙️ Bot sedang maintenance. Coba lagi nanti.", show_alert=True)
        return

    if await is_banned(user_id):
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
    keyboard.append([InlineKeyboardButton("⬅️ Kembali", callback_data="back_start")])
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
        keyboard = [[InlineKeyboardButton("❌ Batalkan", callback_data="back_start")]]
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

async def _buat_order_baru(update, context, query, user_id, user_name, paket, order_changes=0):
    """Helper: buat QRIS + simpan order + kirim ke buyer & admin."""
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
    except Exception:
        pass

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

    sisa_ganti = 2 - order_changes
    caption = (
        f"<b>{esc(paket['emoji'])} {esc(paket['nama']).upper()}</b>\n"
        f"========================\n\n"
        f"📋 <b>Detail Pembayaran</b>\n"
        f"- Harga: {format_harga(amount)}\n"
        f"- Fee: {format_harga(fee)}\n"
        f"- <b>Total: {format_harga(total_payment)}</b>\n\n"
        f"📝 Order ID: <code>{esc(order_id)}</code>\n"
        f"⏰ Berlaku hingga: {expire} WIB\n\n"
        f"========================\n"
        f"📱 <b>Scan QRIS di atas untuk membayar</b>\n\n"
        f"✅ Nominal sudah termasuk fee\n"
        f"✅ Pembayaran otomatis terverifikasi\n"
        f"✅ Link produk dikirim otomatis setelah bayar\n\n"
        f"⏳ Menunggu pembayaran..."
    )

    if sisa_ganti > 0:
        kb = [
            [InlineKeyboardButton(f"🔄 Ganti Paket (sisa {sisa_ganti}x)", callback_data="ganti_paket_list")],
            [InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="back_start")],
        ]
    else:
        kb = [[InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="back_start")]]

    if query:
        try:
            await query.message.delete()
        except Exception:
            pass

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

async def back_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        _stop_payment_task(user_id)
        await hapus_qris_buyer_lama(context.bot, active["order_id"], user_id)
        await set_cooldown_db(user_id)

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

    context.user_data.clear()

    try:
        await query.message.delete()
    except Exception:
        pass

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
    if changes_used >= 2:
        await query.answer("⛔ Batas ganti paket sudah tercapai (2x).", show_alert=True)
        return

    current_paket_id = active['paket_id']
    products = await get_all_products()
    products = [p for p in products if p.get('aktif', True)]

    sisa = 2 - changes_used
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
    await query.answer()
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
    if changes_used >= 2:
        await query.answer("⛔ Batas ganti paket sudah tercapai (2x).", show_alert=True)
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
    _stop_payment_task(user_id)
    await hapus_qris_buyer_lama(context.bot, old_order_id, user_id)
    await hapus_notif_lama(context.bot, old_order_id)

    try:
        await query.message.delete()
    except Exception:
        pass

    await _buat_order_baru(update, context, None, user_id, user_name,
                           new_paket, order_changes=changes_used + 1)

async def ganti_paket_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    active = await get_active_order(user_id)
    if not active:
        await query.answer("❌ Pesanan tidak ditemukan.", show_alert=True)
        return

    paket = await get_product(active['paket_id'])
    if not paket:
        await query.answer("❌ Produk tidak ditemukan.", show_alert=True)
        return

    changes_used = active.get('order_changes', 0)
    sisa_ganti = 2 - changes_used

    caption_back = (
        f"<b>{esc(paket['emoji'])} {esc(paket['nama']).upper()}</b>\n"
        f"========================\n\n"
        f"📝 Order ID: <code>{esc(active['order_id'])}</code>\n\n"
        f"⏳ Menunggu pembayaran..."
    )
    if sisa_ganti > 0:
        kb = [
            [InlineKeyboardButton(f"🔄 Ganti Paket (sisa {sisa_ganti}x)", callback_data="ganti_paket_list")],
            [InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="back_start")],
        ]
    else:
        kb = [[InlineKeyboardButton("❌ Batalkan Pesanan", callback_data="back_start")]]

    try:
        await query.message.edit_caption(caption_back, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        pass

# =================== ASYNCIO PAYMENT TASKS ===================

_payment_tasks: dict = {}
_current_bot = None

def _stop_payment_task(user_id: int):
    task = _payment_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()

def _start_payment_task(bot, order_id: str, paket_id: str, user_id: int,
                         user_name: str, amount: int, timeout_seconds: int = 1800):
    _stop_payment_task(user_id)
    task = asyncio.create_task(
        _payment_poll_loop(bot, order_id, paket_id, user_id, user_name, amount, timeout_seconds)
    )
    _payment_tasks[user_id] = task

def _check_order_status_sync(order_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT status FROM orders WHERE order_id=%s", (order_id,))
            row = c.fetchone()
            return dict(row) if row else None
    finally:
        release_conn(conn)

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

        # TIMEOUT - cek sekali lagi sebelum expired
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

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "<b>⏰ SESI BERAKHIR</b>\n"
                    "========================\n\n"
                    "Pesanan telah dibatalkan otomatis.\n\n"
                    "Alasan: Pembayaran tidak diterima dalam waktu yang ditentukan.\n\n"
                    "Ketik /start untuk membuat pesanan baru."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

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
        _payment_tasks.pop(user_id, None)

async def _handle_payment_success(bot, order_id: str, paket_id: str, user_id: int,
                                   user_name: str, amount: int, trans: dict):
    # Atomic: hanya proses jika masih 'waiting' → mencegah double processing
    success = await mark_order_completed(order_id)
    if not success:
        logger.info(f"[PAYMENT] Order {order_id} sudah diproses sebelumnya, skip.")
        return

    await hapus_qris_buyer_lama(bot, order_id, user_id)

    paket = await get_product(paket_id) or {"emoji": "📦", "nama": "Produk", "harga": amount, "link": DEFAULT_LINK}
    paid_amount = trans.get('amount', amount)

    # ── CEK PREREQUISITE ──
    requires_str = paket.get("requires_paket_ids") or ""
    missing_prereqs = await check_prerequisites_sync(user_id, requires_str)

    if missing_prereqs:
        # Kunci link order ini di database
        await set_order_link_locked(order_id, True)

        missing_names = []
        keyboard_buttons = []
        for pid in missing_prereqs:
            p_obj = await get_product(pid)
            label = f"{p_obj['emoji']} {p_obj['nama']}" if p_obj else f"<code>{esc(pid)}</code>"
            missing_names.append(label)
            # Buat tombol beli langsung untuk prasyarat yang kurang
            if p_obj:
                keyboard_buttons.append([InlineKeyboardButton(f"🛒 Beli {p_obj['nama']} - {format_harga(p_obj['harga'])}", callback_data=f"pilih_{pid}")])

        missing_list = "\n".join(f"  • {n}" for n in missing_names)
        keyboard_buttons.append([InlineKeyboardButton("💬 Chat Admin", url=await get_setting('link_admin', 'https://t.me/Kikukkvd'))])

        # Beritahu buyer — pembayaran diterima tapi link ditahan
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"<b>✅ PEMBAYARAN BERHASIL DITERIMA</b>\n"
                    f"========================\n\n"
                    f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
                    f"💰 Total: {format_harga(paid_amount)}\n"
                    f"🔖 Order ID: <code>{esc(order_id)}</code>\n\n"
                    f"========================\n"
                    f"⚠️ <b>Akses Link Ditahan (Terkunci)</b>\n\n"
                    f"Untuk mengakses paket ini, kamu wajib memiliki:\n"
                    f"{missing_list}\n\n"
                    f"Silakan beli paket di atas agar link otomatis dikirim:"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard_buttons)
            )
        except Exception as e:
            logger.error(f"[PAYMENT] Gagal kirim notif prereq ke buyer {user_id}: {e}")

        await hapus_notif_lama(bot, order_id)
        extra_prereq = (
            f"⏸️ <b>LINK DITAHAN — Syarat belum terpenuhi</b>\n"
            f"Paket yang belum dibeli:\n{missing_list}\n\n"
            f"Klik tombol di bawah untuk kirim link setelah syarat terpenuhi."
        )
        msg_id = await kirim_notif(
            bot,
            _format_order_notif(
                "✅ <b>PEMBAYARAN TERKUNCI</b>",
                user_name, user_id, paket, order_id,
                amount=paid_amount,
                extra=extra_prereq
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📤 Kirim Link VIP (Syarat Terpenuhi)", callback_data=f"admin_kirim_link_prereq|{order_id}")
            ]])
        )
        if msg_id:
            await set_admin_msg_id(order_id, msg_id)
        return

    # ── JIKA SYARAT LENGKAP (NORMAL) ──
    group_link = await generate_group_link(bot, paket, order_id)
    link = group_link or (paket.get("link") or DEFAULT_LINK)
    link_section = _build_link_section(group_link, link)

    kirim_berhasil = False
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"<b>✅ PEMBAYARAN BERHASIL</b>\n"
                f"========================\n\n"
                f"📦 <b>Detail Pesanan</b>\n"
                f"- Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
                f"- Order ID: <code>{esc(order_id)}</code>\n"
                f"- Total: {format_harga(paid_amount)}\n\n"
                f"========================\n"
                f"{link_section}\n\n"
                f"Terima kasih telah berbelanja! 🙏\n\n"
                f"Bantu kami berkembang dengan memberikan ulasan di bawah ini:"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Beri Ulasan / Testimoni", callback_data=f"rate_start|{order_id}")]
            ])
        )
        kirim_berhasil = True
    except Exception as e:
        logger.error(f"[PAYMENT] Gagal kirim link ke buyer {user_id}: {e}")

    await set_sent_link(order_id, link)
    await hapus_notif_lama(bot, order_id)

    extra_paid = "✅ Link produk sudah terkirim ke buyer" if kirim_berhasil else "⚠️ GAGAL kirim link ke buyer - cek manual!"
    msg_id = await kirim_notif(
        bot,
        _format_order_notif(
            "✅ <b>PEMBAYARAN BERHASIL</b>",
            user_name, user_id, paket, order_id,
            amount=paid_amount,
            extra=extra_paid
        )
    )
    if msg_id:
        await set_admin_msg_id(order_id, msg_id)

    # Jalankan penyapu otomatis
    asyncio.create_task(release_order_tertahan(bot, user_id))

# =================== ADMIN: KIRIM LINK PREREQ MANUAL ===================

async def admin_kirim_link_prereq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin konfirmasi kirim link VIP setelah buyer memenuhi syarat prerequisite."""
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id, context):
        await query.answer("⛔ Akses ditolak.", show_alert=True)
        return

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

    await set_sent_link(order_id, link)
    await set_order_link_locked(order_id, False) # Buka kunci aksesnya di database

    if kirim_berhasil:
        await query.edit_message_text(
            query.message.text + f"\n\n✅ <b>Link sudah dikirim ke buyer oleh {esc(query.from_user.full_name)}.</b>",
            parse_mode="HTML"
        )
    else:
        await query.answer("❌ Gagal kirim link ke buyer. Cek bot tidak diblokir buyer.", show_alert=True)

# =================== AUTO-APPROVE & REVOKE JOIN REQUEST ===================

def _check_join_request_sync(user_id, chat_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT o.id FROM orders o
                JOIN products p ON o.paket_id = p.paket_id
                WHERE o.user_id = %s AND o.status = 'completed' AND o.link_locked = FALSE
                AND p.group_chat_id = %s
                ORDER BY o.id DESC LIMIT 1
            """, (user_id, chat_id))
            return c.fetchone()
    finally:
        release_conn(conn)

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
                    logger.error(f"[JOIN] Gagal revoke link: {rev_err}")
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

    order = await get_order_by_id(order_id)
    paket_id_testi = order['paket_id'] if order else ""
    paket = await get_product(paket_id_testi) if order else None
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
    """Kembali ke pesan sukses dengan link produk dari mana saja di flow review."""
    query = update.callback_query
    await query.answer()

    order_id = query.data.split("|")[1]

    context.user_data.pop('awaiting_review_text', None)
    context.user_data.pop('temp_rating', None)

    order = await get_order_by_id(order_id)
    if not order:
        await query.edit_message_text("⚠️ Data pesanan tidak ditemukan.")
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
            f"3. Bot langsung <b>approve otomatis</b> ✅\n\n"
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
    """Kembali ke layar pilih bintang dari layar input teks ulasan."""
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
    await query.answer()

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
    # Refresh detail
    p['aktif'] = new_aktif
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

    label_map = {
        "nama": "nama produk",
        "emoji": "emoji",
        "harga": "harga (angka saja, contoh: 15000)",
        "deskripsi": "deskripsi",
        "link": "link produk",
        "group_chat_id": "ID grup private (contoh: -1001234567890, ketik 'hapus' untuk kosongkan)",
        "requires_paket_ids": "paket_id syarat dipisah koma (contoh: gb_biasa,gb_standar), ketik 'hapus' untuk kosongkan",
    }

    context.user_data['editing_product'] = {'paket_id': paket_id, 'field': field}

    await query.edit_message_text(
        f"<b>✍️ Edit {field.upper()} - {esc(p['emoji'])} {esc(p['nama'])}</b>\n"
        f"========================\n\n"
        f"Nilai saat ini: <code>{esc(str(p[field]))}</code>\n\n"
        f"<i>Kirim {esc(label_map[field])} baru sekarang:</i>",
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

async def cmd_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.message.from_user.id, context):
        return

    orders = await get_all_waiting()
    if not orders:
        await update.message.reply_text(
            "<b>✅ TIDAK ADA ORDER AKTIF</b>\n"
            "========================\n\n"
            "Tidak ada buyer yang sedang menunggu membayar saat ini.",
            parse_mode="HTML"
        )
        return

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

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

def _check_waiting_order_sync(order_id):
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE order_id=%s AND status='waiting'", (order_id,))
            return c.fetchone()
    finally:
        release_conn(conn)

async def admin_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id, context):
        await query.answer("⛔ Akses ditolak.", show_alert=True)
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
    paket = await get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0}

    cancel_amount = order.get('harga_dibayar') or paket.get('harga', 0)
    if cancel_amount:
        await cancel_transaction(order_id, cancel_amount)

    await update_order_status(order_id, 'cancelled')
    _stop_payment_task(target_user_id)
    await hapus_qris_buyer_lama(context.bot, order_id, target_user_id)
    await hapus_notif_lama(context.bot, order_id)

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
                "Hubungi admin jika ada pertanyaan.\n"
                "Ketik /start untuk membuat pesanan baru."
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

    await query.edit_message_text(
        f"✅ <b>Order Dibatalkan</b>\n\n"
        f"👤 Buyer: {esc(order.get('user_name', '-'))}\n"
        f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
        f"📝 Order ID: <code>{esc(order_id)}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Orders", callback_data="admpanel_orders")]])
    )

async def admin_manual_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin melakukan konfirmasi manual pada order berstatus waiting."""
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

    _stop_payment_task(target_user_id)
    
    # Jalankan alur pemrosesan terpadu (checking prasyarat dan auto-release otomatis dilakukan di sini)
    await _handle_payment_success(
        context.bot, 
        order_id, 
        order['paket_id'], 
        target_user_id, 
        order.get('user_name', 'User'), 
        order.get('harga_dibayar') or paket['harga'], 
        {'amount': order.get('harga_dibayar') or paket['harga'], 'status': 'completed'}
    )

    await query.edit_message_text(
        f"✅ <b>Pembayaran Dikonfirmasi Manual</b>\n\n"
        f"👤 Buyer: {esc(order.get('user_name', '-'))}\n"
        f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
        f"📝 Order ID: <code>{esc(order_id)}</code>\n"
        f"ℹ️ Alur prasyarat diproses otomatis (terkirim langsung atau terkunci sesuai syarat).",
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
        if diff > 0:
            label = _format_short(diff) if is_money else str(diff)
            return f"  <i>↑ +{label}</i>"
        elif diff < 0:
            label = _format_short(abs(diff)) if is_money else str(abs(diff))
            return f"  <i>↓ -{label}</i>"
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
        max_cnt = s['products_breakdown'][0]['cnt'] or 1
        total_rev = s['total_revenue'] or 1
        for p in s['products_breakdown']:
            emoji = p.get('emoji') or '📦'
            nama = esc(p.get('nama') or p['paket_id'])
            bars = round((p['cnt'] / max_cnt) * 7)
            bar = '█' * bars + '░' * (7 - bars)
            pct_rev = round(p['total'] / total_rev * 100)
            prod_lines += (
                f"- {esc(emoji)} <b>{nama}</b>\n"
                f"│  <code>{bar}</code>  {p['cnt']}x · {_format_short(p['total'])}  <i>({pct_rev}%)</i>\n"
            )
    else:
        prod_lines = "- <i>Belum ada transaksi produk.</i>\n"

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
        "🗓️ <b>BULAN INI</b>",
        f"- Order Selesai  :  <b>{s['month_completed']}</b>{growth(s['month_completed'], s['last_month_completed'])}",
        f"- Omzet          :  <b>{_format_short(s['month_revenue'])}</b>{growth(s['month_revenue'], s['last_month_revenue'], True)}",
        "",
        "========================",
        "📈 <b>TREN 7 HARI TERAKHIR</b>",
        trend_lines.rstrip('\n'),
        peak_str.rstrip('\n') if peak_str else "",
        "",
        "========================",
        "🏆 <b>ALL TIME</b>",
        f"- Total Omzet     :  <b>{_format_short(s['total_revenue'])}</b>",
        f"- Rata-rata/hari  :  <b>{_format_short(s['avg_daily_revenue'])}</b>  <i>(30 hari)</i>",
        f"- Pembeli Unik    :  <b>{s['total_buyers']}</b> orang",
        f"- Beli Ulang      :  <b>{s['repeat_buyers']}</b> orang",
        f"- Baru Bulan Ini  :  <b>{s['new_buyers_month']}</b> orang",
        f"- AOV             :  <b>{_format_short(s['aov'])}</b>",
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
        "📦 <b>PENJUALAN PER PRODUK</b>",
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
    """Eksport semua tabel penting ke dict. Satu fungsi untuk backup & export."""
    conn = get_conn()
    try:
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
    finally:
        release_conn(conn)
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
    backup_name = f"backup_{now_wib().strftime('%Y%m%d_%H%M%S')}.txt"
    try:
        products, orders, banned, testimonials, settings, admins = await asyncio.to_thread(_generate_full_export_sync)

        lines = []
        lines.append("=== BACKUP HYPER FAMILY STORE ===\n")
        lines.append(f"Tanggal: {now_wib().strftime('%H:%M, %d/%m/%Y')}\n")
        lines.append(f"Versi Backup: 3.0\n\n")

        lines.append(f"--- PRODUK ({len(products)}) ---\n")
        for p in products:
            grp = p.get('group_chat_id') or '-'
            aktif = '✅' if p.get('aktif', True) else '🔴'
            lines.append(
                f"[{p['paket_id']}] {p.get('emoji','')} {p['nama']} - Rp {int(p['harga']):,} "
                f"| Aktif: {aktif} | Link: {p.get('link','-')} | Group: {grp}\n"
            )

        lines.append(f"\n--- ORDERS ({len(orders)}) ---\n")
        for o in orders:
            lines.append(
                f"[{o['id']}] {o['order_id']}\n"
                f"  User: {o.get('user_name','-')} ({o['user_id']})\n"
                f"  Paket: {o['paket_id']} | Status: {o['status']} | "
                f"Harga: Rp {int(o.get('harga_dibayar') or 0):,} | Waktu: {o.get('waktu','-')}\n\n"
            )

        lines.append(f"\n--- BANNED USERS ({len(banned)}) ---\n")
        for b in banned:
            lines.append(
                f"User ID: {b['user_id']} | Alasan: {b.get('reason') or '-'} | Sejak: {b.get('banned_at','-')}\n"
            )

        lines.append(f"\n--- ADMINS ({len(admins)}) ---\n")
        for a in admins:
            lines.append(f"ID: {a['user_id']} | Nama: {a.get('nama','-')} | Ditambahkan: {a.get('added_at','-')}\n")

        lines.append(f"\n--- SETTINGS ({len(settings)}) ---\n")
        for s in settings:
            if s['key'] not in ('managed_groups',):
                lines.append(f"{s['key']}: {s.get('value','-')}\n")

        lines.append(f"\n--- TESTIMONI ({len(testimonials)}) ---\n")
        for t in testimonials:
            lines.append(
                f"[{t['id']}] {t.get('user_name','-')} | Paket: {t['paket_id']} | "
                f"Rating: {t.get('rating',0)}/5 | Status: {t.get('status','-')}\n"
                f"  Review: {t.get('review','-')[:100]}\n"
            )

        content = "".join(lines).encode("utf-8")
        buf = BytesIO(content)
        buf.name = backup_name

        await bot.send_document(
            chat_id=ADMIN_ID,
            document=buf,
            filename=backup_name,
            caption=(
                f"📦 <b>Backup Database</b>\n"
                f"🕒 {now_wib().strftime('%H:%M, %d/%m/%Y')}\n"
                f"📋 {len(orders)} orders | {len(products)} produk | {len(banned)} banned | "
                f"{len(testimonials)} testimoni | {len(admins)} admin"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error backup: {e}", exc_info=True)
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Gagal backup database: {esc(str(e))}", parse_mode="HTML")
        except Exception:
            pass

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

    conn = get_conn()
    try:
        with conn.cursor() as c:
            # Products
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

            # Orders
            for o in orders:
                try:
                    c.execute(
                        """INSERT INTO orders
                           (user_id, user_name, paket_id, order_id, status, waktu, harga_dibayar, sent_link, order_changes, link_locked)
                           VALUES (%(user_id)s, %(user_name)s, %(paket_id)s, %(order_id)s,
                                   %(status)s, %(waktu)s, %(harga_dibayar)s, %(sent_link)s, %(order_changes)s, %(link_locked)s)
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
                            "link_locked":   o.get("link_locked", False),
                        }
                    )
                    ok_o += 1
                except Exception as e:
                    logger.error(f"[IMPORT] order gagal: {e}")
                    fail_o += 1

            # Banned
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

            # Testimonials
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

            # Settings
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

            # Admins
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

            conn.commit()
    finally:
        release_conn(conn)
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
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT * FROM orders WHERE order_id=%s AND user_id=%s AND status='completed'",
                (order_id, user_id)
            )
            return c.fetchone()
    finally:
        release_conn(conn)

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

    if order.get('link_locked'):
        await query.edit_message_text(
            "⚠️ <b>Akses Link Ditahan</b>\n\n"
            "Prasyarat pembelian paket ini belum lengkap. Silakan penuhi syarat terlebih dahulu agar sistem membuka kuncinya secara otomatis.",
            parse_mode="HTML"
        )
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

# =================== BUYER REMINDER ===================
REMINDER_HARI = 3

def _get_buyers_for_reminder_sync(hari: int):
    target_day_start = (now_wib() - timedelta(days=hari)).replace(hour=0, minute=0, second=0, microsecond=0)
    target_day_end = target_day_start + timedelta(days=1)
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT DISTINCT user_id, user_name FROM orders WHERE status='completed' AND created_at >= %s AND created_at < %s",
                (target_day_start, target_day_end)
            )
            return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

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
            logger.error(f"[REMINDER] Error loop: {e}")
            await asyncio.sleep(3600)

async def _auto_backup_loop():
    while True:
        try:
            now = now_wib()
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_secs = (next_midnight - now).total_seconds()
            await asyncio.sleep(wait_secs)
            logger.info("[AUTO_BACKUP] Menjalankan backup harian...")
            await _kirim_backup(_current_bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[AUTO_BACKUP] Error: {e}")
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
            except Exception:
                pass
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
            logger.error(f"[CLEANUP] Error: {e}")

# =================== ADMIN: BROADCAST ===================

_blast_tasks: dict = {}

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
                logger.warning(f"[BLAST] Terkena rate limit, tidur {e.retry_after} detik.")
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
                except Exception:
                    pass

    except asyncio.CancelledError:
        logger.info(f"[BLAST] Broadcast dibatalkan oleh admin {admin_id}.")

    finally:
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
        except Exception:
            pass

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
    """Admin konfirmasi blast setelah preview."""
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

    task = asyncio.create_task(_run_broadcast(query.get_bot(), admin_id, buyers, text_blast))
    _blast_tasks[admin_id] = task

async def blast_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin stop broadcast yang sedang berjalan."""
    query = update.callback_query
    await query.answer("⛔ Menghentikan broadcast...")

    admin_id = query.from_user.id
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

    # --- STATE: BUYER MENGETIK TESTIMONI ---
    if context.user_data.get('awaiting_review_text'):
        context.user_data.pop('awaiting_review_text', None)
        temp = context.user_data.pop('temp_rating', None)

        if not temp:
            await update.message.reply_text("❌ Sesi pengisian ulasan kedaluwarsa. Silakan ulangi.")
            return

        rating = temp['rating']
        order_id = temp['order_id']
        order = await get_order_by_id(order_id)
        if not order:
            await update.message.reply_text("❌ Pesanan tidak ditemukan.")
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

    # --- ADMIN STATES ---
    if await is_admin(user_id, context):

        # --- State: blast - mengetik pesan ---
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

        # --- State: awaiting cari order ---
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
            STATUS_LABEL = {
                'completed': '✅ Selesai / Lunas', 'waiting': '⏳ Menunggu Bayar',
                'pending': '🔄 Diproses Manual', 'cancelled': '❌ Dibatalkan',
                'expired': '⏰ Kedaluwarsa', 'rejected': '🚫 Ditolak',
            }
            status = STATUS_LABEL.get(order['status'], order['status'])
            sent_link = order.get('sent_link') or '-'
            await update.message.reply_text(
                f"<b>🔍 DETAIL ORDER</b>\n"
                f"========================\n\n"
                f"Order ID: <code>{esc(order['order_id'])}</code>\n"
                f"Buyer: {esc(order.get('user_name', '-'))} (<code>{order['user_id']}</code>)\n"
                f"Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
                f"Harga Dibayar: {format_harga(order.get('harga_dibayar') or paket['harga'])}\n"
                f"Status: {status}\n"
                f"Dibuat: {order.get('waktu', '-')}\n"
                f"Link terkirim: {esc(sent_link)}",
                parse_mode="HTML",
                reply_markup=back_orders_kb
            )
            return

        # --- State: awaiting ban user ---
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

        # --- State: awaiting unban user ---
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

        # --- State: awaiting kick user ID ---
        if context.user_data.get('awaiting_kick_userid'):
            context.user_data.pop('awaiting_kick_userid', None)
            try:
                target_id = int(text.strip())
            except ValueError:
                await update.message.reply_text("❌ User ID harus berupa angka. Contoh: <code>123456789</code>", parse_mode="HTML")
                return
            managed_groups = await get_managed_groups()
            if not managed_groups:
                await update.message.reply_text("❌ Belum ada grup terdaftar. Tambahkan grup dulu dari menu Kick.", parse_mode="HTML")
                return
            lines = []
            for gid in managed_groups:
                try:
                    member = await context.bot.get_chat_member(chat_id=int(gid), user_id=target_id)
                    status = member.status
                    if status in ('member', 'administrator', 'creator', 'restricted'):
                        ada = f"✅ Ada ({status})"
                    else:
                        ada = f"❌ Tidak ada ({status})"
                except Exception as e:
                    ada = f"⚠️ Gagal cek: {esc(str(e))}"
                lines.append(f"  - <code>{gid}</code>: {ada}")
            result_text = (
                f"🔍 <b>Hasil Cek User</b> <code>{target_id}</code>\n"
                f"========================\n\n"
                + "\n".join(lines)
                + "\n\n<i>Tekan tombol di bawah untuk mengeluarkan user dari semua grup.</i>"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"👢 Kick dari Semua Grup", callback_data=f"kick_do_kick|{target_id}")],
                [InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_kick")],
            ])
            await update.message.reply_text(result_text, parse_mode="HTML", reply_markup=keyboard)
            return

        # --- State: awaiting tambah grup ---
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

        # --- State: awaiting channel ID ---
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

        # --- State: awaiting testi channel ID ---
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

        # --- State: tambah admin via forward ---
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

        # --- State: ubah link testimoni ---
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

        # --- State: ubah link admin/CS ---
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

        # --- State: tambah produk ---
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

        # --- State: edit field produk ---
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
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE user_id=%s AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,))
            return c.fetchone()
    finally:
        release_conn(conn)

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
    """Admin melakukan konfirmasi manual pada order berstatus pending."""
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
        # Jalankan alur pemrosesan terpadu (checking prasyarat dan auto-release otomatis dilakukan di sini)
        await _handle_payment_success(
            context.bot, 
            order["order_id"], 
            order["paket_id"], 
            user_id, 
            order.get('user_name', 'User'), 
            order.get('harga_dibayar') or paket['harga'], 
            {'amount': order.get('harga_dibayar') or paket['harga'], 'status': 'completed'}
        )

        await query.edit_message_text(
            f"✅ <b>Dikonfirmasi</b>\n"
            f"========================\n\n"
            f"👤 Pembeli: {esc(order['user_name'])}\n"
            f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n\n"
            f"ℹ️ Alur prasyarat diproses otomatis (terkirim langsung atau terkunci sesuai syarat).",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali ke Panel", callback_data="admpanel_back")]])
        )

    elif action == "reject":
        await update_order_status(order["order_id"], 'rejected')
        await hapus_qris_buyer_lama(context.bot, order["order_id"], user_id)
        await hapus_notif_lama(context.bot, order["order_id"])

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

    orders = await get_all_waiting()
    if not orders:
        await query.edit_message_text(
            "<b>✅ TIDAK ADA ORDER AKTIF</b>\n========================\n\nTidak ada buyer yang sedang menunggu membayar.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="admpanel_orders")]])
        )
        return

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
            InlineKeyboardButton(f"✅ Konfirmasi: {o['user_name']}", callback_data=f"adm_konfirm|{o['user_id']}|{o['order_id']}"),
            InlineKeyboardButton(f"❌ Cancel: {o['user_name']}", callback_data=f"adm_cancel|{o['user_id']}|{o['order_id']}")
        ])
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
    except Exception:
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
    """Ubah pesan blast setelah preview."""
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
            except Exception:
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
    context.user_data['awaiting_kick_userid'] = True
    await query.edit_message_text(
        "<b>🔍 CEK USER DI GRUP</b>\n"
        "========================\n\n"
        "Kirim <b>User ID</b> yang ingin dicek.\n"
        "Contoh: <code>123456789</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="admpanel_kick")]])
    )

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
        buttons.append([InlineKeyboardButton(f"🗑️ {gid}", callback_data=f"kick_del_confirm|{gid}")])
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
            await context.bot.ban_chat_member(chat_id=int(gid), user_id=target_id)
            await context.bot.unban_chat_member(chat_id=int(gid), user_id=target_id, only_if_banned=True)
            kicked.append(str(gid))
        except Exception as e:
            failed.append(f"{gid}: {esc(str(e))}")
    result_text = (
        f"✅ User <code>{target_id}</code> berhasil dikick dari:\n"
        + "\n".join(f"  - <code>{g}</code>" for g in kicked)
    )
    if failed:
        result_text += "\n\n❌ Gagal di:\n" + "\n".join(f"  - {f}" for f in failed)
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

# =================== ADMIN: BAN / UNBAN (command) ===================

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
    STATUS_LABEL = {
        'completed': '✅ Selesai / Lunas', 'waiting': '⏳ Menunggu Bayar',
        'pending': '🔄 Diproses Manual', 'cancelled': '❌ Dibatalkan',
        'expired': '⏰ Kedaluwarsa', 'rejected': '🚫 Ditolak',
    }
    status = STATUS_LABEL.get(order['status'], order['status'])
    sent_link = order.get('sent_link') or '-'
    await update.message.reply_text(
        f"<b>🔍 DETAIL ORDER</b>\n"
        f"========================\n\n"
        f"📝 Order ID: <code>{esc(order['order_id'])}</code>\n"
        f"👤 Buyer: {esc(order.get('user_name', '-'))} (<code>{order['user_id']}</code>)\n"
        f"📦 Paket: {esc(paket['emoji'])} {esc(paket['nama'])}\n"
        f"💰 Harga Dibayar: {format_harga(order.get('harga_dibayar') or paket['harga'])}\n"
        f"📊 Status: {status}\n"
        f"🕒 Dibuat: {order.get('waktu', '-')}\n"
        f"🔗 Link terkirim: {esc(sent_link)}",
        parse_mode="HTML"
    )

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

# =================== MAIN ===================

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

    # User commands
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("riwayat", cmd_riwayat))

    # Admin commands
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

    # User callbacks
    app.add_handler(CallbackQueryHandler(buy_callback,         pattern="^buy$"))
    app.add_handler(CallbackQueryHandler(pilih_paket,          pattern="^pilih_"))
    app.add_handler(CallbackQueryHandler(back_start,           pattern="^back_start$"))
    app.add_handler(CallbackQueryHandler(ganti_paket_list,     pattern="^ganti_paket_list$"))
    app.add_handler(CallbackQueryHandler(ganti_paket_konfirm,  pattern="^ganti_paket_konfirm\\|"))
    app.add_handler(CallbackQueryHandler(ganti_paket_exec,     pattern="^ganti_paket_exec\\|"))
    app.add_handler(CallbackQueryHandler(ganti_paket_batal,    pattern="^ganti_paket_batal$"))

    # Kirim ulang link
    app.add_handler(CallbackQueryHandler(resend_group_link, pattern="^resendlink\\|"))

    # Testimoni user callbacks
    app.add_handler(CallbackQueryHandler(handle_rate_start,      pattern="^rate_start\\|"))
    app.add_handler(CallbackQueryHandler(handle_rate_val,        pattern="^rate_val\\|"))
    app.add_handler(CallbackQueryHandler(handle_rate_text_skip,  pattern="^rate_text_skip\\|"))
    app.add_handler(CallbackQueryHandler(handle_rate_skip,       pattern="^rate_skip$"))
    app.add_handler(CallbackQueryHandler(handle_rate_back,       pattern="^rate_back\\|"))
    app.add_handler(CallbackQueryHandler(handle_rate_back_stars, pattern="^rate_back_stars\\|"))

    # Admin kirim link setelah prerequisite terpenuhi
    app.add_handler(CallbackQueryHandler(admin_kirim_link_prereq, pattern="^admin_kirim_link_prereq\\|"))

    # Admin ulasan moderasi
    app.add_handler(CallbackQueryHandler(admin_testi_approve, pattern="^adm_testi_approve\\|"))
    app.add_handler(CallbackQueryHandler(admin_testi_reject,  pattern="^adm_testi_reject\\|"))

    # Produk management
    app.add_handler(CallbackQueryHandler(produk_detail,        pattern="^pd_detail_"))
    app.add_handler(CallbackQueryHandler(produk_edit_field,    pattern="^pd_edit_"))
    app.add_handler(CallbackQueryHandler(produk_toggle_aktif,  pattern="^pd_toggle_"))
    app.add_handler(CallbackQueryHandler(produk_hapus_confirm, pattern="^pd_hapus_(?!ok_)"))
    app.add_handler(CallbackQueryHandler(produk_hapus_exec,    pattern="^pd_hapus_ok_"))
    app.add_handler(CallbackQueryHandler(produk_tambah_start,  pattern="^pd_tambah$"))
    app.add_handler(CallbackQueryHandler(produk_tambah_batal,  pattern="^pd_tambah_batal$"))
    app.add_handler(CallbackQueryHandler(pd_back,              pattern="^pd_back$"))

    # Admin order callbacks
    app.add_handler(CallbackQueryHandler(admin_proses_order,   pattern="^proses_"))
    app.add_handler(CallbackQueryHandler(admin_konfirmasi,     pattern="^(confirm|reject)_"))
    app.add_handler(CallbackQueryHandler(back_orders,          pattern="^back_orders$"))
    app.add_handler(CallbackQueryHandler(admin_cancel_order,   pattern="^adm_cancel\\|"))
    app.add_handler(CallbackQueryHandler(admin_manual_confirm, pattern="^adm_konfirm\\|"))

    # Admin panel callbacks
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

    # Kick feature callbacks
    app.add_handler(CallbackQueryHandler(admpanel_kick,    pattern="^admpanel_kick$"))
    app.add_handler(CallbackQueryHandler(kick_cek_user,    pattern="^kick_cek_user$"))
    app.add_handler(CallbackQueryHandler(kick_add_group,   pattern="^kick_add_group$"))
    app.add_handler(CallbackQueryHandler(kick_del_group,   pattern="^kick_del_group$"))
    app.add_handler(CallbackQueryHandler(kick_del_confirm, pattern="^kick_del_confirm\\|"))
    app.add_handler(CallbackQueryHandler(kick_do_kick,     pattern="^kick_do_kick\\|"))

    # Pengaturan callbacks
    app.add_handler(CallbackQueryHandler(admpanel_setting,              pattern="^admpanel_setting$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_channel_set,  pattern="^admpanel_setting_channel_set$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_channel_off,  pattern="^admpanel_setting_channel_off$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_testich_set,  pattern="^admpanel_setting_testich_set$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_testich_off,  pattern="^admpanel_setting_testich_off$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_channel_test, pattern="^admpanel_setting_channel_test$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_maintenance,  pattern="^admpanel_setting_maintenance$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_link_testi,   pattern="^admpanel_setting_link_testi$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_link_admin,   pattern="^admpanel_setting_link_admin$"))

    # Admin management (super admin only)
    app.add_handler(CallbackQueryHandler(admpanel_admins,       pattern="^admpanel_admins$"))
    app.add_handler(CallbackQueryHandler(admpanel_admin_add,    pattern="^admpanel_admin_add$"))
    app.add_handler(CallbackQueryHandler(admpanel_admin_remove, pattern="^admpanel_admin_remove$"))
    app.add_handler(CallbackQueryHandler(admpanel_admin_del,    pattern="^admpanel_admin_del_"))

    # Blast callbacks
    app.add_handler(CallbackQueryHandler(blast_batal,   pattern="^blast_batal$"))
    app.add_handler(CallbackQueryHandler(blast_confirm, pattern="^blast_confirm$"))
    app.add_handler(CallbackQueryHandler(blast_stop,    pattern="^blast_stop$"))
    app.add_handler(CallbackQueryHandler(blast_retype,  pattern="^blast_retype$"))

    # Auto-approve join request
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Admin: terima file .json untuk import
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.ChatType.PRIVATE,
        handle_json_document
    ))

    # General Message Handler
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND & filters.ChatType.PRIVATE,
        message_handler
    ))

    logger.info("Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
