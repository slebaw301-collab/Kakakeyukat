import os
import sqlite3
import re
import requests
import qrcode
from io import BytesIO
from datetime import datetime, timedelta
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

# =================== KONFIGURASI ===================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
PAKASIR_API_KEY = os.environ.get("PAKASIR_API_KEY")
PAKASIR_SLUG = "atkikukkvd"
PAKASIR_BASE_URL = "https://app.pakasir.com"

DEFAULT_LINK = "https://t.me/Kikukkvd"

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
    conn = sqlite3.connect("orders.db")
    conn.row_factory = sqlite3.Row
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

    # Migration: tambah kolom link jika belum ada (DB lama tidak punya kolom ini)
    try:
        c.execute("ALTER TABLE products ADD COLUMN link TEXT DEFAULT 'https://t.me/Kikukkvd'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Kolom sudah ada, skip

    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
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
            "INSERT OR IGNORE INTO products (paket_id, nama, emoji, deskripsi, harga, link) VALUES (?, ?, ?, ?, ?, ?)",
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
    c.execute("SELECT * FROM products WHERE paket_id=?", (paket_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def add_product(paket_id, nama, emoji, deskripsi, harga, link=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO products (paket_id, nama, emoji, deskripsi, harga, link) VALUES (?, ?, ?, ?, ?, ?)",
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
    c.execute(f"UPDATE products SET {field}=? WHERE paket_id=?", (value, paket_id))
    conn.commit()
    conn.close()

def delete_product(paket_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE paket_id=?", (paket_id,))
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
        "SELECT * FROM orders WHERE user_id=? AND status IN ('waiting','pending') ORDER BY id DESC LIMIT 1",
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

def update_order_status(order_id, status):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
    conn.commit()
    conn.close()

def save_order(user_id, user_name, paket_id, order_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO orders (user_id, user_name, paket_id, order_id, status, waktu) VALUES (?, ?, ?, ?, 'waiting', ?)",
        (user_id, user_name, paket_id, order_id, datetime.now().strftime("%H:%M — %d/%m/%Y"))
    )
    conn.commit()
    conn.close()

# =================== HELPERS ===================

def format_harga(harga):
    return f"Rp {int(harga):,}".replace(",", ".")

def build_main_menu_text():
    products = get_all_products()
    text = (
        "*🛍️ HYPER FAMILY STORE*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Selamat datang! Pilih paket yang tersedia:\n\n"
    )
    for p in products:
        text += (
            f"{p['emoji']} *{p['nama'].upper()}*\n"
            f"├ {p['deskripsi']}\n"
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

async def simpan_msg_user(context, user_id, message_id):
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

# =================== POST INIT ===================

async def post_init(application: Application):
    await application.bot.set_my_commands(
        [BotCommand("start", "Buka toko")],
        scope=BotCommandScopeDefault()
    )
    await application.bot.set_my_commands(
        [
            BotCommand("start",   "Buka toko"),
            BotCommand("produk",  "Kelola produk"),
            BotCommand("pending", "Lihat order pending"),
            BotCommand("link",    "Cek link produk saat ini"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID)
    )

# =================== USER HANDLERS ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    active = get_active_order(user_id)
    if active:
        paket = get_product(active["paket_id"])
        if not paket:
            paket = {"emoji": "📦", "nama": "Produk", "harga": 0}

        trans = get_transaction_detail(active["order_id"], paket["harga"])
        if trans and trans.get("status") == "completed":
            update_order_status(active["order_id"], "pending")
            await update.message.reply_text(
                "✅ Pembayaran sudah diterima! Pesanan sedang diproses...",
                parse_mode="Markdown"
            )
            return

        total = (trans.get("amount", paket["harga"]) + trans.get("fee", 0)) if trans else paket["harga"]
        text = (
            f"*⏳ ORDER AKTIF*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Kamu masih punya pesanan yang belum dibayar:\n\n"
            f"📦 Paket: {paket['emoji']} {paket['nama']}\n"
            f"💰 Total: {format_harga(total)}\n"
            f"📝 Order ID: `{active['order_id']}`\n\n"
            f"_Silakan selesaikan pembayaran atau batalkan pesanan dulu._"
        )
        keyboard = [[InlineKeyboardButton("✕ Batalkan Pesanan", callback_data="back_start")]]
        msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        await simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        return

    msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=build_main_menu_text(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(build_main_menu_keyboard())
    )
    await simpan_msg_user(context, user_id, msg.message_id)
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
            f"{p['emoji']} *{p['nama'].upper()}*\n"
            f"├ {p['deskripsi']}\n"
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
            update_order_status(active["order_id"], "pending")
            await query.answer("✅ Pembayaran sudah diterima!", show_alert=True)
            return
        await query.answer("⏳ Kamu sudah punya invoice aktif!", show_alert=True)
        total = (trans.get("amount", paket_active["harga"]) + trans.get("fee", 0)) if trans else paket_active["harga"]
        caption = (
            f"*⏳ ORDER AKTIF*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 Paket: {esc(paket_active['emoji'])} {esc(paket_active['nama'])}\n"
            f"💰 Total: {format_harga(total)}\n"
            f"📝 Order ID: `{active['order_id']}`\n\n"
            f"⚠️ Selesaikan pembayaran atau batalkan dulu."
        )
        keyboard = [[InlineKeyboardButton("✕ Batalkan", callback_data="back_start")]]
        await query.edit_message_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await query.answer()

    loading_msg = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⏳ Membuat invoice...",
    )

    order_id = f"HFB-{user_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
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
        await simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        return

    # Validasi field penting dari response API
    qris_string = trans_data.get('payment_number', '')
    if not qris_string:
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Gagal membuat QRIS. Silakan coba lagi.\nKetik /start untuk memulai ulang.",
        )
        await simpan_msg_user(context, user_id, msg.message_id)
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
        expire = (datetime.now() + timedelta(minutes=30)).strftime("%H:%M")

    qr_buffer = generate_qr_image(qris_string)

    caption = (
        f"*{paket['emoji']} {paket['nama'].upper()}*\n"
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
    await simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=1)

    context.job_queue.run_repeating(
        check_payment_status,
        interval=30, first=30,
        chat_id=user_id, user_id=user_id,
        name=f"check_{user_id}",
        data={'order_id': order_id, 'paket_id': paket_id, 'user_id': user_id, 'user_name': user_name, 'amount': paket["harga"]}
    )

    try:
        expired_dt = datetime.fromisoformat(expired_at.replace('Z', '+00:00'))
        now = datetime.now(expired_dt.tzinfo)
        delay = (expired_dt - now).total_seconds()
        if delay > 0:
            context.job_queue.run_once(
                auto_cancel, delay,
                chat_id=user_id, user_id=user_id,
                name=f"cancel_{user_id}",
                data={'order_id': order_id, 'amount': paket["harga"]}
            )
    except Exception:
        context.job_queue.run_once(
            auto_cancel, timedelta(minutes=30),
            chat_id=user_id, user_id=user_id,
            name=f"cancel_{user_id}",
            data={'order_id': order_id, 'amount': paket["harga"]}
        )

async def back_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    active = get_active_order(user_id)
    if active:
        paket = get_product(active["paket_id"])
        amount = paket["harga"] if paket else 0
        cancel_transaction(active["order_id"], amount)
        update_order_status(active["order_id"], "cancelled")
        for job in context.job_queue.get_jobs_by_name(f"check_{user_id}"):
            job.schedule_removal()
        for job in context.job_queue.get_jobs_by_name(f"cancel_{user_id}"):
            job.schedule_removal()

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
    await simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=1)

# =================== JOB QUEUE ===================

async def auto_cancel(context: ContextTypes.DEFAULT_TYPE):
    order_id = context.job.data['order_id']
    amount = context.job.data['amount']
    user_id = context.job.user_id

    trans = get_transaction_detail(order_id, amount)
    if trans and trans.get('status') == 'completed':
        return

    cancel_transaction(order_id, amount)
    update_order_status(order_id, 'expired')

    for job in context.job_queue.get_jobs_by_name(f"check_{user_id}"):
        job.schedule_removal()

    msg = await context.bot.send_message(
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
    await simpan_msg_user(context, user_id, msg.message_id)
    await hapus_msg_user_lama(context, user_id, keep_last=2)

async def check_payment_status(context: ContextTypes.DEFAULT_TYPE):
    order_id = context.job.data['order_id']
    paket_id = context.job.data['paket_id']
    user_id = context.job.data['user_id']
    user_name = context.job.data.get('user_name', 'User')
    amount = context.job.data['amount']

    trans = get_transaction_detail(order_id, amount)
    if not trans:
        return

    if trans.get('status') == 'completed':
        paket = get_product(paket_id) or {"emoji": "📦", "nama": "Produk", "harga": amount, "link": DEFAULT_LINK}

        for job in context.job_queue.get_jobs_by_name(f"check_{user_id}"):
            job.schedule_removal()
        for job in context.job_queue.get_jobs_by_name(f"cancel_{user_id}"):
            job.schedule_removal()

        update_order_status(order_id, 'completed')

        link = paket.get("link") or DEFAULT_LINK

        msg = await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"*✅ PEMBAYARAN BERHASIL*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📦 *Detail Pesanan*\n"
                f"├ Paket: {paket['emoji']} {paket['nama']}\n"
                f"├ Order ID: `{order_id}`\n"
                f"└ Total: {format_harga(trans.get('amount', amount))}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔗 *Link Produk*\n"
                f"{link}\n\n"
                f"💾 _Simpan link ini. Produk dapat diakses kapan saja._\n\n"
                f"Terima kasih telah berbelanja! 🙏"
            ),
            parse_mode="Markdown"
        )
        await simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)

        notif = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"*🔔 PEMBAYARAN SELESAI (AUTO)*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 Pembeli: {user_name}\n"
                f"📦 Paket: {paket['emoji']} {paket['nama']}\n"
                f"📝 Order ID: `{order_id}`\n"
                f"💰 Total: {format_harga(trans.get('amount', amount))}\n"
                f"🕐 Waktu: {datetime.now().strftime('%H:%M, %d %b %Y')}\n\n"
                f"✅ Status: LUNAS — Link otomatis terkirim\n\n"
                f"_Link: {link}_"
            ),
            parse_mode="Markdown"
        )
        simpan_admin_msg(context, user_id, notif.message_id)

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
            text += f"{p['emoji']} *{p['nama']}* — {format_harga(p['harga'])}\n"
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

    # format: pd_edit_<paket_id>_<field>
    # paket_id bisa mengandung underscore, jadi kita parse dari kiri
    raw = query.data.replace("pd_edit_", "")
    # field selalu kata terakhir tanpa underscore lain
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
        f"Kamu yakin mau hapus *{p['emoji']} {p['nama']}*?\n"
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
        f"✅ Produk *{p['emoji']} {p['nama']}* berhasil dihapus.",
        parse_mode="Markdown"
    )
    # Kirim menu produk baru setelah hapus
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

