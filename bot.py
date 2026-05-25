import os
import asyncio
import re
import json
import html as html_module
import requests
import qrcode
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from io import BytesIO
from datetime import datetime, timedelta, timezone
from telegram import Update, BotCommand, BotCommandScopeChat, BotCommandScopeDefault, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ChatJoinRequestHandler, filters, ContextTypes
)

def esc(text):
    """Escape Markdown v1 special chars di teks user-provided."""
    for ch in ['_', '*', '`', '[']:
        text = str(text).replace(ch, f'\\{ch}')
    return text

# =================== TIMEZONE ===================
WIB = timezone(timedelta(hours=7))

def now_wib() -> datetime:
    """Waktu sekarang dalam WIB (UTC+7), naive — langsung bisa strftime."""
    return datetime.now(WIB).replace(tzinfo=None)

# =================== KONFIGURASI ===================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
PAKASIR_API_KEY = os.environ.get("PAKASIR_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
PAKASIR_SLUG = "atkikukkvd"
PAKASIR_BASE_URL = "https://app.pakasir.com"

DEFAULT_LINK = "https://t.me/Kikukkvd"

if not TOKEN:
    raise ValueError("BOT_TOKEN tidak di-set!")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID tidak di-set!")
if not PAKASIR_API_KEY:
    raise ValueError("PAKASIR_API_KEY tidak di-set!")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL tidak di-set!")

# =================== PAKASIR API (NON-BLOCKING) ===================

async def create_transaction_qris(order_id, amount, description):
    payload = {
        "project": PAKASIR_SLUG,
        "order_id": order_id,
        "amount": amount,
        "api_key": PAKASIR_API_KEY,
    }
    try:
        response = await asyncio.to_thread(
            requests.post,
            f'{PAKASIR_BASE_URL}/api/transactioncreate/qris',
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        result = response.json()
        if 'payment' in result:
            return result['payment']
        print(f"Pakasir error: {result}")
        return None
    except Exception as e:
        print(f"Error create transaction: {e}")
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
        response = await asyncio.to_thread(
            requests.post,
            f'{PAKASIR_BASE_URL}/api/transactioncancel',
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        return response.json()
    except Exception as e:
        print(f"Error cancel transaction: {e}")
        return None

async def get_transaction_detail(order_id, amount):
    try:
        response = await asyncio.to_thread(
            requests.get,
            f'{PAKASIR_BASE_URL}/api/transactiondetail',
            params={
                'project': PAKASIR_SLUG,
                'amount': amount,
                'order_id': order_id,
                'api_key': PAKASIR_API_KEY,
            },
            timeout=30
        )
        result = response.json()
        if 'transaction' in result:
            return result['transaction']
        return None
    except Exception as e:
        print(f"Error get detail: {e}")
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

# =================== DATABASE (THREAD-SAFE POOL) ===================

_pool: ThreadedConnectionPool = None

def init_pool():
    """Inisialisasi threaded connection pool agar aman diakses di berbagai thread."""
    global _pool
    _pool = ThreadedConnectionPool(2, 20, DATABASE_URL, cursor_factory=RealDictCursor)
    print("[DB] Threaded Connection pool diinisialisasi (min=2, max=20)")

def get_conn():
    """Ambil koneksi dari pool."""
    return _pool.getconn()

def release_conn(conn):
    """Kembalikan koneksi ke pool."""
    if _pool and conn:
        _pool.putconn(conn)

def init_db():
    conn = get_conn()
    try:
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS products (
                paket_id TEXT PRIMARY KEY,
                nama TEXT NOT NULL,
                emoji TEXT DEFAULT '📦',
                deskripsi TEXT DEFAULT '',
                harga INTEGER NOT NULL,
                link TEXT DEFAULT 'https://t.me/Kikukkvd'
            )
        """)

        c.execute("""
            ALTER TABLE products ADD COLUMN IF NOT EXISTS link TEXT DEFAULT 'https://t.me/Kikukkvd'
        """)
        c.execute("""
            ALTER TABLE products ADD COLUMN IF NOT EXISTS group_chat_id TEXT DEFAULT NULL
        """)

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
                waktu TEXT
            )
        """)

        c.execute("""
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS admin_msg_id BIGINT DEFAULT NULL
        """)
        c.execute("""
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS sent_link TEXT DEFAULT NULL
        """)
        c.execute("""
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS harga_dibayar INTEGER DEFAULT 0
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
                expires_at TEXT NOT NULL
            )
        """)

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

# =================== PRODUCT DB FUNCTIONS ===================

def get_all_products():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM products ORDER BY harga ASC")
        return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

def get_product(paket_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM products WHERE paket_id=%s", (paket_id,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        release_conn(conn)

def add_product(paket_id, nama, emoji, deskripsi, harga, link=None, group_chat_id=None):
    conn = get_conn()
    try:
        c = conn.cursor()
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

def update_product_field(paket_id, field, value):
    allowed = {"nama", "emoji", "deskripsi", "harga", "link", "group_chat_id"}
    if field not in allowed:
        return
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(f"UPDATE products SET {field}=%s WHERE paket_id=%s", (value, paket_id))
        conn.commit()
    finally:
        release_conn(conn)

def delete_product(paket_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM products WHERE paket_id=%s", (paket_id,))
        conn.commit()
    finally:
        release_conn(conn)

def make_paket_id(nama):
    pid = re.sub(r'[^a-z0-9]+', '_', nama.lower().strip()).strip('_')
    return pid or "produk"

# =================== ORDER DB FUNCTIONS ===================

def get_active_order(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM orders WHERE user_id=%s AND status IN ('waiting','pending') ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        release_conn(conn)

def get_all_pending():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE status='pending' ORDER BY id ASC")
        return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

def get_all_waiting():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE status='waiting' ORDER BY id ASC")
        return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

def get_buyer_history(user_id, limit=10):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT %s",
            (user_id, limit)
        )
        return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

def get_all_buyers():
    """Mengambil semua buyer yang terdaftar dengan query PostgreSQL yang aman."""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT user_id, user_name, MAX(id) as max_id 
            FROM orders 
            GROUP BY user_id, user_name 
            ORDER BY max_id DESC
        """)
        return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

def get_order_stats():
    conn = get_conn()
    try:
        c = conn.cursor()

        today = now_wib().strftime("%d/%m/%Y")
        this_month = now_wib().strftime("%m/%Y")

        c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status='completed'")
        total_orders = c.fetchone()['cnt']

        c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status='completed' AND waktu LIKE %s", (f"% — {today}",))
        today_orders = c.fetchone()['cnt']

        c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status='completed' AND waktu LIKE %s", (f"%/{this_month}",))
        month_orders = c.fetchone()['cnt']

        c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status IN ('waiting','pending')")
        active_count = c.fetchone()['cnt']

        c.execute("SELECT COUNT(*) as cnt FROM orders WHERE status='cancelled'")
        cancelled_count = c.fetchone()['cnt']

        c.execute("""
            SELECT paket_id, COUNT(*) as cnt FROM orders
            WHERE status='completed'
            GROUP BY paket_id ORDER BY cnt DESC LIMIT 1
        """)
        best_row = c.fetchone()
        best_product = None
        if best_row:
            p = get_product(best_row['paket_id'])
            best_product = f"{p['emoji']} {p['nama']} ({best_row['cnt']}x)" if p else best_row['paket_id']

        c.execute("SELECT COALESCE(SUM(harga_dibayar), 0) as total FROM orders WHERE status='completed'")
        total_revenue = c.fetchone()['total']

        c.execute(
            "SELECT COALESCE(SUM(harga_dibayar), 0) as total FROM orders WHERE status='completed' AND waktu LIKE %s",
            (f"% — {today}",)
        )
        today_revenue = c.fetchone()['total']

        c.execute(
            "SELECT COALESCE(SUM(harga_dibayar), 0) as total FROM orders WHERE status='completed' AND waktu LIKE %s",
            (f"%/{this_month}",)
        )
        month_revenue = c.fetchone()['total']

    finally:
        release_conn(conn)

    return {
        'total_orders': total_orders,
        'today_orders': today_orders,
        'month_orders': month_orders,
        'active_count': active_count,
        'cancelled_count': cancelled_count,
        'best_product': best_product,
        'total_revenue': total_revenue,
        'today_revenue': today_revenue,
        'month_revenue': month_revenue,
    }

def update_order_status(order_id, status):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE orders SET status=%s WHERE order_id=%s", (status, order_id))
        conn.commit()
    finally:
        release_conn(conn)

def get_order_by_id(order_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE order_id=%s", (order_id,))
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        release_conn(conn)

def set_admin_msg_id(order_id, msg_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE orders SET admin_msg_id=%s WHERE order_id=%s", (msg_id, order_id))
        conn.commit()
    finally:
        release_conn(conn)

def set_sent_link(order_id, link):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE orders SET sent_link=%s WHERE order_id=%s", (link, order_id))
        conn.commit()
    finally:
        release_conn(conn)

def save_order(user_id, user_name, paket_id, order_id, harga_dibayar=0):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """INSERT INTO orders (user_id, user_name, paket_id, order_id, status, waktu, harga_dibayar)
               VALUES (%s, %s, %s, %s, 'waiting', %s, %s)""",
            (user_id, user_name, paket_id, order_id,
             now_wib().strftime("%H:%M — %d/%m/%Y"), harga_dibayar)
        )
        conn.commit()
    finally:
        release_conn(conn)

# =================== BAN DB FUNCTIONS ===================

def is_banned(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM banned_users WHERE user_id=%s", (user_id,))
        return c.fetchone() is not None
    finally:
        release_conn(conn)

def ban_user(user_id, reason=""):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """INSERT INTO banned_users (user_id, reason, banned_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET reason=EXCLUDED.reason, banned_at=EXCLUDED.banned_at""",
            (user_id, reason, now_wib().strftime("%H:%M — %d/%m/%Y"))
        )
        conn.commit()
    finally:
        release_conn(conn)

def unban_user(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM banned_users WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        release_conn(conn)

def get_all_banned():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM banned_users ORDER BY banned_at DESC")
        return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

# =================== SETTINGS DB FUNCTIONS ===================

def get_setting(key, default=None):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = c.fetchone()
        return row['value'] if row else default
    finally:
        release_conn(conn)

def set_setting(key, value):
    conn = get_conn()
    try:
        c = conn.cursor()
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

def set_cooldown_db(user_id):
    expires_at = (now_wib() + timedelta(minutes=COOLDOWN_MENIT)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """INSERT INTO cooldowns (user_id, expires_at) VALUES (%s, %s)
               ON CONFLICT (user_id) DO UPDATE SET expires_at=EXCLUDED.expires_at""",
            (user_id, expires_at)
        )
        conn.commit()
    finally:
        release_conn(conn)

def get_cooldown_sisa_db(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT expires_at FROM cooldowns WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        if not row:
            return 0
        try:
            until = datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S')
            sisa = (until - now_wib()).total_seconds()
            return max(0, int(sisa / 60) + 1) if sisa > 0 else 0
        except Exception:
            return 0
    finally:
        release_conn(conn)

def clear_cooldown_db(user_id):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM cooldowns WHERE user_id=%s", (user_id,))
        conn.commit()
    finally:
        release_conn(conn)

# =================== HELPERS ===================

def format_harga(harga):
    return f"Rp {int(harga):,}".replace(",", ".")

def hitung_durasi(waktu_str):
    try:
        order_time = datetime.strptime(waktu_str, "%H:%M — %d/%m/%Y")
        delta = now_wib() - order_time
        total_minutes = int(delta.total_seconds() / 60)
        if total_minutes < 60:
            return f"{total_minutes} menit lalu"
        hours = total_minutes // 60
        minutes = total_minutes % 60
        return f"{hours} jam {minutes} menit lalu"
    except Exception:
        return waktu_str

def _sql_str(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"

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
        print(f"[LINK] Gagal bikin link grup {group_id}: {e}")
        return None

async def get_product_link(bot, paket, order_id):
    group_link = await generate_group_link(bot, paket, order_id)
    return group_link or (paket.get("link") or DEFAULT_LINK)

# =================== STATE ADMIN ===================
_admin_awaiting: dict = {}

# =================== MAINTENANCE ===================

def is_maintenance() -> bool:
    return get_setting('maintenance') == '1'

# =================== NOTIFIKASI ORDER (HAPUS & KIRIM) ===================

def _format_order_notif(judul: str, user_name: str, user_id: int,
                         paket: dict, order_id: str,
                         amount: int = None, extra: str = None) -> str:
    lines = [
        judul,
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 Pembeli  : {html_module.escape(str(user_name))} (<code>{user_id}</code>)",
        f"📦 Paket    : {paket['emoji']} {html_module.escape(paket['nama'])}",
    ]
    if amount is not None:
        lines.append(f"💰 Total    : {format_harga(amount)}")
    lines.append(f"📝 Order ID : <code>{order_id}</code>")
    lines.append(f"🕐 Waktu    : {now_wib().strftime('%H:%M, %d/%m/%Y')}")
    if extra:
        lines.append(f"\nℹ️ {extra}")
    return "\n".join(lines)

async def kirim_notif(bot, text: str):
    """Kirim notifikasi order baru dan kembalikan message_id-nya."""
    channel_id = get_setting('notif_channel_id')
    target = int(channel_id) if channel_id else ADMIN_ID
    try:
        msg = await bot.send_message(chat_id=target, text=text, parse_mode="HTML")
        return msg.message_id
    except Exception as e:
        print(f"[NOTIF] Gagal kirim ke {target}: {e}")
        if target != ADMIN_ID:
            try:
                msg = await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
                return msg.message_id
            except Exception:
                pass
    return None

async def hapus_notif_lama(bot, order_id):
    """Menghapus pesan notifikasi lama di channel atau chat admin."""
    order = get_order_by_id(order_id)
    if not order:
        return
    msg_id = order.get('admin_msg_id')
    if not msg_id:
        return
    
    channel_id = get_setting('notif_channel_id')
    target = int(channel_id) if channel_id else ADMIN_ID
    
    try:
        await bot.delete_message(chat_id=target, message_id=int(msg_id))
    except Exception:
        # Fallback jika target channel berubah, coba hapus di admin chat
        if target != ADMIN_ID:
            try:
                await bot.delete_message(chat_id=ADMIN_ID, message_id=int(msg_id))
            except Exception:
                pass

async def hapus_pesan_admin_order(bot, order_id):
    """Dibungkus menggunakan helper hapus_notif_lama demi kestabilan terpusat."""
    await hapus_notif_lama(bot, order_id)

# =================== MAIN MENU ===================

def build_main_menu_text():
    products = get_all_products()
    text = (
        "*🛍️ HYPER FAMILY STORE*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Selamat datang! Pilih paket yang tersedia:\n\n"
    )
    for p in products:
        text += (
            f"{p['emoji']} *{esc(p['nama']).upper()}*\n"
            f"├ {esc(p['deskripsi'])}\n"
            f"└ {format_harga(p['harga'])}\n\n"
        )
    text += (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💳 QRIS (All E-Wallet)  |  ⚡ 1-5 Menit  |  🕐 24 Jam"
    )
    return text

def build_main_menu_keyboard():
    return [
        [InlineKeyboardButton("🛒 Beli Sekarang", callback_data="buy")],
        [
            InlineKeyboardButton("⭐ Testimoni", url="https://t.me/+7zsdSrwYIG8wOTg1"),
            InlineKeyboardButton("💬 Admin", url="https://t.me/Kikukkvd")
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
            await context.bot.delete_message(chat_id=ADMIN_ID, message_id=msg_id)
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

    if group_link:
        link_text = (
            f"🔗 <b>Link Bergabung (Khusus Kamu)</b>\n"
            f"{link}\n\n"
            f"📋 <b>Cara gabung:</b>\n"
            f"1. Klik link di atas\n"
            f"2. Pencet <b>\"Minta Bergabung\"</b>\n"
            f"3. Bot langsung <b>approve otomatis</b> ✅\n\n"
            f"⚠️ <i>Jangan dishare ke orang lain!</i>"
        )
    else:
        link_text = (
            f"🔗 <b>Link Produk</b>\n"
            f"{link}\n\n"
            f"💾 <i>Simpan link ini. Produk dapat diakses kapan saja.</i>"
        )

    msg = await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"<b>✅ PEMBAYARAN BERHASIL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>Detail Pesanan</b>\n"
            f"├ Paket: {paket['emoji']} {html_module.escape(paket['nama'])}\n"
            f"├ Order ID: <code>{order_id}</code>\n"
            f"└ Total: {format_harga(amount)}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{link_text}\n\n"
            f"Terima kasih telah berbelanja! 🙏\n\n"
            f"Ada masalah dengan link? Pencet tombol di bawah:"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Link Error / Kirim Ulang", callback_data=f"resendlink|{order_id}"),
                InlineKeyboardButton("💬 Chat Admin", url="https://t.me/Kikukkvd")
            ]
        ])
    )
    simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=2)
    return link

# =================== POST INIT ===================

async def post_init(application: Application):
    global _current_bot
    _current_bot = application.bot

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

    waiting_orders = get_all_waiting()
    if waiting_orders:
        print(f"[POST_INIT] Ditemukan {len(waiting_orders)} order aktif, membuat ulang payment tasks...")
        for order in waiting_orders:
            paket = get_product(order['paket_id'])
            if not paket:
                continue
            _start_payment_task(
                application.bot,
                order_id=order['order_id'],
                paket_id=order['paket_id'],
                user_id=order['user_id'],
                user_name=order.get('user_name', 'User'),
                amount=paket['harga'],
                timeout_seconds=1800
            )
            print(f"[POST_INIT] Task dimulai ulang untuk order {order['order_id']}")

    asyncio.create_task(_auto_backup_loop())
    print("[POST_INIT] Auto backup harian dijadwalkan via asyncio task")

    asyncio.create_task(_buyer_reminder_loop(_current_bot))
    print("[POST_INIT] Buyer reminder harian dijadwalkan (jam 10:00 WIB)")

# =================== USER HANDLERS ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id != ADMIN_ID and is_maintenance():
        await update.message.reply_text(
            "🔧 <b>BOT SEDANG MAINTENANCE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Bot sedang dalam perbaikan sementara.\n"
            "Silakan coba lagi nanti.\n\n"
            "Hubungi admin: @Kikukkvd",
            parse_mode="HTML"
        )
        return

    if is_banned(user_id):
        await update.message.reply_text(
            "🚫 *Akun kamu diblokir*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Kamu tidak bisa menggunakan bot ini.\n"
            "Hubungi admin jika ada pertanyaan.",
            parse_mode="Markdown"
        )
        return

    active = get_active_order(user_id)
    if active:
        paket = get_product(active["paket_id"])
        if not paket:
            paket = {"emoji": "📦", "nama": "Produk", "harga": 0, "link": DEFAULT_LINK}

        trans = await get_transaction_detail(active["order_id"], paket["harga"])

        if trans and trans.get("status") == "completed":
            update_order_status(active["order_id"], "completed")
            _stop_payment_task(user_id)

            paid_amount = trans.get("amount", paket["harga"])
            link = await kirim_link_ke_buyer(context, user_id, paket, active["order_id"], paid_amount)

            set_sent_link(active['order_id'], link)
            await hapus_pesan_admin_order(context.bot, active['order_id'])
            
            # Notifikasi Lunas (Hapus notifikasi masuk, kirim notifikasi berhasil baru)
            await hapus_notif_lama(context.bot, active['order_id'])
            msg_id = await kirim_notif(
                context.bot,
                f"✅ <b>ORDER BERHASIL</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 Pembeli: {html_module.escape(active.get('user_name', 'User'))} (<code>{user_id}</code>)\n"
                f"📦 Paket: {paket['emoji']} {html_module.escape(paket['nama'])}\n"
                f"📝 Order ID: <code>{active['order_id']}</code>\n"
                f"💰 Total: {format_harga(paid_amount)}\n"
                f"🕐 Lunas: {now_wib().strftime('%H:%M, %d %b %Y')}\n\n"
                f"✅ Link terkirim ke buyer"
            )
            if msg_id:
                set_admin_msg_id(active['order_id'], msg_id)
            return

        total = (trans.get("amount", paket["harga"]) + trans.get("fee", 0)) if trans else paket["harga"]
        text = (
            f"*⏳ ORDER AKTIF*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Kamu masih punya pesanan yang belum dibayar:\n\n"
            f"📦 Paket: {paket['emoji']} {esc(paket['nama'])}\n"
            f"💰 Total: {format_harga(total)}\n"
            f"📝 Order ID: `{active['order_id']}`\n\n"
            f"_Silakan selesaikan pembayaran atau batalkan pesanan dulu._"
        )
        keyboard = [[InlineKeyboardButton("✕ Batalkan Pesanan", callback_data="back_start")]]
        msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        return

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_main_menu_text(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(build_main_menu_keyboard())
    )
    simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=1)

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != ADMIN_ID and is_maintenance():
        await query.answer("🔧 Bot sedang maintenance. Coba lagi nanti.", show_alert=True)
        return

    if is_banned(user_id):
        await query.answer("🚫 Akun kamu diblokir. Hubungi admin.", show_alert=True)
        return

    await query.answer()

    products = get_all_products()
    text = (
        "*📦 PILIH PAKET*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    for p in products:
        text += (
            f"{p['emoji']} *{esc(p['nama']).upper()}*\n"
            f"├ {esc(p['deskripsi'])}\n"
            f"├ Harga: {format_harga(p['harga'])}\n"
            f"└ Status: Tersedia ✅\n\n"
        )
    text += "━━━━━━━━━━━━━━━━━━━━━━"

    keyboard = [
        [InlineKeyboardButton(f"{p['emoji']} {p['nama']} — {format_harga(p['harga'])}", callback_data=f"pilih_{p['paket_id']}")]
        for p in products
    ]
    keyboard.append([InlineKeyboardButton("← Kembali", callback_data="back_start")])

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def pilih_paket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user_name = query.from_user.full_name

    if is_banned(user_id):
        await query.answer("🚫 Akun kamu diblokir. Hubungi admin.", show_alert=True)
        return

    paket_id = query.data.replace("pilih_", "")
    paket = get_product(paket_id)
    if not paket:
        await query.answer("❌ Produk tidak ditemukan.", show_alert=True)
        return

    active = get_active_order(user_id)
    if active:
        paket_active = get_product(active["paket_id"]) or {"emoji": "📦", "nama": "Produk", "harga": 0}
        trans = await get_transaction_detail(active["order_id"], paket_active["harga"])
        if trans and trans.get("status") == "completed":
            await query.answer("✅ Pembayaran sudah diterima!", show_alert=True)
            return
        await query.answer("⏳ Kamu sudah punya invoice aktif!", show_alert=True)
        total = (trans.get("amount", paket_active["harga"]) + trans.get("fee", 0)) if trans else paket_active["harga"]
        caption = (
            f"*⏳ ORDER AKTIF*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 Paket: {paket_active['emoji']} {esc(paket_active['nama'])}\n"
            f"💰 Total: {format_harga(total)}\n"
            f"📝 Order ID: `{active['order_id']}`\n\n"
            f"⚠️ Selesaikan pembayaran atau batalkan dulu."
        )
        keyboard = [[InlineKeyboardButton("✕ Batalkan", callback_data="back_start")]]
        await query.edit_message_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    sisa = get_cooldown_sisa_db(user_id)
    if sisa > 0:
        await query.answer(
            f"⏳ Kamu baru saja membatalkan order. Coba lagi dalam {sisa} menit.",
            show_alert=True
        )
        return

    await query.answer()

    loading_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ Membuat invoice...",
    )

    order_id = f"HFB-{user_id}-{now_wib().strftime('%Y%m%d%H%M%S')}"
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

    save_order(user_id, user_name, paket_id, order_id, harga_dibayar=total_payment)

    context.user_data['paket_id'] = paket_id
    context.user_data['order_id'] = order_id
    context.user_data['amount'] = paket["harga"]

    expired_at = trans_data.get('expired_at', '')

    try:
        expired_dt = datetime.fromisoformat(expired_at.replace('Z', '+00:00'))
        expire = expired_dt.strftime("%H:%M")
    except Exception:
        expire = (now_wib() + timedelta(minutes=30)).strftime("%H:%M")

    qr_buffer = generate_qr_image(qris_string)

    caption = (
        f"*{paket['emoji']} {esc(paket['nama']).upper()}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 *Detail Pembayaran*\n"
        f"├ Harga: {format_harga(amount)}\n"
        f"├ Fee: {format_harga(fee)}\n"
        f"└ *Total: {format_harga(total_payment)}*\n\n"
        f"📝 Order ID: `{order_id}`\n"
        f"⏰ Berlaku hingga: {expire} WIB\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 *Scan QRIS di atas untuk membayar*\n\n"
        f"✅ Nominal sudah termasuk fee\n"
        f"✅ Pembayaran otomatis terverifikasi\n"
        f"✅ Link produk dikirim otomatis setelah bayar\n\n"
        f"⏳ Menunggu pembayaran..."
    )
    keyboard = [[InlineKeyboardButton("✕ Batalkan Pesanan", callback_data="back_start")]]

    try:
        await query.message.delete()
    except Exception:
        pass

    msg = await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=qr_buffer,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=1)

    # Notifikasi order baru dan simpan message ID ke Database
    msg_id = await kirim_notif(
        context.bot,
        _format_order_notif(
            "🔔 <b>ORDER BARU MASUK</b>",
            user_name, user_id, paket, order_id,
            amount=total_payment,
            extra=f"⏰ Berlaku: {expire} WIB · ⏳ Menunggu pembayaran"
        )
    )
    if msg_id:
        set_admin_msg_id(order_id, msg_id)

    try:
        expired_dt = datetime.fromisoformat(expired_at.replace('Z', '+00:00'))
        now_tz = datetime.now(expired_dt.tzinfo)
        timeout_secs = max(60, int((expired_dt - now_tz).total_seconds()))
    except Exception:
        timeout_secs = 1800

    _start_payment_task(
        context.bot, order_id, paket_id, user_id, user_name,
        paket["harga"], timeout_seconds=timeout_secs
    )

async def back_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    active = get_active_order(user_id)
    if active:
        paket = get_product(active["paket_id"])
        amount = paket["harga"] if paket else 0
        if amount:
            await cancel_transaction(active["order_id"], amount)
        update_order_status(active["order_id"], "cancelled")
        _stop_payment_task(user_id)

        set_cooldown_db(user_id)

        cancelled_order_id = active["order_id"]
        
        # Hapus pesan notifikasi lama di channel/admin
        await hapus_notif_lama(context.bot, cancelled_order_id)

        # Kirim notifikasi dibatalkan oleh buyer
        paket_notif = get_product(active["paket_id"]) or {"emoji": "📦", "nama": active["paket_id"]}
        msg_id = await kirim_notif(
            context.bot,
            _format_order_notif(
                "❌ <b>DIBATALKAN BUYER</b>",
                query.from_user.full_name, user_id, paket_notif, cancelled_order_id
            )
        )
        if msg_id:
            set_admin_msg_id(cancelled_order_id, msg_id)

    context.user_data.clear()

    try:
        await query.message.delete()
    except Exception:
        pass

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_main_menu_text(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(build_main_menu_keyboard())
    )
    simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=1)

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

async def _payment_poll_loop(bot, order_id: str, paket_id: str, user_id: int,
                              user_name: str, amount: int, timeout_seconds: int):
    elapsed = 0
    try:
        while elapsed < timeout_seconds:
            await asyncio.sleep(30)
            elapsed += 30

            conn = get_conn()
            try:
                c = conn.cursor()
                c.execute("SELECT status FROM orders WHERE order_id=%s", (order_id,))
                row = c.fetchone()
            finally:
                release_conn(conn)

            if not row or row['status'] != 'waiting':
                return

            trans = await get_transaction_detail(order_id, amount)
            if not trans:
                continue

            if trans.get('status') == 'completed':
                await _handle_payment_success(bot, order_id, paket_id, user_id, user_name, amount, trans)
                return

        # TIMEOUT: cek sekali lagi sebelum expire
        conn = get_conn()
        try:
            c = conn.cursor()
            c.execute("SELECT status FROM orders WHERE order_id=%s", (order_id,))
            row = c.fetchone()
        finally:
            release_conn(conn)

        if not row or row['status'] != 'waiting':
            return

        trans = await get_transaction_detail(order_id, amount)
        if trans and trans.get('status') == 'completed':
            await _handle_payment_success(bot, order_id, paket_id, user_id, user_name, amount, trans)
            return

        if amount:
            await cancel_transaction(order_id, amount)
        update_order_status(order_id, 'expired')

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "*⏰ SESI BERAKHIR*\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Pesanan telah dibatalkan otomatis.\n\n"
                    "Alasan: Pembayaran tidak diterima dalam waktu yang ditentukan.\n\n"
                    "Ketik /start untuk membuat pesanan baru."
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

        # Hapus pesan notifikasi lama di channel/admin
        await hapus_notif_lama(bot, order_id)

        paket_exp = get_product(paket_id) or {"emoji": "📦", "nama": paket_id}
        msg_id = await kirim_notif(
            bot,
            _format_order_notif(
                "⏰ <b>ORDER EXPIRED</b>",
                user_name, user_id, paket_exp, order_id,
                extra="Buyer tidak bayar sampai waktu habis"
            )
        )
        if msg_id:
            set_admin_msg_id(order_id, msg_id)

    except asyncio.CancelledError:
        pass
    finally:
        _payment_tasks.pop(user_id, None)

async def _handle_payment_success(bot, order_id: str, paket_id: str, user_id: int,
                                   user_name: str, amount: int, trans: dict):
    paket = get_product(paket_id) or {"emoji": "📦", "nama": "Produk", "harga": amount, "link": DEFAULT_LINK}
    update_order_status(order_id, 'completed')

    paid_amount = trans.get('amount', amount)

    group_link = await generate_group_link(bot, paket, order_id)
    link = group_link or (paket.get("link") or DEFAULT_LINK)

    if group_link:
        link_section = (
            f"🔗 <b>Link Bergabung (Khusus Kamu)</b>\n"
            f"{link}\n\n"
            f"📋 <b>Cara gabung:</b>\n"
            f"1. Klik link di atas\n"
            f"2. Pencet <b>\"Minta Bergabung\"</b>\n"
            f"3. Bot langsung <b>approve otomatis</b> ✅\n\n"
            f"⚠️ <i>Jangan dishare ke orang lain!</i>"
        )
    else:
        link_section = (
            f"🔗 <b>Link Produk</b>\n"
            f"{link}\n\n"
            f"💾 <i>Simpan link ini. Produk dapat diakses kapan saja.</i>"
        )

    kirim_berhasil = False
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"<b>✅ PEMBAYARAN BERHASIL</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 <b>Detail Pesanan</b>\n"
                f"├ Paket: {paket['emoji']} {html_module.escape(paket['nama'])}\n"
                f"├ Order ID: <code>{order_id}</code>\n"
                f"└ Total: {format_harga(paid_amount)}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{link_section}\n\n"
                f"Terima kasih telah berbelanja! 🙏"
            ),
            parse_mode="HTML"
        )
        kirim_berhasil = True
    except Exception as e:
        print(f"[PAYMENT] Gagal kirim link ke buyer {user_id}: {e}")

    set_sent_link(order_id, link)

    # Hapus pesan notifikasi lama di channel/admin sebelum kirim yang baru
    await hapus_notif_lama(bot, order_id)

    # Notifikasi pembayaran berhasil
    extra_paid = "✅ Link produk sudah terkirim ke buyer" if kirim_berhasil else "⚠️ GAGAL kirim link ke buyer — cek manual!"
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
        set_admin_msg_id(order_id, msg_id)

    if not kirim_berhasil:
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🚨 <b>ALERT: GAGAL KIRIM LINK</b>\n"
                    f"Order <code>{order_id}</code> sudah lunas tapi link tidak bisa dikirim ke buyer!\n"
                    f"Buyer ID: <code>{user_id}</code> — kirim manual segera!"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

# =================== AUTO-APPROVE JOIN REQUEST ===================

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    join_req = update.chat_join_request
    if not join_req:
        return

    user_id = join_req.from_user.id
    chat_id = str(join_req.chat.id)

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT o.id FROM orders o
            JOIN products p ON o.paket_id = p.paket_id
            WHERE o.user_id = %s AND o.status = 'completed'
            AND p.group_chat_id = %s
            ORDER BY o.id DESC LIMIT 1
        """, (user_id, chat_id))
        row = c.fetchone()
    finally:
        release_conn(conn)

    if row:
        try:
            await join_req.approve()
            print(f"[JOIN] ✅ Approved {user_id} ke chat {chat_id}")
        except Exception as e:
            print(f"[JOIN] Gagal approve {user_id} ke {chat_id}: {e}")
    else:
        try:
            await join_req.decline()
            print(f"[JOIN] ❌ Declined {user_id} ke chat {chat_id} (tidak ada order)")
        except Exception as e:
            print(f"[JOIN] Gagal decline {user_id} ke {chat_id}: {e}")

# =================== ADMIN: KELOLA PRODUK ===================

async def cmd_produk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    await _send_produk_menu(context, chat_id=ADMIN_ID, message=update.message)

async def _send_produk_menu(context, chat_id, message=None, query=None):
    products = get_all_products()

    text = (
        "*📦 MANAJEMEN PRODUK*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    if products:
        for p in products:
            text += f"{p['emoji']} *{esc(p['nama'])}* — {format_harga(p['harga'])}\n"
    else:
        text += "_Belum ada produk._\n"

    text += "\nPilih produk untuk diedit, atau tambah produk baru:"

    keyboard = [
        [InlineKeyboardButton(f"{p['emoji']} {p['nama']}", callback_data=f"pd_detail_{p['paket_id']}")]
        for p in products
    ]
    keyboard.append([InlineKeyboardButton("➕ Tambah Produk Baru", callback_data="pd_tambah")])
    keyboard.append([InlineKeyboardButton("← Kembali ke Panel", callback_data="admpanel_back")])

    markup = InlineKeyboardMarkup(keyboard)

    if query:
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=markup)
    elif message:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=markup)

async def produk_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    paket_id = query.data.replace("pd_detail_", "")
    p = get_product(paket_id)
    if not p:
        await query.edit_message_text("⚠️ Produk tidak ditemukan.")
        return

    grp_info = (
        f"🏢 Group ID: `{esc(p.get('group_chat_id') or 'Tidak di-set')}`\n"
        if p.get('group_chat_id') else
        "🏢 Group ID: _Tidak di-set (pakai link biasa)_\n"
    )

    text = (
        f"*{esc(p['emoji'])} {esc(p['nama'])}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Harga: {format_harga(p['harga'])}\n"
        f"📝 Deskripsi: {esc(p['deskripsi'])}\n"
        f"🔗 Link: `{esc(p['link'])}`\n"
        f"{grp_info}\n"
        f"Pilih field yang mau diubah:"
    )

    keyboard = [
        [
            InlineKeyboardButton("✏️ Nama",       callback_data=f"pd_edit_{paket_id}_nama"),
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
        [InlineKeyboardButton("🗑️ Hapus Produk", callback_data=f"pd_hapus_{paket_id}")],
        [InlineKeyboardButton("← Kembali",       callback_data="pd_back")],
    ]

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def produk_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    raw = query.data.replace("pd_edit_", "")
    FIELDS = ["nama", "emoji", "harga", "deskripsi", "link", "group_chat_id"]
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

    p = get_product(paket_id)
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
    }

    context.user_data['editing_product'] = {'paket_id': paket_id, 'field': field}

    await query.edit_message_text(
        f"*✏️ Edit {field.upper()} — {esc(p['emoji'])} {esc(p['nama'])}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Nilai saat ini: `{esc(str(p[field]))}`\n\n"
        f"_Kirim {label_map[field]} baru sekarang:_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Batal", callback_data=f"pd_detail_{paket_id}")]
        ])
    )

