"""Application configuration loaded from environment variables and YAML."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    }
)

_BOOL_OVERRIDES: frozenset[str] = frozenset(
    {
        "download_photos",
        "download_videos",
        "download_documents",
        "download_audio",
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

    target_chats: str = ""
    config_file: Path | None = None

    database_url: str = "sqlite:///data/archive.db"
    download_dir: Path = Path("downloads")

    download_photos: bool = True
    download_videos: bool = True
    download_documents: bool = True
    download_audio: bool = True
    max_file_size_mb: int = Field(default=500, ge=0)
    download_concurrency: int = Field(default=2, ge=1, le=20)
    download_retries: int = Field(default=3, ge=1, le=10)

    allowed_extensions: str = ""
    ignored_extensions: str = ".exe"
    keywords: str = ""
    log_level: str = "INFO"
    shutdown_timeout_seconds: int = Field(default=30, ge=1, le=600)

    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8686, ge=1, le=65535)
    web_username: str = "archiver"
    web_password: SecretStr | None = None
    web_refresh_seconds: int = Field(default=15, ge=5, le=3600)
    tui_refresh_seconds: int = Field(default=5, ge=1, le=3600)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL must be CRITICAL, ERROR, WARNING, INFO, or DEBUG")
        return normalized

    def require_telegram_credentials(self) -> tuple[int, str]:
        """Return Telegram credentials or raise a safe, secret-free error."""

        api_hash = self.tg_api_hash.get_secret_value() if self.tg_api_hash else ""
        if not self.tg_api_id or not api_hash:
            raise ConfigurationError(
                "TG_API_ID and TG_API_HASH are required. Copy .env.example to .env "
                "and add credentials from https://my.telegram.org."
            )
        return self.tg_api_id, api_hash

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
