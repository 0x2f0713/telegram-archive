"""Delete one local chat archive while preserving Telegram coverage state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.application.archive_records import (
    ChatArchiveDeletionResult,
    ChatArchiveDeletionTarget,
)

logger = logging.getLogger(__name__)


class ChatArchiveDeletionStore(Protocol):
    async def delete_chat_archive(self, telegram_chat_id: int) -> ChatArchiveDeletionTarget | None:
        """Delete stored messages and return unshared media paths for cleanup."""


@dataclass(frozen=True, slots=True)
class _FileCleanup:
    deleted: int = 0
    bytes_deleted: int = 0
    missing: int = 0
    skipped: int = 0
    failed: int = 0


def _inside_root(root: Path, candidate: Path) -> bool:
    return candidate != root and root in candidate.parents


def _remove_empty_parents(root: Path, start: Path) -> None:
    parent = start
    while parent != root and _inside_root(root, parent):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _remove_owned_media(download_dir: Path, media_paths: Sequence[str]) -> _FileCleanup:
    root = download_dir.expanduser().resolve()
    deleted = 0
    bytes_deleted = 0
    missing = 0
    skipped = 0
    failed = 0

    for raw_path in media_paths:
        recorded_path = Path(raw_path).expanduser()
        try:
            target = recorded_path.resolve()
        except OSError:
            skipped += 1
            continue
        if not _inside_root(root, target):
            skipped += 1
            continue

        candidates = (target, target.with_name(f"{target.name}.part"))
        found = False
        for candidate in candidates:
            try:
                if not candidate.exists():
                    continue
                found = True
                if not candidate.is_file():
                    failed += 1
                    continue
                size = candidate.stat().st_size
                candidate.unlink()
                deleted += 1
                bytes_deleted += size
            except OSError:
                failed += 1
        if not found:
            missing += 1
        _remove_empty_parents(root, target.parent)

    return _FileCleanup(deleted, bytes_deleted, missing, skipped, failed)


class ChatArchiveDeletionService:
    """Remove archived messages and app-owned files without changing coverage."""

    def __init__(self, store: ChatArchiveDeletionStore) -> None:
        self.store = store

    async def delete(
        self,
        telegram_chat_id: int,
        download_dir: Path,
    ) -> ChatArchiveDeletionResult | None:
        target = await self.store.delete_chat_archive(telegram_chat_id)
        if target is None:
            return None
        cleanup = await asyncio.to_thread(
            _remove_owned_media,
            download_dir,
            target.media_paths,
        )
        if cleanup.failed or cleanup.skipped:
            logger.warning(
                "Chat %s archive deleted with media cleanup warnings: %s failed, %s skipped",
                telegram_chat_id,
                cleanup.failed,
                cleanup.skipped,
            )
        return ChatArchiveDeletionResult(
            telegram_chat_id=target.telegram_chat_id,
            title=target.title,
            messages_deleted=target.message_count,
            files_deleted=cleanup.deleted,
            bytes_deleted=cleanup.bytes_deleted,
            files_missing=cleanup.missing,
            files_skipped=cleanup.skipped,
            files_failed=cleanup.failed,
        )
