from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from telethon.errors import SessionPasswordNeededError

from app.config import Settings
from app.web.telegram_auth import TelegramAuthStatus, TelegramQrAuthManager


class _FakeQrLogin:
    def __init__(self, *, needs_two_factor: bool = False) -> None:
        self.url = "tg://login?token=sensitive-short-lived-token"
        self.expires = datetime.now(UTC) + timedelta(minutes=1)
        self.ready = asyncio.Event()
        self.needs_two_factor = needs_two_factor

    async def wait(self) -> object:
        await self.ready.wait()
        if self.needs_two_factor:
            raise SessionPasswordNeededError(request=None)
        return SimpleNamespace(id=42, username="archive_owner")


class _FakeClient:
    def __init__(self, *, authorized: bool = False, qr_login: _FakeQrLogin | None = None) -> None:
        self.authorized = authorized
        self.qr = qr_login or _FakeQrLogin()
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> object:
        return SimpleNamespace(id=42, username="archive_owner")

    async def qr_login(self) -> _FakeQrLogin:
        return self.qr


def _settings() -> Settings:
    return Settings(_env_file=None, tg_api_id=12345, tg_api_hash="private-hash")


async def _wait_for_state(
    manager: TelegramQrAuthManager,
    expected: TelegramAuthStatus,
) -> None:
    for _ in range(20):
        if manager.snapshot.status == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Telegram authorization never reached {expected}")


async def test_qr_authorization_connects_without_exposing_token() -> None:
    qr_login = _FakeQrLogin()
    client = _FakeClient(qr_login=qr_login)
    manager = TelegramQrAuthManager(_settings(), client_factory=lambda settings: client)

    pending = await manager.start()
    svg = await manager.qr_svg()

    assert pending.status == TelegramAuthStatus.PENDING
    assert svg is not None and b"<svg" in svg
    assert b"sensitive-short-lived-token" not in svg
    assert "token" not in str(pending.public_dict())

    qr_login.ready.set()
    await _wait_for_state(manager, TelegramAuthStatus.CONNECTED)

    assert manager.snapshot.identity == "@archive_owner"
    assert await manager.qr_svg() is None
    await manager.close()


async def test_qr_two_factor_stays_out_of_browser() -> None:
    qr_login = _FakeQrLogin(needs_two_factor=True)
    client = _FakeClient(qr_login=qr_login)
    manager = TelegramQrAuthManager(_settings(), client_factory=lambda settings: client)

    await manager.start()
    qr_login.ready.set()
    await _wait_for_state(manager, TelegramAuthStatus.TWO_FACTOR)

    assert "python -m app login" in manager.snapshot.detail
    assert await manager.qr_svg() is None
    await manager.close()


async def test_existing_authorized_session_is_detected() -> None:
    client = _FakeClient(authorized=True)
    manager = TelegramQrAuthManager(_settings(), client_factory=lambda settings: client)

    snapshot = await manager.inspect_session()

    assert snapshot.status == TelegramAuthStatus.CONNECTED
    assert snapshot.identity == "@archive_owner"
    assert client.connected is False
    await manager.close()
