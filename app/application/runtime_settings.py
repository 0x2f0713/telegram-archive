"""Shared resolution of durable operator runtime settings."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import Settings, resolve_runtime_overrides
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.settings import RuntimeSettingsRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeSettingsResolution:
    """Resolved settings plus the rows that were safe to apply."""

    settings: Settings
    valid_overrides: dict[str, str]
    invalid_keys: frozenset[str]


async def load_runtime_settings(
    settings: Settings, database: Database
) -> RuntimeSettingsResolution:
    """Load durable overrides and ignore malformed rows with a warning."""
    stored = await RuntimeSettingsRepository(database).overrides()
    effective, valid, invalid = resolve_runtime_overrides(settings, stored)
    for key in sorted(invalid):
        logger.warning("Ignoring invalid runtime setting %s", key)
    return RuntimeSettingsResolution(effective, valid, invalid)
