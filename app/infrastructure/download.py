"""Crash-safe, rate-limit-aware Telethon media downloader."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from time import monotonic
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
from app.infrastructure.telegram.proxy import MTProtoProxyManager
from app.infrastructure.terabox import TeraBoxError, UploadReceipt
from app.infrastructure.transcode import POSTER_SUFFIX
from app.infrastructure.video_cache import VideoRangeCache
from app.utils.file_lock import media_file_lock

logger = logging.getLogger(__name__)
DownloadProgressCallback = Callable[[int, int], None]

MIN_SUSTAINED_SPEED_BYTES = 1_000_000
MIN_SPEED_FILE_BYTES = 4 * 1024 * 1024
SPEED_GRACE_BYTES = 1 * 1024 * 1024
SPEED_GRACE_SECONDS = 3.0
SPEED_LOW_DURATION_SECONDS = 5.0

_VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi"})


class SlowDownloadError(ConnectionError):
    """Raised when a sufficiently large transfer misses the speed floor."""


class DownloadRateGuard:
    """Check sustained per-file throughput while Telethon reports progress."""

    def __init__(self, expected_total: int | None) -> None:
        self.expected_total = expected_total or 0
        self.started_at = monotonic()
        self.low_since: float | None = None

    def observe(self, current: int, total: int) -> None:
        expected_total = total or self.expected_total
        if expected_total < MIN_SPEED_FILE_BYTES:
            return
        now = monotonic()
        if current < SPEED_GRACE_BYTES or now - self.started_at < SPEED_GRACE_SECONDS:
            return
        measured_bytes = max(0, current - SPEED_GRACE_BYTES)
        measured_seconds = max(0.001, now - self.started_at - SPEED_GRACE_SECONDS)
        speed = measured_bytes / measured_seconds
        if speed < MIN_SUSTAINED_SPEED_BYTES:
            self.low_since = self.low_since or now
            if current >= expected_total or now - self.low_since >= SPEED_LOW_DURATION_SECONDS:
                raise SlowDownloadError(
                    f"sustained download speed {speed / 1_000_000:.2f} MB/s is below "
                    f"the required {MIN_SUSTAINED_SPEED_BYTES / 1_000_000:.2f} MB/s"
                )
        else:
            self.low_since = None


class MediaUploader(Protocol):
    """Publishes a finalized buffer file to the remote archive storage."""

    async def upload(
        self, target: Path, progress: DownloadProgressCallback | None = None
    ) -> UploadReceipt: ...


class TelegramDownloadClient(Protocol):
    """Public Telethon surface needed for explicit download request sizing."""

    async def download_file(
        self,
        input_location: object,
        file: str,
        *,
        part_size_kb: int,
        file_size: int | None = None,
        progress_callback: DownloadProgressCallback | None = None,
    ) -> str | bytes | None: ...


class MediaDownloader:
    """Download files under bounded network, upload, and transcode semaphores."""

    def __init__(
        self,
        settings: Settings,
        repository: ArchiveRepository,
        uploader: MediaUploader | None = None,
        video_cache: VideoRangeCache | None = None,
        proxy_manager: MTProtoProxyManager | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.uploader = uploader
        self.video_cache = video_cache
        self.proxy_manager = proxy_manager
        self._download_semaphore = asyncio.Semaphore(settings.download_concurrency)
        self._upload_semaphore = asyncio.Semaphore(settings.terabox_upload_concurrency)
        self._transcode_semaphore = asyncio.Semaphore(settings.transcode_concurrency)
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
                    async with self._transcode_semaphore:
                        await transcode_hevc_to_h264(
                            self.settings, capabilities, target, variant_path
                        )

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
        poster_path = poster_dir / f"{record.id}.poster.jpg"
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
        upload_progress: DownloadProgressCallback | None = None,
        prepare_progress: DownloadProgressCallback | None = None,
    ) -> DownloadResult:
        """Download and publish one file under a shared per-path lock."""

        async with media_file_lock(target):
            return await self._download_locked(
                record, raw_message, target, progress, upload_progress, prepare_progress
            )

    async def _download_locked(
        self,
        record: MessageSnapshot,
        raw_message: object,
        target: Path,
        progress: DownloadProgressCallback | None = None,
        upload_progress: DownloadProgressCallback | None = None,
        prepare_progress: DownloadProgressCallback | None = None,
    ) -> DownloadResult:
        temp_path = target.with_name(f"{target.name}.part")
        transfer_started = False
        try:
            attempt = 0
            while attempt < self.settings.download_retries:
                attempt += 1
                try:
                    await self.repository.mark_download_start(record.id, target)
                    await asyncio.to_thread(self._prepare_target, target, temp_path)
                    download_kwargs: dict[str, object] = {"file": str(temp_path)}
                    expected_total = getattr(record, "media_size", None)
                    if (
                        self.proxy_manager is not None
                        and expected_total is not None
                        and expected_total >= MIN_SPEED_FILE_BYTES
                    ):
                        rate_guard = DownloadRateGuard(expected_total)

                        def guarded_progress(
                            current: int,
                            total: int,
                            _guard: DownloadRateGuard = rate_guard,
                            _progress: DownloadProgressCallback | None = progress,
                        ) -> None:
                            _guard.observe(current, total)
                            if _progress is not None:
                                _progress(current, total)

                        download_kwargs["progress_callback"] = guarded_progress
                    elif progress is not None:
                        download_kwargs["progress_callback"] = progress

                    download_queue_started_at = monotonic()
                    async with self._download_semaphore:
                        download_queue_wait = monotonic() - download_queue_started_at
                        if self.proxy_manager is not None:
                            self.proxy_manager.begin_transfer()
                            transfer_started = True
                        try:
                            download_started_at = monotonic()
                            await self._download_media(
                                raw_message,
                                temp_path,
                                expected_total,
                                download_kwargs,
                            )
                            download_elapsed = max(0.001, monotonic() - download_started_at)
                        finally:
                            if transfer_started:
                                end_transfer = getattr(self.proxy_manager, "end_transfer", None)
                                if callable(end_transfer):
                                    end_transfer()
                                transfer_started = False
                    if not await asyncio.to_thread(temp_path.is_file):
                        raise OSError("Telegram returned no completed media file")
                    downloaded_size = await asyncio.to_thread(temp_path.stat)
                    expected_size = getattr(record, "media_size", None)
                    if expected_size is not None and downloaded_size.st_size != expected_size:
                        raise OSError(
                            "Telegram download size "
                            f"{downloaded_size.st_size} does not match expected {expected_size}"
                        )
                    logger.info(
                        "Downloaded %s (%s bytes in %.2fs, %.2f MB/s, queue_wait=%.2fs, attempts=%s)",
                        target.name,
                        downloaded_size.st_size,
                        download_elapsed,
                        downloaded_size.st_size / download_elapsed / 1_000_000,
                        download_queue_wait,
                        attempt,
                    )
                    size = await asyncio.to_thread(self._finalize, temp_path, target)
                    await self._validate_video(target)
                    # Optimize (faststart, poster, HEVC transcode) BEFORE upload in TeraBox mode
                    # so the optimized file gets uploaded. The preparing report keeps the
                    # operation monitor alive while ffmpeg works on the local file.
                    if prepare_progress is not None:
                        prepare_progress(0, size)
                    prepare_started_at = monotonic()
                    variant_path = await self._optimize(target, record)
                    logger.info(
                        "Prepared %s in %.2fs",
                        target.name,
                        max(0.001, monotonic() - prepare_started_at),
                    )
                    if self.uploader is not None:
                        return await self._publish_to_uploader(
                            record, target, progress, variant_path, upload_progress
                        )
                    size = await asyncio.to_thread(self._current_size, target)
                    await self.repository.mark_download_completed(record.id, target, size)
                    return DownloadResult(True, target, size)
                except asyncio.CancelledError:
                    await self.repository.mark_download_failed(record.id, "Download interrupted")
                    raise
                except SlowDownloadError as exc:
                    logger.warning("%s: %s", target.name, exc)
                    if self.proxy_manager is not None and await self.proxy_manager.rotate():
                        attempt -= 1
                        continue
                    error = f"SlowDownloadError: {exc}"
                    if attempt == self.settings.download_retries:
                        await self.repository.mark_download_failed(record.id, error)
                        return DownloadResult(False, None, None, error)
                    await asyncio.sleep(min(30, 2 ** (attempt - 1)))
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
        finally:
            if transfer_started:
                end_transfer = getattr(self.proxy_manager, "end_transfer", None)
                if callable(end_transfer):
                    end_transfer()
        return DownloadResult(False, None, None, "Download retry loop ended unexpectedly")

    async def _download_media(
        self,
        raw_message: object,
        temp_path: Path,
        expected_total: int | None,
        download_kwargs: dict[str, object],
    ) -> str | bytes | None:
        """Download through Telethon with optional explicit request sizing.

        Telethon's high-level ``download_media`` does not expose ``part_size_kb``.
        Real Telethon messages retain their client, allowing the public low-level
        ``download_file`` API to be used when an operator explicitly selects a
        request size. Test doubles and unsupported media retain the high-level
        path, which is also the safest default because it handles file references
        that expire during long transfers.
        """

        part_size_kb = self.settings.telegram_download_part_size_kb
        client = getattr(raw_message, "_client", None) or getattr(raw_message, "client", None)
        download_file = getattr(client, "download_file", None)
        if part_size_kb and callable(download_file):
            low_level_kwargs: dict[str, object] = {
                "file": str(temp_path),
                "part_size_kb": part_size_kb,
            }
            if expected_total is not None:
                low_level_kwargs["file_size"] = expected_total
            if "progress_callback" in download_kwargs:
                low_level_kwargs["progress_callback"] = download_kwargs["progress_callback"]
            try:
                return await download_file(raw_message, **low_level_kwargs)
            except TypeError as exc:
                logger.debug(
                    "Falling back to Telethon download_media for %s: %s",
                    temp_path.name,
                    exc,
                )
        return await raw_message.download_media(**download_kwargs)  # type: ignore[attr-defined]

    async def _validate_video(self, target: Path) -> None:
        """Reject a corrupt video before ffmpeg or TeraBox can consume it."""

        if target.suffix.casefold() not in _VIDEO_SUFFIXES:
            return
        capabilities = await self._ffmpeg()
        if not capabilities.available:
            return
        if await probe_video_codec(self.settings, capabilities, target) is None:
            raise OSError(f"Downloaded video is invalid or incomplete: {target.name}")

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
        upload_progress: DownloadProgressCallback | None = None,
    ) -> DownloadResult:
        """Upload a just-finalized file; on failure keep the buffer file.

        The file stays in DOWNLOAD_DIR so retry-failed re-uploads it without
        re-downloading from Telegram.
        """
        assert self.uploader is not None
        upload_callback = upload_progress if upload_progress is not None else progress
        try:
            upload_queue_started_at = monotonic()
            async with self._upload_semaphore:
                upload_queue_wait = monotonic() - upload_queue_started_at
                upload_started_at = monotonic()
                receipt = await self.uploader.upload(target, upload_callback)
                upload_elapsed = max(0.001, monotonic() - upload_started_at)
                logger.info(
                    "Uploaded %s (%s bytes in %.2fs, %.2f MB/s, queue_wait=%.2fs)",
                    target.name,
                    receipt.size,
                    upload_elapsed,
                    receipt.size / upload_elapsed / 1_000_000,
                    upload_queue_wait,
                )
                variant_receipt: UploadReceipt | None = None
                if variant_path is not None and self.settings.terabox_store_both:
                    try:
                        variant_receipt = await self.uploader.upload(variant_path, upload_callback)
                        logger.info(
                            "Uploaded H.264 variant %s (%s bytes)",
                            variant_receipt.remote_path,
                            variant_receipt.size,
                        )
                    except Exception as exc:
                        logger.warning("Failed to upload H.264 variant: %s", exc)
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
        variant_remote_path = variant_receipt.remote_path if variant_receipt is not None else None
        await self.repository.mark_download_completed(
            record.id,
            target,
            receipt.size,
            None,
            terabox_remote_path=receipt.remote_path,
            terabox_variant_remote_path=variant_remote_path,
        )
        # Generate local thumbnail for fast gallery loading in TeraBox mode
        if self.settings.thumbnail_cache_dir:
            await self._generate_thumbnail(record, target)
        if self.video_cache is not None and target.suffix.casefold() in _VIDEO_SUFFIXES:
            try:
                seeded = await self.video_cache.seed_file(record.id, target)
                if seeded:
                    logger.info(
                        "Seeded video cache for message %s (%s chunks) from upload buffer",
                        record.id,
                        seeded,
                    )
            except Exception as exc:
                logger.warning("Could not seed video cache for message %s: %s", record.id, exc)
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
        return DownloadResult(True, target, receipt.size)

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
        thumb_path = thumb_dir / f"{record.id}.jpg"
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
        """Publish a buffer while excluding concurrent download/upload readers."""

        async with media_file_lock(buffered_path):
            try:
                return await self._publish_buffered_locked(message_id, buffered_path, progress)
            except OSError as exc:
                error = f"Invalid buffered media: {exc}"
                logger.warning("%s", error)
                await self.repository.mark_download_failed(message_id, error)
                await asyncio.to_thread(buffered_path.unlink, True)
                return DownloadResult(False, buffered_path, None, error)

    async def _publish_buffered_locked(
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
        if not await asyncio.to_thread(buffered_path.is_file):
            return DownloadResult(False, buffered_path, None, "Buffered media disappeared")
        await self._validate_video(buffered_path)
        await self.repository.mark_download_start(message_id, buffered_path)
        return await self._publish_to_uploader(record, buffered_path, progress)
