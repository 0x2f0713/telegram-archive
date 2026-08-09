"""Application configuration loaded from environment variables and YAML."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