async def produk_hapus_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    paket_id = query.data.replace("pd_hapus_", "")
    p = get_product(paket_id)
    if not p:
        await query.answer("Produk tidak ditemukan.", show_alert=True)
        return

    await query.edit_message_text(
        f"*⚠️ Hapus Produk?*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Kamu yakin mau hapus *{esc(p['emoji'])} {esc(p['nama'])}*?\n"
        f"Tindakan ini tidak bisa dibatalkan.",
        parse_mode="Markdown",
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
    p = get_product(paket_id)
    if not p:
        await query.answer("Produk tidak ditemukan.", show_alert=True)
        return

    delete_product(paket_id)

    await query.edit_message_text(
        f"✅ Produk *{esc(p['emoji'])} {esc(p['nama'])}* berhasil dihapus.",
        parse_mode="Markdown"
    )
    await _send_produk_menu(context, chat_id=ADMIN_ID)

async def produk_tambah_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data['adding_product'] = {'step': 'nama'}

    await query.edit_message_text(
        "*➕ TAMBAH PRODUK BARU*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Langkah 1/4\n\n"
        "_Kirim *nama* produk baru:_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Batal", callback_data="pd_tambah_batal")]
        ])
    )

async def produk_tambah_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('adding_product', None)
    await _send_produk_menu(context, chat_id=ADMIN_ID, query=query)

