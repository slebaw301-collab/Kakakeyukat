"""Payload and Telegram delivery for durable broadcasts; no database or network on import."""
from urllib.parse import urlsplit
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

MAX_BUTTONS = 10
STYLES = {"default": None, "biru": "primary", "hijau": "success", "merah": "danger",
          "primary": "primary", "success": "success", "danger": "danger"}

def parse_buttons(text):
    """One line: label | URL | optional colour | optional custom emoji ID."""
    if text.strip() == "-":
        return []
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    if not 1 <= len(rows) <= MAX_BUTTONS:
        raise ValueError("Masukkan 1–10 tombol, atau - untuk menghapus semuanya.")
    buttons = []
    for row in rows:
        parts = [part.strip() for part in row.split("|")]
        if not 2 <= len(parts) <= 4:
            raise ValueError("Format: Nama | URL | warna (opsional) | ID emoji (opsional).")
        label, url = parts[:2]
        colour = (parts[2] or "default").lower() if len(parts) >= 3 else "default"
        emoji = parts[3] if len(parts) == 4 else ""
        if url.startswith("@"):
            url = "https://t.me/" + url[1:]
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ("http", "https", "tg") and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not label or len(label) > 64 or not valid or len(url) > 512 or any(ch.isspace() for ch in url):
            raise ValueError("Nama wajib 1–64 karakter dan URL Telegram/HTTP(S) harus valid.")
        if colour not in STYLES:
            raise ValueError("Warna: default, biru, hijau, atau merah.")
        if emoji and (not emoji.isascii() or not emoji.isdigit() or len(emoji) > 20):
            raise ValueError("ID custom emoji harus berupa angka.")
        buttons.append(dict(label=label, url=url, colour=colour, emoji=emoji))
    return buttons

def button_lines(buttons):
    return "\n".join(" | ".join([b["label"], b["url"], b.get("colour", "default"), b.get("emoji", "")]).rstrip(" |")
                     for b in buttons)

def build_markup(payload):
    buttons = []
    for b in (payload or {}).get("buttons", []):
        extra = {}
        if STYLES[b.get("colour", "default")]:
            extra["style"] = STYLES[b["colour"]]
        if b.get("emoji"):
            extra["icon_custom_emoji_id"] = b["emoji"]
        buttons.append(InlineKeyboardButton(b["label"], url=b["url"], api_kwargs=extra or None))
    columns = 2 if (payload or {}).get("columns") == 2 else 1
    return InlineKeyboardMarkup([buttons[i:i+columns] for i in range(0, len(buttons), columns)]) if buttons else None

async def send_blast_payload(bot, chat_id, body, payload=None, **options):
    payload = payload or {}
    kwargs = dict(chat_id=chat_id, parse_mode="HTML", **options)
    markup = build_markup(payload)
    if markup is not None:
        kwargs["reply_markup"] = markup
    kind = payload.get("kind", "text")
    if kind == "photo":
        return await bot.send_photo(photo=payload["file_id"], caption=body or None, **kwargs)
    if kind == "video":
        return await bot.send_video(video=payload["file_id"], caption=body or None, **kwargs)
    if kind != "text":
        raise ValueError("Unsupported blast media")
    return await bot.send_message(text=body, **kwargs)
