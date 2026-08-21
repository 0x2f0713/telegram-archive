"""Application configuration loaded from environment variables and YAML."""

from __future__ import annotations

import base64
import binascii
import logging
from functools import cached_property
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import yaml
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: Settings keys an operator may edit from the web "Effective configuration"
#: panel. Secrets and environment-sensitive paths intentionally stay outside.
RUNTIME_OVERRIDE_FIELDS: frozenset[str] = frozenset(
    {
        "download_photos",
        "download_videos",
        "download_documents",
        "download_audio",
        "max_file_size_mb",
        "download_concurrency",
        "download_retries",
        "allowed_extensions",
        "ignored_extensions",
        "keywords",
        "log_level",
        "web_refresh_seconds",
        "media_faststart",
        "media_variants",
    }
)

_BOOL_OVERRIDES: frozenset[str] = frozenset(
    {
        "download_photos",
        "download_videos",
        "download_documents",
        "download_audio",
        "media_faststart",
        "media_variants",
    }
)

_INT_OVERRIDES: frozenset[str] = frozenset(
    {
        "max_file_size_mb",
        "download_concurrency",
        "download_retries",
        "web_refresh_seconds",
    }
)

_TRUE_VALUES: frozenset[str] = frozenset({"1", "true", "on", "yes"})


class ConfigurationError(ValueError):
    """Raised when required application configuration is invalid."""


