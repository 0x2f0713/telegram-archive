"""Durable runtime configuration overrides shared by every surface."""

from __future__ import annotations

from sqlalchemy import delete, select

from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import RuntimeSetting, utc_now

__all__ = ["RuntimeSettingsRepository"]


class RuntimeSettingsRepository:
    """Read and replace operator-edited settings overrides."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def overrides(self) -> dict[str, str]:
        async with self.database.sessions() as session:
            rows = await session.execute(select(RuntimeSetting.key, RuntimeSetting.value))
        return dict(rows.all())

    async def set_values(self, values: dict[str, str]) -> None:
        if not values:
            return
        now = utc_now()
        async with self.database.transaction() as session:
            for key, value in values.items():
                setting = await session.get(RuntimeSetting, key)
                if setting is None:
                    session.add(RuntimeSetting(key=key, value=value, updated_at=now))
                else:
                    setting.value = value
                    setting.updated_at = now

    async def replace_values(self, values: dict[str, str]) -> None:
        """Atomically replace the complete set of operator overrides."""
        now = utc_now()
        async with self.database.transaction() as session:
            await session.execute(delete(RuntimeSetting))
            for key, value in values.items():
                session.add(RuntimeSetting(key=key, value=value, updated_at=now))

    async def clear(self) -> None:
        async with self.database.transaction() as session:
            await session.execute(delete(RuntimeSetting))
