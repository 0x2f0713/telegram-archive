"""Transactional message archiving and retry orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from telethon.errors import FloodWaitError, RPCError

from app.application.filenames import output_path
from app.application.media_policy import MediaFilter
from app.config import Settings
from app.domain import ChatInfo, ContentType, DownloadState
from app.infrastructure.download import MediaDownloader
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.telegram.translation import content_types_of, message_data
from app.utils.logging import format_bytes
from app.utils.waiting import wait_or_stop

logger = logging.getLogger(__name__)
DownloadProgressCallback = Callable[[str, int, int], None]


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


RetryProgressCallback = Callable[[RetryProgress], Awaitable[None]]


class ArchiveService:
    def __init__(
        self,
        settings: Settings,
        repository: ArchiveRepository,
        downloader: MediaDownloader,
        content_types: frozenset[ContentType] | None = None,
        download_progress: DownloadProgressCallback | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.downloader = downloader
        self.content_types = content_types
        self.download_progress = download_progress
        self.media_filter = MediaFilter(settings, content_types)
        # A fixed lock stripe set prevents unbounded memory growth in a listener
        # that may process millions of distinct message identities.
        self._message_locks = tuple(asyncio.Lock() for _ in range(256))

    def matches_message(self, raw_message: object) -> bool:
        """Return whether this operation selected any facet of the message."""

        return self.content_types is None or bool(
            content_types_of(raw_message) & self.content_types
        )

    async def process_message(
        self, raw_message: object, chat: ChatInfo, *, edited: bool = False
    ) -> ProcessResult:
        if not self.matches_message(raw_message):
            return ProcessResult(False, False, True)
        data = message_data(raw_message, chat)
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
            if not completed_path and await asyncio.to_thread(target.is_file):
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
            if self.download_progress:

                def progress(current: int, total: int) -> None:
                    self.download_progress(target.name, current, total)

            result = await self.downloader.download(record, raw_message, target, progress)
            if result.completed and result.path and result.size is not None:
                logger.info(
                    "[%s] Downloaded %s (%s)",
                    chat.title,
                    result.path.name,
                    format_bytes(result.size),
                )
                return ProcessResult(created, True, False)

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
        """Retry failed/incomplete media and completed records missing files."""

        candidates = await self.repository.iter_retry_candidates(
            tuple(chats), failed_only=failed_only
        )
        attempted = 0
        completed = 0
        total = len(candidates)
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
        for current, candidate in enumerate(candidates, start=1):
            if stop_event and stop_event.is_set():
                break
            if candidate.download_status == DownloadState.COMPLETED.value and candidate.media_path:
                if await asyncio.to_thread(Path(candidate.media_path).is_file):
                    if progress:
                        await progress(
                            RetryProgress(
                                phase="repairing",
                                current=current,
                                total=total,
                                attempted=attempted,
                                completed=completed,
                                detail="Verified an existing completed file",
                            )
                        )
                    continue
            chat = chats.get(candidate.telegram_chat_id)
            if chat is None:
                if progress:
                    await progress(
                        RetryProgress(
                            phase="repairing",
                            current=current,
                            total=total,
                            attempted=attempted,
                            completed=completed,
                            detail="Skipped a candidate outside the active chat selection",
                        )
                    )
                continue
            if not self.media_filter.media_type_selected(candidate.media_type):
                if progress:
                    await progress(
                        RetryProgress(
                            phase="repairing",
                            current=current,
                            total=total,
                            attempted=attempted,
                            completed=completed,
                            detail="Skipped a media type outside this operation selection",
                        )
                    )
                continue
            attempted += 1
            raw_message = None
            retrieval_error: str | None = None
            for retrieval_attempt in range(1, self.settings.download_retries + 1):
                try:
                    raw_message = await client.get_messages(  # type: ignore[attr-defined]
                        chat.entity, ids=candidate.telegram_message_id
                    )
                    retrieval_error = None
                    break
                except FloodWaitError as exc:
                    wait_seconds = max(1, int(exc.seconds))
                    retrieval_error = f"Telegram FloodWait ({wait_seconds}s)"
                    logger.warning("Telegram FloodWait: waiting %s seconds", wait_seconds)
                    if retrieval_attempt < self.settings.download_retries:
                        if progress:
                            await progress(
                                RetryProgress(
                                    phase="rate-limited",
                                    current=current,
                                    total=total,
                                    attempted=attempted,
                                    completed=completed,
                                    detail=f"Telegram requested a {wait_seconds}s FloodWait",
                                )
                            )
                        if await wait_or_stop(stop_event, wait_seconds):
                            break
                except (RPCError, ConnectionError, TimeoutError) as exc:
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
                            break

            if stop_event and stop_event.is_set():
                break

            if retrieval_error is not None:
                await self.repository.mark_download_failed(candidate.id, retrieval_error)
                logger.error(
                    "[%s] Could not retrieve message %s for retry: %s",
                    chat.title,
                    candidate.telegram_message_id,
                    retrieval_error,
                )
                if progress:
                    await progress(
                        RetryProgress(
                            phase="repairing",
                            current=current,
                            total=total,
                            attempted=attempted,
                            completed=completed,
                            detail=f"Retry failed for message {candidate.telegram_message_id}",
                        )
                    )
                continue
            if raw_message is None:
                await self.repository.mark_download_failed(
                    candidate.id, "Telegram message is no longer available"
                )
                if progress:
                    await progress(
                        RetryProgress(
                            phase="repairing",
                            current=current,
                            total=total,
                            attempted=attempted,
                            completed=completed,
                            detail="Telegram message is no longer available",
                        )
                    )
                continue
            result = await self.process_message(raw_message, chat)
            completed += int(result.downloaded)
            if progress:
                await progress(
                    RetryProgress(
                        phase="repairing",
                        current=current,
                        total=total,
                        attempted=attempted,
                        completed=completed,
                        detail=f"Processed retry candidate {current} of {total}",
                    )
                )
        return attempted, completed