class Settings(BaseSettings):
    """Validated settings with environment variables taking the primary role.

    The optional YAML file is intentionally limited to chat selection. Secrets
    remain environment-only so a convenient chat list cannot accidentally
    become a credential store.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    tg_api_id: int | None = None
    tg_api_hash: SecretStr | None = None
    tg_session_name: Path = Path("data/telegram_session")
    #: Optional MTProto proxy link copied from Telegram or MTPro.XYZ. The
    #: value is secret-bearing and therefore stays environment-only.
    tg_mtproto_proxy: SecretStr | None = None
    #: Automatically discover and probe proxies from the configured provider
    #: when no explicit TG_MTPROTO_PROXY link is supplied. Opt in explicitly
    #: because public proxy availability and performance are variable.
    tg_mtproto_proxy_auto: bool = False
    tg_mtproto_proxy_provider_url: str = "https://mtpro.xyz/mtproto"
    tg_mtproto_proxy_cache_file: Path = Path("data/mtproto_proxy_cache.json")
    tg_mtproto_proxy_probe_limit: int = Field(default=24, ge=1, le=100)
    tg_mtproto_proxy_connect_timeout: int = Field(default=10, ge=2, le=60)

    target_chats: str = ""
    config_file: Path | None = None

    database_url: str = "sqlite:///data/archive.db"
    download_dir: Path = Path("downloads")

    #: Archive storage mode. ``local`` keeps media on the hard drive (the
    #: historical behavior). ``terabox`` uses the hard drive as a temporary
    #: download buffer, uploads each finalized file to TeraBox, verifies the
    #: upload, then removes the local copy; archived bytes are served through
    #: the TeraBox Web API and disposable local caches.
    storage_mode: Literal["local", "terabox"] = "local"

    #: TeraBox session cookie token (the ``ndus`` value from the browser).
    #: Required when ``storage_mode`` is ``terabox``.
    terabox_ndus: SecretStr | None = None
    #: Remote base folder for the archive inside the TeraBox drive.
    terabox_remote_dir: str = "/Telegram Archive"
    #: Chunk size (bytes) for TeraBox superfile2 uploads. 4 MiB is the
    #: protocol default for non-VIP accounts; the server may reject others.
    terabox_chunk_size: int = Field(default=4 * 1024 * 1024, ge=256 * 1024, le=128 * 1024 * 1024)

    download_photos: bool = True
    download_videos: bool = True
    download_documents: bool = True
    download_audio: bool = True
    max_file_size_mb: int = Field(default=500, ge=0)
    download_concurrency: int = Field(default=2, ge=1, le=20)
    #: Explicit Telegram request size in KiB. Zero keeps Telethon's
    #: file-size-aware automatic sizing (128/256/512 KiB).
    telegram_download_part_size_kb: int = Field(default=0, ge=0, le=512)
    #: Number of files that may upload to TeraBox concurrently. Chunks within
    #: one upload remain sequential for protocol and retry safety.
    terabox_upload_concurrency: int = Field(default=1, ge=1, le=4)
    #: Maximum number of HEVC->H.264 ffmpeg transcodes per worker process.
    #: Kept separate from download concurrency because network downloads can
    #: overlap while the hardware video path remains bounded.
    transcode_concurrency: int = Field(default=1, ge=1, le=8)
    download_retries: int = Field(default=3, ge=1, le=10)

    allowed_extensions: str = ""
    ignored_extensions: str = ".exe"
    keywords: str = ""
    log_level: str = "INFO"
    shutdown_timeout_seconds: int = Field(default=30, ge=1, le=600)

    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8686, ge=1, le=65535)
    web_session_secret: SecretStr | None = None
    web_refresh_seconds: int = Field(default=15, ge=5, le=3600)
    tui_refresh_seconds: int = Field(default=5, ge=1, le=3600)

    # ffmpeg is optional; when absent, faststart remuxing, poster generation,
    # and HEVC fallback variants are disabled gracefully.
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    #: Extra library search path for a host-mounted ffmpeg (bind-mounted
    #: binaries). Applied only to the ffmpeg child process, never the app.
    ffmpeg_ld_library_path: str = ""

    #: Optional ssh target (e.g. ``namhh@192.168.1.2``) that runs HEVC
    #: transcodes with its own Rockchip ffmpeg over an NFS-shared archive.
    #: Empty disables remote transcoding; a connectivity failure falls back
    #: to a local transcode.
    ffmpeg_remote_host: str = ""
    #: Remote ffmpeg binary (a wrapper that sets LD_LIBRARY_PATH is expected).
    ffmpeg_remote_bin: str = "/usr/local/bin/ffmpeg"
    #: SSH private key path (inside the app container) for the remote host.
    ffmpeg_remote_identity: str = ""
    #: SSH known_hosts path (inside the app container) for the remote host.
    ffmpeg_remote_known_hosts: str = ""
    #: Host path that ``download_dir`` maps to, used to translate arguments
    #: for remote transcoding (the remote host mounts the same files via NFS).
    host_download_dir: str = ""

    #: Remux completed videos with -movflags +faststart (moov at file start)
    #: so browsers can start playback without fetching the file tail.
    media_faststart: bool = True
    #: Transcode HEVC videos to an H.264 variant on first view and serve
    #: JPEG poster thumbnails in galleries and players.
    media_variants: bool = True

    #: Local directory for cached thumbnails (used in TeraBox mode to avoid
    #: repeating API downloads).
    thumbnail_cache_dir: Path = Path("data/thumbnails")
    #: Maximum pixel dimension (width or height) for generated thumbnails.
    thumbnail_max_dimension: int = Field(default=320, ge=64, le=1280)
    #: WebP quality for thumbnails (1-100).
    thumbnail_quality: int = Field(default=75, ge=10, le=100)

    #: Generate poster frames for videos in TeraBox mode and cache locally.
    terabox_generate_posters: bool = True
    #: Transcode HEVC videos to H.264 in TeraBox mode for browser compatibility.
    terabox_transcode_hevc: bool = True
    #: Store both original and H.264 variant on TeraBox (when transcoding).
    terabox_store_both: bool = True

    #: Local directory for cached video byte ranges (TeraBox mode seeking).
    video_cache_dir: Path = Path("data/video_cache")
    #: Maximum size of video cache in GB.
    video_cache_max_size_gb: int = Field(default=5, ge=1, le=100)
    #: Maximum age of cached video segments in days.
    video_cache_max_age_days: int = Field(default=7, ge=1, le=365)

    #: Hardware decode mode for ffmpeg child processes. ``auto``/``rkmpp``
    #: enables MPP hardware decode when the h264_rkmpp encoder exists; ``none``
    #: forces software decode (hardware encoding is unaffected). Set to
    #: ``none`` where the MPP userspace is built for a different glibc than the
    #: container's (hevc_rkmpp then fails at runtime).
    video_hwaccel: str = "auto"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL must be CRITICAL, ERROR, WARNING, INFO, or DEBUG")
        return normalized

    @field_validator("telegram_download_part_size_kb")
    @classmethod
    def validate_telegram_download_part_size(cls, value: int) -> int:
        if value not in {0, 128, 256, 512}:
            raise ValueError(
                "TELEGRAM_DOWNLOAD_PART_SIZE_KB must be 0, 128, 256, or 512"
            )
        return value

    def require_telegram_credentials(self) -> tuple[int, str]:
        """Return Telegram credentials or raise a safe, secret-free error."""

        api_hash = self.tg_api_hash.get_secret_value() if self.tg_api_hash else ""
        if not self.tg_api_id or not api_hash:
            raise ConfigurationError(
                "TG_API_ID and TG_API_HASH are required. Copy .env.example to .env "
                "and add credentials from https://my.telegram.org."
            )
        return self.tg_api_id, api_hash

    @property
    def mtproto_proxy_config(self) -> tuple[str, int, str] | None:
        """Return ``(host, port, secret)`` for an optional MTProto proxy.

        Telegram and public proxy lists commonly expose either a ``tg://``
        link or an HTTPS deep link. Keeping the link in one environment value
        avoids separately handling a host, port, and secret in deployment
        configuration while still allowing Pydantic to redact it.
        """

        if self.tg_mtproto_proxy is None:
            return None
        raw = self.tg_mtproto_proxy.get_secret_value().strip()
        if not raw:
            return None

        parsed = urlparse(raw)
        if parsed.scheme not in {"tg", "http", "https"}:
            raise ConfigurationError(
                "TG_MTPROTO_PROXY must be a tg://proxy or https://t.me/proxy link"
            )
        if parsed.scheme == "tg" and parsed.netloc.casefold() != "proxy":
            raise ConfigurationError("TG_MTPROTO_PROXY must use the tg://proxy link format")
        if parsed.scheme in {"http", "https"} and parsed.path.rstrip("/").casefold() != "/proxy":
            raise ConfigurationError("TG_MTPROTO_PROXY must use a Telegram /proxy link")

        values = parse_qs(parsed.query, keep_blank_values=True)

        def required_query_value(name: str) -> str:
            entries = values.get(name, [])
            if len(entries) != 1 or not entries[0].strip():
                raise ConfigurationError(
                    f"TG_MTPROTO_PROXY must contain exactly one non-empty {name} value"
                )
            return entries[0].strip()

        host = required_query_value("server")
        raw_port = required_query_value("port")
        secret = required_query_value("secret")
        try:
            port = int(raw_port)
        except ValueError:
            raise ConfigurationError("TG_MTPROTO_PROXY port must be an integer") from None
        if not 1 <= port <= 65535:
            raise ConfigurationError("TG_MTPROTO_PROXY port must be between 1 and 65535")

        self._validate_mtproto_secret(secret)
        return host, port, secret

    @staticmethod
    def _validate_mtproto_secret(secret: str) -> None:
        """Validate the part of an MTProxy secret Telethon will consume."""

        candidate = secret[2:] if secret[:2].casefold() in {"ee", "dd"} else secret
        try:
            decoded = bytes.fromhex(candidate)
        except ValueError:
            try:
                padded = candidate + "=" * (-len(candidate) % 4)
                decoded = base64.b64decode(padded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError, binascii.Error):
                raise ConfigurationError(
                    "TG_MTPROTO_PROXY contains an invalid MTProto secret"
                ) from None
        if len(decoded) < 16:
            raise ConfigurationError(
                "TG_MTPROTO_PROXY secret must contain at least 16 decoded bytes"
            )

    @property
    def terabox_enabled(self) -> bool:
        return self.storage_mode == "terabox"

    def require_terabox_ndus(self) -> str:
        """Return the TeraBox ``ndus`` cookie or raise a secret-free error."""

        if self.terabox_ndus:
            ndus = self.terabox_ndus.get_secret_value().strip()
            if ndus:
                return ndus
        raise ConfigurationError(
            "TERABOX_NDUS is required when STORAGE_MODE=terabox. Set the ndus cookie "
            "value directly from an authenticated TeraBox browser session."
        )

    @cached_property
    def terabox_remote_root(self) -> str:
        base = self.terabox_remote_dir.strip() or "/"
        if not base.startswith("/"):
            base = f"/{base}"
        return base.rstrip("/") or "/"

    def media_storage_roots(self) -> tuple[Path, ...]:
        """Roots where archived media bytes may live, in serving preference order."""

        return (self.download_dir.expanduser().resolve(),)

    def with_terabox_policy(self) -> Settings:
        """Apply TeraBox mode constraints on top of resolved settings.

        Remote-only archives keep pristine originals: HEVC variants would rewrite
        or multiply uploads and are not required for the API-backed cache.
        Faststart remuxing is enabled and poster generation uses the local cache.
        """
        if not self.terabox_enabled:
            return self
        updates: dict[str, object] = {}
        if self.media_variants:
            updates["media_variants"] = False
        if updates:
            logger.info(
                "TeraBox storage mode: media variants (HEVC transcode, old poster system) "
                "are disabled; faststart and local posters are enabled"
            )
            return self.model_copy(update=updates)
        return self

    @staticmethod
    def _parse_csv(value: str) -> tuple[str, ...]:
        return tuple(part.strip() for part in value.split(",") if part.strip())

    @cached_property
    def configured_chat_ids(self) -> tuple[int, ...]:
        """Combine enabled YAML chats and TARGET_CHATS without duplicates."""

        chat_ids: list[int] = []
        for raw_id in self._parse_csv(self.target_chats):
            try:
                chat_ids.append(int(raw_id))
            except ValueError as exc:
                raise ConfigurationError(
                    f"TARGET_CHATS contains an invalid Telegram chat ID: {raw_id!r}"
                ) from exc

        if self.config_file:
            path = self.config_file.expanduser()
            if not path.is_file():
                raise ConfigurationError(f"CONFIG_FILE does not exist: {path}")
            try:
                content: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise ConfigurationError(f"Cannot read YAML config {path}: {exc}") from exc
            if not isinstance(content, dict) or not isinstance(content.get("chats", []), list):
                raise ConfigurationError("YAML config must contain a 'chats' list")
            for item in content.get("chats", []):
                if not isinstance(item, dict) or "id" not in item:
                    raise ConfigurationError("Every YAML chat entry must contain an 'id'")
                enabled = item.get("enabled", True)
                if not isinstance(enabled, bool):
                    raise ConfigurationError("YAML chat 'enabled' values must be true or false")
                if enabled:
                    try:
                        if isinstance(item["id"], bool):
                            raise TypeError
                        chat_ids.append(int(item["id"]))
                    except (TypeError, ValueError) as exc:
                        raise ConfigurationError(
                            f"YAML contains an invalid Telegram chat ID: {item['id']!r}"
                        ) from exc

        return tuple(dict.fromkeys(chat_ids))

    @cached_property
    def allowed_extension_set(self) -> frozenset[str]:
        return frozenset(
            self._normalise_extension(v) for v in self._parse_csv(self.allowed_extensions)
        )

    @cached_property
    def ignored_extension_set(self) -> frozenset[str]:
        return frozenset(
            self._normalise_extension(v) for v in self._parse_csv(self.ignored_extensions)
        )

    @cached_property
    def keyword_set(self) -> tuple[str, ...]:
        return tuple(value.casefold() for value in self._parse_csv(self.keywords))

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @staticmethod
    def _normalise_extension(value: str) -> str:
        value = value.strip().casefold()
        if value and not value.startswith("."):
            value = f".{value}"
        return value


def decode_overrides(overrides: dict[str, str]) -> dict[str, object]:
    """Convert stored override strings into typed values for validation.

    Only keys declared in ``RUNTIME_OVERRIDE_FIELDS`` are returned; anything
    else is dropped, which keeps secret and environment-only settings safe
    even if a crafted or corrupt row reaches the resolver.
    """
    decoded: dict[str, object] = {}
    for key, raw in overrides.items():
        if key not in RUNTIME_OVERRIDE_FIELDS:
            continue
        value = raw.strip()
        if key in _BOOL_OVERRIDES:
            decoded[key] = value.casefold() in _TRUE_VALUES
        elif key in _INT_OVERRIDES:
            try:
                decoded[key] = int(value)
            except ValueError:
                raise ValueError(f"Runtime override {key!r} must be an integer") from None
        else:
            decoded[key] = value
    return decoded


def encode_overrides(settings: Settings) -> dict[str, str]:
    """Canonical string form of every editable field on a settings object."""
    return {
        key: ("true" if getattr(settings, key) else "false")
        if key in _BOOL_OVERRIDES
        else str(getattr(settings, key))
        for key in sorted(RUNTIME_OVERRIDE_FIELDS)
    }


def settings_form_values(settings: Settings) -> dict[str, bool | int | str]:
    """Typed, form-usable values for every editable setting."""
    return {
        key: getattr(settings, key)
        if key in _BOOL_OVERRIDES or key in _INT_OVERRIDES
        else str(getattr(settings, key))
        for key in sorted(RUNTIME_OVERRIDE_FIELDS)
    }


def merge_runtime_form_values(
    settings: Settings, submitted: dict[str, str]
) -> dict[str, bool | int | str]:
    """Overlay submitted form values while retaining their invalid text."""
    form_values = settings_form_values(settings)
    for key, raw in submitted.items():
        if key in _BOOL_OVERRIDES:
            form_values[key] = raw.casefold() == "true"
        elif key in _INT_OVERRIDES:
            try:
                form_values[key] = int(raw)
            except ValueError:
                form_values[key] = raw
        elif key in RUNTIME_OVERRIDE_FIELDS:
            form_values[key] = raw
    return form_values


def runtime_form_values(values: dict[str, list[str]]) -> dict[str, str]:
    """Convert parsed form values into canonical override strings.

    Boolean fields are checkbox keys: present means true, absent false.
    Integer and string fields are stripped and must be non-empty. Keys
    outside ``RUNTIME_OVERRIDE_FIELDS`` are dropped.
    """
    overrides: dict[str, str] = {}
    for key in sorted(RUNTIME_OVERRIDE_FIELDS):
        if key in _BOOL_OVERRIDES:
            overrides[key] = "true" if key in values else "false"
            continue
        raw = (values.get(key, [""])[0] if values.get(key) else "").strip()
        overrides[key] = raw
    return overrides


def apply_runtime_overrides(settings: Settings, overrides: dict[str, str]) -> Settings:
    """Return a re-validated settings copy with DB overrides applied on top.

    Pydantic validation runs again on the merged values, so an invalid row
    (bad log level, out-of-range integer) raises ``ValidationError`` instead
    of silently producing an inconsistent runtime configuration.
    """
    if not overrides:
        return settings
    merged: dict[str, Any] = settings.model_dump()
    merged.update(decode_overrides(overrides))
    return Settings.model_validate(merged)


def resolve_runtime_overrides(
    settings: Settings, overrides: dict[str, str]
) -> tuple[Settings, dict[str, str], frozenset[str]]:
    """Apply valid persisted overrides while isolating corrupt rows.

    Runtime settings are operator-editable data, so one malformed row must not
    prevent the rest of the application from starting. Unknown keys are
    ignored; known keys that fail decoding or settings validation are returned
    separately for logging and repair UI purposes.
    """
    valid: dict[str, str] = {}
    invalid: set[str] = set()
    for key, value in overrides.items():
        if key not in RUNTIME_OVERRIDE_FIELDS:
            continue
        try:
            apply_runtime_overrides(settings, {key: value})
        except (ValidationError, ValueError):
            invalid.add(key)
        else:
            valid[key] = value
    effective = apply_runtime_overrides(settings, valid) if valid else settings
    return effective, valid, frozenset(invalid)
