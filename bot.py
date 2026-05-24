import os
import asyncio
import re
import requests
import qrcode
import psycopg2
from psycopg2.extras import RealDictCursor
from io import BytesIO
from datetime import datetime, timedelta, timezone
from telegram import Update, BotCommand, BotCommandScopeChat, BotCommandScopeDefault, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
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

# Validasi env wajib
if not TOKEN:
    raise ValueError("BOT_TOKEN tidak di-set! Tambahkan ke environment variable.")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID tidak di-set! Tambahkan ke environment variable.")
if not PAKASIR_API_KEY:
    raise ValueError("PAKASIR_API_KEY tidak di-set! Tambahkan ke environment variable.")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL tidak di-set! Tambahkan PostgreSQL di Railway lalu set variable ini.")

# =================== PAKASIR API ===================

def create_transaction_qris(order_id, amount, description):
    payload = {
        "project": PAKASIR_SLUG,
        "order_id": order_id,
        "amount": amount,
        "api_key": PAKASIR_API_KEY,
    }
    try:
        response = requests.post(
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

def cancel_transaction(order_id, amount):
    if not amount:
        return None
    payload = {
        "project": PAKASIR_SLUG,
        "order_id": order_id,
        "amount": amount,
        "api_key": PAKASIR_API_KEY,
    }
    try:
        response = requests.post(
            f'{PAKASIR_BASE_URL}/api/transactioncancel',
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )
        return response.json()
    except Exception as e:
        print(f"Error cancel transaction: {e}")
        return None

def get_transaction_detail(order_id, amount):
    try:
        response = requests.get(
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

# =================== DATABASE ===================

def get_conn():
    """Buat koneksi PostgreSQL. Semua cursor otomatis pakai RealDictCursor."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_conn()
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

    # Migration: tambah kolom link jika belum ada
    c.execute("""
        ALTER TABLE products ADD COLUMN IF NOT EXISTS link TEXT DEFAULT 'https://t.me/Kikukkvd'
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

    # Seed produk awal kalau belum ada
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
    conn.close()

# =================== PRODUCT DB FUNCTIONS ===================

def get_all_products():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM products ORDER BY harga ASC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_product(paket_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE paket_id=%s", (paket_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def add_product(paket_id, nama, emoji, deskripsi, harga, link=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """INSERT INTO products (paket_id, nama, emoji, deskripsi, harga, link)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (paket_id) DO UPDATE SET
               nama=EXCLUDED.nama, emoji=EXCLUDED.emoji,
               deskripsi=EXCLUDED.deskripsi, harga=EXCLUDED.harga, link=EXCLUDED.link""",
        (paket_id, nama, emoji, deskripsi, harga, link or DEFAULT_LINK)
    )
    conn.commit()
    conn.close()

def update_product_field(paket_id, field, value):
    allowed = {"nama", "emoji", "deskripsi", "harga", "link"}
    if field not in allowed:
        return
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE products SET {field}=%s WHERE paket_id=%s", (value, paket_id))
    conn.commit()
    conn.close()

def delete_product(paket_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE paket_id=%s", (paket_id,))
    conn.commit()
    conn.close()

def make_paket_id(nama):
    pid = re.sub(r'[^a-z0-9]+', '_', nama.lower().strip()).strip('_')
    return pid or "produk"

# =================== ORDER DB FUNCTIONS ===================

def get_active_order(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM orders WHERE user_id=%s AND status IN ('waiting','pending') ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_pending():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE status='pending' ORDER BY id ASC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_all_waiting():
    """Ambil semua order yang masih menunggu pembayaran (status='waiting')."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE status='waiting' ORDER BY id ASC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_buyer_history(user_id, limit=10):
    """Ambil riwayat order buyer (10 terakhir)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT %s",
        (user_id, limit)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_all_buyers():
    """Ambil semua user_id buyer yang pernah order (untuk broadcast)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT user_id, user_name FROM orders ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_order_stats():
    """Ambil statistik penjualan dari DB."""
    conn = get_conn()
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

    # Produk terlaris
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

    # Estimasi revenue: join dengan harga produk saat ini
    c.execute("""
        SELECT o.paket_id, COUNT(*) as cnt FROM orders o
        WHERE o.status='completed' GROUP BY o.paket_id
    """)
    revenue_rows = c.fetchall()
    total_revenue = 0
    today_revenue = 0
    month_revenue = 0

    for row in revenue_rows:
        p = get_product(row['paket_id'])
        if p:
            total_revenue += p['harga'] * row['cnt']

    c.execute("""
        SELECT paket_id, COUNT(*) as cnt FROM orders
        WHERE status='completed' AND waktu LIKE %s
        GROUP BY paket_id
    """, (f"% — {today}",))
    for row in c.fetchall():
        p = get_product(row['paket_id'])
        if p:
            today_revenue += p['harga'] * row['cnt']

    c.execute("""
        SELECT paket_id, COUNT(*) as cnt FROM orders
        WHERE status='completed' AND waktu LIKE %s
        GROUP BY paket_id
    """, (f"%/{this_month}",))
    for row in c.fetchall():
        p = get_product(row['paket_id'])
        if p:
            month_revenue += p['harga'] * row['cnt']

    conn.close()

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
    c = conn.cursor()
    c.execute("UPDATE orders SET status=%s WHERE order_id=%s", (status, order_id))
    conn.commit()
    conn.close()

def save_order(user_id, user_name, paket_id, order_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO orders (user_id, user_name, paket_id, order_id, status, waktu) VALUES (%s, %s, %s, %s, 'waiting', %s)",
        (user_id, user_name, paket_id, order_id, now_wib().strftime("%H:%M — %d/%m/%Y"))
    )
    conn.commit()
    conn.close()

# =================== HELPERS ===================

def format_harga(harga):
    return f"Rp {int(harga):,}".replace(",", ".")

def hitung_durasi(waktu_str):
    """Hitung berapa lama sejak order dibuat."""
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

# =================== COOLDOWN (anti-spam order) ===================
COOLDOWN_MENIT = 5

def set_cooldown(context, user_id):
    """Set cooldown setelah user membatalkan order."""
    context.bot_data.setdefault('cooldowns', {})
    context.bot_data['cooldowns'][user_id] = now_wib() + timedelta(minutes=COOLDOWN_MENIT)

def get_cooldown_sisa(context, user_id):
    """Kembalikan sisa menit cooldown, atau 0 jika tidak ada cooldown."""
    cooldowns = context.bot_data.get('cooldowns', {})
    until = cooldowns.get(user_id)
    if not until:
        return 0
    sisa = (until - now_wib()).total_seconds()
    return max(0, int(sisa / 60) + 1) if sisa > 0 else 0

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
    """Kirim link produk ke buyer setelah pembayaran berhasil."""
    link = paket.get("link") or DEFAULT_LINK
    msg = await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"*✅ PEMBAYARAN BERHASIL*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 *Detail Pesanan*\n"
            f"├ Paket: {paket['emoji']} {esc(paket['nama'])}\n"
            f"├ Order ID: `{order_id}`\n"
            f"└ Total: {format_harga(amount)}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 *Link Produk*\n"
            f"{link}\n\n"
            f"💾 _Simpan link ini. Produk dapat diakses kapan saja._\n\n"
            f"Terima kasih telah berbelanja\\! 🙏"
        ),
        parse_mode="Markdown"
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
            BotCommand("start",   "Buka toko"),
            BotCommand("produk",  "Kelola produk"),
            BotCommand("aktif",   "Order aktif (menunggu bayar)"),
            BotCommand("pending", "Order pending (sudah bayar)"),
            BotCommand("stats",   "Statistik penjualan"),
            BotCommand("blast",   "Broadcast pesan ke semua buyer"),
            BotCommand("backup",  "Backup ringkasan data"),
            BotCommand("export",  "Export semua data sebagai SQL (untuk migrasi)"),
            BotCommand("import_sql", "Import data dari file SQL"),
            BotCommand("link",    "Cek link produk saat ini"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID)
    )

    # ===== Re-start asyncio tasks untuk order 'waiting' setelah bot restart =====
    # Saat bot restart, semua task asyncio hilang dari memori.
    # Kita scan DB untuk order yang masih 'waiting' dan buat ulang task-nya.
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

    # ===== Auto backup harian jam 00:00 (asyncio task) =====
    asyncio.create_task(_auto_backup_loop())
    print("[POST_INIT] Auto backup harian dijadwalkan via asyncio task")

    # ===== Buyer reminder harian jam 10:00 WIB =====
    asyncio.create_task(_buyer_reminder_loop(_current_bot))
    print("[POST_INIT] Buyer reminder harian dijadwalkan (jam 10:00 WIB)")

# =================== USER HANDLERS ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    active = get_active_order(user_id)
    if active:
        paket = get_product(active["paket_id"])
        if not paket:
            paket = {"emoji": "📦", "nama": "Produk", "harga": 0, "link": DEFAULT_LINK}

        trans = get_transaction_detail(active["order_id"], paket["harga"])

        # ===== FIX UTAMA: Kalau sudah dibayar, langsung kirim link =====
        # Dulu: status diubah ke 'pending' tapi link TIDAK dikirim → buyer stuck
        # Sekarang: kirim link langsung, selesai
        if trans and trans.get("status") == "completed":
            update_order_status(active["order_id"], "completed")
            _stop_payment_task(user_id)

            paid_amount = trans.get("amount", paket["harga"])
            link = await kirim_link_ke_buyer(context, user_id, paket, active["order_id"], paid_amount)

            # Notif admin
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"*🔔 PEMBAYARAN SELESAI (via /start)*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"👤 Pembeli: {esc(active.get('user_name', 'User'))}\n"
                        f"📦 Paket: {paket['emoji']} {esc(paket['nama'])}\n"
                        f"📝 Order ID: `{active['order_id']}`\n"
                        f"💰 Total: {format_harga(paid_amount)}\n"
                        f"🕐 Waktu: {now_wib().strftime('%H:%M, %d %b %Y')}\n\n"
                        f"✅ Link otomatis terkirim ke buyer\n"
                        f"_Link: {link}_"
                    ),
                    parse_mode="Markdown"
                )
            except Exception:
                pass
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

    paket_id = query.data.replace("pilih_", "")
    paket = get_product(paket_id)
    if not paket:
        await query.answer("❌ Produk tidak ditemukan.", show_alert=True)
        return

    active = get_active_order(user_id)
    if active:
        paket_active = get_product(active["paket_id"]) or {"emoji": "📦", "nama": "Produk", "harga": 0}
        trans = get_transaction_detail(active["order_id"], paket_active["harga"])
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

    # ===== CEK COOLDOWN anti-spam =====
    sisa = get_cooldown_sisa(context, user_id)
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
    trans_data = create_transaction_qris(
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

    save_order(user_id, user_name, paket_id, order_id)

    context.user_data['paket_id'] = paket_id
    context.user_data['order_id'] = order_id
    context.user_data['amount'] = paket["harga"]

    amount = trans_data.get('amount', paket["harga"])
    fee = trans_data.get('fee', 0)
    total_payment = amount + fee
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

    # ===== NOTIFIKASI ADMIN: order baru masuk =====
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"*🔔 ORDER BARU MASUK*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 Pembeli: {esc(user_name)}\n"
                f"📦 Paket: {paket['emoji']} {esc(paket['nama'])}\n"
                f"💰 Total: {format_harga(total_payment)}\n"
                f"📝 Order ID: `{order_id}`\n"
                f"⏰ Berlaku: {expire} WIB\n"
                f"🕐 Dibuat: {now_wib().strftime('%H:%M, %d %b %Y')}\n\n"
                f"⏳ Menunggu pembayaran buyer..."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Gagal notif admin order baru: {e}")

    # Hitung sisa waktu sebelum expired untuk timeout task
    try:
        expired_dt = datetime.fromisoformat(expired_at.replace('Z', '+00:00'))
        now_tz = datetime.now(expired_dt.tzinfo)
        timeout_secs = max(60, int((expired_dt - now_tz).total_seconds()))
    except Exception:
        timeout_secs = 1800  # default 30 menit

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
            cancel_transaction(active["order_id"], amount)
        update_order_status(active["order_id"], "cancelled")
        _stop_payment_task(user_id)
        # ===== Set cooldown anti-spam setelah cancel =====
        set_cooldown(context, user_id)

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
# Menggantikan job_queue — tidak perlu install extra dependency apapun.
# Satu asyncio task per user, aktif selama menunggu pembayaran.

_payment_tasks: dict = {}  # user_id (int) -> asyncio.Task
_current_bot = None        # Di-set saat post_init, dipakai oleh _auto_backup_loop

def _stop_payment_task(user_id: int):
    """Batalkan task polling pembayaran untuk user ini."""
    task = _payment_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()

def _start_payment_task(bot, order_id: str, paket_id: str, user_id: int,
                         user_name: str, amount: int, timeout_seconds: int = 1800):
    """Mulai background asyncio task untuk cek pembayaran setiap 30 detik."""
    _stop_payment_task(user_id)  # cancel task lama kalau ada
    task = asyncio.create_task(
        _payment_poll_loop(bot, order_id, paket_id, user_id, user_name, amount, timeout_seconds)
    )
    _payment_tasks[user_id] = task

async def _payment_poll_loop(bot, order_id: str, paket_id: str, user_id: int,
                              user_name: str, amount: int, timeout_seconds: int):
    """
    Cek status pembayaran ke Pakasir tiap 30 detik.
    Auto-cancel order setelah timeout_seconds.
    """
    elapsed = 0
    try:
        while elapsed < timeout_seconds:
            await asyncio.sleep(30)
            elapsed += 30

            # Cek apakah order masih aktif di DB (mungkin sudah dibatalkan user/admin)
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT status FROM orders WHERE order_id=%s", (order_id,))
            row = c.fetchone()
            conn.close()

            if not row or row['status'] != 'waiting':
                return  # Sudah selesai/dibatalkan, stop polling

            # Cek ke Pakasir API
            trans = get_transaction_detail(order_id, amount)
            if not trans:
                continue

            if trans.get('status') == 'completed':
                await _handle_payment_success(bot, order_id, paket_id, user_id, user_name, amount, trans)
                return

        # ===== TIMEOUT: auto cancel =====
        # Cek sekali lagi sebelum expire
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT status FROM orders WHERE order_id=%s", (order_id,))
        row = c.fetchone()
        conn.close()

        if not row or row['status'] != 'waiting':
            return

        trans = get_transaction_detail(order_id, amount)
        if trans and trans.get('status') == 'completed':
            await _handle_payment_success(bot, order_id, paket_id, user_id, user_name, amount, trans)
            return

        # Benar-benar expired
        if amount:
            cancel_transaction(order_id, amount)
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

    except asyncio.CancelledError:
        pass  # Task dibatalkan oleh _stop_payment_task(), normal
    finally:
        _payment_tasks.pop(user_id, None)

async def _handle_payment_success(bot, order_id: str, paket_id: str, user_id: int,
                                   user_name: str, amount: int, trans: dict):
    """Proses pembayaran sukses: kirim link ke buyer + notif admin."""
    paket = get_product(paket_id) or {"emoji": "📦", "nama": "Produk", "harga": amount, "link": DEFAULT_LINK}
    update_order_status(order_id, 'completed')

    paid_amount = trans.get('amount', amount)
    link = paket.get("link") or DEFAULT_LINK

    # Kirim link ke buyer
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"*✅ PEMBAYARAN BERHASIL*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 *Detail Pesanan*\n"
                f"├ Paket: {paket['emoji']} {esc(paket['nama'])}\n"
                f"├ Order ID: `{order_id}`\n"
                f"└ Total: {format_harga(paid_amount)}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔗 *Link Produk*\n"
                f"{link}\n\n"
                f"💾 _Simpan link ini. Produk dapat diakses kapan saja._\n\n"
                f"Terima kasih telah berbelanja\\! 🙏"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[PAYMENT] Gagal kirim link ke buyer {user_id}: {e}")

    # Notif admin
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"*🔔 PEMBAYARAN SELESAI (AUTO)*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 Pembeli: {esc(user_name)}\n"
                f"📦 Paket: {paket['emoji']} {esc(paket['nama'])}\n"
                f"📝 Order ID: `{order_id}`\n"
                f"💰 Total: {format_harga(paid_amount)}\n"
                f"🕐 Waktu: {now_wib().strftime('%H:%M, %d %b %Y')}\n\n"
                f"✅ Status: LUNAS — Link otomatis terkirim\n\n"
                f"_Link: {link}_"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[PAYMENT] Gagal notif admin: {e}")

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

    text = (
        f"*{esc(p['emoji'])} {esc(p['nama'])}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 Harga: {format_harga(p['harga'])}\n"
        f"📝 Deskripsi: {esc(p['deskripsi'])}\n"
        f"🔗 Link: `{esc(p['link'])}`\n\n"
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
        ],
        [
            InlineKeyboardButton("🗑️ Hapus Produk", callback_data=f"pd_hapus_{paket_id}"),
        ],
        [
            InlineKeyboardButton("← Kembali",    callback_data="pd_back"),
        ],
    ]

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def produk_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    raw = query.data.replace("pd_edit_", "")
    FIELDS = ["nama", "emoji", "harga", "deskripsi", "link"]
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
    """Tampilkan semua order yang sedang menunggu pembayaran."""
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
    """Admin membatalkan order buyer yang masih menunggu bayar."""
    query = update.callback_query
    await query.answer()

    # Format: adm_cancel|user_id|order_id
    parts = query.data.split("|")
    if len(parts) != 3:
        await query.answer("Format tidak valid.", show_alert=True)
        return

    target_user_id = int(parts[1])
    order_id = parts[2]

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id=%s AND status='waiting'", (order_id,))
    order = c.fetchone()
    conn.close()

    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah selesai/dibatalkan.")
        return

    order = dict(order)
    paket = get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0}

    # Cancel di Pakasir
    if paket['harga']:
        cancel_transaction(order_id, paket['harga'])

    update_order_status(order_id, 'cancelled')

    # Stop payment task asyncio untuk user ini
    _stop_payment_task(target_user_id)

    # Notif ke buyer
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
    """Admin konfirmasi pembayaran secara manual — kirim link langsung ke buyer."""
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    # Format: adm_konfirm|user_id|order_id
    parts = query.data.split("|")
    if len(parts) != 3:
        await query.answer("Format tidak valid.", show_alert=True)
        return

    target_user_id = int(parts[1])
    order_id = parts[2]

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE order_id=%s AND status='waiting'", (order_id,))
    order = c.fetchone()
    conn.close()

    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah selesai/dibatalkan.")
        return

    order = dict(order)
    paket = get_product(order['paket_id']) or {"emoji": "📦", "nama": order['paket_id'], "harga": 0, "link": DEFAULT_LINK}

    # Stop task polling dan tandai selesai
    _stop_payment_task(target_user_id)
    update_order_status(order_id, 'completed')

    link = paket.get("link") or DEFAULT_LINK
    harga = paket.get("harga", 0)

    # Kirim link ke buyer
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                f"*✅ PEMBAYARAN DIKONFIRMASI*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 *Detail Pesanan*\n"
                f"├ Paket: {paket['emoji']} {esc(paket['nama'])}\n"
                f"├ Order ID: `{order_id}`\n"
                f"└ Total: {format_harga(harga)}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔗 *Link Produk*\n"
                f"{link}\n\n"
                f"💾 _Simpan link ini. Produk dapat diakses kapan saja._\n\n"
                f"Terima kasih telah berbelanja\\! 🙏"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[KONFIRMASI MANUAL] Gagal kirim link ke buyer {target_user_id}: {e}")

    # Update pesan admin
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
        f"└ Estimasi Omzet: {format_harga(s['today_revenue'])}\n\n"
        f"📆 *Bulan Ini*\n"
        f"├ Order Selesai: {s['month_orders']}\n"
        f"└ Estimasi Omzet: {format_harga(s['month_revenue'])}\n\n"
        f"🏆 *All Time*\n"
        f"├ Total Order Selesai: {s['total_orders']}\n"
        f"├ Total Dibatalkan: {s['cancelled_count']}\n"
        f"├ Order Aktif Sekarang: {s['active_count']}\n"
        f"└ Estimasi Total Omzet: {format_harga(s['total_revenue'])}\n\n"
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
        text += (
            f"{paket['emoji']} *{esc(paket['nama'])}*\n"
            f"├ Status: {status}\n"
            f"├ Harga: {format_harga(paket['harga'])}\n"
            f"└ {o['waktu']}\n\n"
        )

    await update.message.reply_text(text, parse_mode="Markdown")

