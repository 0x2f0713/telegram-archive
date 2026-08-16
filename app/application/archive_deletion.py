"""Delete one local chat archive while preserving Telegram coverage state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.application.archive_records import (
    ChatArchiveDeletionResult,
    ChatArchiveDeletionTarget,
)

logger = logging.getLogger(__name__)

#: Deletes one archived file served from a non-download root (e.g. the
#: read-only TeraBox FUSE mount). Returns True when the file is gone.
MediaFileDeleter = Callable[[Path], Awaitable[bool]]


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


def _stat_size(target: Path) -> int:
    try:
        return target.stat().st_size if target.is_file() else 0
    except OSError:
        return 0


def _remove_owned_media(
    download_dir: Path,
    media_paths: Sequence[str],
    *,
    ignore_remote: bool = False,
) -> _FileCleanup:
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
            if ignore_remote:
                # Remote-deleter pass counts these; skip silently here.
                continue
            skipped += 1
            continue
        if not _inside_root(root, target):
            if ignore_remote:
                # Remote-deleter pass owns paths outside DOWNLOAD_DIR; skip
                # silently so they are not double-counted.
                continue
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


def _plan_remote_targets(
    media_paths: Sequence[str], download_dir: Path
) -> tuple[list[tuple[str, Path | None]], int]:
    """Resolve each media path for remote cleanup without blocking the loop.

    Returns ``([(raw_path, resolved_or_None)...], skipped_count)``. Paths
    inside the download buffer are deferred to the local cleanup pass, so
    those entries carry a None target and are skipped here.
    """

    root = download_dir.expanduser().resolve()
    planned: list[tuple[str, Path | None]] = []
    skipped = 0
    for raw_path in media_paths:
        recorded_path = Path(raw_path).expanduser()
        try:
            target = recorded_path.resolve()
        except OSError:
            skipped += 1
            continue
        if _inside_root(root, target) or target == root:
            planned.append((raw_path, None))
            continue
        planned.append((raw_path, target))
    return planned, skipped


async def _remove_remote_media(
    media_paths: Sequence[str],
    download_dir: Path,
    remove_remote: MediaFileDeleter,
) -> _FileCleanup:
    """Delete archived files that live outside DOWNLOAD_DIR (e.g. TeraBox mount).

    Files inside the download buffer are left for the local cleanup pass.
    """

    planned, skipped = await asyncio.to_thread(_plan_remote_targets, media_paths, download_dir)
    deleted = 0
    bytes_deleted = 0
    missing = 0
    failed = 0

    for raw_path, target in planned:
        if target is None:
            continue
        size = await asyncio.to_thread(_stat_size, target)
        try:
            gone = await remove_remote(target)
        except Exception:
            logger.exception("Remote media deletion failed for %s", raw_path)
            failed += 1
            continue
        if gone:
            deleted += 1
            bytes_deleted += size
        elif await asyncio.to_thread(target.is_file):
            failed += 1
        else:
            missing += 1

    return _FileCleanup(deleted, bytes_deleted, missing, skipped, failed)


class ChatArchiveDeletionService:
    """Remove archived messages and app-owned files without changing coverage."""

    def __init__(self, store: ChatArchiveDeletionStore) -> None:
        self.store = store

    async def delete(
        self,
        telegram_chat_id: int,
        download_dir: Path,
        remove_remote: MediaFileDeleter | None = None,
    ) -> ChatArchiveDeletionResult | None:
        target = await self.store.delete_chat_archive(telegram_chat_id)
        if target is None:
            return None
        cleanup = await asyncio.to_thread(
            _remove_owned_media,
            download_dir,
            target.media_paths,
            ignore_remote=remove_remote is not None,
        )
        if remove_remote is not None:
            remote_cleanup = await _remove_remote_media(
                target.media_paths, download_dir, remove_remote
            )
            # The local pass already counted unresolvable paths as skipped;
            # avoid double-counting them here.
            cleanup = _FileCleanup(
                deleted=cleanup.deleted + remote_cleanup.deleted,
                bytes_deleted=cleanup.bytes_deleted + remote_cleanup.bytes_deleted,
                missing=cleanup.missing + remote_cleanup.missing,
                skipped=cleanup.skipped,
                failed=cleanup.failed + remote_cleanup.failed,
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
