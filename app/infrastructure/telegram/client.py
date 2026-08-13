"""Safe Telethon client construction, authentication, and dialog resolution."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError

from app.config import Settings
from app.domain import ChatInfo
from app.infrastructure.telegram.translation import chat_info

logger = logging.getLogger(__name__)


def flood_wait_seconds(error: Exception) -> int | None:
    """Translate Telethon's rate-limit exception into an adapter-neutral delay."""

    return max(1, int(error.seconds)) if isinstance(error, FloodWaitError) else None


def is_transient_telegram_error(error: Exception) -> bool:
    """Return whether a Telegram call is safe to retry after a short delay."""

    return isinstance(error, (RPCError, ConnectionError, TimeoutError))


class TelegramAccessError(RuntimeError):
    """Raised when the authenticated account cannot access a requested chat."""


def create_client(settings: Settings) -> TelegramClient:
    api_id, api_hash = settings.require_telegram_credentials()
    session_path = settings.tg_session_name.expanduser()
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        auto_reconnect=True,
        connection_retries=10,
        retry_delay=2,
        request_retries=5,
        flood_sleep_threshold=60,
        catch_up=True,
    )


async def login(client: TelegramClient) -> None:
    """Run Telethon's interactive user login without handling secrets ourselves."""

    await client.start()
    me = await client.get_me()
    identity = f"@{me.username}" if getattr(me, "username", None) else str(me.id)
    logger.info("Telegram authenticated as %s", identity)


async def connect_authorized(client: TelegramClient) -> None:
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise TelegramAccessError("Telegram session is not authenticated. Run: python -m app login")


async def accessible_dialogs(client: TelegramClient) -> list[ChatInfo]:
    """Return only dialogs Telegram exposes to the authenticated account."""

    dialogs: list[ChatInfo] = []
    async for dialog in client.iter_dialogs():
        dialogs.append(chat_info(dialog.entity, dialog_id=dialog.id, title=dialog.name))
    return dialogs


async def resolve_configured_chats(
    client: TelegramClient, target_ids: Iterable[int]
) -> dict[int, ChatInfo]:
    """Resolve targets exclusively from the account's accessible dialog list."""

    return resolve_accessible_chats(await accessible_dialogs(client), target_ids)


def resolve_accessible_chats(
    dialogs: Iterable[ChatInfo], target_ids: Iterable[int]
) -> dict[int, ChatInfo]:
    """Select targets from a previously fetched accessible dialog list."""

    requested = tuple(dict.fromkeys(int(value) for value in target_ids))
    if not requested:
        return {}
    available = {dialog.telegram_chat_id: dialog for dialog in dialogs}
    missing = [chat_id for chat_id in requested if chat_id not in available]
    if missing:
        values = ", ".join(str(value) for value in missing)
        raise TelegramAccessError(
            "The authenticated account cannot resolve these configured chat IDs: "
            f"{values}. Check the IDs and confirm they appear in `python -m app chats`."
        )
    return {chat_id: available[chat_id] for chat_id in requested}
