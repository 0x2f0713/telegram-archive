"""Canonical Telegram content categories used by CLI and web operations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.telegram.entities import classify_media


class ContentTypeSelectionError(ValueError):
    """Raised when an operator supplies an unknown or empty content selection."""


@dataclass(frozen=True, slots=True)
class ContentTypeOption:
    key: str
    label: str
    description: str
    downloadable: bool


CONTENT_TYPE_OPTIONS = (
    ContentTypeOption(
        "text",
        "Text & captions",
        "Archive messages containing text, including media captions.",
        False,
    ),
    ContentTypeOption("photo", "Photos & images", "Download Telegram photos and images.", True),
    ContentTypeOption("video", "Videos", "Download standard Telegram video files.", True),
    ContentTypeOption(
        "video_note",
        "Video notes",
        "Download round video messages separately from standard videos.",
        True,
    ),
    ContentTypeOption("voice", "Voice messages", "Download spoken voice-note audio.", True),
    ContentTypeOption("audio", "Audio & music", "Download music and attached audio.", True),
    ContentTypeOption(
        "animation",
        "Animations & GIFs",
        "Download Telegram animations and GIF-like media.",
        True,
    ),
    ContentTypeOption(
        "sticker",
        "Stickers",
        "Download static, animated, and video sticker documents.",
        True,
    ),
    ContentTypeOption(
        "document",
        "Documents & PDFs",
        "Download files, PDFs, archives, and other Telegram documents.",
        True,
    ),
    ContentTypeOption(
        "other",
        "Other Telegram content",
        "Archive metadata for polls, contacts, locations, and service media; some have no file.",
        False,
    ),
)
CONTENT_TYPE_KEYS = tuple(option.key for option in CONTENT_TYPE_OPTIONS)
ALL_CONTENT_TYPES = frozenset(CONTENT_TYPE_KEYS)
MEDIA_CONTENT_TYPES = frozenset(
    option.key for option in CONTENT_TYPE_OPTIONS if option.downloadable
)

_ALIASES = {
    "image": "photo",
    "images": "photo",
    "photos": "photo",
    "videos": "video",
    "round_video": "video_note",
    "round_videos": "video_note",
    "video_message": "video_note",
    "video_messages": "video_note",
    "voice_message": "voice",
    "voice_messages": "voice",
    "music": "audio",
    "gif": "animation",
    "gifs": "animation",
    "animations": "animation",
    "stickers": "sticker",
    "file": "document",
    "files": "document",
    "documents": "document",
    "pdf": "document",
    "pdfs": "document",
    "poll": "other",
    "polls": "other",
    "contact": "other",
    "contacts": "other",
    "location": "other",
    "locations": "other",
    "service": "other",
}


def normalize_content_types(values: Iterable[str]) -> frozenset[str]:
    """Validate aliases and return canonical content-type keys."""

    selected: set[str] = set()
    unknown: list[str] = []
    for raw_value in values:
        for raw_part in raw_value.split(","):
            part = raw_part.strip().casefold().replace("-", "_").replace(" ", "_")
            if not part:
                continue
            canonical = _ALIASES.get(part, part)
            if canonical not in ALL_CONTENT_TYPES:
                unknown.append(raw_part.strip())
            else:
                selected.add(canonical)
    if unknown:
        supported = ", ".join(CONTENT_TYPE_KEYS)
        raise ContentTypeSelectionError(
            f"Unknown content type {unknown[0]!r}. Supported types: {supported}"
        )
    if not selected:
        raise ContentTypeSelectionError("Select at least one Telegram content type")
    return frozenset(selected)


def canonical_content_type_list(values: Iterable[str]) -> list[str]:
    """Return selected keys in stable UI/documentation order."""

    selected = frozenset(values)
    return [key for key in CONTENT_TYPE_KEYS if key in selected]


def message_content_types(message: Any) -> frozenset[str]:
    """Classify every relevant facet of one raw Telethon message."""

    types: set[str] = set()
    if str(getattr(message, "message", "") or "").strip():
        types.add("text")
    media_type = classify_media(message)
    if media_type in MEDIA_CONTENT_TYPES:
        types.add(media_type)
    elif media_type == "unsupported":
        types.add("other")
    if not types:
        # Telegram service events may have neither text nor downloadable media.
        types.add("other")
    return frozenset(types)
