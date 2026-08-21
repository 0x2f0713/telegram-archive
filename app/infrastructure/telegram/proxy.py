"""Automatic MTProto proxy discovery, probing, caching, and rotation."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from pydantic import SecretStr
from telethon import TelegramClient, connection
from telethon.sessions import MemorySession

from app.config import ConfigurationError, Settings

logger = logging.getLogger(__name__)

_ATOB_RE = re.compile(r"atob\(['\"]([^'\"]+)['\"]\)")
_LINK_RE = re.compile(r"(?:tg://proxy|https?://t\.me/proxy)\?[^\s\"'<>]+")
_SCRIPT_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_OBJECT_RE = re.compile(r'\{"host"\s*:\s*".*?\}', re.DOTALL)
_MIN_SECRET_BYTES = 16
_DEFAULT_PROVIDER_URL = "https://mtpro.xyz/mtproto"


@dataclass(frozen=True, slots=True)
class MTProtoProxy:
    """Validated MTProxy endpoint with a secret-safe representation."""

    host: str
    port: int
    secret: str

    def __repr__(self) -> str:
        return f"MTProtoProxy(host={self.host!r}, port={self.port!r}, secret=<redacted>)"

    @property
    def tuple(self) -> tuple[str, int, str]:
        return self.host, self.port, self.secret

    @property
    def link(self) -> str:
        query = urlencode({"server": self.host, "port": self.port, "secret": self.secret})
        return f"tg://proxy?{query}"

    @classmethod
    def from_link(cls, value: str) -> MTProtoProxy:
        raw = value.strip()
        parsed = urlparse(raw)
        if parsed.scheme not in {"tg", "http", "https"}:
            raise ConfigurationError(
                "MTProto proxy must be a tg://proxy or https://t.me/proxy link"
            )
        if parsed.scheme == "tg" and parsed.netloc.casefold() != "proxy":
            raise ConfigurationError("MTProto proxy must use the tg://proxy link format")
        if parsed.scheme in {"http", "https"} and parsed.path.rstrip("/").casefold() != "/proxy":
            raise ConfigurationError("MTProto proxy must use a Telegram /proxy link")

        values = parse_qs(parsed.query, keep_blank_values=True)

        def required(name: str) -> str:
            entries = values.get(name, [])
            if len(entries) != 1 or not entries[0].strip():
                raise ConfigurationError(
                    f"MTProto proxy must contain exactly one non-empty {name} value"
                )
            return entries[0].strip()

        host = required("server")
        raw_port = required("port")
        secret = required("secret")
        try:
            port = int(raw_port)
        except ValueError:
            raise ConfigurationError("MTProto proxy port must be an integer") from None
        if not 1 <= port <= 65535:
            raise ConfigurationError("MTProto proxy port must be between 1 and 65535")
        _validate_secret(secret)
        return cls(host, port, secret)

    @classmethod
    def from_record(cls, value: object) -> MTProtoProxy:
        if not isinstance(value, dict):
            raise ConfigurationError("MTProto proxy record must be an object")
        host = value.get("host")
        port = value.get("port")
        secret = value.get("secret")
        if not isinstance(host, str) or not isinstance(port, int) or isinstance(port, bool):
            raise ConfigurationError("MTProto proxy record has invalid host or port")
        if not isinstance(secret, str):
            raise ConfigurationError("MTProto proxy record has an invalid secret")
        return cls.from_link(
            f"tg://proxy?{urlencode({'server': host, 'port': port, 'secret': secret})}"
        )

    def as_record(self) -> dict[str, str | int]:
        return {"host": self.host, "port": self.port, "secret": self.secret}


def _validate_secret(secret: str) -> None:
    candidate = secret[2:] if secret[:2].casefold() in {"ee", "dd"} else secret
    try:
        decoded = bytes.fromhex(candidate)
    except ValueError:
        try:
            padded = candidate + "=" * (-len(candidate) % 4)
            decoded = base64.b64decode(padded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError, binascii.Error):
            raise ConfigurationError("MTProto proxy contains an invalid secret") from None
    if len(decoded) < _MIN_SECRET_BYTES:
        raise ConfigurationError("MTProto proxy secret must contain at least 16 decoded bytes")


def parse_proxy_page(content: str) -> tuple[MTProtoProxy, ...]:
    """Extract proxy entries from current and legacy MTPro.XYZ page formats."""

    candidates: list[MTProtoProxy] = []
    seen: set[tuple[str, int, str]] = set()

    def add_link(link: str) -> None:
        link = unescape(link).rstrip("),;]")
        try:
            proxy = MTProtoProxy.from_link(link)
        except ConfigurationError:
            return
        if proxy.tuple not in seen:
            seen.add(proxy.tuple)
            candidates.append(proxy)

    for match in _LINK_RE.finditer(content):
        add_link(match.group(0))

    decoded_payloads: list[str] = []
    for match in _ATOB_RE.finditer(content):
        try:
            decoded_payloads.append(base64.b64decode(match.group(1)).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, binascii.Error):
            continue

    for payload in decoded_payloads:
        for array_match in re.finditer(r"return\s+(\[.*?\])\s*;", payload, re.DOTALL):
            try:
                records = json.loads(array_match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(records, list):
                for record in records:
                    try:
                        proxy = MTProtoProxy.from_record(record)
                    except ConfigurationError:
                        continue
                    if proxy.tuple not in seen:
                        seen.add(proxy.tuple)
                        candidates.append(proxy)
        for object_match in _OBJECT_RE.finditer(payload):
            try:
                proxy = MTProtoProxy.from_record(json.loads(object_match.group(0)))
            except (ConfigurationError, json.JSONDecodeError):
                continue
            if proxy.tuple not in seen:
                seen.add(proxy.tuple)
                candidates.append(proxy)

    return tuple(candidates)


def _fetch_provider_page(url: str, timeout: float) -> str:
    def fetch(target: str) -> str:
        request = Request(
            target,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/javascript",
                "User-Agent": "telegram-archiver/1.0 MTProto proxy discovery",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    page = fetch(url)
    provider_host = urlparse(url).hostname
    scripts: list[str] = []
    for source in _SCRIPT_RE.findall(page):
        script_url = urljoin(url, source)
        if urlparse(script_url).hostname != provider_host:
            continue
        try:
            scripts.append(fetch(script_url))
        except OSError:
            logger.debug("Could not fetch MTPro.XYZ proxy script", exc_info=True)
    return "\n".join((page, *scripts))


def _cache_records(path: Path, proxies: tuple[MTProtoProxy, ...]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"provider": _DEFAULT_PROVIDER_URL, "proxies": [proxy.as_record() for proxy in proxies]},
        separators=(",", ":"),
    )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary.write(payload)
        temporary.flush()
        os.fchmod(temporary.fileno(), 0o600)
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _read_cache(path: Path) -> tuple[MTProtoProxy, ...]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
        records = payload.get("proxies", []) if isinstance(payload, dict) else []
        result: list[MTProtoProxy] = []
        seen: set[tuple[str, int, str]] = set()
        for record in records:
            try:
                proxy = MTProtoProxy.from_record(record)
            except ConfigurationError:
                continue
            if proxy.tuple not in seen:
                seen.add(proxy.tuple)
                result.append(proxy)
        return tuple(result)
    except (OSError, json.JSONDecodeError, TypeError):
        return ()


async def _probe_proxy(settings: Settings, proxy: MTProtoProxy) -> tuple[MTProtoProxy, float] | None:
    api_id, api_hash = settings.require_telegram_credentials()
    client = TelegramClient(
        MemorySession(),
        api_id,
        api_hash,
        connection=connection.ConnectionTcpMTProxyRandomizedIntermediate,
        proxy=proxy.tuple,
        auto_reconnect=False,
        connection_retries=1,
        request_retries=1,
        timeout=settings.tg_mtproto_proxy_connect_timeout,
    )
    started = monotonic()
    try:
        await asyncio.wait_for(
            client.connect(), timeout=settings.tg_mtproto_proxy_connect_timeout
        )
        if not client.is_connected():
            return None
        return proxy, monotonic() - started
    except Exception as exc:
        logger.debug("MTProto proxy probe failed for %s:%s: %s", proxy.host, proxy.port, type(exc).__name__)
        return None
    finally:
        with suppress(Exception):
            await asyncio.wait_for(client.disconnect(), timeout=5)


class MTProtoProxyManager:
    """Own candidate ordering and rotate an attached Telethon client."""

    def __init__(self, settings: Settings, candidates: tuple[MTProtoProxy, ...]) -> None:
        self.settings = settings
        self.candidates = candidates
        self.current_index = 0
        self.client: TelegramClient | None = None
        self._lock = asyncio.Lock()
        self._tried_indices: set[int] = {0} if candidates else set()

    @property
    def current(self) -> MTProtoProxy | None:
        return self.candidates[self.current_index] if self.candidates else None

    def attach(self, client: TelegramClient) -> None:
        self.client = client
        if self.current is not None:
            client.set_proxy(self.current.tuple)

    def begin_transfer(self) -> None:
        """Allow each new media transfer to try every candidate once."""

        if self.candidates and len(self._tried_indices) >= len(self.candidates):
            self._tried_indices = {self.current_index}

    async def rotate(self) -> bool:
        """Reconnect the attached client through the next candidate."""

        if self.client is None or len(self.candidates) < 2:
            return False
        async with self._lock:
            for offset in range(1, len(self.candidates)):
                index = (self.current_index + offset) % len(self.candidates)
                if index in self._tried_indices:
                    continue
                candidate = self.candidates[index]
                self._tried_indices.add(index)
                try:
                    await self.client.disconnect()
                    self.client.set_proxy(candidate.tuple)
                    await self.client.connect()
                    if not self.client.is_connected():
                        raise ConnectionError("proxy client did not reconnect")
                except Exception as exc:
                    logger.warning(
                        "MTProto proxy rotation failed for %s:%s: %s",
                        candidate.host,
                        candidate.port,
                        type(exc).__name__,
                    )
                    continue
                self.current_index = index
                logger.warning(
                    "Rotated MTProto proxy to %s:%s after a slow transfer",
                    candidate.host,
                    candidate.port,
                )
                return True
            current = self.current
            if current is not None:
                try:
                    await self.client.disconnect()
                    self.client.set_proxy(current.tuple)
                    await self.client.connect()
                except Exception:
                    logger.warning("Could not restore the current MTProto proxy", exc_info=True)
        return False


async def prepare_mtproto_proxy(
    settings: Settings,
) -> tuple[Settings, MTProtoProxyManager | None]:
    """Resolve, probe, and select the proxy used by a worker process."""

    manual = settings.mtproto_proxy_config
    if manual is not None:
        proxy = MTProtoProxy(*manual)
        return settings, MTProtoProxyManager(settings, (proxy,))
    if not settings.tg_mtproto_proxy_auto or not settings.tg_api_id or not settings.tg_api_hash:
        return settings, None

    cached_candidates = _read_cache(settings.tg_mtproto_proxy_cache_file)
    candidates: tuple[MTProtoProxy, ...] = ()
    provider_error: Exception | None = None
    try:
        content = await asyncio.to_thread(
            _fetch_provider_page,
            settings.tg_mtproto_proxy_provider_url,
            settings.tg_mtproto_proxy_connect_timeout,
        )
        candidates = parse_proxy_page(content)
        if candidates:
            await asyncio.to_thread(_cache_records, settings.tg_mtproto_proxy_cache_file, candidates)
        else:
            provider_error = ConfigurationError("MTPro.XYZ returned no valid MTProto proxies")
    except Exception as exc:
        provider_error = exc

    if not candidates:
        candidates = cached_candidates
    if not candidates:
        detail = f": {type(provider_error).__name__}" if provider_error else ""
        raise ConfigurationError(
            "Automatic MTProto proxy discovery failed; no usable MTPro.XYZ proxy is available"
            f"{detail}"
        ) from provider_error

    candidates = candidates[: settings.tg_mtproto_proxy_probe_limit]
    probes = await asyncio.gather(*(_probe_proxy(settings, proxy) for proxy in candidates))
    ranked = sorted((result for result in probes if result is not None), key=lambda item: item[1])
    if not ranked:
        if cached_candidates and cached_candidates != candidates:
            fallback_probes = await asyncio.gather(
                *(_probe_proxy(settings, proxy) for proxy in cached_candidates)
            )
            ranked = sorted(
                (result for result in fallback_probes if result is not None), key=lambda item: item[1]
            )
    if not ranked:
        raise ConfigurationError("No MTPro.XYZ proxy could establish a Telegram connection")

    ordered = tuple(proxy for proxy, _latency in ranked)
    selected = ordered[0]
    await asyncio.to_thread(_cache_records, settings.tg_mtproto_proxy_cache_file, ordered)
    effective = settings.model_copy(update={"tg_mtproto_proxy": SecretStr(selected.link)})
    logger.info(
        "Selected MTProto proxy %s:%s from %s reachable candidates",
        selected.host,
        selected.port,
        len(ordered),
    )
    return effective, MTProtoProxyManager(effective, ordered)
