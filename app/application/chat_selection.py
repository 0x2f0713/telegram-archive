"""Discover accessible Telegram dialogs and resolve durable archive targets."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.domain import ChatInfo, ChatSelection

DialogLoader = Callable[[object], Awaitable[Sequence[ChatInfo]]]
ChatResolver = Callable[[Iterable[ChatInfo], Iterable[int]], dict[int, ChatInfo]]
ClientFactory = Callable[[], object]
ClientConnector = Callable[[object], Awaitable[None]]


class ChatArchiveWriter(Protocol):
    async def upsert_chats(self, chats: Sequence[ChatInfo]) -> None: ...


class ChatSelectionStore(Protocol):
    async def policy(self) -> ChatSelection: ...


@dataclass(frozen=True, slots=True)
class ChatDiscovery:
    """Current Telegram dialogs together with the effective archive policy."""

    dialogs: tuple[ChatInfo, ...]
    policy: ChatSelection
    effective_chat_ids: tuple[int, ...]


class ChatSelectionService:
    """Single policy resolver used by CLI, web, TUI, sync, and listener."""

    def __init__(
        self,
        configured_chat_ids: tuple[int, ...],
        archive: ChatArchiveWriter,
        selections: ChatSelectionStore,
        dialog_loader: DialogLoader,
        chat_resolver: ChatResolver,
        *,
        client_factory: ClientFactory | None = None,
        client_connector: ClientConnector | None = None,
    ) -> None:
        self.configured_chat_ids = configured_chat_ids
        self.archive = archive
        self.selections = selections
        self.dialog_loader = dialog_loader
        self.chat_resolver = chat_resolver
        self.client_factory = client_factory
        self.client_connector = client_connector

    async def discover_with_client(self, client: object) -> ChatDiscovery:
        dialogs = tuple(await self.dialog_loader(client))
        await self.archive.upsert_chats(dialogs)
        policy = await self.selections.policy()
        effective_ids = policy.effective_ids(
            legacy_ids=self.configured_chat_ids,
            available_ids=(dialog.telegram_chat_id for dialog in dialogs),
        )
        return ChatDiscovery(dialogs, policy, effective_ids)

    async def discover(self) -> ChatDiscovery:
        """Open the local authorized session and refresh accessible dialogs."""

        if self.client_factory is None or self.client_connector is None:
            raise RuntimeError("This chat-selection service has no managed Telegram client")
        client = self.client_factory()
        try:
            await self.client_connector(client)
            return await self.discover_with_client(client)
        finally:
            await client.disconnect()  # type: ignore[attr-defined]

    async def resolve_with_client(self, client: object) -> dict[int, ChatInfo]:
        discovery = await self.discover_with_client(client)
        return self.chat_resolver(discovery.dialogs, discovery.effective_chat_ids)

    def validate_specific(
        self, discovery: ChatDiscovery, chat_ids: Iterable[int]
    ) -> tuple[int, ...]:
        """Return normalized IDs only after checking current Telegram visibility."""

        selected = tuple(dict.fromkeys(int(chat_id) for chat_id in chat_ids))
        self.chat_resolver(discovery.dialogs, selected)
        return selected
