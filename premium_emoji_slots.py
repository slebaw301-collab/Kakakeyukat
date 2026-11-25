"""Stable destinations exposed by the Custom Emoji Manager."""

from contextvars import ContextVar
from functools import wraps

surface = ContextVar('premium_emoji_surface', default='other')
SETTINGS_PREFIX = 'premium-emoji:'

# Screen keys are persisted. Keep them stable when editing the menu labels.
CATEGORIES = {
    'home': ('Menu Utama', {
        'welcome': ('Sambutan', '👋💋✨📦💎💸🎥📞🛒📖⭐📜💬👥'),
        'help': ('Cara Order & Bantuan', '📖⬅💬❓'),
        'history': ('Riwayat Pembeli', '📜✅⏳🔄❌⏰🚫⬅'),
    }),
    'catalog': ('Katalog & Produk', {
        'catalog': ('Daftar Paket', '📦⬅➡'),
        'product': ('Detail Paket & Preview', '📦🎬💰♾🟢🔴👀🔍✅⬅📄'),
        'admin_products': ('Kelola Produk', '📦✅❌➕⬅⬜✍😀💰📝🔗🏢🔒👀🗑🟢🔴'),
    }),
    'payment': ('Pembayaran', {
        'invoice': ('Invoice & QRIS', '📋📝⏰📱✅⏳🌐🔄❌📌🚀'),
        'payment_status': ('Status Pembayaran', '✅❌⏳⏰⚠📦💰📝'),
        'payment_change': ('Ganti Paket & Batalkan Pesanan', '📦📌❌✅🔄⬅🌐💰'),
    }),
    'reviews': ('Testimoni', {
        'review_post': ('Publikasi Testimoni', '💠✅👤📦💰🧾🕒🛒💬'),
        'review_admin': ('Testimoni Admin', '⭐✅❌⚠⬅'),
    }),
    'notifications': ('Notifikasi', {
        'order_notification': ('Notifikasi Admin', '📢📦👤💰🧾🕒🔗✅❌⏳'),
        'buyer_reminder': ('Pengingat Pembeli', '📢⏰✅📦💰🔗💬'),
        'delivery_alert': ('Peringatan Gagal Kirim', '🚨👤📦🔖'),
    }),
    'delivery': ('Pengiriman Akses & Resend', {
        'access': ('Pesan Akses Produk', '🎉🔓📌💰✅🔗🔒'),
        'requirements': ('Syarat Akses', '✅📦🔒📊❌🔐🎯⏳📋'),
        'resend_menu': ('Pilihan Produk Resend', '🔄📦⬅➡'),
        'resend_access': ('Link Resend', '🔄📌💰✅🔗🔒⚠'),
    }),
    'claim': ('Claim Free & Auto-kick', {
        'claim_intro': ('Halaman Claim', '🎁📌📢🔰✅⚠'),
        'claim_verify': ('Hasil Verifikasi Claim', '🎁✅❌🔗⛔⏳🔒⚠'),
        'claim_admin': ('Pengaturan Claim Free', '🎁📢🔰✅🔴🔘📊⬅'),
        'auto_kick': ('Notifikasi Auto-kick', '🔒🔄⚠✅'),
    }),
    'admin': ('Panel Admin', {
        'admin_settings': (
        ('notifications_on', 'Set Channel Notif', '📢', 'admpanel_setting_channel_set'),
        ('notifications_off', 'Matikan Notif', '🔕', 'admpanel_setting_channel_off'),
        ('reviews_on', 'Set Channel Testi', '⭐', 'admpanel_setting_testich_set'),
        ('reviews_off', 'Matikan Testi', '🔕', 'admpanel_setting_testich_off'),
        ('notifications_test', 'Test Notifikasi', '📨', 'admpanel_setting_channel_test'),
        ('reviews_link', 'Ubah Link Testimoni', '⭐', 'admpanel_setting_link_testi'),
        ('contact_link', 'Ubah Link Admin/CS', '💬', 'admpanel_setting_link_admin'),
        ('group_name', 'Nama Tombol Grup', '✏', 'admpanel_setting_group_name'),
        ('group_link', 'Link Tombol Grup', '🔗', 'admpanel_setting_group_link'),
    ),
    'admin_main': ('Panel Utama', '⚙🎁💎📦📋📊📢💾🚫👢👥'),
        'admin_settings': ('Pengaturan Admin', '⚙📢💠✅🔕⭐💬👥✏🔗📨⬅'),
        'admin_emoji': ('Pengaturan Premium Emoji', '💎✅⚠🔘👀⬅'),
        'admin_other': ('Order, User & Data Admin', '📋📦👤✅❌⚠⬅📊💾🚫'),
    }),
    'other': ('Bagian Lainnya', {
        'broadcast': ('Broadcast', '📢✅❌🔄⏸▶⛔⬅'),
        'other': ('Pesan Sistem Lainnya', '✅❌⚠📦⏳🔗'),
        'global': ('Mapping Bawaan Seluruh Bot', ''),
    }),
}

