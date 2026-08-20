"""Read-optimized queries shared by the web dashboard and terminal UI."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import String, case, cast, func, or_, select

from app.application.dashboard import (
    GALLERY_IMAGE_MIME_TYPES,
    ActivityPoint,
    ArchivedChatPage,
    ChatMediaQuery,
    ChatSummary,
    MessagePage,
    MessageQuery,
    MessageView,
)
from app.domain import DownloadState, display_chat_title
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import Chat, Message, utc_now


def _message_columns():
    return (
        Message.id,
        Message.telegram_chat_id,
        func.coalesce(Chat.title, "Unknown chat").label("chat_title"),
        Message.telegram_message_id,
        Message.sender_id,
        Message.sender_name,
        Message.text,
        Message.message_date,
        Message.edit_date,
        Message.reply_to_message_id,
        Message.grouped_id,
        Message.has_media,
        Message.media_type,
        Message.media_path,
        Message.media_variant_path,
        Message.media_size,
        Message.mime_type,
        Message.filename,
        Message.download_status,
        Message.download_error,
        Message.download_attempts,
        Message.terabox_remote_path,
        Message.terabox_variant_remote_path,
    )


class DashboardRepository:
    """Parameterized read model for archive exploration surfaces."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def chat_summaries(self) -> tuple[ChatSummary, ...]:
        aggregate = (
            select(
                Message.telegram_chat_id.label("chat_id"),
                func.count(Message.id).label("message_count"),
                func.sum(case((Message.has_media.is_(True), 1), else_=0)).label("media_count"),
                func.sum(
                    case((Message.download_status == DownloadState.COMPLETED.value, 1), else_=0)
                ).label("completed_count"),
                func.sum(
                    case((Message.download_status == DownloadState.FAILED.value, 1), else_=0)
                ).label("failed_count"),
                func.max(Message.message_date).label("newest_message_date"),
            )
            .group_by(Message.telegram_chat_id)
            .subquery()
        )
        statement = (
            select(
                Chat.telegram_chat_id,
                Chat.title,
                Chat.username,
                Chat.type,
                Chat.last_synced_message_id,
                func.coalesce(aggregate.c.message_count, 0),
                func.coalesce(aggregate.c.media_count, 0),
                func.coalesce(aggregate.c.completed_count, 0),
                func.coalesce(aggregate.c.failed_count, 0),
                aggregate.c.newest_message_date,
            )
            .outerjoin(aggregate, aggregate.c.chat_id == Chat.telegram_chat_id)
            .order_by(Chat.title.collate("NOCASE"))
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return tuple(
            ChatSummary(
                row.telegram_chat_id,
                display_chat_title(
                    row.telegram_chat_id,
                    row.title,
                    row.username,
                    row.type,
                ),
                row.username,
                row.type,
                row.last_synced_message_id,
                row[5],
                row[6],
                row[7],
                row[8],
                row[9],
            )
            for row in rows
        )

    async def archived_chat_summaries(
        self,
        search: str = "",
        page: int = 1,
        page_size: int = 50,
        include_chat_id: int | None = None,
    ) -> ArchivedChatPage:
        """Return one searchable page of chats with archived messages."""

        normalized_search = search.strip()[:100]
        normalized_page = max(1, page)
        normalized_page_size = min(100, max(1, page_size))
        aggregate = (
            select(
                Message.telegram_chat_id.label("chat_id"),
                func.count(Message.id).label("message_count"),
                func.sum(case((Message.has_media.is_(True), 1), else_=0)).label("media_count"),
                func.sum(
                    case((Message.download_status == DownloadState.COMPLETED.value, 1), else_=0)
                ).label("completed_count"),
                func.sum(
                    case((Message.download_status == DownloadState.FAILED.value, 1), else_=0)
                ).label("failed_count"),
                func.max(Message.message_date).label("newest_message_date"),
            )
            .group_by(Message.telegram_chat_id)
            .subquery()
        )
        archive_condition = func.coalesce(aggregate.c.message_count, 0) > 0
        if include_chat_id is not None:
            archive_condition = or_(
                archive_condition,
                Chat.telegram_chat_id == include_chat_id,
            )
        statement = (
            select(
                Chat.telegram_chat_id,
                Chat.title,
                Chat.username,
                Chat.type,
                Chat.last_synced_message_id,
                func.coalesce(aggregate.c.message_count, 0),
                func.coalesce(aggregate.c.media_count, 0),
                func.coalesce(aggregate.c.completed_count, 0),
                func.coalesce(aggregate.c.failed_count, 0),
                aggregate.c.newest_message_date,
            )
            .outerjoin(aggregate, aggregate.c.chat_id == Chat.telegram_chat_id)
            .where(archive_condition)
        )
        count_statement = (
            select(func.count(Chat.id))
            .outerjoin(aggregate, aggregate.c.chat_id == Chat.telegram_chat_id)
            .where(archive_condition)
        )
        if normalized_search:
            pattern = f"%{normalized_search}%"
            search_condition = or_(
                Chat.title.ilike(pattern),
                Chat.username.ilike(pattern),
                Chat.type.ilike(pattern),
                cast(Chat.telegram_chat_id, String).ilike(pattern),
            )
            statement = statement.where(search_condition)
            count_statement = count_statement.where(search_condition)
        order_by = []
        if include_chat_id is not None:
            order_by.append(case((Chat.telegram_chat_id == include_chat_id, 0), else_=1))
        async with self.database.sessions() as session:
            total = int(await session.scalar(count_statement) or 0)
            pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
            normalized_page = min(normalized_page, pages)
            statement = (
                statement.order_by(
                    *order_by,
                    aggregate.c.newest_message_date.desc(),
                    Chat.title.collate("NOCASE"),
                )
                .offset((normalized_page - 1) * normalized_page_size)
                .limit(normalized_page_size)
            )
            rows = (await session.execute(statement)).all()
        return ArchivedChatPage(
            items=tuple(
                ChatSummary(
                    row.telegram_chat_id,
                    display_chat_title(
                        row.telegram_chat_id,
                        row.title,
                        row.username,
                        row.type,
                    ),
                    row.username,
                    row.type,
                    row.last_synced_message_id,
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                )
                for row in rows
            ),
            total=total,
            page=normalized_page,
            page_size=normalized_page_size,
        )

    @staticmethod
    def _message_conditions(query: MessageQuery):
        conditions = []
        if query.chat_id is not None:
            conditions.append(Message.telegram_chat_id == query.chat_id)
        if query.status:
            conditions.append(Message.download_status == query.status)
        if query.media_type:
            conditions.append(Message.media_type == query.media_type)
        if query.media_only:
            conditions.append(Message.has_media.is_(True))
        if query.since:
            conditions.append(Message.message_date >= datetime.combine(query.since, time.min))
        if query.until:
            conditions.append(
                Message.message_date < datetime.combine(query.until + timedelta(days=1), time.min)
            )
        if query.search:
            pattern = f"%{query.search}%"
            conditions.append(
                or_(
                    Message.text.ilike(pattern),
                    Message.sender_name.ilike(pattern),
                    Message.filename.ilike(pattern),
                    Chat.title.ilike(pattern),
                )
            )
        return conditions

    async def messages(self, query: MessageQuery | None = None) -> MessagePage:
        query = (query or MessageQuery()).normalized()
        conditions = self._message_conditions(query)
        join_condition = Chat.telegram_chat_id == Message.telegram_chat_id
        order_by = {
            "oldest": (Message.message_date.asc(), Message.telegram_message_id.asc()),
            "largest": (
                Message.media_size.is_(None),
                Message.media_size.desc(),
                Message.message_date.desc(),
            ),
            "most_retried": (
                Message.download_attempts.desc(),
                Message.message_date.desc(),
            ),
        }.get(query.sort, (Message.message_date.desc(), Message.telegram_message_id.desc()))
        statement = (
            select(*_message_columns())
            .select_from(Message)
            .outerjoin(Chat, join_condition)
            .where(*conditions)
            .order_by(*order_by)
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
        )
        count_statement = (
            select(func.count(Message.id))
            .select_from(Message)
            .outerjoin(Chat, join_condition)
            .where(*conditions)
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
            total = int(await session.scalar(count_statement) or 0)
        return MessagePage(
            items=tuple(MessageView(*row) for row in rows),
            total=total,
            page=query.page,
            page_size=query.page_size,
        )

    async def chat_media(self, query: ChatMediaQuery) -> MessagePage:
        """Return one newest-first page of browser-previewable media for a chat."""

        query = query.normalized()
        normalized_mime = func.lower(func.coalesce(Message.mime_type, ""))
        photo_condition = normalized_mime.in_(GALLERY_IMAGE_MIME_TYPES)
        video_condition = normalized_mime.like("video/%")
        kind_condition = {
            "photos": photo_condition,
            "videos": video_condition,
        }.get(query.kind, or_(photo_condition, video_condition))
        conditions = (
            Message.telegram_chat_id == query.chat_id,
            Message.download_status == DownloadState.COMPLETED.value,
            or_(Message.media_path.is_not(None), Message.terabox_remote_path.is_not(None)),
            kind_condition,
        )
        count_statement = select(func.count(Message.id)).where(*conditions)
        async with self.database.sessions() as session:
            total = int(await session.scalar(count_statement) or 0)
            pages = max(1, (total + query.page_size - 1) // query.page_size)
            page = min(query.page, pages)
            statement = (
                select(*_message_columns())
                .select_from(Message)
                .outerjoin(Chat, Chat.telegram_chat_id == Message.telegram_chat_id)
                .where(*conditions)
                .order_by(Message.message_date.desc(), Message.telegram_message_id.desc())
                .offset((page - 1) * query.page_size)
                .limit(query.page_size)
            )
            rows = (await session.execute(statement)).all()
        return MessagePage(
            items=tuple(MessageView(*row) for row in rows),
            total=total,
            page=page,
            page_size=query.page_size,
        )

    async def message(self, message_id: int) -> MessageView | None:
        statement = (
            select(*_message_columns())
            .select_from(Message)
            .outerjoin(Chat, Chat.telegram_chat_id == Message.telegram_chat_id)
            .where(Message.id == message_id)
        )
        async with self.database.sessions() as session:
            row = (await session.execute(statement)).one_or_none()
        return MessageView(*row) if row else None

    async def chat_summary(self, telegram_chat_id: int) -> ChatSummary | None:
        """Return one chat's archive summary, or ``None`` when unknown."""
        try:
            return next(
                summary
                for summary in await self.chat_summaries()
                if summary.telegram_chat_id == telegram_chat_id
            )
        except StopIteration:
            return None

    async def chat_thread(
        self,
        telegram_chat_id: int,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> MessagePage:
        """Return one chat's messages newest-first, ready for conversation view.

        ``page`` counts from the newest end: page 1 is the most recent batch.
        Items are returned in ascending (oldest -> newest) order so templates
        can render a thread that reads top to bottom like Telegram.
        """
        page = max(1, page)
        page_size = min(100, max(1, page_size))
        order_by = (Message.message_date.desc(), Message.telegram_message_id.desc())
        statement = (
            select(*_message_columns())
            .select_from(Message)
            .outerjoin(Chat, Chat.telegram_chat_id == Message.telegram_chat_id)
            .where(Message.telegram_chat_id == telegram_chat_id)
            .order_by(*order_by)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        count_statement = select(func.count(Message.id)).where(
            Message.telegram_chat_id == telegram_chat_id
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
            total = int(await session.scalar(count_statement) or 0)
        return MessagePage(
            items=tuple(MessageView(*row) for row in reversed(rows)),
            total=total,
            page=page,
            page_size=page_size,
        )

    async def resolve_reply_targets(
        self, telegram_chat_id: int, telegram_message_ids: tuple[int, ...]
    ) -> dict[int, int]:
        """Map Telegram message IDs to local archive IDs within one chat."""
        if not telegram_message_ids:
            return {}
        statement = select(Message.telegram_message_id, Message.id).where(
            Message.telegram_chat_id == telegram_chat_id,
            Message.telegram_message_id.in_(telegram_message_ids),
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return {int(telegram_id): int(local_id) for telegram_id, local_id in rows}

    async def status_counts(self) -> tuple[tuple[str, int], ...]:
        statement = (
            select(Message.download_status, func.count(Message.id))
            .group_by(Message.download_status)
            .order_by(func.count(Message.id).desc())
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return tuple((status, int(count)) for status, count in rows)

    async def media_counts(self) -> tuple[tuple[str, int], ...]:
        statement = (
            select(Message.media_type, func.count(Message.id))
            .where(Message.media_type.is_not(None))
            .group_by(Message.media_type)
            .order_by(func.count(Message.id).desc())
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return tuple((media_type, int(count)) for media_type, count in rows if media_type)

    async def attention_messages(self, limit: int = 12) -> tuple[MessageView, ...]:
        """Return media records that currently need operator attention."""

        limit = min(100, max(1, limit))
        priority = case(
            (Message.download_status == DownloadState.FAILED.value, 0),
            (Message.download_status == DownloadState.DOWNLOADING.value, 1),
            else_=2,
        )
        statement = (
            select(*_message_columns())
            .select_from(Message)
            .outerjoin(Chat, Chat.telegram_chat_id == Message.telegram_chat_id)
            .where(
                Message.download_status.in_(
                    (
                        DownloadState.FAILED.value,
                        DownloadState.DOWNLOADING.value,
                        DownloadState.PENDING.value,
                    )
                )
            )
            .order_by(priority, Message.updated_at.desc())
            .limit(limit)
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return tuple(MessageView(*row) for row in rows)

    async def completed_media_paths(self) -> tuple[str, ...]:
        statement = select(Message.media_path).where(
            Message.download_status == DownloadState.COMPLETED.value,
            Message.media_path.is_not(None),
        )
        async with self.database.sessions() as session:
            rows = (await session.scalars(statement)).all()
        return tuple(path for path in rows if path)

    async def album_messages(
        self, telegram_chat_id: int, grouped_id: int
    ) -> tuple[MessageView, ...]:
        statement = (
            select(*_message_columns())
            .select_from(Message)
            .outerjoin(Chat, Chat.telegram_chat_id == Message.telegram_chat_id)
            .where(
                Message.telegram_chat_id == telegram_chat_id,
                Message.grouped_id == grouped_id,
            )
            .order_by(Message.telegram_message_id)
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return tuple(MessageView(*row) for row in rows)

    async def activity(self, days: int = 14) -> tuple[ActivityPoint, ...]:
        days = min(90, max(1, days))
        today = utc_now().date()
        first_day = today - timedelta(days=days - 1)
        statement = (
            select(func.date(Message.message_date), func.count(Message.id))
            .where(Message.message_date >= datetime.combine(first_day, datetime.min.time()))
            .group_by(func.date(Message.message_date))
            .order_by(func.date(Message.message_date))
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
        counts = {date.fromisoformat(day): int(count) for day, count in rows}
        return tuple(
            ActivityPoint(
                day=first_day + timedelta(days=offset),
                count=counts.get(first_day + timedelta(days=offset), 0),
            )
            for offset in range(days)
        )
