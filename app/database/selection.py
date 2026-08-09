"""Durable chat-selection policy shared by every runtime surface."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, select

from app.database.models import ArchiveSelectionPolicy, Chat, SelectedChat, utc_now
from app.database.session import Database

SelectionMode = Literal["environment", "specific", "all"]


@dataclass(frozen=True, slots=True)
class ChatSelection:
    """Persisted selection state; ``environment`` means no database override."""

    mode: SelectionMode
    selected_chat_ids: tuple[int, ...] = ()

    def effective_ids(
        self,
        *,
        legacy_ids: Iterable[int],
        available_ids: Iterable[int],
    ) -> tuple[int, ...]:
        """Resolve this policy against a known or currently accessible ID set."""

        if self.mode == "environment":
            source = legacy_ids
        elif self.mode == "specific":
            source = self.selected_chat_ids
        else:
            source = available_ids
        return tuple(dict.fromkeys(int(chat_id) for chat_id in source))


class ChatSelectionRepository:
    """Read and atomically replace the singleton chat-selection policy."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def policy(self) -> ChatSelection:
        async with self.database.sessions() as session:
            policy = await session.get(ArchiveSelectionPolicy, 1)
            if policy is None:
                return ChatSelection("environment")
            if policy.mode not in {"specific", "all"}:
                raise RuntimeError(
                    "Archive selection policy is invalid; restore the database or select chats again"
                )
            selected = tuple(
                await session.scalars(
                    select(SelectedChat.telegram_chat_id).order_by(SelectedChat.telegram_chat_id)
                )
            )
        mode: SelectionMode = "all" if policy.mode == "all" else "specific"
        return ChatSelection(mode, selected if mode == "specific" else ())

    async def known_chat_ids(self) -> tuple[int, ...]:
        async with self.database.sessions() as session:
            values = await session.scalars(
                select(Chat.telegram_chat_id).order_by(Chat.telegram_chat_id)
            )
            return tuple(values)

    async def effective_known_ids(self, legacy_ids: Iterable[int]) -> tuple[int, ...]:
        policy = await self.policy()
        known_ids = await self.known_chat_ids() if policy.mode == "all" else ()
        return policy.effective_ids(legacy_ids=legacy_ids, available_ids=known_ids)

    async def set_specific(self, chat_ids: Iterable[int]) -> ChatSelection:
        selected = tuple(sorted(set(int(chat_id) for chat_id in chat_ids)))
        async with self.database.sessions() as session, session.begin():
            await session.execute(delete(SelectedChat))
            policy = await session.get(ArchiveSelectionPolicy, 1)
            if policy is None:
                session.add(ArchiveSelectionPolicy(id=1, mode="specific"))
            else:
                policy.mode = "specific"
                policy.updated_at = utc_now()
            session.add_all(SelectedChat(telegram_chat_id=chat_id) for chat_id in selected)
        return ChatSelection("specific", selected)

    async def set_all(self) -> ChatSelection:
        async with self.database.sessions() as session, session.begin():
            await session.execute(delete(SelectedChat))
            policy = await session.get(ArchiveSelectionPolicy, 1)
            if policy is None:
                session.add(ArchiveSelectionPolicy(id=1, mode="all"))
            else:
                policy.mode = "all"
                policy.updated_at = utc_now()
        return ChatSelection("all")

    async def use_environment(self) -> ChatSelection:
        async with self.database.sessions() as session, session.begin():
            await session.execute(delete(SelectedChat))
            await session.execute(
                delete(ArchiveSelectionPolicy).where(ArchiveSelectionPolicy.id == 1)
            )
        return ChatSelection("environment")
