from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.models import Message
from app.database.repository import ArchiveRepository
from app.database.session import Database
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
