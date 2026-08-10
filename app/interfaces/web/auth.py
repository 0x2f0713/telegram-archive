"""Short-lived Telegram QR authorization for the local web interface."""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum

import qrcode
import qrcode.image.svg
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.custom.qrlogin import QRLogin

from app.config import ConfigurationError, Settings
from app.infrastructure.telegram.client import create_client

logger = logging.getLogger(__name__)
ClientFactory = Callable[[Settings], TelegramClient]


class TelegramAuthStatus(StrEnum):
    """Browser-safe states for the QR authorization lifecycle."""

    IDLE = "idle"
    PENDING = "pending"
    CONNECTED = "connected"
    EXPIRED = "expired"
    TWO_FACTOR = "two_factor"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TelegramAuthSnapshot:
    """Public state that never contains the QR token or Telegram secrets."""

    status: TelegramAuthStatus
    detail: str
    identity: str | None = None
    expires_at: datetime | None = None

    def public_dict(self) -> dict[str, str | None]:
        values = asdict(self)
        values["status"] = self.status.value
        if self.expires_at is not None:
            values["expires_at"] = self.expires_at.astimezone(UTC).isoformat()
        return values


def _identity(user: object) -> str:
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    full_name = " ".join(
        part
        for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
        if part
    )
    if full_name:
        return full_name
    return f"Telegram user {getattr(user, 'id', 'connected')}"


def _qr_svg(value: str) -> bytes:
    """Render a Telegram login token as SVG without retaining the token text."""

    image = qrcode.make(
        value,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=10,
        border=3,
    )
    output = io.BytesIO()
    image.save(output)
    return output.getvalue()


class TelegramQrAuthManager:
    """Coordinate a single local QR authorization attempt at a time."""

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: ClientFactory = create_client,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._lock = asyncio.Lock()
        self._snapshot = TelegramAuthSnapshot(
            TelegramAuthStatus.IDLE,
            "Connect an existing Telegram account to initialize this archive.",
        )
        self._svg: bytes | None = None
        self._task: asyncio.Task[None] | None = None
        self._client: TelegramClient | None = None
        self._generation = 0

    @property
    def snapshot(self) -> TelegramAuthSnapshot:
        return self._snapshot

    async def inspect_session(self) -> TelegramAuthSnapshot:
        """Inspect the local Telethon session unless an authorization is active."""

        async with self._lock:
            if self._snapshot.status not in {
                TelegramAuthStatus.IDLE,
                TelegramAuthStatus.CONNECTED,
            }:
                return self._snapshot
            try:
                client = self._client_factory(self._settings)
            except ConfigurationError as exc:
                self._snapshot = TelegramAuthSnapshot(
                    TelegramAuthStatus.UNAVAILABLE,
                    str(exc),
                )
                return self._snapshot

            try:
                await client.connect()
                if await client.is_user_authorized():
                    user = await client.get_me()
                    self._snapshot = TelegramAuthSnapshot(
                        TelegramAuthStatus.CONNECTED,
                        "Telegram is connected. This archive can access the same chats as this account.",
                        identity=_identity(user),
                    )
                else:
                    self._snapshot = TelegramAuthSnapshot(
                        TelegramAuthStatus.IDLE,
                        "Connect an existing Telegram account to initialize this archive.",
                    )
            except Exception as exc:
                logger.warning("Telegram session inspection failed: %s", type(exc).__name__)
                self._snapshot = TelegramAuthSnapshot(
                    TelegramAuthStatus.FAILED,
                    "Telegram could not be reached. Check the network and try again.",
                )
            finally:
                await client.disconnect()
            return self._snapshot

    async def start(self) -> TelegramAuthSnapshot:
        """Start waiting for an official Telegram QR authorization token."""

        async with self._lock:
            if self._snapshot.status == TelegramAuthStatus.PENDING and self._task:
                return self._snapshot
            self._generation += 1
            generation = self._generation
            self._svg = None
            try:
                client = self._client_factory(self._settings)
                await client.connect()
                if await client.is_user_authorized():
                    user = await client.get_me()
                    await client.disconnect()
                    self._snapshot = TelegramAuthSnapshot(
                        TelegramAuthStatus.CONNECTED,
                        "Telegram is already connected on this machine.",
                        identity=_identity(user),
                    )
                    return self._snapshot

                qr_login = await client.qr_login()
                self._svg = _qr_svg(qr_login.url)
                self._client = client
                self._snapshot = TelegramAuthSnapshot(
                    TelegramAuthStatus.PENDING,
                    "Scan this code in Telegram before it expires.",
                    expires_at=qr_login.expires,
                )
                self._task = asyncio.create_task(
                    self._wait_for_authorization(client, qr_login, generation),
                    name="telegram-web-qr-login",
                )
            except ConfigurationError as exc:
                self._snapshot = TelegramAuthSnapshot(
                    TelegramAuthStatus.UNAVAILABLE,
                    str(exc),
                )
            except Exception as exc:
                logger.warning("Telegram QR authorization could not start: %s", type(exc).__name__)
                if "client" in locals():
                    await client.disconnect()
                self._snapshot = TelegramAuthSnapshot(
                    TelegramAuthStatus.FAILED,
                    "A QR code could not be created. Check the connection and try again.",
                )
            return self._snapshot

    async def qr_svg(self) -> bytes | None:
        """Return the current QR image only while its wait task is active."""

        async with self._lock:
            if self._snapshot.status != TelegramAuthStatus.PENDING:
                return None
            return self._svg

    async def _wait_for_authorization(
        self,
        client: TelegramClient,
        qr_login: QRLogin,
        generation: int,
    ) -> None:
        try:
            user = await qr_login.wait()
        except TimeoutError:
            await self._update_if_current(
                generation,
                TelegramAuthSnapshot(
                    TelegramAuthStatus.EXPIRED,
                    "The QR code expired. Create a fresh code and scan it promptly.",
                ),
            )
        except SessionPasswordNeededError:
            await self._update_if_current(
                generation,
                TelegramAuthSnapshot(
                    TelegramAuthStatus.TWO_FACTOR,
                    "This account requires a 2FA password. Finish safely in the terminal with "
                    "python -m app login.",
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telegram QR authorization failed: %s", type(exc).__name__)
            await self._update_if_current(
                generation,
                TelegramAuthSnapshot(
                    TelegramAuthStatus.FAILED,
                    "Telegram did not complete the connection. Create a new code and try again.",
                ),
            )
        else:
            await self._update_if_current(
                generation,
                TelegramAuthSnapshot(
                    TelegramAuthStatus.CONNECTED,
                    "Telegram is connected. You can now choose chats to archive.",
                    identity=_identity(user),
                ),
            )
        finally:
            await client.disconnect()
            async with self._lock:
                if generation == self._generation:
                    self._svg = None
                    self._client = None
                    self._task = None

    async def _update_if_current(
        self,
        generation: int,
        snapshot: TelegramAuthSnapshot,
    ) -> None:
        async with self._lock:
            if generation == self._generation:
                self._snapshot = snapshot

    async def close(self) -> None:
        """Cancel the pending wait and disconnect cleanly during application shutdown."""

        async with self._lock:
            self._generation += 1
            task = self._task
            client = self._client
            self._task = None
            self._client = None
            self._svg = None
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if client and client.is_connected():
            await client.disconnect()
