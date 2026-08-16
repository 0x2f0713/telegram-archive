"""Shared resolution of durable operator runtime settings."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from app.config import Settings, resolve_runtime_overrides

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeSettingsResolution:
    """Resolved settings plus the rows that were safe to apply."""

    settings: Settings
    valid_overrides: dict[str, str]
    invalid_keys: frozenset[str]


class RuntimeSettingsReader(Protocol):
    async def overrides(self) -> dict[str, str]: ...


async def load_runtime_settings(
    settings: Settings, repository: RuntimeSettingsReader
) -> RuntimeSettingsResolution:
    """Load durable overrides and ignore malformed rows with a warning."""
    stored = await repository.overrides()
    effective, valid, invalid = resolve_runtime_overrides(settings, stored)
    for key in sorted(invalid):
        logger.warning("Ignoring invalid runtime setting %s", key)
    return RuntimeSettingsResolution(effective, valid, invalid)
