"""Durable chat-selection policy persistence shared by every runtime surface."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select

from app.domain import ChatSelection, SelectionMode
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import (
    ArchiveSelectionPolicy,
    Chat,
    SelectedChat,
    utc_now,
)

__all__ = ["ChatSelection", "ChatSelectionRepository", "SelectionMode"]


class ChatSelectionRepository:
    """Read and atomically replace the singleton chat-selection policy."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def policy(self) -> ChatSelection:
        async with self.database.sessions() as session:
            policy = await session.get(ArchiveSelectionPolicy, 1)
            if policy is None:
                return ChatSelection(SelectionMode.ENVIRONMENT)
            if policy.mode not in {"specific", "all"}:
                raise RuntimeError(
                    "Archive selection policy is invalid; restore the database or select chats again"
                )
            selected = tuple(
                await session.scalars(
                    select(SelectedChat.telegram_chat_id).order_by(SelectedChat.telegram_chat_id)
                )
            )
        mode = SelectionMode.ALL if policy.mode == "all" else SelectionMode.SPECIFIC
        return ChatSelection(mode, selected if mode == SelectionMode.SPECIFIC else ())

    async def known_chat_ids(self) -> tuple[int, ...]:
        async with self.database.sessions() as session:
            values = await session.scalars(
                select(Chat.telegram_chat_id).order_by(Chat.telegram_chat_id)
            )
            return tuple(values)

    async def effective_known_ids(self, legacy_ids: Iterable[int]) -> tuple[int, ...]:
        policy = await self.policy()
        known_ids = await self.known_chat_ids() if policy.mode == SelectionMode.ALL else ()
        return policy.effective_ids(legacy_ids=legacy_ids, available_ids=known_ids)

    async def set_specific(self, chat_ids: Iterable[int]) -> ChatSelection:
        selected = tuple(sorted(set(int(chat_id) for chat_id in chat_ids)))
        async with self.database.transaction() as session:
            await session.execute(delete(SelectedChat))
            policy = await session.get(ArchiveSelectionPolicy, 1)
            if policy is None:
                session.add(ArchiveSelectionPolicy(id=1, mode="specific"))
            else:
                policy.mode = "specific"
                policy.updated_at = utc_now()
            session.add_all(SelectedChat(telegram_chat_id=chat_id) for chat_id in selected)
        return ChatSelection(SelectionMode.SPECIFIC, selected)

    async def set_all(self) -> ChatSelection:
        async with self.database.transaction() as session:
            await session.execute(delete(SelectedChat))
            policy = await session.get(ArchiveSelectionPolicy, 1)
            if policy is None:
                session.add(ArchiveSelectionPolicy(id=1, mode="all"))
            else:
                policy.mode = "all"
                policy.updated_at = utc_now()
        return ChatSelection(SelectionMode.ALL)

    async def use_environment(self) -> ChatSelection:
        async with self.database.transaction() as session:
            await session.execute(delete(SelectedChat))
            await session.execute(
                delete(ArchiveSelectionPolicy).where(ArchiveSelectionPolicy.id == 1)
            )
        return ChatSelection(SelectionMode.ENVIRONMENT)
