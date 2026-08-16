"""Crash-safe, rate-limit-aware Telethon media downloader."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from telethon.errors import FloodWaitError, RPCError

from app.application.archive_records import DownloadResult, MessageSnapshot
from app.config import Settings
from app.infrastructure.ffmpeg import (
    FfmpegCapabilities,
    extract_poster,
    probe_capabilities,
    remux_faststart,
)
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.terabox import TeraBoxError, UploadReceipt
from app.infrastructure.transcode import POSTER_SUFFIX

logger = logging.getLogger(__name__)
DownloadProgressCallback = Callable[[int, int], None]

_VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi"})


class MediaUploader(Protocol):
    """Publishes a finalized buffer file to the remote archive storage."""

    async def upload(
        self, target: Path, progress: DownloadProgressCallback | None = None
    ) -> UploadReceipt: ...


class MediaDownloader:
    """Download one file at a time under a configurable global semaphore."""

    def __init__(
        self,
        settings: Settings,
        repository: ArchiveRepository,
        uploader: MediaUploader | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.uploader = uploader
        self._semaphore = asyncio.Semaphore(settings.download_concurrency)
        self._capabilities: FfmpegCapabilities | None = None

    async def _ffmpeg(self) -> FfmpegCapabilities:
        if self._capabilities is None:
            self._capabilities = await probe_capabilities(self.settings)
        return self._capabilities

    async def _optimize(self, target: Path) -> None:
        """Remux for instant playback and cache a poster frame when enabled."""
        capabilities = await self._ffmpeg()
        if not capabilities.available:
            return
        if self.settings.media_faststart:
            await remux_faststart(self.settings, capabilities, target)
        if self.settings.media_variants and target.suffix.casefold() in _VIDEO_SUFFIXES:
            poster = target.with_name(f"{target.stem}{POSTER_SUFFIX}")
            await extract_poster(self.settings, capabilities, target, poster)

    async def download(
        self,
        record: MessageSnapshot,
        raw_message: object,
        target: Path,
        progress: DownloadProgressCallback | None = None,
    ) -> DownloadResult:
        temp_path = target.with_name(f"{target.name}.part")
        async with self._semaphore:
            for attempt in range(1, self.settings.download_retries + 1):
                try:
                    await self.repository.mark_download_start(record.id, target)
                    await asyncio.to_thread(self._prepare_target, target, temp_path)
                    download_kwargs = {"file": str(temp_path)}
                    if progress is not None:
                        download_kwargs["progress_callback"] = progress
                    downloaded_path = await raw_message.download_media(  # type: ignore[attr-defined]
                        **download_kwargs
                    )
                    if not downloaded_path or not await asyncio.to_thread(temp_path.is_file):
                        raise OSError("Telegram returned no completed media file")
                    size = await asyncio.to_thread(self._finalize, temp_path, target)
                    if self.uploader is not None:
                        return await self._publish_to_uploader(record.id, target, progress)
                    await self._optimize(target)
                    size = await asyncio.to_thread(self._current_size, target)
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

    @staticmethod
    def _current_size(target: Path) -> int:
        return target.stat().st_size

    async def _publish_to_uploader(
        self,
        message_id: int,
        target: Path,
        progress: DownloadProgressCallback | None,
    ) -> DownloadResult:
        """Upload a just-finalized file; on failure keep the buffer file.

        The file stays in DOWNLOAD_DIR so retry-failed re-uploads it without
        re-downloading from Telegram.
        """
        assert self.uploader is not None
        try:
            receipt = await self.uploader.upload(target, progress)
        except asyncio.CancelledError:
            await self.repository.mark_download_failed(message_id, "Upload interrupted")
            raise
        except TeraBoxError as exc:
            error = f"TeraBox upload failed: {exc}"
            await self.repository.mark_download_failed(message_id, error)
            return DownloadResult(False, target, None, error)
        logger.info(
            "Uploaded %s to TeraBox (%s bytes, md5=%s)",
            receipt.remote_path,
            receipt.size,
            receipt.md5,
        )
        await asyncio.to_thread(target.unlink, True)
        await self.repository.mark_download_completed(message_id, receipt.mount_path, receipt.size)
        return DownloadResult(True, receipt.mount_path, receipt.size)

    async def publish_buffered(
        self,
        message_id: int,
        buffered_path: Path,
        progress: DownloadProgressCallback | None = None,
    ) -> DownloadResult | None:
        """Publish an existing DOWNLOAD_DIR file to remote storage.

        Returns None when no uploader is configured (local storage mode).
        """
        if self.uploader is None:
            return None
        await self.repository.mark_download_start(message_id, buffered_path)
        return await self._publish_to_uploader(message_id, buffered_path, progress)
