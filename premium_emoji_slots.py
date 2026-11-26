"""Stable destinations exposed by the Custom Emoji Manager."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cached_property, wraps
import json
import re

surface = ContextVar('premium_emoji_surface', default='other')
SETTINGS_PREFIX = 'premium-emoji:'
BACKUP_SETTING = 'premium-emoji-backup:semantic-v1'

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
}


@contextmanager
def emoji_scope(name):
    token = surface.set(name)
    try:
        yield
    finally:
        surface.reset(token)


def emoji_surface(name):
    def decorate(handler):
        @wraps(handler)
        async def scoped(*args, **kwargs):
            with emoji_scope(name):
                return await handler(*args, **kwargs)
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


# Existing location keys above remain readable for the migration and for
# user-supplied text. These roles apply only to audited buyer templates/actions.
ROLES = {
    'contact': 'Hubungi Admin', 'reviews': 'Testimoni', 'catalog': 'Daftar Paket',
    'price': 'Harga', 'package': 'Paket Produk', 'payment': 'Pembayaran',
    'success': 'Berhasil', 'failure': 'Gagal', 'waiting': 'Menunggu',
    'back': 'Kembali', 'resend': 'Resend', 'access': 'Akses Produk',
    'preview': 'Preview', 'verify': 'Verifikasi', 'claim': 'Claim Free',
    'welcome': 'Sambutan', 'brand': 'Hyper Store', 'benefits': 'Keunggulan & Update',
    'help': 'Cara Order & Bantuan', 'history': 'Riwayat Pembelian',
    'order': 'Order ID', 'warning': 'Peringatan', 'expiry': 'Batas Waktu',
    'change': 'Ganti Paket', 'cancel': 'Batal', 'buy': 'Beli Paket',
    'next': 'Halaman Berikutnya', 'home': 'Menu Utama', 'group': 'Grup Komunitas',
    'description': 'Deskripsi & Isi Produk', 'available': 'Produk Tersedia',
    'unavailable': 'Produk Tidak Tersedia', 'blocked': 'Akses Ditolak',
    'requirements': 'Syarat Produk', 'progress': 'Progres Syarat',
    'backup': 'Grup Backup', 'locked': 'Akses Terbatas',
    'maintenance': 'Pemeliharaan Bot', 'reminder': 'Pengingat Pembeli',
    'rejected': 'Pesanan Ditolak',
}

SCREENS = {
    'welcome': 'Menu Utama', 'help': 'Cara Order & Bantuan', 'catalog': 'Katalog',
    'product': 'Detail Produk', 'invoice': 'Invoice & QRIS',
    'payment_status': 'Status Pembayaran', 'payment_change': 'Ubah Pesanan',
    'history': 'Riwayat Pembelian', 'access': 'Pengiriman Akses',
    'requirements': 'Syarat Akses', 'resend_menu': 'Daftar Resend',
    'resend_access': 'Pengiriman Resend', 'buyer_reminder': 'Pengingat Pembeli',
    'claim_intro': 'Halaman Claim Free', 'claim_verify': 'Hasil Claim Free',
    'auto_kick': 'Notifikasi Auto-kick', 'other': 'Pemberitahuan Buyer',
    'buyer_admin_notice': 'Notifikasi Pesanan dari Admin',
    'buyer_manual_access': 'Akses Dikirim Manual',
}

LEGACY_SCREENS = {'buyer_admin_notice': 'admin_other', 'buyer_manual_access': 'requirements'}


def legacy_screen(screen):
    return LEGACY_SCREENS.get(screen, screen)


PLAIN_SURFACES = frozenset({
    'admin', 'admin_main', 'admin_settings', 'admin_emoji', 'admin_other',
    'admin_products', 'claim_admin', 'review_admin', 'review_post',
    'order_notification', 'delivery_alert', 'blast_admin',
})


@dataclass(frozen=True)
class Location:
    screen: str
    field: str
    role: str
    glyph: str
    action: str = ''

    @cached_property
    def key(self):
        suffix = self.action or f'{ord(self.glyph[0]):x}'
        return f'special/{self.screen}/{self.field}/{self.role}/{suffix}'

    @cached_property
    def legacy_keys(self):
        keys = (glyph_key(legacy_screen(self.screen), self.field, self.glyph),
                glyph_key('global', self.field, self.glyph))
        return (button_key(self.screen, self.action), *keys) if self.action and self.action != 'badge' else keys

    @property
    def label(self):
        field = 'Tombol' if self.field == 'button' else 'Pesan / caption'
        labels = {'join': 'Gabung Grup VIP', 'open': 'Buka Akses', 'badge': 'Ikon Produk'}
        detail = labels.get(self.action, self.glyph)
        return f'{SCREENS[self.screen]} · {field} · {detail}'


# Each entry gives a template role and the existing fallback glyph. A rule
# before the generic entry distinguishes shared glyphs by the visible prose.
TEXT = {
    'welcome': 'welcome:👋 brand:💋 benefits:✨🎥 catalog:📦 access:💎 price:💸 contact:📞',
    'help': 'help:📖❓📋 buy:🛒 contact:💬',
    'catalog': 'catalog:📦',
    'product': 'package:📦 description:🎬 price:💰 access:♾ available:🟢 unavailable:🔴 preview:👀🔍 failure:❌',
    'invoice': 'payment:📋📱 verify:✅ order:📝 expiry:⏰ waiting:⏳ price:💰 failure:❌ requirements:📌 access:🚀',
    'payment_status': 'package:📦 price:💰 order:📝 success:✅ failure:❌ waiting:⏳ expiry:⏰ warning:⚠ blocked:🚫',
    'payment_change': 'package:📦 change:🔄 price:💰 order:📝🔖 failure:❌ success:✅ warning:⚠ blocked:⛔ requirements:📌 progress:📋 cancel:⏸',
    'history': 'history:📜📋 success:✅ waiting:⏳🔄 cancel:❌ expiry:⏰ rejected:🚫',
    'access': 'success:🎉✅ access:🔓🔗 package:📌 price:💰 locked:🔒',
    'requirements': 'success:✅ package:📦 requirements:📋 locked:🔒 progress:📊🎯 waiting:⏳ failure:❌ blocked:🚫',
    'resend_menu': 'resend:🔄📦 warning:⚠ blocked:🚫',
    'resend_access': 'resend:🔄 access:🔗 success:✅ package:📌 price:💰 locked:🔒 warning:⚠ blocked:🚫',
    'buyer_reminder': 'welcome:👋 brand:🛒 reminder:✨',
    'claim_intro': 'claim:🎁📌 requirements:📋 reviews:✅ locked:🔒',
    'claim_verify': 'claim:🎁 success:✅ failure:❌ waiting:⏳',
    'auto_kick': 'locked:⚠',
    'buyer_admin_notice': 'cancel:❌ waiting:⏳ blocked:🚫',
    'buyer_manual_access': 'access:🔓🔗 package:📌 price:💰 success:✅ locked:🔒',
    'other': 'maintenance:⚙⚠ blocked:🚫 order:📝 package:📦 price:💰 waiting:⏳⏱ expiry:⏰ failure:❌',
}

TEXT_EXCEPTIONS = (
    ('other', '❌', r'PESANAN DIBATALKAN', 'cancel'),
    ('other', '❌', r'PESANAN DITOLAK', 'rejected'),
    ('buyer_admin_notice', '❌', r'PESANAN DITOLAK', 'rejected'),
    ('payment_status', '⚠', r'BOT SEDANG MAINTENANCE', 'maintenance'),
    ('requirements', '⚠', r'BOT SEDANG MAINTENANCE', 'maintenance'),
    ('resend_menu', '⚠', r'Daftar produk belum bisa dimuat', 'failure'),
    ('resend_access', '⚠', r'(?:Pilihan tidak valid|Pembelian tidak ditemukan|Link .+ belum (?:bisa dibuat|berhasil dikirim))', 'failure'),
    ('resend_access', '⚠', r'Produk sudah tidak tersedia', 'unavailable'),
    ('resend_access', '⚠', r'Akses produk belum tersedia', 'locked'),
    ('invoice', '✅', r'Nominal sudah termasuk', 'price'),
    ('invoice', '✅', r'Link produk dikirim', 'access'),
    ('payment_change', '❌', r'(?:untuk membeli|di bawah)', 'requirements'),
    ('requirements', '❌', r'(?:untuk membeli)', 'requirements'),
    ('requirements', '✅', r'Akses dikirim', 'access'),
    ('claim_intro', '✅', r'Bergabung ke Grup/Channel Backup', 'backup'),
    ('claim_verify', '🎁', r'Akses:', 'access'),
)

# Literal button labels describe URL actions and dynamic callback variants.
# Look them up only on the screens where those buttons are actually built.
BUTTON_RULES = (
    ('welcome', 'catalog', '🛒', 'Daftar Paket', 'catalog'),
    ('welcome', 'help', '📖', 'Cara Order', 'how'),
    ('welcome', 'reviews', '⭐', 'Testimoni', 'reviews'),
    ('welcome', 'history', '📜', 'Riwayat', 'history'),
    ('welcome help', 'contact', '💬', 'Hubungi Admin', 'contact'),
    ('welcome', 'group', '👥', '', 'group'),
    ('catalog product help history payment_change resend_menu', 'back', '⬅', 'Kembali|Sebelumnya|Batal', ''),
    ('catalog resend_menu', 'next', '➡', 'Berikutnya', ''),
    ('product', 'payment', '✅', 'Lanjut Bayar', 'pay'),
    ('product', 'description', '📄', 'Deskripsi lengkap', 'description'),
    ('invoice payment_change', 'payment', '🌐', 'Buka Halaman Pembayaran', 'checkout'),
    ('invoice payment_change', 'change', '🔄', 'Ganti Paket', 'change'),
    ('payment_change', 'change', '✅', 'Ya, Ganti', ''),
    ('invoice payment_change payment_status other', 'cancel', '❌', 'Batal|Batalkan', 'cancel'),
    ('payment_change requirements', 'buy', '🛒', 'Beli', ''),
    ('history', 'resend', '🔄', 'Kirim Ulang Link', ''),
    ('resend_menu', 'home', '🏠', 'Menu Utama', ''),
    ('buyer_reminder', 'catalog', '🛍', 'Lihat Katalog', ''),
    ('access resend_access buyer_manual_access', 'access', '🔗', 'GABUNG GRUP VIP', 'join'),
    ('access resend_access buyer_manual_access', 'access', '🔗', 'BUKA PRODUK', 'open'),
    ('resend_access', 'resend', '🔄', 'Pilih Produk Lain', 'again'),
    ('claim_intro claim_verify', 'reviews', '📢', 'Gabung Testimoni', 'join_review'),
    ('claim_intro claim_verify', 'backup', '🔰', 'Gabung Backup', 'join_backup'),
    ('claim_intro claim_verify', 'verify', '✅', 'Verifikasi & Claim', 'verify'),
    ('claim_verify', 'access', '🔗', 'Masuk Grup/Channel Free', 'open'),
    ('auto_kick', 'access', '🔄', 'Pulihkan Akses', ''),
)

_TEXT_LOCATIONS = {}
for _screen, _entries in TEXT.items():
    for _entry in _entries.split():
        _role, _glyphs = _entry.split(':')
        for _glyph in _glyphs:
            _TEXT_LOCATIONS.setdefault((_screen, _glyph), Location(_screen, 'text', _role, _glyph))

_TEXT_MATCHES = {}
for _screen, _glyph, _suffix, _role in TEXT_EXCEPTIONS:
    _TEXT_MATCHES.setdefault((_screen, _glyph), []).append(
        (re.compile(_suffix, re.I), Location(_screen, 'text', _role, _glyph))
    )

_BUTTON_MATCHES = {}
for _screens, _role, _glyph, _label, _action in BUTTON_RULES:
    for _screen in _screens.split():
        # Legacy actions were only installed on certain screens.
        _legacy = _action if any(role == _action for role, *_ in BUTTONS.get(_screen, ())) else ''
        _loc = Location(_screen, 'button', _role, _glyph, _legacy)
        _pattern = re.compile(rf'(?:{_label})(?:\b|$)', re.I) if _label else None
        _BUTTON_MATCHES.setdefault(_screen, []).append((_pattern, _loc))

# Product-specific selections still lead. The generic badge is a semantic
# placement of its own, using the actual product glyph as the legacy fallback.
PRODUCT_LOCATIONS = {
    (screen, field): Location(screen, field, 'package', '📦', 'badge')
    for field, screens in {
        'text': 'other product invoice payment_status payment_change history requirements',
        'button': 'catalog payment_change resend_menu',
    }.items() for screen in screens.split()
}

LOCATIONS = {role: [] for role in ROLES}
for _loc in dict.fromkeys([
    *_TEXT_LOCATIONS.values(),
    *(_loc for _matches in _TEXT_MATCHES.values() for _, _loc in _matches),
    *(_loc for _matches in _BUTTON_MATCHES.values() for _, _loc in _matches),
    *PRODUCT_LOCATIONS.values(),
]):
    LOCATIONS[_loc.role].append(_loc)
LOCATIONS = {role: tuple(locations) for role, locations in LOCATIONS.items() if locations}
ALL_LOCATIONS = tuple(loc for locations in LOCATIONS.values() for loc in locations)


def text_location(screen, glyph, text, offset):
    key = screen, glyph
    for pattern, location in _TEXT_MATCHES.get(key, ()):
        if pattern.match(re.sub(r'<[^>]*>', '', text[offset:offset + 160].split('\n', 1)[0]).lstrip()):
            return location
    return _TEXT_LOCATIONS.get(key)


def button_location(screen, label, symbol, row, button, legacy_key):
    for pattern, location in _BUTTON_MATCHES.get(screen, ()):
        if location.action and button_key(screen, location.action) == legacy_key:
            return location
        if location.role == 'group':
            if row == 3 and button.url:
                return location
        elif symbol == location.glyph and pattern and pattern.match(label):
            return location
    return None


def decode_choice(value):
    try:
        emoji_id, alt = json.loads(value)
        if re.fullmatch(r'[0-9]{1,20}', emoji_id) and isinstance(alt, str) and alt:
            return emoji_id, alt
    except (TypeError, ValueError):
        pass
    return None


def slot_migration(raw):
    """Copy each existing selection to its semantic location, if applicable."""
    valid = {key for key, value in raw.items() if decode_choice(value)}
    for location in ALL_LOCATIONS:
        if location.action == 'badge':
            continue
        for key in location.legacy_keys:
            if key in valid:
                yield location.key, raw[key]
                break
