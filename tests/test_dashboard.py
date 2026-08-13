from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.application.dashboard import DashboardService, MessageQuery
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.read_models import DashboardRepository
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.persistence.selection import ChatSelectionRepository
from tests.helpers import make_chat, make_message


@pytest.fixture
async def database(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'dashboard.db'}")
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


async def _seed_archive(database: Database, media_path: Path) -> None:
    archive = ArchiveRepository(database)
    now = datetime.now(UTC)
    await archive.upsert_chat(make_chat(title="Release Room"))
    completed, _ = await archive.upsert_message(
        make_message(
            telegram_message_id=10,
            sender_name="Alice",
            text="Quarterly release package",
            message_date=now - timedelta(hours=2),
        )
    )
    await archive.mark_download_completed(completed.id, media_path, 4096)
    failed, _ = await archive.upsert_message(
        make_message(
            telegram_message_id=11,
            sender_name="Bob",
            text="Failed document",
            message_date=now - timedelta(hours=1),
        )
    )
    await archive.mark_download_failed(failed.id, "temporary filesystem error")
    await archive.upsert_message(
        make_message(
            telegram_message_id=12,
            sender_name="Alice",
            text="Metadata only",
            message_date=now,
            has_media=False,
            media_type=None,
            media_size=None,
            telegram_document_id=None,
            mime_type=None,
            original_filename=None,
            extension="",
        )
    )


async def test_dashboard_aggregates_archive_health(database: Database, tmp_path: Path) -> None:
    await _seed_archive(database, tmp_path / "report.pdf")

    overview = await DashboardService(
        ArchiveRepository(database),
        DashboardRepository(database),
        ChatSelectionRepository(database),
        (-1001234567890,),
    ).overview()

    assert overview.stats.total_messages == 3
    assert overview.stats.downloaded_files == 1
    assert overview.stats.downloaded_bytes == 4096
    assert overview.stats.failed_downloads == 1
    assert overview.configured_chat_ids == (-1001234567890,)
    assert len(overview.chats) == 1
    assert overview.chats[0].message_count == 3
    assert overview.chats[0].media_count == 2
    assert overview.chats[0].completed_count == 1
    assert overview.chats[0].failed_count == 1
    assert sum(point.count for point in overview.activity) == 3


async def test_dashboard_message_filters_and_pagination(database: Database, tmp_path: Path) -> None:
    await _seed_archive(database, tmp_path / "report.pdf")
    dashboard = DashboardRepository(database)

    search = await dashboard.messages(MessageQuery(search="quarterly", page_size=1))
    failed = await dashboard.messages(MessageQuery(status="FAILED", media_only=True))
    second_page = await dashboard.messages(MessageQuery(page=2, page_size=2))
    latest_day = datetime.now(UTC).date()
    latest_only = await dashboard.messages(MessageQuery(since=latest_day))
    oldest_first = await dashboard.messages(MessageQuery(sort="oldest"))
    largest_first = await dashboard.messages(MessageQuery(sort="largest"))

    assert search.total == 1
    assert search.items[0].sender_name == "Alice"
    assert search.pages == 1
    assert failed.total == 1
    assert failed.items[0].download_error == "temporary filesystem error"
    assert second_page.total == 3
    assert len(second_page.items) == 1
    assert second_page.pages == 2
    assert latest_only.total == 3
    assert oldest_first.items[0].telegram_message_id == 10
    assert largest_first.items[0].telegram_message_id == 10