# =================== ADMIN: BACKUP DATABASE ===================

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return

    await update.message.reply_text("⏳ Membuat backup database...")
    await _kirim_backup(context.bot)

async def _kirim_backup(bot):
    """Export data orders & products dari PostgreSQL ke file teks, lalu kirim ke admin."""
    backup_name = f"backup_{now_wib().strftime('%Y%m%d_%H%M%S')}.txt"
    try:
        conn = get_conn()
        c = conn.cursor()

        c.execute("SELECT * FROM orders ORDER BY id DESC")
        orders = c.fetchall()

        c.execute("SELECT * FROM products ORDER BY harga ASC")
        products = c.fetchall()
        conn.close()

        lines = []
        lines.append(f"=== BACKUP HYPER FAMILY STORE ===\n")
        lines.append(f"Tanggal: {now_wib().strftime('%H:%M, %d/%m/%Y')}\n\n")

        lines.append(f"--- PRODUK ({len(products)}) ---\n")
        for p in products:
            lines.append(f"[{p['paket_id']}] {p['emoji']} {p['nama']} — Rp {p['harga']:,} | Link: {p['link']}\n")

        lines.append(f"\n--- ORDERS ({len(orders)}) ---\n")
        for o in orders:
            lines.append(
                f"[{o['id']}] {o['order_id']}\n"
                f"  User: {o['user_name']} ({o['user_id']})\n"
                f"  Paket: {o['paket_id']} | Status: {o['status']} | Waktu: {o['waktu']}\n\n"
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
                f"📋 {len(orders)} orders | {len(products)} produk"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error backup: {e}")
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Gagal backup database: {e}")
        except Exception:
            pass

# =================== ADMIN: EXPORT & IMPORT SQL ===================

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export seluruh data (products + orders) sebagai file .sql — bisa di-import ke DB manapun."""
    if update.message.from_user.id != ADMIN_ID:
        return

    await update.message.reply_text("⏳ Menyiapkan export SQL...")

    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM products ORDER BY harga ASC")
        products = c.fetchall()
        c.execute("SELECT * FROM orders ORDER BY id ASC")
        orders = c.fetchall()
        conn.close()

        lines = []
        lines.append("-- ================================================\n")
        lines.append("-- HYPER FAMILY STORE — Full Data Export\n")
        lines.append(f"-- Tanggal: {now_wib().strftime('%H:%M, %d/%m/%Y')}\n")
        lines.append(f"-- Products: {len(products)} | Orders: {len(orders)}\n")
        lines.append("-- Cara pakai: kirim file ini ke bot via /import_sql\n")
        lines.append("-- ================================================\n\n")

        # Products
        lines.append("-- PRODUCTS\n")
        for p in products:
            def sql_str(v):
                if v is None:
                    return "NULL"
                return "'" + str(v).replace("'", "''") + "'"
            lines.append(
                f"INSERT INTO products (paket_id, nama, emoji, deskripsi, harga, link) "
                f"VALUES ({sql_str(p['paket_id'])}, {sql_str(p['nama'])}, {sql_str(p['emoji'])}, "
                f"{sql_str(p['deskripsi'])}, {p['harga']}, {sql_str(p['link'])}) "
                f"ON CONFLICT (paket_id) DO UPDATE SET "
                f"nama=EXCLUDED.nama, emoji=EXCLUDED.emoji, deskripsi=EXCLUDED.deskripsi, "
                f"harga=EXCLUDED.harga, link=EXCLUDED.link;\n"
            )

        lines.append("\n-- ORDERS\n")
        for o in orders:
            def sql_str(v):
                if v is None:
                    return "NULL"
                return "'" + str(v).replace("'", "''") + "'"
            lines.append(
                f"INSERT INTO orders (user_id, user_name, paket_id, order_id, status, waktu) "
                f"VALUES ({o['user_id']}, {sql_str(o['user_name'])}, {sql_str(o['paket_id'])}, "
                f"{sql_str(o['order_id'])}, {sql_str(o['status'])}, {sql_str(o['waktu'])}) "
                f"ON CONFLICT (order_id) DO NOTHING;\n"
            )

        filename = f"export_{now_wib().strftime('%Y%m%d_%H%M%S')}.sql"
        content = "".join(lines).encode("utf-8")
        buf = BytesIO(content)
        buf.name = filename

        await update.message.reply_document(
            document=buf,
            filename=filename,
            caption=(
                f"✅ Export Berhasil\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 Products: {len(products)}\n"
                f"📋 Orders: {len(orders)}\n"
                f"🕐 {now_wib().strftime('%H:%M, %d/%m/%Y')}\n\n"
                f"Kirim file ini ke bot dengan /import_sql untuk restore."
            )
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Gagal export: {e}")


async def cmd_import_sql(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Perintah /import_sql — minta admin kirim file .sql."""
    if update.message.from_user.id != ADMIN_ID:
        return

    context.user_data['awaiting_sql_import'] = True
    await update.message.reply_text(
        "*📥 IMPORT DATA SQL*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Kirim file `.sql` yang didapat dari `/export`.\n\n"
        "⚠️ Data yang sudah ada *tidak akan dihapus* — hanya ditambah/diperbarui.\n"
        "_Kirim file sekarang..._",
        parse_mode="Markdown"
    )


async def handle_sql_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima file .sql dari admin dan jalankan isinya ke database."""
    if update.message.from_user.id != ADMIN_ID:
        return
    if not context.user_data.get('awaiting_sql_import'):
        return

    doc = update.message.document
    if not doc or not doc.file_name.endswith('.sql'):
        await update.message.reply_text("❌ File harus berformat `.sql`. Coba lagi dengan /import_sql.")
        return

    context.user_data.pop('awaiting_sql_import', None)
    status_msg = await update.message.reply_text("⏳ Membaca file SQL...")

    try:
        file = await context.bot.get_file(doc.file_id)
        sql_bytes = await file.download_as_bytearray()
        sql_text = sql_bytes.decode("utf-8")

        # Pisah per statement (berakhir dengan ;)
        statements = [s.strip() for s in sql_text.split(";") if s.strip() and not s.strip().startswith("--")]

        conn = get_conn()
        c = conn.cursor()
        ok = 0
        gagal = 0
        for stmt in statements:
            # Hanya izinkan INSERT (keamanan — tidak ada DROP/DELETE/etc.)
            if not stmt.upper().lstrip().startswith("INSERT"):
                continue
            try:
                c.execute(stmt)
                ok += 1
            except Exception as e:
                print(f"[IMPORT] Gagal eksekusi statement: {e}\n{stmt[:80]}")
                gagal += 1

        conn.commit()
        conn.close()

        await status_msg.edit_text(
            f"✅ *Import Selesai*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✔️ Berhasil: {ok} statement\n"
            f"❌ Gagal: {gagal} statement\n\n"
            f"_Semua produk dan riwayat order sudah ter-restore._",
            parse_mode="Markdown"
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Gagal import: {e}")


# =================== BUYER REMINDER ===================
REMINDER_HARI = 3  # Kirim reminder X hari setelah buyer terakhir beli

def get_buyers_for_reminder(hari: int):
    """Ambil buyer yang terakhir beli tepat X hari lalu (untuk reminder harian)."""
    target_date = (now_wib() - timedelta(days=hari)).strftime("%d/%m/%Y")
    conn = get_conn()
    c = conn.cursor()
    # Buyer yang punya completed order di tanggal target
    c.execute(
        "SELECT DISTINCT user_id, user_name FROM orders WHERE status='completed' AND waktu LIKE %s",
        (f"% — {target_date}",)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

async def _send_buyer_reminders(bot):
    """Kirim pesan pengingat ke buyer yang membeli tepat REMINDER_HARI hari lalu."""
    buyers = get_buyers_for_reminder(REMINDER_HARI)
    if not buyers:
        return
    print(f"[REMINDER] Mengirim reminder ke {len(buyers)} buyer...")
    for buyer in buyers:
        try:
            await bot.send_message(
                chat_id=buyer['user_id'],
                text=(
                    f"👋 Halo *{esc(buyer['user_name'])}*\\!\n\n"
                    f"Sudah *{REMINDER_HARI} hari* sejak kamu belanja di *Hyper Family Store* 🛍️\n\n"
                    f"Puas dengan produknya? Mau belanja lagi?\n"
                    f"Kami punya paket menarik yang siap dikirim langsung\\!\n\n"
                    f"Ketik /start untuk lihat katalog kami 😊"
                ),
                parse_mode="Markdown"
            )
            await asyncio.sleep(0.5)  # Delay antar pesan agar tidak kena flood limit
        except Exception as e:
            print(f"[REMINDER] Gagal kirim ke {buyer['user_id']}: {e}")

async def _buyer_reminder_loop(bot):
    """Asyncio loop: cek dan kirim reminder setiap hari jam 10:00 WIB."""
    while True:
        try:
            now = now_wib()
            # Hitung waktu ke jam 10:00 hari ini atau besok
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
    """Asyncio loop: backup DB otomatis setiap hari jam 00:00."""
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
            await asyncio.sleep(3600)  # Retry sejam kemudian jika error

# =================== ADMIN: BROADCAST ===================

async def cmd_blast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return

    buyers = get_all_buyers()
    jumlah = len(buyers)
    if jumlah == 0:
        await update.message.reply_text("❌ Belum ada buyer yang terdaftar.")
        return

    context.user_data['blasting'] = True
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
    context.user_data.pop('blasting', None)
    await query.edit_message_text("✅ Broadcast dibatalkan.")

# =================== ADMIN: MESSAGE HANDLER ===================

async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.from_user.id != ADMIN_ID:
        return

    text = update.message.text.strip() if update.message.text else ""

    # --- State: broadcast blast ---
    if context.user_data.get('blasting'):
        context.user_data.pop('blasting', None)

        buyers = get_all_buyers()
        jumlah = len(buyers)

        status_msg = await update.message.reply_text(f"📢 Mengirim ke {jumlah} buyer...")

        sent = 0
        failed = 0
        for b in buyers:
            try:
                await context.bot.send_message(
                    chat_id=b['user_id'],
                    text=text,
                    parse_mode="Markdown"
                )
                sent += 1
            except Exception:
                failed += 1

        try:
            await status_msg.delete()
        except Exception:
            pass

        await update.message.reply_text(
            f"*✅ BROADCAST SELESAI*\n"
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

        value = int(text) if field == 'harga' else text
        update_product_field(paket_id, field, value)
        context.user_data.pop('editing_product', None)

        p = get_product(paket_id)
        await update.message.reply_text(
            f"✅ {field.capitalize()} berhasil diupdate!\n\nNilai baru: {value}"
        )
        if p:
            detail_text = (
                f"*{esc(p['emoji'])} {esc(p['nama'])}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💰 Harga: {format_harga(p['harga'])}\n"
                f"📝 Deskripsi: {esc(p['deskripsi'])}\n"
                f"🔗 Link: `{esc(p['link'])}`\n\n"
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
                [InlineKeyboardButton("🔗 Link",          callback_data=f"pd_edit_{paket_id}_link")],
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

# =================== ADMIN: LINK (legacy) ===================

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    products = get_all_products()
    text = "*🔗 LINK PRODUK SAAT INI*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for p in products:
        text += f"{p['emoji']} *{esc(p['nama'])}*\n└ `{p['link']}`\n\n"
    text += "_Ketik /produk untuk mengubah link._"
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
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE user_id=%s AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,))
    order = c.fetchone()
    conn.close()

    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah diproses.")
        return

    order = dict(order)
    paket = get_product(order["paket_id"]) or {"emoji": "📦", "nama": order["paket_id"], "harga": 0, "deskripsi": "-"}
    trans = get_transaction_detail(order["order_id"], paket["harga"]) if order["order_id"] else None
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
    await context.bot.send_message(chat_id=ADMIN_ID, text=caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

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

    await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_konfirmasi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action = parts[0]
    user_id = int(parts[1])

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE user_id=%s AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,))
    order = c.fetchone()
    conn.close()

    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah diproses.")
        return

    order = dict(order)
    paket = get_product(order["paket_id"]) or {"emoji": "📦", "nama": order["paket_id"], "harga": 0, "deskripsi": "-", "link": DEFAULT_LINK}

    if action == "confirm":
        link = paket.get("link") or DEFAULT_LINK
        msg = await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"*✅ PESANAN SELESAI*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 *Detail*\n"
                f"├ Paket: {paket['emoji']} {esc(paket['nama'])}\n"
                f"└ Konten: {esc(paket['deskripsi'])}\n\n"
                f"🔗 *Link Produk*\n"
                f"{link}\n\n"
                f"💾 _Simpan link ini. Produk dapat diakses kapan saja._\n\n"
                f"Terima kasih telah berbelanja\\! 🙏"
            ),
            parse_mode="Markdown"
        )
        simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        update_order_status(order["order_id"], 'completed')

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

# =================== MAIN ===================

def main():
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
    app.add_handler(CommandHandler("produk",  cmd_produk))
    app.add_handler(CommandHandler("pending", admin_pending))
    app.add_handler(CommandHandler("aktif",   cmd_aktif))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("blast",   cmd_blast))
    app.add_handler(CommandHandler("backup",     cmd_backup))
    app.add_handler(CommandHandler("export",     cmd_export))
    app.add_handler(CommandHandler("import_sql", cmd_import_sql))
    app.add_handler(CommandHandler("link",       cmd_link))

    # User callbacks
    app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy$"))
    app.add_handler(CallbackQueryHandler(pilih_paket,  pattern="^pilih_"))
    app.add_handler(CallbackQueryHandler(back_start,   pattern="^back_start$"))

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

    # Blast callback
    app.add_handler(CallbackQueryHandler(blast_batal, pattern="^blast_batal$"))

    # Admin: terima file .sql untuk import
    app.add_handler(MessageHandler(
        filters.Document.ALL & filters.Chat(ADMIN_ID),
        handle_sql_document
    ))

    # Admin message handler (untuk state tambah/edit produk)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Chat(ADMIN_ID),
        admin_message_handler
    ))

    print("Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
