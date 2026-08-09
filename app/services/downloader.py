"""Crash-safe, rate-limit-aware Telethon media downloader."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from telethon.errors import FloodWaitError, RPCError

from app.config import Settings
from app.database.repository import ArchiveRepository, MessageSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DownloadResult:
    completed: bool
    path: Path | None
    size: int | None
    error: str | None = None


class MediaDownloader:
    """Download one file at a time under a configurable global semaphore."""

    def __init__(self, settings: Settings, repository: ArchiveRepository) -> None:
        self.settings = settings
        self.repository = repository
        self._semaphore = asyncio.Semaphore(settings.download_concurrency)

    async def download(
        self,
        record: MessageSnapshot,
        raw_message: object,
        target: Path,
    ) -> DownloadResult:
        temp_path = target.with_name(f"{target.name}.part")
        async with self._semaphore:
            for attempt in range(1, self.settings.download_retries + 1):
                try:
                    await self.repository.mark_download_start(record.id, target)
                    await asyncio.to_thread(self._prepare_target, target, temp_path)
                    downloaded_path = await raw_message.download_media(file=str(temp_path))  # type: ignore[attr-defined]
                    if not downloaded_path or not await asyncio.to_thread(temp_path.is_file):
                        raise OSError("Telegram returned no completed media file")
                    size = await asyncio.to_thread(self._finalize, temp_path, target)
                    await self.repository.mark_download_completed(record.id, target, size)
                    return DownloadResult(True, target, size)
                except asyncio.CancelledError:
                    await self.repository.mark_download_failed(record.id, "Download interrupted")
                    raise
                except FloodWaitError as exc:
                    wait_seconds = max(1, int(exc.seconds))
                    logger.warning("Telegram FloodWait: waiting %s seconds", wait_seconds)
                    if attempt == self.settings.download_retries:
                        error = f"Telegram FloodWait ({wait_seconds}s) exhausted retry budget"
                        await self.repository.mark_download_failed(record.id, error)
                        return DownloadResult(False, None, None, error)
                    await asyncio.sleep(wait_seconds)
                except (RPCError, ConnectionError, TimeoutError, OSError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    if attempt == self.settings.download_retries:
                        await self.repository.mark_download_failed(record.id, error)
                        return DownloadResult(False, None, None, error)
                    delay = min(30, 2 ** (attempt - 1))
                    logger.warning(
                        "Download attempt %s/%s failed; retrying in %ss: %s",
                        attempt,
                        self.settings.download_retries,
                        delay,
                        error,
                    )
                    await asyncio.sleep(delay)
                except Exception as exc:
                    # Unknown failures should be recorded, not silently hidden
                    # or repeatedly hammered against Telegram.
                    error = f"{type(exc).__name__}: {exc}"
                    await self.repository.mark_download_failed(record.id, error)
                    return DownloadResult(False, None, None, error)
        return DownloadResult(False, None, None, "Download retry loop ended unexpectedly")

    @staticmethod
    def _prepare_target(target: Path, temp_path: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # A prior interrupted file is evidence of an incomplete attempt, never
        # a resumable Telethon stream.
        temp_path.unlink(missing_ok=True)

    @staticmethod
    def _finalize(temp_path: Path, target: Path) -> int:
        os.replace(temp_path, target)
        return target.stat().st_size
