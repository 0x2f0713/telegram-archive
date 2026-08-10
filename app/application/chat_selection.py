"""Discover accessible Telegram dialogs and resolve durable archive targets."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from telethon import TelegramClient

from app.config import Settings
from app.domain import ChatInfo
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.persistence.selection import ChatSelection, ChatSelectionRepository
from app.infrastructure.telegram.client import (
    accessible_dialogs,
    connect_authorized,
    create_client,
    resolve_accessible_chats,
)


@dataclass(frozen=True, slots=True)
class ChatDiscovery:
    """Current Telegram dialogs together with the effective archive policy."""

    dialogs: tuple[ChatInfo, ...]
    policy: ChatSelection
    effective_chat_ids: tuple[int, ...]


class ChatSelectionService:
    """Single policy resolver used by CLI, web, TUI, sync, and listener."""

    def __init__(self, settings: Settings, database_repository: ArchiveRepository) -> None:
        self.settings = settings
        self.archive = database_repository
        self.selections = ChatSelectionRepository(database_repository.database)

    async def discover_with_client(self, client: TelegramClient) -> ChatDiscovery:
        dialogs = tuple(await accessible_dialogs(client))
        await self.archive.upsert_chats(dialogs)
        policy = await self.selections.policy()
        effective_ids = policy.effective_ids(
            legacy_ids=self.settings.configured_chat_ids,
            available_ids=(dialog.telegram_chat_id for dialog in dialogs),
        )
        return ChatDiscovery(dialogs, policy, effective_ids)

    async def discover(self) -> ChatDiscovery:
        """Open the local authorized session and refresh accessible dialogs."""

        client = create_client(self.settings)
        try:
            await connect_authorized(client)
            return await self.discover_with_client(client)
        finally:
            await client.disconnect()

    async def resolve_with_client(self, client: TelegramClient) -> dict[int, ChatInfo]:
        discovery = await self.discover_with_client(client)
        return resolve_accessible_chats(discovery.dialogs, discovery.effective_chat_ids)

    def validate_specific(
        self, discovery: ChatDiscovery, chat_ids: Iterable[int]
    ) -> tuple[int, ...]:
        """Return normalized IDs only after checking current Telegram visibility."""

        selected = tuple(dict.fromkeys(int(chat_id) for chat_id in chat_ids))
        resolve_accessible_chats(discovery.dialogs, selected)
        return selected
