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
from sqlalchemy.exc import OperationalError
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

    async def initialize(self, *, legacy_terabox_root: str = "/Telegram Archive") -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            columns = {
                row[1]
                for row in (await connection.execute(text("PRAGMA table_info(messages)"))).all()
            }
            for column in ("terabox_remote_path", "terabox_variant_remote_path"):
                if column not in columns:
                    try:
                        await connection.execute(
                            text(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
                        )
                    except OperationalError as exc:
                        # A worker and web process can initialize the same
                        # SQLite file concurrently. Treat a raced duplicate
                        # column as success, but preserve all other failures.
                        if "duplicate column name" not in str(exc).casefold():
                            raise

            # Older releases stored an absolute filesystem-mount path in media_path.
            # Convert the archive-root suffix to the canonical remote path and
            # clear the obsolete filesystem locator. This is intentionally
            # idempotent and runs before any repository reads the database.
            root = legacy_terabox_root.strip() or "/Telegram Archive"
            if not root.startswith("/"):
                root = f"/{root}"
            root = root.rstrip("/") or "/"
            rows = (
                await connection.execute(
                    text(
                        "SELECT id, media_path, media_variant_path "
                        "FROM messages WHERE media_path IS NOT NULL "
                        "AND terabox_remote_path IS NULL"
                    )
                )
            ).all()
            for message_id, media_path, variant_path in rows:
                remote = self._legacy_remote_path(str(media_path), root)
                if remote is None:
                    continue
                variant_remote = (
                    self._legacy_remote_path(str(variant_path), root)
                    if variant_path
                    else None
                )
                await connection.execute(
                    text(
                        "UPDATE messages SET media_path = NULL, media_variant_path = NULL, "
                        "terabox_remote_path = :remote, "
                        "terabox_variant_remote_path = :variant WHERE id = :id"
                    ),
                    {"id": message_id, "remote": remote, "variant": variant_remote},
                )

    @staticmethod
    def _legacy_remote_path(value: str, root: str) -> str | None:
        """Extract a remote archive path from an old absolute mount path."""

        marker = root if root != "/" else "//"
        index = value.find(marker)
        if index < 0:
            return None
        candidate = value[index:]
        if candidate != root and not candidate.startswith(f"{root}/"):
            return None
        return candidate

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