# =================== ADMIN: MESSAGE HANDLER ===================

async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.from_user.id != ADMIN_ID:
        return

    text = update.message.text.strip() if update.message.text else ""

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

            # Pastikan paket_id unik
            existing = get_product(paket_id)
            if existing:
                paket_id = f"{paket_id}_{int(datetime.now().timestamp())}"

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
        # Kirim ulang detail produk
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
        text += f"{p['emoji']} *{p['nama']}*\n└ `{p['link']}`\n\n"
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
        text += f"• {paket['emoji']} {o['user_name']} — {paket['nama']} — {o['waktu']}\n"
        keyboard.append([InlineKeyboardButton(f"👤 Proses: {o['user_name']}", callback_data=f"proses_{o['user_id']}")])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_proses_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.replace("proses_", ""))

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,))
    order = c.fetchone()
    conn.close()

    if not order:
        await query.edit_message_text("⚠️ Order tidak ditemukan atau sudah diproses.")
        return

    order = dict(order)
    paket = get_product(order["paket_id"]) or {"emoji": "📦", "nama": order["paket_id"], "harga": 0, "deskripsi": "-"}
    trans = get_transaction_detail(order["order_id"], paket["harga"]) if order["order_id"] else None

    caption = (
        f"*{paket['emoji']} {paket['nama'].upper()}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Pembeli: {order['user_name']} (`{order['user_id']}`)\n"
        f"📦 Konten: {paket['deskripsi']}\n"
        f"💰 Total: {format_harga(paket['harga'])}\n"
        f"🕐 Waktu: {order['waktu']}\n"
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
        text += f"• {paket['emoji']} {o['user_name']} — {paket['nama']} — {o['waktu']}\n"
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
    c.execute("SELECT * FROM orders WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (user_id,))
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
                f"├ Paket: {paket['emoji']} {paket['nama']}\n"
                f"└ Konten: {paket['deskripsi']}\n\n"
                f"🔗 *Link Produk*\n"
                f"{link}\n\n"
                f"💾 _Simpan link ini. Produk dapat diakses kapan saja._\n\n"
                f"Terima kasih telah berbelanja! 🙏"
            ),
            parse_mode="Markdown"
        )
        await simpan_msg_user(context, user_id, msg.message_id)
        await hapus_msg_user_lama(context, user_id, keep_last=2)
        update_order_status(order["order_id"], 'completed')

        await query.edit_message_text(
            f"*✅ DIKONFIRMASI*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 Pembeli: {order['user_name']}\n"
            f"📦 Paket: {paket['emoji']} {paket['nama']}\n\n"
            f"✅ Link produk otomatis terkirim ke buyer.",
            parse_mode="Markdown"
        )

    elif action == "reject":
        update_order_status(order["order_id"], 'rejected')
        await query.edit_message_text(
            f"*❌ DITOLAK*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 Pembeli: {order['user_name']}\n"
            f"📦 Paket: {paket['emoji']} {paket['nama']}",
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
        await simpan_msg_user(context, user_id, msg.message_id)
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
    app.add_handler(CommandHandler("start", start))

    # Admin commands
    app.add_handler(CommandHandler("produk",  cmd_produk))
    app.add_handler(CommandHandler("pending", admin_pending))
    app.add_handler(CommandHandler("link",    cmd_link))

    # User callbacks
    app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy$"))
    app.add_handler(CallbackQueryHandler(pilih_paket,  pattern="^pilih_"))
    app.add_handler(CallbackQueryHandler(back_start,   pattern="^back_start$"))

    # Produk management callbacks
    app.add_handler(CallbackQueryHandler(produk_detail,       pattern="^pd_detail_"))
    app.add_handler(CallbackQueryHandler(produk_edit_field,   pattern="^pd_edit_"))
    app.add_handler(CallbackQueryHandler(produk_hapus_confirm,pattern="^pd_hapus_(?!ok_)"))
    app.add_handler(CallbackQueryHandler(produk_hapus_exec,   pattern="^pd_hapus_ok_"))
    app.add_handler(CallbackQueryHandler(produk_tambah_start, pattern="^pd_tambah$"))
    app.add_handler(CallbackQueryHandler(produk_tambah_batal, pattern="^pd_tambah_batal$"))
    app.add_handler(CallbackQueryHandler(pd_back,             pattern="^pd_back$"))

    # Admin order callbacks
    app.add_handler(CallbackQueryHandler(admin_proses_order, pattern="^proses_"))
    app.add_handler(CallbackQueryHandler(admin_konfirmasi,   pattern="^(confirm|reject)_"))
    app.add_handler(CallbackQueryHandler(back_orders,        pattern="^back_orders$"))

    # Admin message handler (untuk state tambah/edit produk)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Chat(ADMIN_ID),
        admin_message_handler
    ))

    print("Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
