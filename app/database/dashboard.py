"""Read-optimized queries shared by the web dashboard and terminal UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import case, func, or_, select

from app.database.models import Chat, Message, utc_now
from app.database.repository import ArchiveRepository, ArchiveStats
from app.database.selection import ChatSelectionRepository
from app.database.session import Database
from app.telegram.entities import display_chat_title


@dataclass(frozen=True, slots=True)
class ChatSummary:
    telegram_chat_id: int
    title: str
    username: str | None
    type: str
    last_synced_message_id: int | None
    message_count: int
    media_count: int
    completed_count: int
    failed_count: int
    newest_message_date: datetime | None


@dataclass(frozen=True, slots=True)
class MessageView:
    id: int
    telegram_chat_id: int
    chat_title: str
    telegram_message_id: int
    sender_id: int | None
    sender_name: str | None
    text: str | None
    message_date: datetime
    edit_date: datetime | None
    reply_to_message_id: int | None
    grouped_id: int | None
    has_media: bool
    media_type: str | None
    media_path: str | None
    media_size: int | None
    mime_type: str | None
    filename: str | None
    download_status: str
    download_error: str | None
    download_attempts: int


@dataclass(frozen=True, slots=True)
class MessageQuery:
    search: str = ""
    chat_id: int | None = None
    status: str = ""
    media_type: str = ""
    media_only: bool = False
    since: date | None = None
    until: date | None = None
    sort: str = "newest"
    page: int = 1
    page_size: int = 30

    def normalized(self) -> MessageQuery:
        return MessageQuery(
            search=self.search.strip()[:200],
            chat_id=self.chat_id,
            status=self.status.strip().casefold()[:32],
            media_type=self.media_type.strip().casefold()[:32],
            media_only=self.media_only,
            since=self.since,
            until=self.until,
            sort=(
                self.sort.strip().casefold()
                if self.sort.strip().casefold() in {"newest", "oldest", "largest", "most_retried"}
                else "newest"
            ),
            page=max(1, self.page),
            page_size=min(100, max(1, self.page_size)),
        )


@dataclass(frozen=True, slots=True)
class MessagePage:
    items: tuple[MessageView, ...]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


@dataclass(frozen=True, slots=True)
class ActivityPoint:
    day: date
    count: int


@dataclass(frozen=True, slots=True)
class DashboardOverview:
    stats: ArchiveStats
    chats: tuple[ChatSummary, ...]
    recent_messages: tuple[MessageView, ...]
    attention_messages: tuple[MessageView, ...]
    status_counts: tuple[tuple[str, int], ...]
    media_counts: tuple[tuple[str, int], ...]
    activity: tuple[ActivityPoint, ...]
    configured_chat_ids: tuple[int, ...]


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
        Message.media_size,
        Message.mime_type,
        Message.filename,
        Message.download_status,
        Message.download_error,
        Message.download_attempts,
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
                func.sum(case((Message.download_status == "completed", 1), else_=0)).label(
                    "completed_count"
                ),
                func.sum(case((Message.download_status == "failed", 1), else_=0)).label(
                    "failed_count"
                ),
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
            (Message.download_status == "failed", 0),
            (Message.download_status == "downloading", 1),
            else_=2,
        )
        statement = (
            select(*_message_columns())
            .select_from(Message)
            .outerjoin(Chat, Chat.telegram_chat_id == Message.telegram_chat_id)
            .where(Message.download_status.in_(("failed", "downloading", "pending")))
            .order_by(priority, Message.updated_at.desc())
            .limit(limit)
        )
        async with self.database.sessions() as session:
            rows = (await session.execute(statement)).all()
        return tuple(MessageView(*row) for row in rows)

    async def completed_media_paths(self) -> tuple[str, ...]:
        statement = select(Message.media_path).where(
            Message.download_status == "completed",
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


class DashboardService:
    """Build complete, presentation-agnostic dashboard snapshots."""

    def __init__(self, database: Database, configured_chat_ids: tuple[int, ...]) -> None:
        self.database = database
        self.legacy_chat_ids = configured_chat_ids
        self.dashboard = DashboardRepository(database)
        self.archive = ArchiveRepository(database)
        self.selections = ChatSelectionRepository(database)

    async def overview(self, days: int = 14) -> DashboardOverview:
        stats = await self.archive.stats()
        chats = await self.dashboard.chat_summaries()
        recent = await self.dashboard.messages(MessageQuery(page_size=10))
        configured_chat_ids = await self.selections.effective_known_ids(self.legacy_chat_ids)
        return DashboardOverview(
            stats=stats,
            chats=chats,
            recent_messages=recent.items,
            attention_messages=await self.dashboard.attention_messages(),
            status_counts=await self.dashboard.status_counts(),
            media_counts=await self.dashboard.media_counts(),
            activity=await self.dashboard.activity(days),
            configured_chat_ids=configured_chat_ids,
        )
