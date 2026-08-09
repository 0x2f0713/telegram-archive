"""Async SQLAlchemy engine and schema initialization."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database.models import Base


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
        self.engine: AsyncEngine = create_async_engine(
            self.url,
            connect_args={"timeout": 30},
        )
        event.listen(self.engine.sync_engine, "connect", self._configure_sqlite)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

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

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def healthcheck(self) -> None:
        async with self.sessions() as session:
            await session.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()
