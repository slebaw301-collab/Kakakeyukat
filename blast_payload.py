"""Payload and Telegram delivery for durable broadcasts; no database or network on import."""
from urllib.parse import urlsplit
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest, Forbidden
from premium_emoji_slots import emoji_scope

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

# Telegram offsets use UTF-16 units; retain the original text verbatim rather than
# trimming, escaping, or recomputing offsets in Python.
_NATIVE_FORMATS = frozenset({
    "bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre",
    "text_link", "text_mention", "custom_emoji", "blockquote", "expandable_blockquote",
})
_CUSTOM_EMOJI_REJECTION = re.compile(
    r"custom[\\s_-]*emoji|emoji[-_ ]id|tg-emoji|emoji.{0,28}premium|premium.{0,28}emoji",
    re.IGNORECASE,
)

def capture_blast_body(message, payload, legacy_text=""):
    """Capture native formatting in JSON-safe form, or retain the old HTML editor."""
    media = payload.get("kind", "text") != "text"
    raw = (getattr(message, "caption", None) if media else getattr(message, "text", None)) or ""
    entities = getattr(message, "caption_entities" if media else "entities", None) or ()
    native = [entity.to_dict() for entity in entities if entity.type in _NATIVE_FORMATS]
    payload.pop("entities", None)  # Retyping must never retain offsets from a previous body.
    if native:
        payload["entities"] = native
        return raw
    return raw if media else legacy_text

def custom_emoji_count(entities):
    return sum(1 for entity in (entities or ()) if
               (entity.get("type") if isinstance(entity, dict) else entity.type) == "custom_emoji")

async def send_blast_payload(bot, chat_id, body, payload=None, **options):
    payload = payload or {}
    kwargs = dict(chat_id=chat_id, parse_mode="HTML", **options)
    markup = build_markup(payload)
    if markup is not None:
        kwargs["reply_markup"] = markup
    kind = payload.get("kind", "text")
    if kind == "photo":
        send = bot.send_photo
        kwargs.update(photo=payload["file_id"], caption=body or None)
    elif kind == "video":
        send = bot.send_video
        kwargs.update(video=payload["file_id"], caption=body or None)
    elif kind == "text":
        send = bot.send_message
        kwargs["text"] = body
    else:
        raise ValueError("Unsupported blast media")

    # Old persisted jobs (and manually entered HTML) retain their original path.
    if "entities" not in payload:
        return await send(**kwargs)

    entities = payload["entities"]
    field = "entities" if kind == "text" else "caption_entities"
    kwargs["parse_mode"] = None
    kwargs[field] = [MessageEntity.de_json(entity, None) for entity in entities]
    # Preserve explicit keyboard emoji and native formatting as authored. Do not
    # let automatic substitutions or the engine's repair change this payload.
    with emoji_scope("blast_admin"):
        try:
            return await send(**kwargs)
        except (BadRequest, Forbidden) as exc:
            if not custom_emoji_count(entities) or not _CUSTOM_EMOJI_REJECTION.search(str(exc)):
                raise
            # Only retry an explicitly rejected Telegram request. Never retry
            # uncertain network outcomes, 429s, or unrelated permission failures.
            kwargs[field] = [MessageEntity.de_json(entity, None) for entity in entities
                             if entity["type"] != "custom_emoji"]
            return await send(**kwargs)
