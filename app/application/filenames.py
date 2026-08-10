"""Portable, traversal-safe archive path generation."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from app.domain import ChatInfo, MessageData

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate without splitting a Unicode code point or exceeding FS bytes."""

    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def sanitize_filename(value: str, *, replacement: str = "_", max_length: int = 180) -> str:
    """Return one safe portable path component."""

    value = unicodedata.normalize("NFKC", value)
    value = _INVALID_CHARS.sub(replacement, value)
    value = _WHITESPACE.sub(" ", value).strip(" .")
    value = re.sub(r"_+", "_", value)
    if not value:
        value = "unnamed"

    stem, suffix = Path(value).stem.rstrip(" ."), Path(value).suffix
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    # Linux commonly limits a component to 255 bytes. A conservative byte
    # ceiling also remains valid on Windows and macOS and handles emoji safely.
    suffix = _truncate_utf8(suffix, min(32, max(0, max_length - 1)))
    available = max(1, max_length - len(suffix.encode("utf-8")))
    stem = _truncate_utf8(stem, available).rstrip(" .") or "unnamed"
    return f"{stem}{suffix}"


def media_filename(message: MessageData) -> str:
    if message.original_filename:
        original = sanitize_filename(Path(message.original_filename).name)
        return sanitize_filename(f"{message.telegram_message_id}_{original}")
    extension = message.extension if message.extension.startswith(".") else f".{message.extension}"
    if extension == ".":
        extension = {
            "photo": ".jpg",
            "video": ".mp4",
            "video_note": ".mp4",
            "animation": ".mp4",
            "voice": ".ogg",
            "audio": ".mp3",
            "sticker": ".webp",
        }.get(message.media_type or "", "")
    return sanitize_filename(
        f"{message.telegram_message_id}_{message.media_type or 'media'}{extension}"
    )


def output_path(base_dir: Path, chat: ChatInfo, message: MessageData) -> Path:
    date = message.message_date
    chat_name = sanitize_filename(chat.title, max_length=100)
    chat_directory = sanitize_filename(f"{chat.telegram_chat_id}_{chat_name}", max_length=140)
    relative = Path(chat_directory) / f"{date.year:04d}" / f"{date.month:02d}" / f"{date.day:02d}"
    target = base_dir / relative / media_filename(message)

    # All components above are sanitized, but retain an explicit containment
    # assertion as defense in depth if this function evolves.
    root = base_dir.expanduser().resolve()
    resolved = target.expanduser().resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("Generated media path escapes DOWNLOAD_DIR")
    return target
