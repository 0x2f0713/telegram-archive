from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.application.dashboard import ChatMediaQuery, DashboardService, MessageQuery
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


async def test_chat_media_is_visual_completed_scoped_filtered_and_paginated(
    database: Database,
    tmp_path: Path,
) -> None:
    archive = ArchiveRepository(database)
    chat_id = -1001234567890
    other_chat_id = -1001234567891
    await archive.upsert_chats(
        (
            make_chat(telegram_chat_id=chat_id, title="Gallery Room"),
            make_chat(telegram_chat_id=other_chat_id, title="Other Room"),
        )
    )
    base = datetime.now(UTC)

    async def complete_media(
        message_id: int,
        mime_type: str,
        filename: str,
        *,
        target_chat_id: int = chat_id,
    ) -> int:
        record, _ = await archive.upsert_message(
            make_message(
                telegram_chat_id=target_chat_id,
                telegram_message_id=message_id,
                message_date=base + timedelta(minutes=message_id),
                mime_type=mime_type,
                original_filename=filename,
                extension=Path(filename).suffix,
            )
        )
        media_path = tmp_path / str(target_chat_id) / filename
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"media")
        await archive.mark_download_completed(record.id, media_path, media_path.stat().st_size)
        return record.id

    photo_id = await complete_media(21, "image/jpeg", "photo.jpg")
    video_id = await complete_media(22, "video/mp4", "video.mp4")
    await complete_media(23, "application/pdf", "document.pdf")
    await complete_media(24, "image/png", "other.png", target_chat_id=other_chat_id)
    failed, _ = await archive.upsert_message(
        make_message(
            telegram_message_id=25,
            message_date=base + timedelta(minutes=25),
            mime_type="image/webp",
            original_filename="failed.webp",
            extension=".webp",
        )
    )
    await archive.mark_download_failed(failed.id, "network error")
    dashboard = DashboardRepository(database)

    all_media = await dashboard.chat_media(ChatMediaQuery(chat_id=chat_id))
    photos = await dashboard.chat_media(ChatMediaQuery(chat_id=chat_id, kind="photos"))
    videos = await dashboard.chat_media(ChatMediaQuery(chat_id=chat_id, kind="videos"))
    last_page = await dashboard.chat_media(ChatMediaQuery(chat_id=chat_id, page=99, page_size=1))

    assert [message.id for message in all_media.items] == [video_id, photo_id]
    assert [message.id for message in photos.items] == [photo_id]
    assert [message.id for message in videos.items] == [video_id]
    assert last_page.page == 2
    assert last_page.pages == 2
    assert [message.id for message in last_page.items] == [photo_id]


async def test_archived_chat_summaries_are_searchable_paginated_and_pin_active_chat(
    database: Database,
    tmp_path: Path,
) -> None:
    await _seed_archive(database, tmp_path / "report.pdf")
    archive = ArchiveRepository(database)
    older_chat_id = -1001234567891
    active_empty_chat_id = -1001234567892
    await archive.upsert_chats(
        (
            make_chat(
                telegram_chat_id=older_chat_id,
                title="Earlier Archive",
                username="earlier_archive",
            ),
            make_chat(
                telegram_chat_id=active_empty_chat_id,
                title="Active Empty Room",
                username="active_empty",
            ),
        )
    )
    await archive.upsert_message(
        make_message(
            telegram_chat_id=older_chat_id,
            telegram_message_id=20,
            message_date=datetime.now(UTC) - timedelta(days=2),
        )
    )
    dashboard = DashboardRepository(database)

    archived = await dashboard.archived_chat_summaries()
    active_search = await dashboard.archived_chat_summaries(
        "active_empty",
        include_chat_id=active_empty_chat_id,
    )
    active_only = await dashboard.archived_chat_summaries(
        page_size=1,
        include_chat_id=active_empty_chat_id,
    )
    second_page = await dashboard.archived_chat_summaries(page=2, page_size=1)
    last_page = await dashboard.archived_chat_summaries(page=99, page_size=1)
    id_search = await dashboard.archived_chat_summaries(str(older_chat_id))

    assert [chat.title for chat in archived.items] == ["Release Room", "Earlier Archive"]
    assert archived.total == 2
    assert archived.pages == 1
    assert [chat.title for chat in active_search.items] == ["Active Empty Room"]
    assert active_search.items[0].message_count == 0
    assert [chat.telegram_chat_id for chat in active_only.items] == [active_empty_chat_id]
    assert active_only.total == 3
    assert active_only.pages == 3
    assert [chat.title for chat in second_page.items] == ["Earlier Archive"]
    assert last_page.page == 2
    assert [chat.title for chat in last_page.items] == ["Earlier Archive"]
    assert [chat.telegram_chat_id for chat in id_search.items] == [older_chat_id]
