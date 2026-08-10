"""The media artifact carried by an archived message.

A Telegram message may carry zero or one downloadable media artifact. The
artifact has its own lifecycle (``DownloadState``) independent of the message
metadata, which is what makes the archive crash-safe and resumable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MediaType(StrEnum):
    """Canonical Telegram media taxonomy used everywhere in the domain."""

    PHOTO = "photo"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    VOICE = "voice"
    AUDIO = "audio"
    ANIMATION = "animation"
    STICKER = "sticker"
    DOCUMENT = "document"
    UNSUPPORTED = "unsupported"

    @property
    def display_name(self) -> str:
        return self.value.replace("_", " ").title()


class DownloadState(StrEnum):
    """Lifecycle of one media artifact.

    ``NOT_APPLICABLE`` is not really a download state: it marks a message
    that carries no artifact at all. It is kept as a state so the persistence
    column can store one value per message.
    """

    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_APPLICABLE = "not_applicable"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DownloadState.COMPLETED,
            DownloadState.FAILED,
            DownloadState.SKIPPED,
            DownloadState.NOT_APPLICABLE,
        }

    @property
    def attention_priority(self) -> int:
        """Lower values are more urgent in the operator attention queue."""
        return {
            DownloadState.FAILED: 0,
            DownloadState.DOWNLOADING: 1,
            DownloadState.PENDING: 2,
        }.get(self, 9)


@dataclass(frozen=True, slots=True)
class MediaArtifact:
    """Metadata describing a downloadable media artifact on a message.

    Deliberately free of Telethon objects: the raw message stays at the
    infrastructure boundary and is passed to the download adapter directly.
    """

    media_type: MediaType | None
    media_size: int | None
    telegram_document_id: int | None
    mime_type: str | None
    original_filename: str | None
    extension: str = ""
