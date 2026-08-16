"""Application-owned records exchanged with archive persistence adapters.

These immutable records describe what the archive use cases need from storage.
They intentionally contain no SQLAlchemy models or database/session concepts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MessageSnapshot:
    id: int
    telegram_chat_id: int
    telegram_message_id: int
    has_media: bool
    media_path: str | None
    media_size: int | None
    download_status: str
    download_attempts: int


@dataclass(frozen=True, slots=True)
class RetryCandidate:
    id: int
    telegram_chat_id: int
    telegram_message_id: int
    media_path: str | None
    download_status: str
    media_type: str | None


@dataclass(frozen=True, slots=True)
class ChatNewest:
    telegram_chat_id: int
    title: str
    message_id: int | None
    message_date: datetime | None


@dataclass(frozen=True, slots=True)
class ArchiveStats:
    total_messages: int
    downloaded_files: int
    downloaded_bytes: int
    failed_downloads: int
    skipped_downloads: int
    newest_by_chat: tuple[ChatNewest, ...]


@dataclass(frozen=True, slots=True)
class ChatArchiveDeletionTarget:
    telegram_chat_id: int
    title: str
    message_count: int
    media_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChatArchiveDeletionResult:
    telegram_chat_id: int
    title: str
    messages_deleted: int
    files_deleted: int
    bytes_deleted: int
    files_missing: int
    files_skipped: int
    files_failed: int

    @property
    def cleanup_complete(self) -> bool:
        return self.files_skipped == 0 and self.files_failed == 0


@dataclass(frozen=True, slots=True)
class DownloadResult:
    completed: bool
    path: Path | None
    size: int | None
    error: str | None = None
