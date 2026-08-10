"""Incremental oldest-to-newest historical synchronization."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from telethon.errors import FloodWaitError, RPCError

from app.application.archive import ArchiveService, ProcessResult
from app.domain import ChatInfo, ContentType
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.telegram.translation import content_types_of
from app.utils.waiting import wait_or_stop

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    chats: int = 0
    messages: int = 0
    downloads: int = 0


@dataclass(frozen=True, slots=True)
class SyncProgress:
    phase: str
    chat_id: int | None
    chat_title: str | None
    chat_index: int
    chats_total: int
    chats_completed: int
    chat_messages: int
    messages_processed: int
    downloads_completed: int
    detail: str


SyncProgressCallback = Callable[[SyncProgress], Awaitable[None]]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def sync_history(
    client: object,
    chats: Mapping[int, ChatInfo],
    archive: ArchiveService,
    repository: ArchiveRepository,
    *,
    limit: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    concurrency: int = 1,
    content_types: frozenset[ContentType] | None = None,
    stop_event: asyncio.Event | None = None,
    progress: SyncProgressCallback | None = None,
) -> SyncResult:
    """Synchronize chats with bounded processing and ordered durable checkpoints.

    Multiple messages may be archived concurrently so media transfers can overlap.
    Results are settled oldest-first before advancing the checkpoint, which prevents
    a crash from skipping an older in-flight message when a newer transfer finishes
    first.
    """

    bounded_range = since is not None or until is not None
    worker_count = max(1, int(concurrency))
    explicit_content_types = tuple(sorted(content_types)) if content_types is not None else ()
    total_messages = 0
    total_downloads = 0
    completed_chats = 0
    chats_total = len(chats)
    for chat_index, chat in enumerate(chats.values(), start=1):
        if stop_event and stop_event.is_set():
            break
        await repository.upsert_chat(chat)
        content_checkpoints: dict[str, int | None] = {}
        if bounded_range:
            cursor = 0
        elif explicit_content_types:
            content_checkpoints = await repository.get_content_checkpoints(
                chat.telegram_chat_id,
                explicit_content_types,
            )
            cursor = min((value or 0) for value in content_checkpoints.values())
        else:
            checkpoint = await repository.get_checkpoint(chat.telegram_chat_id)
            cursor = checkpoint or 0
        processed = 0
        scheduled = 0
        transient_failures = 0
        finished = False
        pending: deque[tuple[int, asyncio.Task[ProcessResult]]] = deque()
        logger.info(
            "[%s] Starting history sync%s",
            chat.title,
            f" after message {cursor}" if cursor else "",
        )
        if progress:
            await progress(
                SyncProgress(
                    phase="syncing",
                    chat_id=chat.telegram_chat_id,
                    chat_title=chat.title,
                    chat_index=chat_index,
                    chats_total=chats_total,
                    chats_completed=completed_chats,
                    chat_messages=0,
                    messages_processed=total_messages,
                    downloads_completed=total_downloads,
                    detail=f"Syncing {chat.title}",
                )
            )

        async def settle_oldest(
            queue: deque[tuple[int, asyncio.Task[ProcessResult]]],
            current_chat: ChatInfo,
            current_chat_index: int,
            chats_done: int,
            checkpoints: dict[str, int | None],
        ) -> None:
            nonlocal processed, total_messages, total_downloads
            message_id, task = queue.popleft()
            result = await task
            processed += 1
            total_messages += 1
            total_downloads += int(result.downloaded)
            if not bounded_range and explicit_content_types:
                await repository.advance_content_checkpoints(
                    current_chat.telegram_chat_id,
                    explicit_content_types,
                    message_id,
                )
                for content_type in explicit_content_types:
                    existing = checkpoints.get(content_type) or 0
                    checkpoints[content_type] = max(existing, message_id)
            elif not bounded_range:
                await repository.advance_checkpoint(current_chat.telegram_chat_id, message_id)
            if progress:
                await progress(
                    SyncProgress(
                        phase="syncing",
                        chat_id=current_chat.telegram_chat_id,
                        chat_title=current_chat.title,
                        chat_index=current_chat_index,
                        chats_total=chats_total,
                        chats_completed=chats_done,
                        chat_messages=processed,
                        messages_processed=total_messages,
                        downloads_completed=total_downloads,
                        detail=f"Processed message {message_id} in {current_chat.title}",
                    )
                )

        try:
            while not finished and (limit is None or scheduled < limit):
                if stop_event and stop_event.is_set():
                    break
                try:
                    async for raw_message in client.iter_messages(  # type: ignore[attr-defined]
                        chat.entity,
                        reverse=True,
                        min_id=cursor,
                    ):
                        if stop_event and stop_event.is_set():
                            break
                        message_id = int(raw_message.id)
                        cursor = max(cursor, message_id)
                        message_date = _utc(raw_message.date)
                        if since and message_date < since:
                            continue
                        if until and message_date >= until:
                            finished = True
                            break
                        if content_types is not None:
                            matching_types = content_types_of(raw_message) & content_types
                            if not matching_types:
                                continue
                            if not any(
                                message_id > (content_checkpoints.get(content_type) or 0)
                                for content_type in matching_types
                            ):
                                continue
                        pending.append(
                            (
                                message_id,
                                asyncio.create_task(
                                    archive.process_message(raw_message, chat),
                                    name=(f"sync-{chat.telegram_chat_id}-{message_id}"),
                                ),
                            )
                        )
                        scheduled += 1
                        if len(pending) >= worker_count:
                            await settle_oldest(
                                pending,
                                chat,
                                chat_index,
                                completed_chats,
                                content_checkpoints,
                            )
                        if limit is not None and scheduled >= limit:
                            break
                    else:
                        finished = True
                    transient_failures = 0
                except FloodWaitError as exc:
                    wait_seconds = max(1, int(exc.seconds))
                    logger.warning("Telegram FloodWait: waiting %s seconds", wait_seconds)
                    if progress:
                        await progress(
                            SyncProgress(
                                phase="rate-limited",
                                chat_id=chat.telegram_chat_id,
                                chat_title=chat.title,
                                chat_index=chat_index,
                                chats_total=chats_total,
                                chats_completed=completed_chats,
                                chat_messages=processed,
                                messages_processed=total_messages,
                                downloads_completed=total_downloads,
                                detail=f"Telegram requested a {wait_seconds}s FloodWait",
                            )
                        )
                    if await wait_or_stop(stop_event, wait_seconds):
                        break
                except (RPCError, ConnectionError, TimeoutError) as exc:
                    transient_failures += 1
                    if transient_failures > 5:
                        raise RuntimeError(
                            f"History sync for {chat.title} failed after retries: {exc}"
                        ) from exc
                    delay = min(30, 2 ** (transient_failures - 1))
                    logger.warning(
                        "[%s] Telegram history request failed; retrying in %ss: %s",
                        chat.title,
                        delay,
                        exc,
                    )
                    if progress:
                        await progress(
                            SyncProgress(
                                phase="reconnecting",
                                chat_id=chat.telegram_chat_id,
                                chat_title=chat.title,
                                chat_index=chat_index,
                                chats_total=chats_total,
                                chats_completed=completed_chats,
                                chat_messages=processed,
                                messages_processed=total_messages,
                                downloads_completed=total_downloads,
                                detail=f"Telegram connection retry in {delay}s",
                            )
                        )
                    if await wait_or_stop(stop_event, delay):
                        break

            # A safe stop stops accepting messages but lets the small bounded
            # in-flight set finish so completed files can be atomically renamed.
            while pending:
                await settle_oldest(
                    pending,
                    chat,
                    chat_index,
                    completed_chats,
                    content_checkpoints,
                )
        finally:
            if pending:
                tasks = [task for _message_id, task in pending]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                pending.clear()

        if stop_event and stop_event.is_set():
            break
        if not bounded_range and explicit_content_types and cursor:
            await repository.advance_content_checkpoints(
                chat.telegram_chat_id,
                explicit_content_types,
                cursor,
            )
        completed_chats += 1
        logger.info("[%s] History sync complete: %s messages", chat.title, processed)
        if progress:
            await progress(
                SyncProgress(
                    phase="chat-complete",
                    chat_id=chat.telegram_chat_id,
                    chat_title=chat.title,
                    chat_index=chat_index,
                    chats_total=chats_total,
                    chats_completed=completed_chats,
                    chat_messages=processed,
                    messages_processed=total_messages,
                    downloads_completed=total_downloads,
                    detail=f"Finished {chat.title}: {processed} messages",
                )
            )

    return SyncResult(
        chats=completed_chats,
        messages=total_messages,
        downloads=total_downloads,
    )
