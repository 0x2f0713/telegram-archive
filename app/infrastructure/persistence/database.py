"""Async SQLAlchemy engine and schema initialization."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.infrastructure.persistence.models import Base

logger = logging.getLogger(__name__)
SQLITE_POOL_SIZE = 3
SQLITE_POOL_TIMEOUT_SECONDS = 60
SLOW_CHECKOUT_SECONDS = 10


def async_database_url(url: str) -> str:
    if url.startswith("sqlite+aiosqlite:"):
        return url
    if url.startswith("sqlite:"):
        return url.replace("sqlite:", "sqlite+aiosqlite:", 1)
    raise ValueError("Only SQLite DATABASE_URL values are supported")


class Database:
    """Owns the database engine and short-lived session factory."""

    def __init__(self, url: str) -> None:
        self.url = async_database_url(url)
        self._ensure_sqlite_directory()
        self._write_lock = asyncio.Lock()
        self.engine: AsyncEngine = create_async_engine(
            self.url,
            connect_args={"timeout": 30},
            # Archive workers plus web/progress traffic need a small bounded
            # amount of headroom. The application write gate serializes writers
            # before checkout while WAL permits concurrent readers.
            pool_size=SQLITE_POOL_SIZE,
            max_overflow=0,
            pool_timeout=SQLITE_POOL_TIMEOUT_SECONDS,
        )
        event.listen(self.engine.sync_engine, "connect", self._configure_sqlite)
        event.listen(self.engine.sync_engine, "checkout", self._track_checkout)
        event.listen(self.engine.sync_engine, "checkin", self._track_checkin)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        logger.debug(
            "SQLite connection queue configured: pool size %s, no overflow, %ss wait",
            SQLITE_POOL_SIZE,
            SQLITE_POOL_TIMEOUT_SECONDS,
        )

    def _ensure_sqlite_directory(self) -> None:
        parsed = make_url(self.url)
        database = parsed.database
        if database and database != ":memory:":
            Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    def _track_checkout(
        self,
        _dbapi_connection: object,
        connection_record: Any,
        _connection_proxy: object,
    ) -> None:
        """Warn when one task retains a pooled connection unusually long."""

        try:
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
        except RuntimeError:
            return
        token = object()
        connection_record.info["checkout_token"] = token
        connection_record.info["checkout_started"] = monotonic()
        connection_record.info["checkout_task"] = task.get_name() if task else "unknown"
        connection_record.info["checkout_warning"] = loop.call_later(
            SLOW_CHECKOUT_SECONDS,
            self._warn_slow_checkout,
            connection_record,
            token,
        )

    def _warn_slow_checkout(self, connection_record: Any, token: object) -> None:
        if connection_record.info.get("checkout_token") is not token:
            return
        elapsed = monotonic() - float(connection_record.info["checkout_started"])
        logger.warning(
            "SQLite connection checked out for %.1fs by task %s; %s",
            elapsed,
            connection_record.info.get("checkout_task", "unknown"),
            self.engine.sync_engine.pool.status(),
        )

    @staticmethod
    def _track_checkin(_dbapi_connection: object, connection_record: Any) -> None:
        warning = connection_record.info.pop("checkout_warning", None)
        if warning is not None:
            warning.cancel()
        connection_record.info.pop("checkout_token", None)
        connection_record.info.pop("checkout_started", None)
        connection_record.info.pop("checkout_task", None)

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Open one write transaction after entering the SQLite writer queue."""

        async with self._write_lock:
            async with self.sessions() as session, session.begin():
                yield session

    async def healthcheck(self) -> None:
        async with self.sessions() as session:
            await session.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()
