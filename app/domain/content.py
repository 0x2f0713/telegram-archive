"""The Telegram content taxonomy and selection vocabulary.

``ContentType`` is the operator-facing vocabulary (what may be archived),
while ``MediaType`` in ``domain.artifacts`` is the artifact taxonomy Telegram
exposes. This module keeps the mapping between the two in one place.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.domain.artifacts import MediaType
from app.domain.messages import MessageData


class ContentType(StrEnum):
    """Canonical content categories used by CLI, web, and TUI operations."""

    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    VOICE = "voice"
    AUDIO = "audio"
    ANIMATION = "animation"
    STICKER = "sticker"
    DOCUMENT = "document"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            ContentType.TEXT: "Text & captions",
            ContentType.PHOTO: "Photos & images",
            ContentType.VIDEO: "Videos",
            ContentType.VIDEO_NOTE: "Video notes",
            ContentType.VOICE: "Voice messages",
            ContentType.AUDIO: "Audio & music",
            ContentType.ANIMATION: "Animations & GIFs",
            ContentType.STICKER: "Stickers",
            ContentType.DOCUMENT: "Documents & PDFs",
            ContentType.OTHER: "Other Telegram content",
        }[self]

    @property
    def description(self) -> str:
        return {
            ContentType.TEXT: "Archive messages containing text, including media captions.",
            ContentType.PHOTO: "Download Telegram photos and images.",
            ContentType.VIDEO: "Download standard Telegram video files.",
            ContentType.VIDEO_NOTE: "Download round video messages separately from standard videos.",
            ContentType.VOICE: "Download spoken voice-note audio.",
            ContentType.AUDIO: "Download music and attached audio.",
            ContentType.ANIMATION: "Download Telegram animations and GIF-like media.",
            ContentType.STICKER: "Download static, animated, and video sticker documents.",
            ContentType.DOCUMENT: "Download files, PDFs, archives, and other Telegram documents.",
            ContentType.OTHER: (
                "Archive metadata for polls, contacts, locations, and service media; "
                "some have no file."
            ),
        }[self]

    @property
    def downloadable(self) -> bool:
        return self in MEDIA_CONTENT_TYPES


@dataclass(frozen=True, slots=True)
class ContentTypeOption:
    """One stable option presented by CLI and web content pickers."""

    key: ContentType
    label: str
    description: str
    downloadable: bool


CONTENT_TYPE_ORDER = tuple(ContentType)
ALL_CONTENT_TYPES = frozenset(ContentType)
MEDIA_CONTENT_TYPES = frozenset(
    content_type
    for content_type in ContentType
    if content_type
    in {
        ContentType.PHOTO,
        ContentType.VIDEO,
        ContentType.VIDEO_NOTE,
        ContentType.VOICE,
        ContentType.AUDIO,
        ContentType.ANIMATION,
        ContentType.STICKER,
        ContentType.DOCUMENT,
    }
)

CONTENT_TYPE_OPTIONS = tuple(
    ContentTypeOption(
        key=content_type,
        label=content_type.label,
        description=content_type.description,
        downloadable=content_type.downloadable,
    )
    for content_type in CONTENT_TYPE_ORDER
)

MEDIA_TYPE_TO_CONTENT = {
    MediaType.PHOTO: ContentType.PHOTO,
    MediaType.VIDEO: ContentType.VIDEO,
    MediaType.VIDEO_NOTE: ContentType.VIDEO_NOTE,
    MediaType.VOICE: ContentType.VOICE,
    MediaType.AUDIO: ContentType.AUDIO,
    MediaType.ANIMATION: ContentType.ANIMATION,
    MediaType.STICKER: ContentType.STICKER,
    MediaType.DOCUMENT: ContentType.DOCUMENT,
}
CONTENT_TO_MEDIA = {value: key for key, value in MEDIA_TYPE_TO_CONTENT.items()}

_ALIASES = {
    "image": ContentType.PHOTO,
    "images": ContentType.PHOTO,
    "photos": ContentType.PHOTO,
    "videos": ContentType.VIDEO,
    "round_video": ContentType.VIDEO_NOTE,
    "round_videos": ContentType.VIDEO_NOTE,
    "video_message": ContentType.VIDEO_NOTE,
    "video_messages": ContentType.VIDEO_NOTE,
    "voice_message": ContentType.VOICE,
    "voice_messages": ContentType.VOICE,
    "music": ContentType.AUDIO,
    "gif": ContentType.ANIMATION,
    "gifs": ContentType.ANIMATION,
    "animations": ContentType.ANIMATION,
    "stickers": ContentType.STICKER,
    "file": ContentType.DOCUMENT,
    "files": ContentType.DOCUMENT,
    "documents": ContentType.DOCUMENT,
    "pdf": ContentType.DOCUMENT,
    "pdfs": ContentType.DOCUMENT,
    "poll": ContentType.OTHER,
    "polls": ContentType.OTHER,
    "contact": ContentType.OTHER,
    "contacts": ContentType.OTHER,
    "location": ContentType.OTHER,
    "locations": ContentType.OTHER,
    "service": ContentType.OTHER,
}


class ContentTypeSelectionError(ValueError):
    """Raised when an operator supplies an unknown or empty content selection."""


def normalize_content_types(values: Iterable[str]) -> frozenset[ContentType]:
    """Validate aliases and return canonical content-type keys."""

    selected: set[ContentType] = set()
    unknown: list[str] = []
    for raw_value in values:
        for raw_part in raw_value.split(","):
            part = raw_part.strip().casefold().replace("-", "_").replace(" ", "_")
            if not part:
                continue
            canonical = _ALIASES.get(part)
            if canonical is None:
                try:
                    canonical = ContentType(part)
                except ValueError:
                    unknown.append(raw_part.strip())
                    continue
            selected.add(canonical)
    if unknown:
        supported = ", ".join(content_type.value for content_type in CONTENT_TYPE_ORDER)
        raise ContentTypeSelectionError(
            f"Unknown content type {unknown[0]!r}. Supported types: {supported}"
        )
    if not selected:
        raise ContentTypeSelectionError("Select at least one Telegram content type")
    return frozenset(selected)


def canonical_content_type_list(values: Iterable[ContentType]) -> list[str]:
    """Return selected keys in stable UI/documentation order."""

    selected = frozenset(values)
    return [content_type.value for content_type in CONTENT_TYPE_ORDER if content_type in selected]


def classify_content(text: str | None, media_type: MediaType | None) -> frozenset[ContentType]:
    """Classify every relevant facet of one message into content categories."""

    types: set[ContentType] = set()
    if (text or "").strip():
        types.add(ContentType.TEXT)
    if media_type in MEDIA_TYPE_TO_CONTENT:
        types.add(MEDIA_TYPE_TO_CONTENT[media_type])
    elif media_type == MediaType.UNSUPPORTED:
        types.add(ContentType.OTHER)
    if not types:
        # Telegram service events may have neither text nor downloadable media.
        types.add(ContentType.OTHER)
    return frozenset(types)


def message_content_types(data: MessageData) -> frozenset[ContentType]:
    """Classify one persisted-domain message into content categories."""

    media_type = data.artifact.media_type if data.artifact else None
    return classify_content(data.text, media_type)
