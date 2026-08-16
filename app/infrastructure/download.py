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
    extract_thumbnail,
    probe_capabilities,
    probe_video_codec,
    remux_faststart,
    transcode_hevc_to_h264,
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

    async def _optimize(self, target: Path, record: MessageSnapshot | None = None) -> Path | None:
        """Remux for instant playback, transcode HEVC, and cache a poster frame when enabled.

        In TeraBox mode: faststart remux + HEVC→H.264 transcode + poster to local thumbnail cache.
        Returns the path to the H.264 variant if transcoded, else None.
        In local mode: faststart remux + poster next to video file.
        """
        capabilities = await self._ffmpeg()
        if not capabilities.available:
            return None
        if self.settings.media_faststart:
            await remux_faststart(self.settings, capabilities, target)

        is_terabox = self.uploader is not None
        variant_path: Path | None = None

        if target.suffix.casefold() in _VIDEO_SUFFIXES:
            # HEVC to H.264 transcode in TeraBox mode
            if (
                is_terabox
                and self.settings.terabox_transcode_hevc
                and capabilities.can_transcode_hevc
            ):
                codec = await probe_video_codec(self.settings, capabilities, target)
                if codec == "hevc":
                    variant_path = target.with_name(f"{target.stem}.h264.mp4")
                    await transcode_hevc_to_h264(self.settings, capabilities, target, variant_path)

            # Poster generation
            if is_terabox and self.settings.terabox_generate_posters:
                if self.settings.thumbnail_cache_dir and record is not None:
                    await self._generate_poster(record, target, capabilities)
            elif self.settings.media_variants:
                poster = target.with_name(f"{target.stem}{POSTER_SUFFIX}")
                await extract_poster(self.settings, capabilities, target, poster)

        return variant_path

    async def _generate_poster(
        self, record: MessageSnapshot, source: Path, capabilities: FfmpegCapabilities
    ) -> None:
        """Generate a WebP poster frame for a video in TeraBox mode."""
        if not self.settings.thumbnail_cache_dir:
            return
        poster_dir = self.settings.thumbnail_cache_dir.expanduser().resolve() / str(
            record.telegram_chat_id
        )
        try:
            poster_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create poster dir %s: %s", poster_dir, exc)
            return
        poster_path = poster_dir / f"{record.id}.poster.webp"
        if await asyncio.to_thread(poster_path.is_file):
            return  # Already exists
        try:
            # Extract frame at 1s, scale to max 480px width, WebP quality 75
            await extract_thumbnail(
                self.settings,
                capabilities,
                source,
                poster_path,
                480,  # max dimension for poster
                75,
            )
        except Exception as exc:
            logger.warning("Poster generation failed for %s: %s", source, exc)

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
                    # Optimize (faststart, poster, HEVC transcode) BEFORE upload in TeraBox mode
                    # so the optimized file gets uploaded
                    variant_path = await self._optimize(target, record)
                    if self.uploader is not None:
                        return await self._publish_to_uploader(
                            record, target, progress, variant_path
                        )
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
        record: MessageSnapshot,
        target: Path,
        progress: DownloadProgressCallback | None,
        variant_path: Path | None = None,
    ) -> DownloadResult:
        """Upload a just-finalized file; on failure keep the buffer file.

        The file stays in DOWNLOAD_DIR so retry-failed re-uploads it without
        re-downloading from Telegram.
        """
        assert self.uploader is not None
        try:
            receipt = await self.uploader.upload(target, progress)
        except asyncio.CancelledError:
            await self.repository.mark_download_failed(record.id, "Upload interrupted")
            raise
        except TeraBoxError as exc:
            error = f"TeraBox upload failed: {exc}"
            await self.repository.mark_download_failed(record.id, error)
            return DownloadResult(False, target, None, error)
        logger.info(
            "Uploaded %s to TeraBox (%s bytes, md5=%s)",
            receipt.remote_path,
            receipt.size,
            receipt.md5,
        )
        variant_mount_path: str | None = None
        if variant_path is not None and self.settings.terabox_store_both:
            try:
                variant_receipt = await self.uploader.upload(variant_path, progress)
                variant_mount_path = variant_receipt.mount_path
                logger.info(
                    "Uploaded H.264 variant %s to TeraBox (%s bytes, md5=%s)",
                    variant_receipt.remote_path,
                    variant_receipt.size,
                    variant_receipt.md5,
                )
            except Exception as exc:
                logger.warning("Failed to upload H.264 variant: %s", exc)
        await self.repository.mark_download_completed(
            record.id, receipt.mount_path, receipt.size, variant_mount_path
        )
        # Generate local thumbnail for fast gallery loading in TeraBox mode
        if self.settings.thumbnail_cache_dir:
            await self._generate_thumbnail(record, target)
        try:
            await asyncio.to_thread(target.unlink, True)
        except OSError as exc:
            # The record is already completed; a leftover buffer is republished
            # via rapid-upload dedupe and removed on the next pass.
            logger.warning("Could not remove uploaded buffer %s: %s", target, exc)
        if variant_path is not None:
            try:
                await asyncio.to_thread(variant_path.unlink, True)
            except OSError:
                pass
        return DownloadResult(True, receipt.mount_path, receipt.size)

    async def _generate_thumbnail(self, record: MessageSnapshot, source: Path) -> None:
        """Generate a WebP thumbnail for the downloaded media."""
        if not self.settings.thumbnail_cache_dir:
            return
        capabilities = await self._ffmpeg()
        if not capabilities.available:
            return
        thumb_dir = self.settings.thumbnail_cache_dir.expanduser().resolve() / str(
            record.telegram_chat_id
        )
        try:
            thumb_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create thumbnail dir %s: %s", thumb_dir, exc)
            return
        thumb_path = thumb_dir / f"{record.id}.webp"
        if await asyncio.to_thread(thumb_path.is_file):
            return  # Already exists
        try:
            await extract_thumbnail(
                self.settings,
                capabilities,
                source,
                thumb_path,
                self.settings.thumbnail_max_dimension,
                self.settings.thumbnail_quality,
            )
        except Exception as exc:
            logger.warning("Thumbnail generation failed for %s: %s", source, exc)

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
        record = await self.repository.get_message_by_id(message_id)
        if record is None:
            logger.warning("Cannot publish buffered file: message %s not found", message_id)
            return None
        await self.repository.mark_download_start(message_id, buffered_path)
        return await self._publish_to_uploader(record, buffered_path, progress)
