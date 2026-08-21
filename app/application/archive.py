"""Transactional message archiving and retry orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.application.archive_records import (
    DownloadResult,
    MessageSnapshot,
    RetryCandidate,
)
from app.application.filenames import output_path
from app.application.media_policy import MediaFilter
from app.config import Settings
from app.domain import ChatInfo, ContentType, DownloadState, MessageData
from app.utils.logging import format_bytes
from app.utils.waiting import wait_or_stop

logger = logging.getLogger(__name__)
DownloadProgressCallback = Callable[[str, int, int, str], None]


@dataclass(frozen=True, slots=True)
class ProcessResult:
    created: bool
    downloaded: bool
    skipped: bool


@dataclass(frozen=True, slots=True)
class RetryProgress:
    phase: str
    current: int
    total: int
    attempted: int
    completed: int
    detail: str
    chat_id: int | None = None
    chat_title: str | None = None


@dataclass(frozen=True, slots=True)
class _RetryOutcome:
    candidate: RetryCandidate
    chat: ChatInfo
    result: ProcessResult | None
    stopped: bool = False
    detail: str = ""


RetryProgressCallback = Callable[[RetryProgress], Awaitable[None]]
MessageParser = Callable[[object, ChatInfo], MessageData]
ContentClassifier = Callable[[object], frozenset[ContentType]]
RateLimitDelay = Callable[[Exception], int | None]
TransientErrorPredicate = Callable[[Exception], bool]


class ArchiveWriter(Protocol):
    async def upsert_message(self, data: MessageData) -> tuple[MessageSnapshot, bool]: ...

    async def mark_download_completed(
        self, message_id: int, media_path: Path, media_size: int
    ) -> None: ...

    async def mark_download_skipped(self, message_id: int, reason: str) -> None: ...

    async def mark_download_failed(self, message_id: int, error: str) -> None: ...

    async def iter_retry_candidates(
        self, chat_ids: Sequence[int], *, failed_only: bool = False
    ) -> Sequence[RetryCandidate]: ...


class ArchiveDownloader(Protocol):
    async def download(
        self,
        record: MessageSnapshot,
        raw_message: object,
        target: Path,
        progress: Callable[[int, int], None] | None = None,
        upload_progress: Callable[[int, int], None] | None = None,
        prepare_progress: Callable[[int, int], None] | None = None,
    ) -> DownloadResult: ...

    async def publish_buffered(
        self,
        message_id: int,
        buffered_path: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> DownloadResult | None: ...


class ArchiveService:
    def __init__(
        self,
        settings: Settings,
        repository: ArchiveWriter,
        downloader: ArchiveDownloader,
        message_parser: MessageParser,
        content_classifier: ContentClassifier,
        rate_limit_delay: RateLimitDelay,
        is_transient_error: TransientErrorPredicate,
        content_types: frozenset[ContentType] | None = None,
        download_progress: DownloadProgressCallback | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.downloader = downloader
        self.message_parser = message_parser
        self.content_classifier = content_classifier
        self.rate_limit_delay = rate_limit_delay
        self.is_transient_error = is_transient_error
        self.content_types = content_types
        self.download_progress = download_progress
        self.media_filter = MediaFilter(settings, content_types)
        # A fixed lock stripe set prevents unbounded memory growth in a listener
        # that may process millions of distinct message identities.
        self._message_locks = tuple(asyncio.Lock() for _ in range(256))

    def matches_message(self, raw_message: object) -> bool:
        """Return whether this operation selected any facet of the message."""

        return self.content_types is None or bool(
            self.content_classifier(raw_message) & self.content_types
        )

    def _report_terminal_failure(self, target: Path) -> None:
        """Remove a terminally failed transfer from live operation progress."""

        if self.download_progress:
            self.download_progress(target.name, 0, 0, "failed")

    async def process_message(
        self, raw_message: object, chat: ChatInfo, *, edited: bool = False
    ) -> ProcessResult:
        if not self.matches_message(raw_message):
            return ProcessResult(False, False, True)
        data = self.message_parser(raw_message, chat)
        key = (data.telegram_chat_id, data.telegram_message_id)
        lock = self._message_locks[hash(key) % len(self._message_locks)]
        async with lock:
            record, created = await self.repository.upsert_message(data)
            action = "updated" if edited or not created else "saved"
            logger.info("[%s] Message %s %s", chat.title, data.telegram_message_id, action)

            if not data.has_media:
                return ProcessResult(created, False, False)

            target = output_path(self.settings.download_dir, chat, data)
            recorded_path = Path(record.media_path) if record.media_path else None
            recorded_exists = bool(recorded_path and await asyncio.to_thread(recorded_path.is_file))
            completed_path = recorded_path if recorded_exists else None

            if self.settings.terabox_enabled:
                if record.download_status == DownloadState.COMPLETED.value and record.terabox_remote_path:
                    logger.info(
                        "[SKIP] [%s] Message %s media already archived at %s",
                        chat.title,
                        data.telegram_message_id,
                        record.terabox_remote_path,
                    )
                    return ProcessResult(created, False, True)
                # A buffer file in DOWNLOAD_DIR means the bytes are local but
                # possibly never uploaded (crash between download and upload,
                # or a failed unlink after a completed upload). Publish it;
                # the rapid-upload dedupe makes re-publishing effectively free,
                # and publish_buffered removes the copy afterwards.
                if await asyncio.to_thread(target.is_file):
                    upload_progress = None
                    if self.download_progress:

                        def upload_progress(current: int, total: int) -> None:
                            self.download_progress(target.name, current, total, "uploading")

                    try:
                        publish_result = await self.downloader.publish_buffered(
                            record.id, target, upload_progress
                        )
                    except asyncio.CancelledError:
                        self._report_terminal_failure(target)
                        raise
                    except Exception:
                        self._report_terminal_failure(target)
                        raise
                    if publish_result is not None and publish_result.completed:
                        return ProcessResult(created, True, False)
                    self._report_terminal_failure(target)
                    return ProcessResult(created, False, False)
            elif not completed_path and await asyncio.to_thread(target.is_file):
                completed_path = target
            if completed_path:
                size = (await asyncio.to_thread(completed_path.stat)).st_size
                if record.download_status != "completed" or record.media_size != size:
                    await self.repository.mark_download_completed(record.id, completed_path, size)
                logger.info(
                    "[SKIP] [%s] Message %s media already archived",
                    chat.title,
                    data.telegram_message_id,
                )
                return ProcessResult(created, False, True)

            decision = self.media_filter.evaluate(data)
            if not decision.allowed:
                reason = decision.reason or "Filtered"
                await self.repository.mark_download_skipped(record.id, reason)
                logger.info(
                    "[SKIP] [%s] Message %s: %s", chat.title, data.telegram_message_id, reason
                )
                return ProcessResult(created, False, True)

            progress = None
            upload_progress = None
            prepare_progress = None
            if self.download_progress:

                def progress(current: int, total: int) -> None:
                    self.download_progress(target.name, current, total, "downloading")

                def upload_progress(current: int, total: int) -> None:
                    self.download_progress(target.name, current, total, "uploading")

                def prepare_progress(current: int, total: int) -> None:
                    self.download_progress(target.name, current, total, "preparing")

            try:
                result = await self.downloader.download(
                    record, raw_message, target, progress, upload_progress, prepare_progress
                )
            except asyncio.CancelledError:
                self._report_terminal_failure(target)
                raise
            except Exception:
                self._report_terminal_failure(target)
                raise
            if result.completed and result.path and result.size is not None:
                logger.info(
                    "[%s] Downloaded %s (%s)",
                    chat.title,
                    result.path.name,
                    format_bytes(result.size),
                )
                return ProcessResult(created, True, False)

            self._report_terminal_failure(target)
            logger.error(
                "[%s] Download failed for message %s: %s",
                chat.title,
                data.telegram_message_id,
                result.error,
            )
            return ProcessResult(created, False, False)

    async def retry_candidates(
        self,
        client: object,
        chats: Mapping[int, ChatInfo],
        *,
        failed_only: bool = False,
        stop_event: asyncio.Event | None = None,
        progress: RetryProgressCallback | None = None,
    ) -> tuple[int, int]:
        """Retry failed/incomplete media with bounded parallel processing.

        Candidates run through a sliding window of at most
        ``download_concurrency`` workers so media transfers overlap. Results
        settle oldest-first, which keeps the durable failure marks and
        progress updates ordered.
        """
        candidates = await self.repository.iter_retry_candidates(
            tuple(chats), failed_only=failed_only
        )
        attempted = 0
        completed = 0
        skipped = 0
        settled = 0
        total = len(candidates)
        worker_count = max(1, self.settings.download_concurrency)
        pending: deque[tuple[asyncio.Task[_RetryOutcome]]] = deque()
        if progress:
            await progress(
                RetryProgress(
                    phase="repairing",
                    current=0,
                    total=total,
                    attempted=0,
                    completed=0,
                    detail=f"Found {total} media candidate{'s' if total != 1 else ''}",
                )
            )

        async def settle_oldest(
            queue: deque[tuple[asyncio.Task[_RetryOutcome]]],
        ) -> None:
            nonlocal completed, settled
            task = queue.popleft()
            outcome = await task
            settled += 1
            if outcome.result is not None:
                completed += int(outcome.result.downloaded)
            if progress:
                if outcome.stopped:
                    detail = (
                        f"Stopped before processing message {outcome.candidate.telegram_message_id}"
                    )
                elif outcome.detail:
                    detail = outcome.detail
                else:
                    detail = f"Processed retry candidate {settled} of {total}"
                await progress(
                    RetryProgress(
                        phase="repairing",
                        current=skipped + settled,
                        total=total,
                        attempted=attempted,
                        completed=completed,
                        detail=detail,
                        chat_id=outcome.candidate.telegram_chat_id,
                        chat_title=outcome.chat.title,
                    )
                )

        try:
            for candidate in candidates:
                if stop_event and stop_event.is_set():
                    break
                chat = chats.get(candidate.telegram_chat_id)
                chat_title = chat.title if chat is not None else None
                if (
                    candidate.download_status == DownloadState.COMPLETED.value
                    and (
                        candidate.media_path
                        or candidate.terabox_remote_path
                    )
                ):
                    if candidate.terabox_remote_path or await asyncio.to_thread(
                        Path(candidate.media_path).is_file  # type: ignore[arg-type]
                    ):
                        skipped += 1
                        if progress:
                            await progress(
                                RetryProgress(
                                    phase="repairing",
                                    current=skipped + settled,
                                    total=total,
                                    attempted=attempted,
                                    completed=completed,
                                    detail="Verified an existing completed file",
                                    chat_id=candidate.telegram_chat_id,
                                    chat_title=chat_title,
                                )
                            )
                        continue
                if chat is None:
                    skipped += 1
                    if progress:
                        await progress(
                            RetryProgress(
                                phase="repairing",
                                current=skipped + settled,
                                total=total,
                                attempted=attempted,
                                completed=completed,
                                detail="Skipped a candidate outside the active chat selection",
                                chat_id=candidate.telegram_chat_id,
                            )
                        )
                    continue
                if not self.media_filter.media_type_selected(candidate.media_type):
                    skipped += 1
                    if progress:
                        await progress(
                            RetryProgress(
                                phase="repairing",
                                current=skipped + settled,
                                total=total,
                                attempted=attempted,
                                completed=completed,
                                detail="Skipped a media type outside this operation selection",
                                chat_id=candidate.telegram_chat_id,
                                chat_title=chat_title,
                            )
                        )
                    continue
                attempted += 1
                if progress:
                    await progress(
                        RetryProgress(
                            phase="repairing",
                            current=skipped + settled,
                            total=total,
                            attempted=attempted,
                            completed=completed,
                            detail=f"Checking media in {chat_title}",
                            chat_id=candidate.telegram_chat_id,
                            chat_title=chat_title,
                        )
                    )
                pending.append(
                    asyncio.create_task(
                        self._retry_one(client, candidate, chat, stop_event),
                        name=(
                            f"retry-{candidate.telegram_chat_id}-{candidate.telegram_message_id}"
                        ),
                    )
                )
                if len(pending) >= worker_count:
                    await settle_oldest(pending)

            # A safe stop stops accepting candidates but lets the bounded
            # in-flight set finish so completed files can be accounted.
            while pending:
                await settle_oldest(pending)
        finally:
            if pending:
                tasks = list(pending)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                pending.clear()

        return attempted, completed

    async def _retry_one(
        self,
        client: object,
        candidate: RetryCandidate,
        chat: ChatInfo,
        stop_event: asyncio.Event | None,
    ) -> _RetryOutcome:
        """Fetch one retry candidate and archive it, honoring stop requests."""
        raw_message: object | None = None
        retrieval_error: str | None = None
        for retrieval_attempt in range(1, self.settings.download_retries + 1):
            try:
                raw_message = await client.get_messages(  # type: ignore[attr-defined]
                    chat.entity, ids=candidate.telegram_message_id
                )
                retrieval_error = None
                break
            except Exception as exc:
                wait_seconds = self.rate_limit_delay(exc)
                if wait_seconds is not None:
                    retrieval_error = f"Telegram FloodWait ({wait_seconds}s)"
                    logger.warning("Telegram FloodWait: waiting %s seconds", wait_seconds)
                    if retrieval_attempt < self.settings.download_retries:
                        if await wait_or_stop(stop_event, wait_seconds):
                            return _RetryOutcome(candidate, chat, None, stopped=True)
                elif self.is_transient_error(exc):
                    retrieval_error = f"{type(exc).__name__}: {exc}"
                    if retrieval_attempt < self.settings.download_retries:
                        delay = min(30, 2 ** (retrieval_attempt - 1))
                        logger.warning(
                            "[%s] Message retrieval failed; retrying in %ss: %s",
                            chat.title,
                            delay,
                            exc,
                        )
                        if await wait_or_stop(stop_event, delay):
                            return _RetryOutcome(candidate, chat, None, stopped=True)
                else:
                    raise

        if stop_event and stop_event.is_set():
            return _RetryOutcome(candidate, chat, None, stopped=True)
        if retrieval_error is not None:
            await self.repository.mark_download_failed(candidate.id, retrieval_error)
            logger.error(
                "[%s] Could not retrieve message %s for retry: %s",
                chat.title,
                candidate.telegram_message_id,
                retrieval_error,
            )
            return _RetryOutcome(
                candidate,
                chat,
                None,
                detail=f"Retry failed for message {candidate.telegram_message_id}",
            )
        if raw_message is None:
            await self.repository.mark_download_failed(
                candidate.id, "Telegram message is no longer available"
            )
            return _RetryOutcome(
                candidate,
                chat,
                None,
                detail="Telegram message is no longer available",
            )
        result = await self.process_message(raw_message, chat)
        return _RetryOutcome(candidate, chat, result)
