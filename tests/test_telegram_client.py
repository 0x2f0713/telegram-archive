from __future__ import annotations

from pathlib import Path

from telethon import connection

from app.config import Settings
from app.infrastructure.telegram import client as telegram_client


def test_create_client_uses_configured_mtproto_proxy(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_client(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(telegram_client, "TelegramClient", fake_client)
    settings = Settings(
        _env_file=None,
        tg_api_id=12345,
        tg_api_hash="private-hash",
        tg_session_name=tmp_path / "telegram_session",
        tg_mtproto_proxy=(
            "tg://proxy?server=proxy.example&port=443&"
            "secret=dd00000000000000000000000000000000"
        ),
    )

    telegram_client.create_client(settings)

    assert captured["connection"] is connection.ConnectionTcpMTProxyRandomizedIntermediate
    assert captured["proxy"] == (
        "proxy.example",
        443,
        "dd00000000000000000000000000000000",
    )
