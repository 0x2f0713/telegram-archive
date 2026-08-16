"""Focused persistence operations for chats, messages, and statistics."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.application.archive_records import (
    ArchiveStats,
    ChatArchiveDeletionTarget,
    ChatNewest,
    MessageSnapshot,
    RetryCandidate,
)
from app.domain import ChatInfo, DownloadState, MessageData
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import Chat, ContentSyncCheckpoint, Message, utc_now


def _snapshot(message: Message) -> MessageSnapshot:
    return MessageSnapshot(
        id=message.id,
        telegram_chat_id=message.telegram_chat_id,
        telegram_message_id=message.telegram_message_id,
        has_media=message.has_media,
        media_path=message.media_path,
        media_size=message.media_size,
        download_status=message.download_status,
        download_attempts=message.download_attempts,
    )


class ArchiveRepository:
    """Repository using one transaction per durable state transition."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert_chat(self, chat: ChatInfo) -> None:
        await self.upsert_chats((chat,))

    async def upsert_chats(self, chats: Sequence[ChatInfo]) -> None:
        """Refresh accessible dialog metadata in one database transaction."""

        if not chats:
            return
        chat_by_id = {chat.telegram_chat_id: chat for chat in chats}
        async with self.database.transaction() as session:
            records: dict[int, Chat] = {}
            chat_ids = tuple(chat_by_id)
            # Stay below conservative SQLite variable limits for accounts with
            # very large dialog lists while retaining a single transaction.
            for offset in range(0, len(chat_ids), 500):
                chunk = chat_ids[offset : offset + 500]
                records.update(
                    {
                        record.telegram_chat_id: record
                        for record in await session.scalars(
                            select(Chat).where(Chat.telegram_chat_id.in_(chunk))
                        )
                    }
                )
            for chat_id, chat in chat_by_id.items():
                record = records.get(chat_id)
                if record is None:
                    session.add(
                        Chat(
                            telegram_chat_id=chat.telegram_chat_id,
                            title=chat.title,
                            username=chat.username,
                            type=chat.type,
                        )
                    )
                else:
                    record.title = chat.title
                    record.username = chat.username
                    record.type = chat.type

    async def get_checkpoint(self, telegram_chat_id: int) -> int | None:
        async with self.database.sessions() as session:
            return await session.scalar(
                select(Chat.last_synced_message_id).where(Chat.telegram_chat_id == telegram_chat_id)
            )

    async def delete_chat_archive(
        self,
        telegram_chat_id: int,
    ) -> ChatArchiveDeletionTarget | None:
        """Delete one chat's messages while preserving identity and sync checkpoints.

        Media paths still referenced by another chat are omitted from the cleanup
        target so filesystem cleanup cannot break retained archive records.
        """

        async with self.database.transaction() as session:
            chat = await session.scalar(
                select(Chat).where(Chat.telegram_chat_id == telegram_chat_id)
            )
            if chat is None:
                return None
            message_count = int(
                await session.scalar(
                    select(func.count(Message.id)).where(
                        Message.telegram_chat_id == telegram_chat_id
                    )
                )
                or 0
            )
            if message_count == 0:
                return None
            recorded_paths = tuple(
                path
                for path in await session.scalars(
                    select(Message.media_path)
                    .where(
                        Message.telegram_chat_id == telegram_chat_id,
                        Message.media_path.is_not(None),
                    )
                    .distinct()
                )
                if path
            )
            shared_paths: set[str] = set()
            for offset in range(0, len(recorded_paths), 500):
                chunk = recorded_paths[offset : offset + 500]
                shared_paths.update(
                    path
                    for path in await session.scalars(
                        select(Message.media_path)
                        .where(
                            Message.telegram_chat_id != telegram_chat_id,
                            Message.media_path.in_(chunk),
                        )
                        .distinct()
                    )
                    if path
                )
            await session.execute(
                delete(Message).where(Message.telegram_chat_id == telegram_chat_id)
            )
            return ChatArchiveDeletionTarget(
                telegram_chat_id=telegram_chat_id,
                title=chat.title,
                message_count=message_count,
                media_paths=tuple(path for path in recorded_paths if path not in shared_paths),
            )

    async def advance_checkpoint(self, telegram_chat_id: int, message_id: int) -> None:
        async with self.database.transaction() as session:
            await session.execute(
                update(Chat)
                .where(Chat.telegram_chat_id == telegram_chat_id)
                .where(
                    (Chat.last_synced_message_id.is_(None))
                    | (Chat.last_synced_message_id < message_id)
                )
                .values(last_synced_message_id=message_id, updated_at=utc_now())
            )

    async def get_content_checkpoints(
        self,
        telegram_chat_id: int,
        content_types: Sequence[str],
    ) -> dict[str, int | None]:
        """Return a high-water mark for each explicit content category."""

        checkpoints = {content_type: None for content_type in content_types}
        if not checkpoints:
            return checkpoints
        async with self.database.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        ContentSyncCheckpoint.content_type,
                        ContentSyncCheckpoint.last_scanned_message_id,
                    ).where(
                        ContentSyncCheckpoint.telegram_chat_id == telegram_chat_id,
                        ContentSyncCheckpoint.content_type.in_(tuple(checkpoints)),
                    )
                )
            ).all()
        checkpoints.update({content_type: message_id for content_type, message_id in rows})
        return checkpoints

    async def advance_content_checkpoints(
        self,
        telegram_chat_id: int,
        content_types: Sequence[str],
        message_id: int,
    ) -> None:
        """Advance selected category marks monotonically in one SQLite statement."""

        if not content_types:
            return
        now = utc_now()
        statement = sqlite_insert(ContentSyncCheckpoint).values(
            [
                {
                    "telegram_chat_id": telegram_chat_id,
                    "content_type": content_type,
                    "last_scanned_message_id": message_id,
                    "created_at": now,
                    "updated_at": now,
                }
                for content_type in content_types
            ]
        )
        statement = statement.on_conflict_do_update(
            index_elements=(
                ContentSyncCheckpoint.telegram_chat_id,
                ContentSyncCheckpoint.content_type,
            ),
            set_={
                "last_scanned_message_id": func.max(
                    ContentSyncCheckpoint.last_scanned_message_id,
                    statement.excluded.last_scanned_message_id,
                ),
                "updated_at": now,
            },
        )
        async with self.database.transaction() as session:
            await session.execute(statement)

    async def upsert_message(self, data: MessageData) -> tuple[MessageSnapshot, bool]:
        async with self.database.transaction() as session:
            result = await session.execute(
                select(Message).where(
                    Message.telegram_chat_id == data.telegram_chat_id,
                    Message.telegram_message_id == data.telegram_message_id,
                )
            )
            record = result.scalar_one_or_none()
            created = record is None
            initial_status = (
                DownloadState.PENDING.value
                if data.has_media
                else DownloadState.NOT_APPLICABLE.value
            )
            if record is None:
                record = Message(
                    telegram_chat_id=data.telegram_chat_id,
                    telegram_message_id=data.telegram_message_id,
                    download_status=initial_status,
                )
                session.add(record)

            # These fields may legitimately change after a Telegram edit. Do not
            # overwrite download state here; it is updated in explicit commits.
            record.sender_id = data.sender_id
            record.sender_name = data.sender_name
            record.text = data.text
            record.message_date = data.message_date
            record.edit_date = data.edit_date
            record.reply_to_message_id = data.reply_to_message_id
            record.grouped_id = data.grouped_id
            record.has_media = data.has_media
            record.media_type = data.media_type
            if record.download_status != DownloadState.COMPLETED.value:
                record.media_size = data.media_size
            record.telegram_document_id = data.telegram_document_id
            record.mime_type = data.mime_type
            record.filename = data.original_filename
            if not data.has_media:
                record.download_status = DownloadState.NOT_APPLICABLE.value
                record.download_error = None
            await session.flush()
            return _snapshot(record), created

    async def get_message(
        self, telegram_chat_id: int, telegram_message_id: int
    ) -> MessageSnapshot | None:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(Message).where(
                    Message.telegram_chat_id == telegram_chat_id,
                    Message.telegram_message_id == telegram_message_id,
                )
            )
            record = result.scalar_one_or_none()
            return _snapshot(record) if record else None

    async def get_message_by_id(self, message_id: int) -> MessageSnapshot | None:
        async with self.database.sessions() as session:
            result = await session.execute(select(Message).where(Message.id == message_id))
            record = result.scalar_one_or_none()
            return _snapshot(record) if record else None

    async def mark_download_start(self, message_id: int, media_path: Path) -> None:
        async with self.database.transaction() as session:
            await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    download_status=DownloadState.DOWNLOADING.value,
                    download_error=None,
                    download_attempts=Message.download_attempts + 1,
                    media_path=str(media_path),
                    updated_at=utc_now(),
                )
            )

    async def mark_download_completed(
        self, message_id: int, media_path: Path, media_size: int
    ) -> None:
        async with self.database.transaction() as session:
            await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    download_status=DownloadState.COMPLETED.value,
                    download_error=None,
                    media_path=str(media_path),
                    media_size=media_size,
                    updated_at=utc_now(),
                )
            )

    async def mark_download_skipped(self, message_id: int, reason: str) -> None:
        async with self.database.transaction() as session:
            await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    download_status=DownloadState.SKIPPED.value,
                    download_error=reason,
                    updated_at=utc_now(),
                )
            )

    async def mark_download_failed(self, message_id: int, error: str) -> None:
        async with self.database.transaction() as session:
            await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    download_status=DownloadState.FAILED.value,
                    download_error=error[:4000],
                    updated_at=utc_now(),
                )
            )

    async def iter_retry_candidates(
        self,
        chat_ids: Sequence[int],
        *,
        failed_only: bool = False,
        batch_size: int = 500,
    ) -> list[RetryCandidate]:
        """Return lightweight media candidates for repair/retry.

        Results are deliberately not ORM instances so no session remains open
        while Telegram and filesystem operations are performed.
        """

        if not chat_ids:
            return []
        statuses = (
            (DownloadState.FAILED.value,)
            if failed_only
            else (
                DownloadState.PENDING.value,
                DownloadState.DOWNLOADING.value,
                DownloadState.FAILED.value,
                DownloadState.COMPLETED.value,
            )
        )
        candidates: list[RetryCandidate] = []
        last_id = 0
        while True:
            async with self.database.sessions() as session:
                rows = (
                    await session.execute(
                        select(
                            Message.id,
                            Message.telegram_chat_id,
                            Message.telegram_message_id,
                            Message.media_path,
                            Message.download_status,
                            Message.media_type,
                        )
                        .where(
                            Message.id > last_id,
                            Message.telegram_chat_id.in_(chat_ids),
                            Message.has_media.is_(True),
                            Message.download_status.in_(statuses),
                        )
                        .order_by(Message.id)
                        .limit(batch_size)
                    )
                ).all()
            if not rows:
                break
            candidates.extend(RetryCandidate(*row) for row in rows)
            last_id = rows[-1].id
        return candidates

    async def completed_video_paths(self) -> tuple[tuple[str, int], ...]:
        """Return ``(media_path, media_size)`` for every completed video file.

        Results are plain rows so no session stays open while the caller
        performs filesystem work.
        """
        statement = (
            select(Message.media_path, Message.media_size)
            .where(
                Message.download_status == DownloadState.COMPLETED.value,
                Message.media_path.is_not(None),
                or_(
                    Message.media_type.in_(("video", "video_note")),
                    func.lower(func.coalesce(Message.mime_type, "")).like("video/%"),
                ),
            )
            .order_by(Message.id)
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return tuple((path, int(size or 0)) for path, size in rows if path)

    async def stats(self) -> ArchiveStats:
        async with self.database.sessions() as session:
            total_messages = int(await session.scalar(select(func.count(Message.id))) or 0)
            downloaded_files = int(
                await session.scalar(
                    select(func.count(Message.id)).where(
                        Message.download_status == DownloadState.COMPLETED.value
                    )
                )
                or 0
            )
            downloaded_bytes = int(
                await session.scalar(
                    select(func.coalesce(func.sum(Message.media_size), 0)).where(
                        Message.download_status == "completed"
                    )
                )
                or 0
            )
            failed = int(
                await session.scalar(
                    select(func.count(Message.id)).where(
                        Message.download_status == DownloadState.FAILED.value
                    )
                )
                or 0
            )
            skipped = int(
                await session.scalar(
                    select(func.count(Message.id)).where(
                        Message.download_status == DownloadState.SKIPPED.value
                    )
                )
                or 0
            )

            newest_rows = (
                await session.execute(
                    select(
                        Chat.telegram_chat_id,
                        Chat.title,
                        Message.telegram_message_id,
                        Message.message_date,
                    )
                    .outerjoin(
                        Message,
                        (Message.telegram_chat_id == Chat.telegram_chat_id)
                        & (
                            Message.telegram_message_id
                            == select(func.max(Message.telegram_message_id))
                            .where(Message.telegram_chat_id == Chat.telegram_chat_id)
                            .correlate(Chat)
                            .scalar_subquery()
                        ),
                    )
                    .order_by(Chat.title)
                )
            ).all()
        return ArchiveStats(
            total_messages=total_messages,
            downloaded_files=downloaded_files,
            downloaded_bytes=downloaded_bytes,
            failed_downloads=failed,
            skipped_downloads=skipped,
            newest_by_chat=tuple(ChatNewest(*row) for row in newest_rows),
        )
