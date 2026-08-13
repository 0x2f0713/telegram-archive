import asyncio
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import Message
from app.infrastructure.persistence.repository import ArchiveRepository
from tests.helpers import make_chat, make_message


@pytest.fixture
async def database(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'archive.db'}")
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


async def test_repository_detects_duplicate_message(database: Database) -> None:
    repository = ArchiveRepository(database)

    first, first_created = await repository.upsert_message(make_message())
    second, second_created = await repository.upsert_message(make_message(text="edited"))

    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    assert (await repository.stats()).total_messages == 1


async def test_database_unique_constraint_is_authoritative(database: Database) -> None:
    values = dict(
        telegram_chat_id=-1001,
        telegram_message_id=7,
        message_date=make_message().message_date,
        has_media=False,
        download_status="not_applicable",
    )
    async with database.sessions() as session:
        session.add_all([Message(**values), Message(**values)])
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_download_state_transitions_are_durable(database: Database, tmp_path: Path) -> None:
    repository = ArchiveRepository(database)
    record, _ = await repository.upsert_message(make_message())
    final = tmp_path / "file.pdf"

    await repository.mark_download_start(record.id, final)
    downloading = await repository.get_message(-1001234567890, 42)
    assert downloading is not None
    assert downloading.download_status == "downloading"
    assert downloading.download_attempts == 1

    await repository.mark_download_completed(record.id, final, 123)
    completed = await repository.get_message(-1001234567890, 42)
    assert completed is not None
    assert completed.download_status == "completed"
    assert completed.media_size == 123


async def test_stats_reports_newest_message_per_chat(database: Database) -> None:
    repository = ArchiveRepository(database)
    await repository.upsert_chat(make_chat())
    await repository.upsert_message(make_message(telegram_message_id=41))
    await repository.upsert_message(make_message(telegram_message_id=42))

    stats = await repository.stats()

    assert len(stats.newest_by_chat) == 1
    assert stats.newest_by_chat[0].message_id == 42


async def test_content_checkpoints_are_independent_and_monotonic(database: Database) -> None:
    repository = ArchiveRepository(database)
    chat_id = make_chat().telegram_chat_id

    await repository.advance_content_checkpoints(chat_id, ("photo", "video"), 20)
    await repository.advance_content_checkpoints(chat_id, ("photo",), 10)
    await repository.advance_content_checkpoints(chat_id, ("voice",), 7)

    checkpoints = await repository.get_content_checkpoints(
        chat_id,
        ("photo", "video", "voice", "audio"),
    )

    assert checkpoints == {"photo": 20, "video": 20, "voice": 7, "audio": None}


async def test_concurrent_archive_writes_share_the_sqlite_connection(database: Database) -> None:
    """Concurrent producers queue cleanly instead of exhausting SQLite connections."""

    repository = ArchiveRepository(database)
    await asyncio.gather(
        *(
            repository.upsert_message(make_message(telegram_message_id=message_id))
            for message_id in range(100, 140)
        )
    )

    assert (await repository.stats()).total_messages == 40


async def test_sqlite_pool_is_a_single_connection_queue(database: Database) -> None:
    pool = database.engine.sync_engine.pool

    assert pool.size() == 1
    assert pool.timeout() == 60
    assert pool._max_overflow == 0  # type: ignore[attr-defined]
    assert pool.checkedout() == 0