async def pd_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('adding_product', None)
    context.user_data.pop('editing_product', None)
    await _send_produk_menu(context, chat_id=ADMIN_ID, query=query)

# =================== ADMIN: ORDER AKTIF ===================

async def cmd_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return

    orders = get_all_waiting()
    if not orders:
        await update.message.reply_text(
            "*✅ TIDAK ADA ORDER AKTIF*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Tidak ada buyer yang sedang menunggu membayar saat ini.",
            parse_mode="Markdown"
        )
        return

    text = f"*⏳ ORDER MENUNGGU BAYAR ({len(orders)})*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    keyboard = []
    for o in orders:
        paket = get_product(o["paket_id"]) or {"emoji": "📦", "nama": o["paket_id"], "harga": 0}
        durasi = hitung_durasi(o["waktu"])
        text += (
            f"• {paket['emoji']} *{esc(o['user_name'])}*\n"
            f"  Paket: {esc(paket['nama'])} — {format_harga(paket['harga'])}\n"
            f"  Dibuat: {durasi}\n"
            f"  ID: `{o['order_id']}`\n\n"
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

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    if len(parts) != 3:
        await query.answer("Format tidak valid.", show_alert=True)
        return

    target_user_id = int(parts[1])
    order_id = parts[2]

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE order_id=%s AND status='waiting'", (order_id,))
        order = c.fetchone()
    finally:
        release_conn(conn)

    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah selesai/dibatalkan.")
        return

    order = dict(order)
    paket = get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0}

    if paket['harga']:
        await cancel_transaction(order_id, paket['harga'])

    update_order_status(order_id, 'cancelled')
    _stop_payment_task(target_user_id)

    # Hapus pesan notifikasi lama di channel/admin
    await hapus_notif_lama(context.bot, order_id)

    # Kirim notifikasi dibatalkan oleh admin baru
    msg_id = await kirim_notif(
        context.bot,
        _format_order_notif(
            "❌ <b>DIBATALKAN ADMIN</b>",
            order.get('user_name', '-'), target_user_id, paket, order_id
        )
    )
    if msg_id:
        set_admin_msg_id(order_id, msg_id)

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                "*❌ PESANAN DIBATALKAN*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Pesanan kamu telah dibatalkan oleh admin.\n\n"
                "Hubungi admin jika ada pertanyaan.\n"
                "Ketik /start untuk membuat pesanan baru."
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await query.edit_message_text(
        f"✅ *Order berhasil dibatalkan*\n\n"
        f"👤 Buyer: {esc(order.get('user_name', '-'))}\n"
        f"📦 Paket: {paket['emoji']} {esc(paket['nama'])}\n"
        f"📝 Order ID: `{order_id}`",
        parse_mode="Markdown"
    )

