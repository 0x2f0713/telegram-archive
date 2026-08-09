"""Incremental oldest-to-newest historical synchronization."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from telethon.errors import FloodWaitError, RPCError

from app.database.repository import ArchiveRepository
from app.services.archive import ArchiveService
from app.telegram.entities import ChatInfo

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


async def _wait_or_stop(stop_event: asyncio.Event | None, seconds: int) -> bool:
    if stop_event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except TimeoutError:
        return False


async def sync_history(
    client: object,
    chats: Mapping[int, ChatInfo],
    archive: ArchiveService,
    repository: ArchiveRepository,
    *,
    limit: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    stop_event: asyncio.Event | None = None,
    progress: SyncProgressCallback | None = None,
) -> SyncResult:
    """Synchronize chats, committing each message and checkpoint independently."""

    bounded_range = since is not None or until is not None
    total_messages = 0
    total_downloads = 0
    completed_chats = 0
    chats_total = len(chats)
    for chat_index, chat in enumerate(chats.values(), start=1):
        if stop_event and stop_event.is_set():
            break
        await repository.upsert_chat(chat)
        checkpoint = await repository.get_checkpoint(chat.telegram_chat_id)
        cursor = 0 if bounded_range else (checkpoint or 0)
        processed = 0
        transient_failures = 0
        finished = False
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

        while not finished and (limit is None or processed < limit):
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
                    result = await archive.process_message(raw_message, chat)
                    processed += 1
                    total_messages += 1
                    total_downloads += int(result.downloaded)
                    if not bounded_range:
                        await repository.advance_checkpoint(chat.telegram_chat_id, message_id)
                    if progress:
                        await progress(
                            SyncProgress(
                                phase="syncing",
                                chat_id=chat.telegram_chat_id,
                                chat_title=chat.title,
                                chat_index=chat_index,
                                chats_total=chats_total,
                                chats_completed=completed_chats,
                                chat_messages=processed,
                                messages_processed=total_messages,
                                downloads_completed=total_downloads,
                                detail=f"Processed message {message_id} in {chat.title}",
                            )
                        )
                    if limit is not None and processed >= limit:
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
                if await _wait_or_stop(stop_event, wait_seconds):
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
                if await _wait_or_stop(stop_event, delay):
                    break

        if stop_event and stop_event.is_set():
            break
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
