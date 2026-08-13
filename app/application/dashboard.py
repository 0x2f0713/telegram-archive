"""Presentation-neutral archive queries and dashboard orchestration.

The application owns these query objects and result records. Persistence
adapters implement the protocols below, while web and terminal interfaces only
consume the application-facing vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.application.archive_records import ArchiveStats


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
class ArchivedChatPage:
    items: tuple[ChatSummary, ...]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


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
        sort = self.sort.strip().casefold()
        return MessageQuery(
            search=self.search.strip()[:200],
            chat_id=self.chat_id,
            status=self.status.strip().casefold()[:32],
            media_type=self.media_type.strip().casefold()[:32],
            media_only=self.media_only,
            since=self.since,
            until=self.until,
            sort=sort if sort in {"newest", "oldest", "largest", "most_retried"} else "newest",
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


class ArchiveStatsReader(Protocol):
    async def stats(self) -> ArchiveStats: ...


class DashboardReader(Protocol):
    async def chat_summaries(self) -> tuple[ChatSummary, ...]: ...

    async def archived_chat_summaries(
        self,
        search: str = "",
        page: int = 1,
        page_size: int = 50,
        include_chat_id: int | None = None,
    ) -> ArchivedChatPage: ...

    async def messages(self, query: MessageQuery | None = None) -> MessagePage: ...

    async def attention_messages(self, limit: int = 12) -> tuple[MessageView, ...]: ...

    async def status_counts(self) -> tuple[tuple[str, int], ...]: ...

    async def media_counts(self) -> tuple[tuple[str, int], ...]: ...

    async def activity(self, days: int = 14) -> tuple[ActivityPoint, ...]: ...


class ChatSelectionReader(Protocol):
    async def effective_known_ids(self, legacy_ids: tuple[int, ...]) -> tuple[int, ...]: ...


class DashboardService:
    """Build complete dashboard snapshots from injected read ports."""

    def __init__(
        self,
        archive: ArchiveStatsReader,
        dashboard: DashboardReader,
        selections: ChatSelectionReader,
        configured_chat_ids: tuple[int, ...],
    ) -> None:
        self.archive = archive
        self.dashboard = dashboard
        self.selections = selections
        self.legacy_chat_ids = configured_chat_ids

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