async def admin_manual_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    parts = query.data.split("|")
    if len(parts) != 3:
        await query.answer("Format tidak valid.", show_alert=True)
        return

    target_user_id = int(parts[1])
    order_id = parts[2]

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE order_id=%s AND status='waiting'", (order_id,))
        order = c.fetchone()
    finally:
        release_conn(conn)

    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah selesai/dibatalkan.")
        return

    order = dict(order)
    paket = get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0, "link": DEFAULT_LINK}

    _stop_payment_task(target_user_id)
    update_order_status(order_id, 'completed')

    group_link = await generate_group_link(context.bot, paket, order_id)
    link = group_link or (paket.get("link") or DEFAULT_LINK)
    harga = paket.get("harga", 0)

    if group_link:
        link_section = (
            f"🔗 <b>Link Bergabung (Khusus Kamu)</b>\n"
            f"{link}\n\n"
            f"📋 <b>Cara gabung:</b>\n"
            f"1. Klik link di atas\n"
            f"2. Pencet <b>\"Minta Bergabung\"</b>\n"
            f"3. Bot langsung <b>approve otomatis</b> ✅\n\n"
            f"⚠️ <i>Jangan dishare ke orang lain!</i>"
        )
    else:
        link_section = (
            f"🔗 <b>Link Produk</b>\n"
            f"{link}\n\n"
            f"💾 <i>Simpan link ini. Produk dapat diakses kapan saja.</i>"
        )

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                f"<b>✅ PEMBAYARAN DIKONFIRMASI</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 <b>Detail Pesanan</b>\n"
                f"├ Paket: {paket['emoji']} {html_module.escape(paket['nama'])}\n"
                f"├ Order ID: <code>{order_id}</code>\n"
                f"└ Total: {format_harga(harga)}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{link_section}\n\n"
                f"Terima kasih telah berbelanja! 🙏"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"[KONFIRMASI MANUAL] Gagal kirim link ke buyer {target_user_id}: {e}")

    set_sent_link(order_id, link)

    # Hapus pesan notifikasi lama di channel/admin
    await hapus_notif_lama(context.bot, order_id)

    # Kirim notifikasi baru lunas (dikonfirmasi manual)
    msg_id = await kirim_notif(
        context.bot,
        _format_order_notif(
            "✅ <b>DIKONFIRMASI MANUAL</b>",
            order.get('user_name', '-'), target_user_id, paket, order_id,
            amount=harga
        )
    )
    if msg_id:
        set_admin_msg_id(order_id, msg_id)

    await query.edit_message_text(
        f"✅ *Pembayaran dikonfirmasi manual*\n\n"
        f"👤 Buyer: {esc(order.get('user_name', '-'))}\n"
        f"📦 Paket: {paket['emoji']} {esc(paket['nama'])}\n"
        f"📝 Order ID: `{order_id}`\n"
        f"🔗 Link sudah terkirim ke buyer.",
        parse_mode="Markdown"
    )

# =================== ADMIN: STATISTIK ===================

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return

    s = get_order_stats()
    text = (
        f"*📊 STATISTIK PENJUALAN*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 *Hari Ini*\n"
        f"├ Order Selesai: {s['today_orders']}\n"
        f"└ Omzet: {format_harga(s['today_revenue'])}\n\n"
        f"📆 *Bulan Ini*\n"
        f"├ Order Selesai: {s['month_orders']}\n"
        f"└ Omzet: {format_harga(s['month_revenue'])}\n\n"
        f"🏆 *All Time*\n"
        f"├ Total Order Selesai: {s['total_orders']}\n"
        f"├ Total Dibatalkan: {s['cancelled_count']}\n"
        f"├ Order Aktif Sekarang: {s['active_count']}\n"
        f"└ Total Omzet: {format_harga(s['total_revenue'])}\n\n"
    )
    if s['best_product']:
        text += f"🥇 *Produk Terlaris:* {esc(s['best_product'])}\n\n"
    text += f"_Update: {now_wib().strftime('%H:%M, %d/%m/%Y')}_"

    await update.message.reply_text(text, parse_mode="Markdown")

# =================== USER: RIWAYAT ORDER ===================

