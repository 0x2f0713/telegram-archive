from __future__ import annotations

import base64
import json

import pytest

from app.config import Settings
from app.infrastructure.download import DownloadRateGuard, SlowDownloadError
from app.infrastructure.telegram import proxy as proxy_module
from app.infrastructure.telegram.proxy import MTProtoProxy, MTProtoProxyManager


def _secret(prefix: str = "ee") -> str:
    return prefix + ("00" * 16)


def test_provider_parser_reads_encoded_mtpro_payload_and_deduplicates() -> None:
    record = {"host": "proxy.example", "port": 443, "secret": _secret()}
    payload = f"(function() {{ return [{json.dumps(record)}]; }})()"
    encoded = base64.b64encode(payload.encode()).decode()
    page = (
        f"<a href=\"tg://proxy?server=proxy.example&port=443&secret={_secret()}\">x</a>"
        f"<script>const data = atob('{encoded}')</script>"
    )

    proxies = proxy_module.parse_proxy_page(page)

    assert proxies == (MTProtoProxy("proxy.example", 443, _secret()),)


def test_provider_parser_accepts_base64_secret() -> None:
    secret = base64.urlsafe_b64encode(b"0123456789abcdef").decode().rstrip("=")

    proxy = MTProtoProxy.from_link(
        f"https://t.me/proxy?server=proxy.example&port=8443&secret={secret}"
    )

    assert proxy.host == "proxy.example"
    assert proxy.port == 8443
    assert proxy.secret == secret


@pytest.mark.asyncio
async def test_prepare_proxy_selects_fastest_probe_and_updates_settings(
    monkeypatch, tmp_path
) -> None:
    first = MTProtoProxy("slow.example", 443, _secret())
    second = MTProtoProxy("fast.example", 443, _secret("dd"))
    settings = Settings(
        _env_file=None,
        tg_api_id=123,
        tg_api_hash="hash",
        tg_mtproto_proxy_auto=True,
        tg_mtproto_proxy_cache_file=tmp_path / "proxy-cache.json",
    )

    monkeypatch.setattr(proxy_module, "_fetch_provider_page", lambda *_: "provider")
    monkeypatch.setattr(proxy_module, "parse_proxy_page", lambda _page: (first, second))

    async def probe(_settings, proxy):
        return proxy, 2.0 if proxy is first else 0.5

    monkeypatch.setattr(proxy_module, "_probe_proxy", probe)

    effective, manager = await proxy_module.prepare_mtproto_proxy(settings)

    assert manager is not None
    assert manager.current is second
    assert effective.mtproto_proxy_config == second.tuple
    assert (tmp_path / "proxy-cache.json").is_file()


@pytest.mark.asyncio
async def test_proxy_manager_rotates_attached_client() -> None:
    first = MTProtoProxy("first.example", 443, _secret())
    second = MTProtoProxy("second.example", 443, _secret())
    manager = MTProtoProxyManager(
        Settings(_env_file=None),
        (first, second),
    )

    class FakeClient:
        def __init__(self) -> None:
            self.proxies: list[tuple[str, int, str]] = []
            self.connected = False

        def set_proxy(self, proxy) -> None:
            self.proxies.append(proxy)

        async def disconnect(self) -> None:
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        def is_connected(self) -> bool:
            return self.connected

    client = FakeClient()
    manager.attach(client)  # type: ignore[arg-type]
    client.connected = True

    assert await manager.rotate() is True
    assert manager.current is second
    assert client.proxies[-1] == second.tuple


@pytest.mark.asyncio
async def test_proxy_manager_defers_rotation_while_another_transfer_is_active() -> None:
    first = MTProtoProxy("first.example", 443, _secret())
    second = MTProtoProxy("second.example", 443, _secret())
    manager = MTProtoProxyManager(Settings(_env_file=None), (first, second))

    class FakeClient:
        def __init__(self) -> None:
            self.proxies: list[tuple[str, int, str]] = []
            self.connected = True
            self.disconnects = 0

        def set_proxy(self, proxy) -> None:
            self.proxies.append(proxy)

        async def disconnect(self) -> None:
            self.disconnects += 1
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        def is_connected(self) -> bool:
            return self.connected

    client = FakeClient()
    manager.attach(client)  # type: ignore[arg-type]
    manager.begin_transfer()
    manager.begin_transfer()

    assert await manager.rotate() is False
    assert client.disconnects == 0
    assert manager.current is first

    manager.end_transfer()
    manager.end_transfer()
    assert await manager.rotate() is True


def test_download_rate_guard_ignores_small_files() -> None:
    guard = DownloadRateGuard(2 * 1024 * 1024)
    guard.observe(2 * 1024 * 1024, 2 * 1024 * 1024)


def test_download_rate_guard_raises_after_sustained_low_rate(monkeypatch) -> None:
    now = 0.0
    monkeypatch.setattr("app.infrastructure.download.monotonic", lambda: now)
    guard = DownloadRateGuard(8 * 1024 * 1024)

    now = 3.0
    guard.observe(2 * 1024 * 1024, 8 * 1024 * 1024)
    now = 8.0
    guard.observe(2 * 1024 * 1024, 8 * 1024 * 1024)
    now = 14.0
    with pytest.raises(SlowDownloadError, match="below"):
        guard.observe(3 * 1024 * 1024, 8 * 1024 * 1024)