# Match at the relevant screen only. Fixed URL buttons use their existing label.
# An empty selector for the optional welcome group matches that screen's last row.
BUTTONS = {
    'welcome': (
        ('catalog', 'Daftar Paket', '🛒', 'buy'),
        ('how', 'Cara Order', '📖', 'buyer_how'),
        ('reviews', 'Testimoni', '⭐', '@⭐ Testimoni'),
        ('history', 'Riwayat', '📜', 'buyer_history'),
        ('contact', 'Hubungi Admin', '💬', '@💬 Hubungi Admin'),
        ('group', 'Grup Hyper Family', '👥', ''),
    ),
    'product': (
        ('pay', 'Lanjut Bayar', '✅', 'confirm_buy_'),
        ('description', 'Deskripsi Lengkap', '📄', 'buyer_desc_'),
    ),
    'invoice': (
        ('checkout', 'Halaman Pembayaran', '🌐', '@🌐 Buka Halaman Pembayaran'),
        ('change', 'Ganti Paket', '🔄', 'ganti_paket_list'),
        ('cancel', 'Batalkan Pesanan', '❌', 'cancel_order'),
    ),
    'review_post': (
        ('order', 'Order Sekarang', '🛒', '@🛒 Order Sekarang'),
        ('contact', 'Hubungi Admin', '💬', '@💬 Hubungi Admin'),
    ),
    'access': (
        ('join', 'Gabung Grup VIP', '🔗', '@🔗 GABUNG GRUP VIP'),
        ('open', 'Buka Produk', '🔗', '@🔗 BUKA PRODUK'),
    ),
    'resend_access': (
        ('join', 'Gabung Grup VIP', '🔗', '@🔗 GABUNG GRUP VIP'),
        ('open', 'Buka Produk', '🔗', '@🔗 BUKA PRODUK'),
        ('again', 'Pilih Produk Lain', '🔄', 'resend_menu'),
    ),
    'claim_intro': (
        ('join_review', 'Gabung Testimoni', '📢', '@📢 Gabung Testimoni'),
        ('join_backup', 'Gabung Backup', '🔰', '@🔰 Gabung Backup'),
        ('verify', 'Verifikasi & Claim', '✅', 'cf_verify'),
    ),
    'claim_verify': (
        ('open', 'Masuk Grup/Channel Free', '🔗', '@🔗 Masuk Grup/Channel Free'),
    ),
    'claim_admin': (
        ('testi', 'Atur Testimoni', '📢', 'cf_testi'),
        ('backup', 'Atur Backup', '🔰', 'cf_backup'),
        ('backup_toggle', 'Syarat Backup', '🔘', 'cf_backup_toggle'),
        ('free', 'Atur Free', '🎁', 'cf_free'),
        ('kick', 'Auto-kick', '🔘', 'cf_kick_toggle'),
        ('toggle', 'Aktifkan/Nonaktifkan Claim', '🔘', 'cf_toggle'),
    ),
    'admin_main': (
        ('products', 'Produk', '📦', 'admpanel_produk'),
        ('orders', 'Orders', '📋', 'admpanel_orders'),
        ('stats', 'Statistik', '📊', 'admpanel_stats'),
        ('blast_status', 'Status Blast', '📊', 'admpanel_blast_status'),
    ),
}


def emoji_surface(name):
    def decorate(handler):
        @wraps(handler)
        async def scoped(*args, **kwargs):
            token = surface.set(name)
            try:
                return await handler(*args, **kwargs)
            finally:
                surface.reset(token)
        return scoped
    return decorate


def glyph_key(screen, field, symbol):
    return f'{screen}/{field}/{ord(symbol[0]):x}'


def button_key(screen, role):
    return f'{screen}/action/{role}'


def button_role(screen, button, row):
    for role, _, symbol, selector in BUTTONS.get(screen, ()):
        if not selector:
            match = row == 3 and button.url
        elif selector.startswith('@'):
            match = button.url and (
                button.text == selector[1:] or
                (button.api_kwargs.get('icon_custom_emoji_id') and button.text == selector.partition(' ')[2])
            )
        else:
            callback = button.callback_data
            match = isinstance(callback, str) and (
                callback.startswith(selector) if selector.endswith('_') else
                callback == selector or callback.startswith(selector + '|')
            )
        if match:
            return button_key(screen, role), symbol
    return None, None