async def cmd_riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    orders = get_buyer_history(user_id)

    if not orders:
        await update.message.reply_text(
            "*📋 RIWAYAT ORDER*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Kamu belum pernah melakukan pembelian.\n\n"
            "Ketik /start untuk mulai belanja.",
            parse_mode="Markdown"
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

    text = f"*📋 RIWAYAT ORDER (10 terakhir)*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for o in orders:
        paket = get_product(o['paket_id']) or {"emoji": "📦", "nama": o['paket_id'], "harga": 0}
        status = STATUS_LABEL.get(o['status'], o['status'])
        harga = o.get('harga_dibayar') or paket['harga']
        text += (
            f"{paket['emoji']} *{esc(paket['nama'])}*\n"
            f"├ Status: {status}\n"
            f"├ Harga: {format_harga(harga)}\n"
            f"└ {o['waktu']}\n\n"
        )

    await update.message.reply_text(text, parse_mode="Markdown")

# =================== BACKUP & EXPORT ===================

def _generate_json_export():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM products ORDER BY harga ASC")
        products = [dict(r) for r in c.fetchall()]
        c.execute("SELECT * FROM orders ORDER BY id ASC")
        orders = [dict(r) for r in c.fetchall()]
        c.execute("SELECT * FROM banned_users ORDER BY banned_at DESC")
        banned = [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

    payload = {
        "meta": {
            "app": "Hyper Family Store",
            "exported_at": now_wib().strftime("%H:%M, %d/%m/%Y"),
            "version": "2.0",
            "counts": {
                "products": len(products),
                "orders": len(orders),
                "banned_users": len(banned),
            }
        },
        "products": products,
        "orders": orders,
        "banned_users": banned,
    }

    return json.dumps(payload, ensure_ascii=False, indent=2, default=str), len(products), len(orders), len(banned)

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏳ Membuat backup database...")
    await _kirim_backup(context.bot)

async def _kirim_backup(bot):
    backup_name = f"backup_{now_wib().strftime('%Y%m%d_%H%M%S')}.txt"
    try:
        conn = get_conn()
        try:
            c = conn.cursor()
            c.execute("SELECT * FROM orders ORDER BY id DESC")
            orders = c.fetchall()
            c.execute("SELECT * FROM products ORDER BY harga ASC")
            products = c.fetchall()
            c.execute("SELECT * FROM banned_users ORDER BY banned_at DESC")
            banned = c.fetchall()
        finally:
            release_conn(conn)

        lines = []
        lines.append(f"=== BACKUP HYPER FAMILY STORE ===\n")
        lines.append(f"Tanggal: {now_wib().strftime('%H:%M, %d/%m/%Y')}\n\n")

        lines.append(f"--- PRODUK ({len(products)}) ---\n")
        for p in products:
            grp = p.get('group_chat_id') or '-'
            lines.append(
                f"[{p['paket_id']}] {p['emoji']} {p['nama']} — Rp {p['harga']:,} "
                f"| Link: {p['link']} | Group: {grp}\n"
            )

        lines.append(f"\n--- ORDERS ({len(orders)}) ---\n")
        for o in orders:
            lines.append(
                f"[{o['id']}] {o['order_id']}\n"
                f"  User: {o['user_name']} ({o['user_id']})\n"
                f"  Paket: {o['paket_id']} | Status: {o['status']} | "
                f"Harga: Rp {o.get('harga_dibayar') or 0:,} | Waktu: {o['waktu']}\n\n"
            )

        lines.append(f"\n--- BANNED USERS ({len(banned)}) ---\n")
        for b in banned:
            lines.append(
                f"User ID: {b['user_id']} | Alasan: {b['reason'] or '-'} | Sejak: {b['banned_at']}\n"
            )

        content = "".join(lines).encode("utf-8")
        buf = BytesIO(content)
        buf.name = backup_name

        await bot.send_document(
            chat_id=ADMIN_ID,
            document=buf,
            filename=backup_name,
            caption=(
                f"📦 *Backup Database*\n"
                f"🕐 {now_wib().strftime('%H:%M, %d/%m/%Y')}\n"
                f"📋 {len(orders)} orders | {len(products)} produk | {len(banned)} banned"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error backup: {e}")
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Gagal backup database: {e}")
        except Exception:
            pass

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏳ Menyiapkan export JSON...")
    try:
        json_content, n_products, n_orders, n_banned = _generate_json_export()
        filename = f"export_{now_wib().strftime('%Y%m%d_%H%M%S')}.json"
        buf = BytesIO(json_content.encode("utf-8"))
        buf.name = filename
        await update.message.reply_document(
            document=buf,
            filename=filename,
            caption=(
                f"✅ *Export Berhasil*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 Products: {n_products}\n"
                f"📋 Orders: {n_orders}\n"
                f"🚫 Banned users: {n_banned}\n"
                f"🕐 {now_wib().strftime('%H:%M, %d/%m/%Y')}\n\n"
                f"Kirim file ini ke bot dengan /import\\_json untuk restore."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal export: {e}")

async def cmd_import_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    context.user_data['awaiting_json_import'] = True
    await update.message.reply_text(
        "*📥 IMPORT DATA JSON*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Kirim file `.json` yang didapat dari `/export`.\n\n"
        "⚠️ Data yang sudah ada *tidak akan dihapus* — hanya ditambah/diperbarui.\n"
        "_Kirim file sekarang..._",
        parse_mode="Markdown"
    )

async def handle_json_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if not context.user_data.get('awaiting_json_import'):
        return

    doc = update.message.document
    if not doc or not doc.file_name.endswith('.json'):
        await update.message.reply_text("❌ File harus berformat `.json`. Coba lagi dengan /import_json.")
        return

    context.user_data.pop('awaiting_json_import', None)
    status_msg = await update.message.reply_text("⏳ Membaca file JSON...")

    try:
        file = await context.bot.get_file(doc.file_id)
        raw = await file.download_as_bytearray()
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        await status_msg.edit_text(f"❌ Gagal membaca file JSON: {e}")
        return

    if "products" not in data and "orders" not in data:
        await status_msg.edit_text("❌ Format JSON tidak valid. Pastikan file berasal dari /export.")
        return

    products  = data.get("products",   [])
    orders    = data.get("orders",     [])
    banned    = data.get("banned_users", [])

    ok_p = ok_o = ok_b = 0
    fail_p = fail_o = fail_b = 0

    conn = get_conn()
    try:
        c = conn.cursor()

        for p in products:
            try:
                c.execute(
                    """INSERT INTO products (paket_id, nama, emoji, deskripsi, harga, link, group_chat_id)
                       VALUES (%(paket_id)s, %(nama)s, %(emoji)s, %(deskripsi)s, %(harga)s, %(link)s, %(group_chat_id)s)
                       ON CONFLICT (paket_id) DO UPDATE SET
                           nama=EXCLUDED.nama, emoji=EXCLUDED.emoji,
                           deskripsi=EXCLUDED.deskripsi, harga=EXCLUDED.harga,
                           link=EXCLUDED.link, group_chat_id=EXCLUDED.group_chat_id""",
                    {
                        "paket_id":     p.get("paket_id"),
                        "nama":         p.get("nama"),
                        "emoji":        p.get("emoji", "📦"),
                        "deskripsi":    p.get("deskripsi", ""),
                        "harga":        int(p.get("harga", 0)),
                        "link":         p.get("link", DEFAULT_LINK),
                        "group_chat_id": p.get("group_chat_id"),
                    }
                )
                ok_p += 1
            except Exception as e:
                print(f"[IMPORT] produk gagal: {e}")
                fail_p += 1

        for o in orders:
            try:
                c.execute(
                    """INSERT INTO orders
                       (user_id, user_name, paket_id, order_id, status, waktu, harga_dibayar, sent_link)
                       VALUES (%(user_id)s, %(user_name)s, %(paket_id)s, %(order_id)s,
                               %(status)s, %(waktu)s, %(harga_dibayar)s, %(sent_link)s)
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
                    }
                )
                ok_o += 1
            except Exception as e:
                print(f"[IMPORT] order gagal: {e}")
                fail_o += 1

        for b in banned:
            try:
                c.execute(
                    """INSERT INTO banned_users (user_id, reason, banned_at)
                       VALUES (%(user_id)s, %(reason)s, %(banned_at)s)
                       ON CONFLICT (user_id) DO NOTHING""",
                    {
                        "user_id":   b.get("user_id"),
                        "reason":    b.get("reason", ""),
                        "banned_at": b.get("banned_at", now_wib().strftime("%H:%M — %d/%m/%Y")),
                    }
                )
                ok_b += 1
            except Exception as e:
                print(f"[IMPORT] banned user gagal: {e}")
                fail_b += 1

        conn.commit()
    finally:
        release_conn(conn)

    await status_msg.edit_text(
        f"✅ *Import JSON Selesai*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 Products: {ok_p} berhasil, {fail_p} gagal\n"
        f"📋 Orders: {ok_o} berhasil, {fail_o} gagal\n"
        f"🚫 Banned: {ok_b} berhasil, {fail_b} gagal\n\n"
        f"_Semua data sudah ter-restore._",
        parse_mode="Markdown"
    )

# =================== KIRIM ULANG LINK ===================

async def resend_group_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    parts = query.data.split("|")
    order_id = parts[1] if len(parts) > 1 else None
    if not order_id:
        await query.edit_message_text("⚠️ Order ID tidak valid.")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM orders WHERE order_id=%s AND user_id=%s AND status='completed'",
            (order_id, user_id)
        )
        order = c.fetchone()
    finally:
        release_conn(conn)

    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau belum lunas.")
        return

    order = dict(order)
    paket = get_product(order['paket_id'])
    if not paket:
        await query.edit_message_text("⚠️ Produk tidak ditemukan.")
        return

    new_link = await generate_group_link(context.bot, paket, order_id)

    if new_link:
        await query.edit_message_text(
            f"✅ *Link Baru Berhasil Dibuat!*\n\n"
            f"🔗 {new_link}\n\n"
            f"📋 *Cara gabung:*\n"
            f"1. Klik link di atas\n"
            f"2. Pencet *\"Minta Bergabung\"*\n"
            f"3. Bot langsung approve otomatis ✅\n\n"
            f"_Segera join ya\\!_",
            parse_mode="Markdown"
        )
    else:
        fallback_link = paket.get("link") or DEFAULT_LINK
        await query.edit_message_text(
            f"✅ *Link Produk*\n\n"
            f"🔗 {fallback_link}\n\n"
            f"_Produk ini pakai link biasa._",
            parse_mode="Markdown"
        )

# =================== BUYER REMINDER ===================
REMINDER_HARI = 3

def get_buyers_for_reminder(hari: int):
    target_date = (now_wib() - timedelta(days=hari)).strftime("%d/%m/%Y")
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT DISTINCT user_id, user_name FROM orders WHERE status='completed' AND waktu LIKE %s",
            (f"% — {target_date}",)
        )
        return [dict(r) for r in c.fetchall()]
    finally:
        release_conn(conn)

async def _send_buyer_reminders(bot):
    buyers = get_buyers_for_reminder(REMINDER_HARI)
    if not buyers:
        return
    print(f"[REMINDER] Mengirim reminder ke {len(buyers)} buyer...")
    for buyer in buyers:
        try:
            await bot.send_message(
                chat_id=buyer['user_id'],
                text=(
                    f"👋 Halo *{esc(buyer['user_name'])}*\!\n\n"
                    f"Sudah *{REMINDER_HARI} hari* sejak kamu belanja di *Hyper Family Store* 🛍️\n\n"
                    f"Puas dengan produknya? Mau belanja lagi?\n"
                    f"Kami punya paket menarik yang siap dikirim langsung\!\n\n"
                    f"Ketik /start untuk lihat katalog kami 😊"
                ),
                parse_mode="Markdown"
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[REMINDER] Gagal kirim ke {buyer['user_id']}: {e}")

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
            print(f"[REMINDER] Error loop: {e}")
            await asyncio.sleep(3600)

async def _auto_backup_loop():
    while True:
        try:
            now = now_wib()
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_secs = (next_midnight - now).total_seconds()
            await asyncio.sleep(wait_secs)
            print("[AUTO_BACKUP] Menjalankan backup harian...")
            await _kirim_backup(_current_bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[AUTO_BACKUP] Error: {e}")
            try:
                if _current_bot:
                    await _current_bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f"⚠️ *AUTO BACKUP GAGAL*\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                            f"Error: `{esc(str(e))}`\n"
                            f"🕐 {now_wib().strftime('%H:%M, %d/%m/%Y')}\n\n"
                            f"_Jalankan /backup secara manual._"
                        ),
                        parse_mode="Markdown"
                    )
            except Exception:
                pass
            await asyncio.sleep(3600)

# =================== ADMIN: BROADCAST ===================

async def cmd_blast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return

    buyers = get_all_buyers()
    jumlah = len(buyers)
    if jumlah == 0:
        await update.message.reply_text("❌ Belum ada buyer yang terdaftar.")
        return

    _admin_awaiting[ADMIN_ID] = 'blasting'
    await update.message.reply_text(
        f"*📢 BROADCAST PESAN*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Total penerima: *{jumlah} buyer*\n\n"
        f"Kirim pesan yang mau di-blast sekarang.\n"
        f"_Mendukung teks biasa, bold, italic (format Markdown)._\n\n"
        f"⚠️ Pesan akan langsung dikirim ke semua buyer.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Batal", callback_data="blast_batal")]
        ])
    )

async def blast_batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _admin_awaiting.pop(ADMIN_ID, None)
    await query.edit_message_text("✅ Broadcast dibatalkan.")

# =================== ADMIN: MESSAGE HANDLER ===================

async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.from_user.id != ADMIN_ID:
        return

    text = update.message.text.strip() if update.message.text else ""

    # --- State: awaiting cari order ---
    if context.user_data.get('awaiting_cari'):
        context.user_data.pop('awaiting_cari', None)
        order_id = text.strip()
        try:
            order = get_order_by_id(order_id)
        except Exception as e:
            await update.message.reply_text(f"❌ Gagal mengambil data order: {e}")
            return
        if not order:
            await update.message.reply_text(f"❌ Order tidak ditemukan.\n\nID yang dicari: {order_id}")
            return
        paket = get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0}
        STATUS_LABEL = {
            'completed': '✅ Selesai / Lunas', 'waiting': '⏳ Menunggu Bayar',
            'pending': '🔄 Diproses Manual', 'cancelled': '❌ Dibatalkan',
            'expired': '⏰ Kedaluwarsa', 'rejected': '🚫 Ditolak',
        }
        status = STATUS_LABEL.get(order['status'], order['status'])
        sent_link = order.get('sent_link') or '-'
        await update.message.reply_text(
            f"🔍 *DETAIL ORDER*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Order ID: `{order['order_id']}`\n"
            f"Buyer: {esc(order.get('user_name', '-'))} (`{order['user_id']}`)\n"
            f"Paket: {paket['emoji']} {esc(paket['nama'])}\n"
            f"Harga Dibayar: {format_harga(order.get('harga_dibayar') or paket['harga'])}\n"
            f"Status: {status}\n"
            f"Dibuat: {order.get('waktu', '-')}\n"
            f"Link terkirim: {sent_link}",
            parse_mode="Markdown"
        )
        return

    # --- State: awaiting ban user ---
    if context.user_data.get('awaiting_ban'):
        context.user_data.pop('awaiting_ban', None)
        parts = text.split(None, 1)
        try:
            target_id = int(parts[0])
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Format salah. Contoh: `123456789 spam`", parse_mode="Markdown")
            return
        if target_id == ADMIN_ID:
            await update.message.reply_text("❌ Tidak bisa ban diri sendiri.")
            return
        reason = parts[1] if len(parts) > 1 else "Tidak ada alasan"
        ban_user(target_id, reason)
        await update.message.reply_text(
            f"🚫 *User Berhasil Dibanned*\n\n"
            f"👤 User ID: `{target_id}`\n"
            f"📝 Alasan: {esc(reason)}",
            parse_mode="Markdown"
        )
        return

    # --- State: awaiting unban user ---
    if context.user_data.get('awaiting_unban'):
        context.user_data.pop('awaiting_unban', None)
        try:
            target_id = int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ User ID harus berupa angka.", parse_mode="Markdown")
            return
        if not is_banned(target_id):
            await update.message.reply_text(f"⚠️ User `{target_id}` tidak ada dalam daftar ban.", parse_mode="Markdown")
            return
        unban_user(target_id)
        await update.message.reply_text(
            f"✅ *User Berhasil Di-unban*\n\n👤 User ID: `{target_id}`",
            parse_mode="Markdown"
        )
        return

    # --- State: awaiting channel ID ---
    if context.user_data.get('awaiting_channel_id'):
        context.user_data.pop('awaiting_channel_id', None)
        val = text.strip()
        if val.lower() == 'hapus':
            set_setting('notif_channel_id', None)
            await update.message.reply_text(
                "✅ Channel notifikasi berhasil *dinonaktifkan*.",
                parse_mode="Markdown"
            )
        else:
            if not val.lstrip('-').isdigit():
                await update.message.reply_text(
                    "❌ Format channel ID tidak valid.\n"
                    "Contoh: `-1001234567890`\n"
                    "Ketik `hapus` untuk menonaktifkan.",
                    parse_mode="Markdown"
                )
                return
            set_setting('notif_channel_id', val)
            try:
                await context.bot.send_message(
                    chat_id=int(val),
                    text=(
                        f"✅ <b>Channel notifikasi aktif!</b>\n\n"
                        f"Bot Hyper Family Store akan mengirim update status order ke channel ini.\n"
                        f"🕐 {now_wib().strftime('%H:%M, %d/%m/%Y')}"
                    ),
                    parse_mode="HTML"
                )
                await update.message.reply_text(
                    f"✅ *Channel ID berhasil disimpan!*\n\n"
                    f"ID: `{val}`\n\n"
                    f"Pesan test sudah dikirim ke channel.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                await update.message.reply_text(
                    f"⚠️ *Channel ID disimpan, tapi gagal kirim pesan test.*\n\n"
                    f"Error: `{esc(str(e))}`\n\n"
                    f"Pastikan bot sudah dijadikan *admin* di channel tersebut.",
                    parse_mode="Markdown"
                )
        return

    # --- State: broadcast blast ---
    if _admin_awaiting.get(ADMIN_ID) == 'blasting':
        _admin_awaiting.pop(ADMIN_ID, None)
        buyers = get_all_buyers()
        jumlah = len(buyers)

        status_msg = await update.message.reply_text(f"📢 Mengirim ke {jumlah} buyer...")

        blast_text = (
            "📢 PESAN DARI ADMIN HYPER FAMILY STORE\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            + text
        )

        sent = 0
        failed = 0
        for b in buyers:
            try:
                await context.bot.send_message(chat_id=b['user_id'], text=blast_text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)

        try:
            await status_msg.delete()
        except Exception:
            pass

        await update.message.reply_text(
            f"✅ *BROADCAST SELESAI*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ Terkirim: {sent}\n"
            f"❌ Gagal: {failed}\n"
            f"📊 Total: {jumlah}",
            parse_mode="Markdown"
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
                "*➕ TAMBAH PRODUK BARU*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Langkah 2/4\n\n"
                f"Nama: *{esc(text)}*\n\n"
                "_Kirim *emoji* untuk produk ini (contoh: 🔥):_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("← Batal", callback_data="pd_tambah_batal")]
                ])
            )
            return

        if step == 'emoji':
            adding['emoji'] = text
            adding['step'] = 'deskripsi'
            await update.message.reply_text(
                "*➕ TAMBAH PRODUK BARU*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Langkah 3/4\n\n"
                f"Nama: *{esc(adding['nama'])}*\n"
                f"Emoji: {text}\n\n"
                "_Kirim *deskripsi* produk (contoh: 500+ Video Premium):_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("← Batal", callback_data="pd_tambah_batal")]
                ])
            )
            return

        if step == 'deskripsi':
            adding['deskripsi'] = text
            adding['step'] = 'harga'
            await update.message.reply_text(
                "*➕ TAMBAH PRODUK BARU*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Langkah 4/4\n\n"
                f"Nama: *{esc(adding['nama'])}*\n"
                f"Emoji: {adding['emoji']}\n"
                f"Deskripsi: {esc(text)}\n\n"
                "_Kirim *harga* (angka saja, contoh: 15000):_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("← Batal", callback_data="pd_tambah_batal")]
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

            existing = get_product(paket_id)
            if existing:
                paket_id = f"{paket_id}_{int(now_wib().timestamp())}"

            add_product(paket_id, nama, emoji, deskripsi, harga)
            context.user_data.pop('adding_product', None)

            await update.message.reply_text(
                f"✅ Produk berhasil ditambahkan!\n\n"
                f"{emoji} {nama}\n"
                f"📝 {deskripsi}\n"
                f"💰 {format_harga(harga)}\n\n"
                f"Jangan lupa set link-nya lewat /produk."
            )
            await _send_produk_menu(context, chat_id=ADMIN_ID)
            return

    # --- State: edit field produk ---
    editing = context.user_data.get('editing_product')
    if editing:
        paket_id = editing['paket_id']
        field = editing['field']

        if field == 'harga' and not text.isdigit():
            await update.message.reply_text("❌ Harga harus berupa angka. Coba lagi:")
            return

        if field == 'group_chat_id':
            value = text.strip() if text.strip() and text.strip().lower() != 'hapus' else None
        else:
            value = int(text) if field == 'harga' else text

        update_product_field(paket_id, field, value)
        context.user_data.pop('editing_product', None)

        await update.message.reply_text(
            f"✅ {field.capitalize()} berhasil diupdate!\nNilai baru: {value if value else '(kosong)'}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← Kembali ke Panel", callback_data="admpanel_back")
            ]])
        )
        p = get_product(paket_id)
        if p:
            grp_info = (
                f"🏢 Group ID: `{esc(p.get('group_chat_id') or 'Tidak di-set')}`\n"
                if p.get('group_chat_id') else
                "🏢 Group ID: _Tidak di-set (pakai link biasa)_\n"
            )
            detail_text = (
                f"*{esc(p['emoji'])} {esc(p['nama'])}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💰 Harga: {format_harga(p['harga'])}\n"
                f"📝 Deskripsi: {esc(p['deskripsi'])}\n"
                f"🔗 Link: `{esc(p['link'])}`\n"
                f"{grp_info}\n"
                f"Pilih field yang mau diubah:"
            )
            keyboard = [
                [
                    InlineKeyboardButton("✏️ Nama",       callback_data=f"pd_edit_{paket_id}_nama"),
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
                [InlineKeyboardButton("🗑️ Hapus Produk",  callback_data=f"pd_hapus_{paket_id}")],
                [InlineKeyboardButton("← Kembali",        callback_data="pd_back")],
            ]
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=detail_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return

# =================== ADMIN: LINK ===================

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    products = get_all_products()
    text = "*🔗 LINK PRODUK SAAT INI*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for p in products:
        grp = p.get('group_chat_id')
        if grp:
            text += f"{p['emoji']} *{esc(p['nama'])}*\n├ 🏢 Group: `{grp}`\n└ 🔗 Fallback: `{p['link']}`\n\n"
        else:
            text += f"{p['emoji']} *{esc(p['nama'])}*\n└ `{p['link']}`\n\n"
    text += "_Ketik /produk untuk mengubah link atau Group ID._"
    await update.message.reply_text(text, parse_mode="Markdown")

# =================== ADMIN: PENDING ORDERS ===================

async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return

    orders = get_all_pending()
    if not orders:
        await update.message.reply_text(
            "*✅ TIDAK ADA ORDER PENDING*\n━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
        return

    text = f"*📋 ORDER PENDING ({len(orders)})*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    keyboard = []
    for o in orders:
        paket = get_product(o["paket_id"]) or {"emoji": "📦", "nama": o["paket_id"]}
        durasi = hitung_durasi(o["waktu"])
        text += f"• {paket['emoji']} {esc(o['user_name'])} — {esc(paket['nama'])} — {durasi}\n"
        keyboard.append([InlineKeyboardButton(f"👤 Proses: {o['user_name']}", callback_data=f"proses_{o['user_id']}")])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_proses_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.replace("proses_", ""))

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE user_id=%s AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,))
        order = c.fetchone()
    finally:
        release_conn(conn)

    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah diproses.")
        return

    order = dict(order)
    paket = get_product(order["paket_id"]) or {"emoji": "📦", "nama": order["paket_id"], "harga": 0, "deskripsi": "-"}
    trans = await get_transaction_detail(order["order_id"], paket["harga"]) if order["order_id"] else None
    durasi = hitung_durasi(order["waktu"])

    caption = (
        f"*{paket['emoji']} {esc(paket['nama']).upper()}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Pembeli: {esc(order['user_name'])} (`{order['user_id']}`)\n"
        f"📦 Konten: {esc(paket['deskripsi'])}\n"
        f"💰 Total: {format_harga(paket['harga'])}\n"
        f"🕐 Dibuat: {durasi}\n"
    )
    if trans:
        caption += f"\n📝 Order ID: `{order['order_id']}`\n"
        caption += f"💳 Status: {'✅ Lunas' if trans['status'] == 'completed' else '⏳ Belum Bayar'}\n"
        if trans['status'] == 'completed':
            caption += f"💵 Dibayar: {format_harga(trans.get('amount', paket['harga']))}\n"

    keyboard = [
        [
            InlineKeyboardButton("✅ Konfirmasi", callback_data=f"confirm_{user_id}"),
            InlineKeyboardButton("❌ Tolak",      callback_data=f"reject_{user_id}"),
        ],
        [InlineKeyboardButton("← Kembali", callback_data="back_orders")]
    ]

    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=ADMIN_ID, text=caption, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def back_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass

    orders = get_all_pending()
    if not orders:
        await context.bot.send_message(chat_id=ADMIN_ID, text="✅ Tidak ada order pending saat ini.")
        return

    text = f"*📋 ORDER PENDING ({len(orders)})*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    keyboard = []
    for o in orders:
        paket = get_product(o["paket_id"]) or {"emoji": "📦", "nama": o["paket_id"]}
        durasi = hitung_durasi(o["waktu"])
        text += f"• {paket['emoji']} {esc(o['user_name'])} — {esc(paket['nama'])} — {durasi}\n"
        keyboard.append([InlineKeyboardButton(f"👤 Proses: {o['user_name']}", callback_data=f"proses_{o['user_id']}")])

    await context.bot.send_message(
        chat_id=ADMIN_ID, text=text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_konfirmasi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action = parts[0]
    user_id = int(parts[1])

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM orders WHERE user_id=%s AND status='pending' ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        order = c.fetchone()
    finally:
        release_conn(conn)

    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah diproses.")
        return

    order = dict(order)
    paket = get_product(order["paket_id"]) or {"emoji": "📦", "nama": order["paket_id"], "harga": 0, "link": DEFAULT_LINK}

    if action == "confirm":
        group_link = await generate_group_link(context.bot, paket, order["order_id"])
        link = group_link or (paket.get("link") or DEFAULT_LINK)

        if group_link:
            link_section = (
                f"🔗 <b>Link Bergabung (Khusus Kamu)</b>\n"
                f"{link}\n\n"
                f"📋 <b>Cara gabung:</b>\n"
                f"1. Klik link di atas\n"
                f"2. Pencet <b>\"Minta Bergabung\"</b>\n"
                f"3. Bot langsung <b>approve otomatis</b> ✅\n\n"
                f"⚠️ <i>Jangan dishare ke orang lain!</i>"
            )
        else:
            link_section = (
                f"🔗 <b>Link Produk</b>\n"
                f"{link}\n\n"
                f"💾 <i>Simpan link ini. Produk dapat diakses kapan saja.</i>"
            )

        msg = await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"<b>✅ PESANAN SELESAI</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 <b>Detail</b>\n"
                f"├ Paket: {paket['emoji']} {html_module.escape(paket['nama'])}\n"
                f"└ Konten: {html_module.escape(paket['deskripsi'])}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{link_section}\n\n"
                f"Terima kasih telah berbelanja! 🙏"
            ),
            parse_mode="HTML"
        )
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        update_order_status(order["order_id"], 'completed')
        set_sent_link(order["order_id"], link)

        # Hapus pesan notifikasi lama di channel/admin
        await hapus_notif_lama(context.bot, order["order_id"])

        # Notifikasi order selesai (konfirmasi manual)
        msg_id = await kirim_notif(
            context.bot,
            _format_order_notif(
                "✅ <b>ORDER SELESAI (KONFIRMASI MANUAL)</b>",
                order['user_name'], user_id, paket, order['order_id']
            )
        )
        if msg_id:
            set_admin_msg_id(order["order_id"], msg_id)

        await query.edit_message_text(
            f"*✅ DIKONFIRMASI*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 Pembeli: {esc(order['user_name'])}\n"
            f"📦 Paket: {paket['emoji']} {esc(paket['nama'])}\n\n"
            f"✅ Link produk otomatis terkirim ke buyer.",
            parse_mode="Markdown"
        )

    elif action == "reject":
        update_order_status(order["order_id"], 'rejected')
        
        # Hapus pesan notifikasi lama di channel/admin
        await hapus_notif_lama(context.bot, order["order_id"])

        # Kirim notifikasi ditolak baru
        msg_id = await kirim_notif(
            context.bot,
            _format_order_notif(
                "❌ <b>ORDER DITOLAK</b>",
                order['user_name'], user_id, paket, order['order_id']
            )
        )
        if msg_id:
            set_admin_msg_id(order["order_id"], msg_id)

        await query.edit_message_text(
            f"*❌ DITOLAK*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 Pembeli: {esc(order['user_name'])}\n"
            f"📦 Paket: {paket['emoji']} {esc(paket['nama'])}",
            parse_mode="Markdown"
        )
        msg = await context.bot.send_message(
            chat_id=user_id,
            text=(
                "*❌ PESANAN DITOLAK*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Maaf, pesanan Anda tidak dapat diproses.\n\n"
                "Kemungkinan penyebab:\n"
                "• Pembayaran tidak valid\n"
                "• Bukti transfer tidak sesuai\n"
                "• Produk tidak tersedia\n\n"
                "Ketik /start untuk mencoba lagi atau hubungi admin."
            ),
            parse_mode="Markdown"
        )
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        await hapus_admin_msg(context, user_id)

# =================== ADMIN: PANEL UTAMA ===================

def build_admin_panel_keyboard():
    return InlineKeyboardMarkup([
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
            InlineKeyboardButton("⚙️ Pengaturan",     callback_data="admpanel_setting"),
        ],
    ])

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "*⚙️ PANEL ADMIN*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Pilih menu yang ingin dibuka:",
        parse_mode="Markdown",
        reply_markup=build_admin_panel_keyboard()
    )

async def admpanel_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*⚙️ PANEL ADMIN*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Pilih menu yang ingin dibuka:",
        parse_mode="Markdown",
        reply_markup=build_admin_panel_keyboard()
    )

async def admpanel_produk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _send_produk_menu(context, chat_id=ADMIN_ID, query=query)

async def admpanel_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏳ Order Aktif",   callback_data="admpanel_orders_aktif"),
            InlineKeyboardButton("🔄 Order Pending", callback_data="admpanel_orders_pending"),
        ],
        [InlineKeyboardButton("🔍 Cari Order",    callback_data="admpanel_orders_cari")],
        [InlineKeyboardButton("← Kembali", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(
        "*📋 ORDERS*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Pilih jenis order:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def admpanel_orders_aktif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    orders = get_all_waiting()
    if not orders:
        await query.edit_message_text(
            "*✅ TIDAK ADA ORDER AKTIF*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Tidak ada buyer yang sedang menunggu membayar.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Kembali", callback_data="admpanel_orders")]])
        )
        return
    text = f"*⏳ ORDER MENUNGGU BAYAR ({len(orders)})*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    keyboard = []
    for o in orders:
        paket = get_product(o["paket_id"]) or {"emoji": "📦", "nama": o["paket_id"], "harga": 0}
        durasi = hitung_durasi(o["waktu"])
        text += (
            f"• {paket['emoji']} *{esc(o['user_name'])}*\n"
            f"  Paket: {esc(paket['nama'])} — {format_harga(paket['harga'])}\n"
            f"  Dibuat: {durasi}\n"
            f"  ID: `{o['order_id']}`\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(f"✅ {o['user_name']}", callback_data=f"adm_konfirm|{o['user_id']}|{o['order_id']}"),
            InlineKeyboardButton(f"❌ Cancel",           callback_data=f"adm_cancel|{o['user_id']}|{o['order_id']}"),
        ])
    keyboard.append([InlineKeyboardButton("← Kembali", callback_data="admpanel_orders")])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def admpanel_orders_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    orders = get_all_pending()
    if not orders:
        await query.edit_message_text(
            "*✅ TIDAK ADA ORDER PENDING*\n━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Kembali", callback_data="admpanel_orders")]])
        )
        return
    text = f"*📋 ORDER PENDING ({len(orders)})*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    keyboard = []
    for o in orders:
        paket = get_product(o["paket_id"]) or {"emoji": "📦", "nama": o["paket_id"]}
        durasi = hitung_durasi(o["waktu"])
        text += f"• {paket['emoji']} {esc(o['user_name'])} — {esc(paket['nama'])} — {durasi}\n"
        keyboard.append([InlineKeyboardButton(f"👤 Proses: {o['user_name']}", callback_data=f"proses_{o['user_id']}")])
    keyboard.append([InlineKeyboardButton("← Kembali", callback_data="admpanel_orders")])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def admpanel_orders_cari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*🔍 CARI ORDER*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Ketik Order ID yang ingin dicari:\n"
        "Contoh: HFB-123456789-20250524143000",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Batal", callback_data="admpanel_orders")]])
    )
    context.user_data['awaiting_cari'] = True

async def admpanel_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    s = get_order_stats()
    text = (
        f"*📊 STATISTIK PENJUALAN*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 *Hari Ini*\n"
        f"├ Order Selesai: {s['today_orders']}\n"
        f"└ Omzet: {format_harga(s['today_revenue'])}\n\n"
        f"📆 *Bulan Ini*\n"
        f"├ Order Selesai: {s['month_orders']}\n"
        f"└ Omzet: {format_harga(s['month_revenue'])}\n\n"
        f"🏆 *All Time*\n"
        f"├ Total Order Selesai: {s['total_orders']}\n"
        f"├ Total Dibatalkan: {s['cancelled_count']}\n"
        f"├ Order Aktif Sekarang: {s['active_count']}\n"
        f"└ Total Omzet: {format_harga(s['total_revenue'])}\n\n"
    )
    if s['best_product']:
        text += f"🥇 *Produk Terlaris:* {esc(s['best_product'])}\n\n"
    text += f"_Update: {now_wib().strftime('%H:%M, %d/%m/%Y')}_"
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Kembali", callback_data="admpanel_back")]])
    )

async def admpanel_blast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    buyers = get_all_buyers()
    jumlah = len(buyers)
    if jumlah == 0:
        await query.edit_message_text(
            "❌ Belum ada buyer yang terdaftar.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Kembali", callback_data="admpanel_back")]])
        )
        return
    await query.edit_message_text(
        f"📢 *BROADCAST PESAN*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Total penerima: *{jumlah} buyer*\n\n"
        f"Ketik & kirim pesan yang mau di-blast sekarang.\n"
        f"Pesan kamu akan dikirim dengan header toko secara otomatis.\n\n"
        f"⚠️ Langsung terkirim ke semua buyer setelah kamu send.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="blast_batal")]])
    )
    _admin_awaiting[ADMIN_ID] = 'blasting'

async def admpanel_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Backup",       callback_data="admpanel_data_backup"),
            InlineKeyboardButton("📤 Export SQL",   callback_data="admpanel_data_export"),
        ],
        [
            InlineKeyboardButton("📥 Import SQL",   callback_data="admpanel_data_import"),
            InlineKeyboardButton("🔗 Cek Link",     callback_data="admpanel_data_link"),
        ],
        [InlineKeyboardButton("← Kembali", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(
        "*💾 DATA & BACKUP*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Pilih aksi:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def admpanel_data_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Membuat backup database...")
    await _kirim_backup(context.bot)
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text="✅ Backup selesai. Ketik /admin untuk kembali ke panel.",
    )

async def admpanel_data_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Menyiapkan export JSON...")
    try:
        json_content, n_products, n_orders, n_banned = _generate_json_export()
        filename = f"export_{now_wib().strftime('%Y%m%d_%H%M%S')}.json"
        buf = BytesIO(json_content.encode("utf-8"))
        buf.name = filename
        await context.bot.send_document(
            chat_id=ADMIN_ID,
            document=buf,
            filename=filename,
            caption=(
                f"✅ Export Berhasil\n"
                f"📦 {n_products} produk | 📋 {n_orders} orders | 🚫 {n_banned} banned\n"
                f"Kirim file ini ke bot via /import_json untuk restore."
            )
        )
    except Exception as e:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Gagal export: {e}")

async def admpanel_data_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_json_import'] = True
    await query.edit_message_text(
        "*📥 IMPORT DATA JSON*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Kirim file `.json` yang didapat dari Export.\n\n"
        "⚠️ Data yang sudah ada *tidak akan dihapus*.\n"
        "_Kirim file sekarang..._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Batal", callback_data="admpanel_data")]])
    )

async def admpanel_data_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = get_all_products()
    text = "*🔗 LINK PRODUK SAAT INI*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for p in products:
        grp = p.get('group_chat_id')
        if grp:
            text += f"{p['emoji']} *{esc(p['nama'])}*\n├ 🏢 Group: `{grp}`\n└ 🔗 Fallback: `{p['link']}`\n\n"
        else:
            text += f"{p['emoji']} *{esc(p['nama'])}*\n└ `{p['link']}`\n\n"
    text += "_Ubah link lewat menu Produk._"
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Kembali", callback_data="admpanel_data")]])
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
        [InlineKeyboardButton("← Kembali", callback_data="admpanel_back")],
    ])
    await query.edit_message_text(
        "*🚫 KELOLA USER*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Pilih aksi:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def admpanel_user_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*🚫 BAN USER*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Ketik User ID dan alasan (opsional):\n"
        "Format: user\\_id alasan\n"
        "Contoh: `123456789 spam`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Batal", callback_data="admpanel_user")]])
    )
    context.user_data['awaiting_ban'] = True

async def admpanel_user_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*✅ UNBAN USER*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Ketik User ID yang mau di-unban:\n"
        "Contoh: `123456789`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Batal", callback_data="admpanel_user")]])
    )
    context.user_data['awaiting_unban'] = True

async def admpanel_user_daftar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    banned = get_all_banned()
    if not banned:
        await query.edit_message_text(
            "*🚫 DAFTAR BAN*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Belum ada user yang dibanned.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Kembali", callback_data="admpanel_user")]])
        )
        return
    text = f"*🚫 DAFTAR BAN ({len(banned)} user)*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for b in banned:
        text += (
            f"👤 ID: `{b['user_id']}`\n"
            f"📝 Alasan: {esc(b['reason'] or '-')}\n"
            f"🕐 Dibanned: {b['banned_at']}\n\n"
        )
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Kembali", callback_data="admpanel_user")]])
    )

# =================== ADMIN: PENGATURAN ===================

async def admpanel_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    channel_id = get_setting('notif_channel_id')
    if channel_id:
        ch_status = f"✅ Aktif — ID: <code>{html_module.escape(channel_id)}</code>"
    else:
        ch_status = "🔕 Nonaktif — belum diset"

    maint_on = is_maintenance()
    maint_status = "🔧 ON — bot maintenance" if maint_on else "✅ OFF — bot normal"
    maint_btn_label = "🟢 Matikan Maintenance" if maint_on else "🔧 Aktifkan Maintenance"

    text = (
        "<b>⚙️ PENGATURAN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>📢 Channel Notifikasi Order</b>\n"
        f"{ch_status}\n"
        "<i>Semua update status order dikirim ke channel. Jika tidak diset, notif ke admin chat.</i>\n\n"
        "<b>🔧 Maintenance Mode</b>\n"
        f"{maint_status}\n"
        "<i>Saat ON, buyer tidak bisa akses bot (kecuali admin).</i>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Set Channel ID", callback_data="admpanel_setting_channel_set"),
            InlineKeyboardButton("🔕 Nonaktifkan",    callback_data="admpanel_setting_channel_off"),
        ],
        [InlineKeyboardButton("📨 Test Notifikasi",   callback_data="admpanel_setting_channel_test")],
        [InlineKeyboardButton(maint_btn_label,         callback_data="admpanel_setting_maintenance")],
        [InlineKeyboardButton("← Kembali",            callback_data="admpanel_back")],
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)

async def admpanel_setting_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    was_on = is_maintenance()
    set_setting('maintenance', '0' if was_on else '1')

    if was_on:
        msg = "✅ <b>Maintenance mode dinonaktifkan.</b>\n\nBot kembali normal — buyer bisa akses."
    else:
        msg = "🔧 <b>Maintenance mode diaktifkan.</b>\n\nBuyer tidak bisa akses bot sampai maintenance dimatikan."

    await query.edit_message_text(
        msg,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("← Kembali ke Pengaturan", callback_data="admpanel_setting")
        ]])
    )

async def admpanel_setting_channel_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_channel_id'] = True
    await query.edit_message_text(
        "*✏️ SET CHANNEL ID*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Ketik Channel ID tujuan notifikasi:\n"
        "Contoh: `-1001234567890`\n\n"
        "Cara dapat channel ID:\n"
        "1\\. Forward pesan dari channel ke @userinfobot\n"
        "2\\. Atau tambahkan @getidsbot ke channel\n\n"
        "_Ketik `hapus` untuk menonaktifkan channel\\._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("← Batal", callback_data="admpanel_setting")
        ]])
    )

async def admpanel_setting_channel_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    set_setting('notif_channel_id', None)
    await query.edit_message_text(
        "🔕 *Channel notifikasi dinonaktifkan.*\n\n"
        "Notifikasi order tidak akan dikirim ke channel manapun.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("← Kembali ke Pengaturan", callback_data="admpanel_setting")
        ]])
    )

async def admpanel_setting_channel_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    channel_id = get_setting('notif_channel_id')
    if not channel_id:
        await query.answer("❌ Channel ID belum diset!", show_alert=True)
        return

    try:
        await context.bot.send_message(
            chat_id=int(channel_id),
            text=(
                f"📨 <b>Test Notifikasi</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"✅ Bot berhasil mengirim pesan ke channel ini.\n"
                f"Semua update status order akan muncul di sini.\n\n"
                f"🕐 {now_wib().strftime('%H:%M, %d/%m/%Y')}"
            ),
            parse_mode="HTML"
        )
        await query.answer("✅ Pesan test berhasil dikirim!", show_alert=True)
    except Exception as e:
        await query.answer(f"❌ Gagal: {str(e)[:100]}", show_alert=True)

# =================== ADMIN: BAN / UNBAN ===================

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Cara pakai: `/ban <user_id> [alasan]`\n\nContoh: `/ban 123456789 spam`",
            parse_mode="Markdown"
        )
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID harus berupa angka.")
        return
    if target_id == ADMIN_ID:
        await update.message.reply_text("❌ Tidak bisa ban diri sendiri.")
        return
    reason = " ".join(args[1:]) if len(args) > 1 else "Tidak ada alasan"
    ban_user(target_id, reason)
    await update.message.reply_text(
        f"🚫 *User Berhasil Dibanned*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 User ID: `{target_id}`\n"
        f"📝 Alasan: {esc(reason)}\n"
        f"🕐 Waktu: {now_wib().strftime('%H:%M, %d %b %Y')}\n\n"
        f"_Gunakan /unban {target_id} untuk mencabut ban._",
        parse_mode="Markdown"
    )

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Cara pakai: `/unban <user_id>`\n\nContoh: `/unban 123456789`",
            parse_mode="Markdown"
        )
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID harus berupa angka.")
        return
    if not is_banned(target_id):
        await update.message.reply_text(f"⚠️ User `{target_id}` tidak ada dalam daftar ban.", parse_mode="Markdown")
        return
    unban_user(target_id)
    await update.message.reply_text(
        f"✅ *User Berhasil Di-unban*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 User ID: `{target_id}`\n"
        f"🕐 Waktu: {now_wib().strftime('%H:%M, %d %b %Y')}",
        parse_mode="Markdown"
    )

async def cmd_daftar_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    banned = get_all_banned()
    if not banned:
        await update.message.reply_text(
            "*🚫 DAFTAR BAN*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Belum ada user yang dibanned.",
            parse_mode="Markdown"
        )
        return
    text = f"*🚫 DAFTAR BAN ({len(banned)} user)*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for b in banned:
        text += (
            f"👤 ID: `{b['user_id']}`\n"
            f"📝 Alasan: {esc(b['reason'] or '-')}\n"
            f"🕐 Dibanned: {b['banned_at']}\n"
            f"↩️ Unban: `/unban {b['user_id']}`\n\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown")

# =================== ADMIN: CARI ORDER ===================

async def cmd_cari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Cara pakai: `/cari <order_id>`\n\nContoh: `/cari HFB-123456789-20250524143000`",
            parse_mode="Markdown"
        )
        return

    order_id = args[0].strip()
    order = get_order_by_id(order_id)

    if not order:
        await update.message.reply_text(
            f"❌ Order `{esc(order_id)}` tidak ditemukan.",
            parse_mode="Markdown"
        )
        return

    paket = get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0}
    STATUS_LABEL = {
        'completed': '✅ Selesai / Lunas',
        'waiting':   '⏳ Menunggu Bayar',
        'pending':   '🔄 Diproses Manual',
        'cancelled': '❌ Dibatalkan',
        'expired':   '⏰ Kedaluwarsa',
        'rejected':  '🚫 Ditolak',
    }
    status = STATUS_LABEL.get(order['status'], order['status'])
    sent_link = order.get('sent_link')
    link_info = f"🔗 Link Terkirim: {sent_link}" if sent_link else "🔗 Link: _Belum terkirim_"

    text = (
        f"*🔍 DETAIL ORDER*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📝 Order ID: `{order['order_id']}`\n"
        f"👤 Buyer: {esc(order.get('user_name', '-'))} (`{order['user_id']}`)\n"
        f"📦 Paket: {paket['emoji']} {esc(paket['nama'])}\n"
        f"💰 Harga Dibayar: {format_harga(order.get('harga_dibayar') or paket['harga'])}\n"
        f"📊 Status: {status}\n"
        f"🕐 Dibuat: {order.get('waktu', '-')}\n"
        f"{link_info}"
    )

    await update.message.reply_text(text, parse_mode="Markdown")

# =================== MAIN ===================

def main():
    init_pool()
    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # User commands
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("riwayat", cmd_riwayat))

    # Admin commands
    app.add_handler(CommandHandler("admin",      cmd_admin))
    app.add_handler(CommandHandler("produk",     cmd_produk))
    app.add_handler(CommandHandler("pending",    admin_pending))
    app.add_handler(CommandHandler("aktif",      cmd_aktif))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("blast",      cmd_blast))
    app.add_handler(CommandHandler("backup",     cmd_backup))
    app.add_handler(CommandHandler("export",     cmd_export))
    app.add_handler(CommandHandler("import_json", cmd_import_json))
    app.add_handler(CommandHandler("link",       cmd_link))
    app.add_handler(CommandHandler("ban",        cmd_ban))
    app.add_handler(CommandHandler("unban",      cmd_unban))
    app.add_handler(CommandHandler("daftar_ban", cmd_daftar_ban))
    app.add_handler(CommandHandler("cari",       cmd_cari))

    # User callbacks
    app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy$"))
    app.add_handler(CallbackQueryHandler(pilih_paket,  pattern="^pilih_"))
    app.add_handler(CallbackQueryHandler(back_start,   pattern="^back_start$"))

    # Kirim ulang link
    app.add_handler(CallbackQueryHandler(resend_group_link, pattern="^resendlink\\|"))

    # Produk management callbacks
    app.add_handler(CallbackQueryHandler(produk_detail,        pattern="^pd_detail_"))
    app.add_handler(CallbackQueryHandler(produk_edit_field,    pattern="^pd_edit_"))
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

    # Pengaturan callbacks
    app.add_handler(CallbackQueryHandler(admpanel_setting,              pattern="^admpanel_setting$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_channel_set,  pattern="^admpanel_setting_channel_set$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_channel_off,  pattern="^admpanel_setting_channel_off$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_channel_test, pattern="^admpanel_setting_channel_test$"))
    app.add_handler(CallbackQueryHandler(admpanel_setting_maintenance,  pattern="^admpanel_setting_maintenance$"))

    # Blast callback
    app.add_handler(CallbackQueryHandler(blast_batal, pattern="^blast_batal$"))

    # Auto-approve join request
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Admin: terima file .json untuk import
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.Chat(ADMIN_ID),
        handle_json_document
    ))

    # Admin message handler
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Chat(ADMIN_ID),
        admin_message_handler
    ))

    print("Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
