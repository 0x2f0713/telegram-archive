"""Safe Telethon client construction, authentication, and dialog resolution."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from telethon import TelegramClient, connection, types
from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import MemorySession

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


def _session_file(session_name: Path | str) -> Path:
    path = Path(session_name).expanduser()
    if not path.suffix:
        path = Path(str(path) + ".session")
    return path


class ReadOnlySession(MemorySession):
    """In-memory Telethon session populated from the archiver's SQLite session file.

    ``save``, ``process_entities`` persistence, and ``close`` all stay in memory
    (inherited from :class:`MemorySession`), so the session is never written back
    and never locks the file.
    """

    def __init__(self, session_path: Path) -> None:
        super().__init__()
        self.filename = str(session_path)


def load_readonly_session(session_name: Path | str) -> ReadOnlySession:
    """Load auth credentials and cached entities read-only from the session file.

    The persistent archive listener owns Telethon's SQLite session for writing.
    When a short-lived process (web operation, one-shot CLI command) opens the
    same file with Telethon's writable session, its long state/entity writes
    race the listener and fail with ``database is locked``. Loading the
    credentials into memory instead removes the contention entirely.
    """

    path = _session_file(session_name)
    if not path.is_file():
        raise TelegramAccessError("Telegram session is not authenticated. Run: python -m app login")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise TelegramAccessError(f"Telegram session file is unreadable: {exc}") from exc
    try:
        row = connection.execute(
            "SELECT dc_id, server_address, port, auth_key, takeout_id FROM sessions LIMIT 1"
        ).fetchone()
        if row is None or row[3] is None or not any(row[3]):
            raise TelegramAccessError(
                "Telegram session is not authenticated. Run: python -m app login"
            )
        session = ReadOnlySession(path)
        session._dc_id = row[0] or 0
        session._server_address = row[1]
        session._port = row[2]
        session._auth_key = AuthKey(data=row[3])
        session._takeout_id = row[4]
        for entity_id, entity_hash, username, phone, name in connection.execute(
            "SELECT id, hash, username, phone, name FROM entities"
        ):
            if entity_id == 0:  # synthetic self-marker row, not a real entity
                continue
            session._entities.add((entity_id, entity_hash, username, phone, name))
        try:
            state_rows = connection.execute(
                "SELECT id, pts, qts, date, seq FROM update_state"
            ).fetchall()
        except sqlite3.Error:  # very old session schema without update_state
            state_rows = []
        for entity_id, pts, qts, timestamp, seq in state_rows:
            state_date = (
                datetime.fromtimestamp(timestamp, tz=UTC) if timestamp is not None else None
            )
            session.set_update_state(
                entity_id, types.updates.State(pts, qts, state_date, seq, unread_count=0)
            )
        return session
    finally:
        connection.close()


def _network_options(settings: Settings) -> dict[str, object]:
    """Build the optional Telethon MTProto-proxy connection settings."""

    proxy = settings.mtproto_proxy_config
    if proxy is None:
        return {}
    logger.info("Telegram MTProto proxy enabled for %s:%s", proxy[0], proxy[1])
    return {
        "connection": connection.ConnectionTcpMTProxyRandomizedIntermediate,
        "proxy": proxy,
    }


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
        **_network_options(settings),
    )


def create_readonly_client(settings: Settings) -> TelegramClient:
    """Client that shares the account without ever writing the session file.

    Use this for everything except ``login``/QR authorization and the
    long-lived listener, which must stay on the writable SQLite session so
    Telethon can persist auth refreshes and update state.
    """

    api_id, api_hash = settings.require_telegram_credentials()
    return TelegramClient(
        load_readonly_session(settings.tg_session_name),
        api_id,
        api_hash,
        auto_reconnect=True,
        connection_retries=10,
        retry_delay=2,
        request_retries=5,
        flood_sleep_threshold=60,
        **_network_options(settings),
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
