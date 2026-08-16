from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.application.archive_deletion import ChatArchiveDeletionService
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.read_models import DashboardRepository
from app.infrastructure.persistence.repository import ArchiveRepository
from tests.helpers import make_chat, make_message, make_no_media_message


@pytest.fixture
async def database(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'deletion.db'}")
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


async def test_delete_chat_archive_removes_owned_files_and_preserves_archive_state(
    database: Database,
    tmp_path: Path,
) -> None:
    archive = ArchiveRepository(database)
    dashboard = DashboardRepository(database)
    chat_id = -1001234567890
    other_chat_id = -1001234567891
    await archive.upsert_chats(
        (
            make_chat(telegram_chat_id=chat_id, title="Delete Room"),
            make_chat(telegram_chat_id=other_chat_id, title="Keep Room"),
        )
    )
    await archive.advance_checkpoint(chat_id, 900)
    await archive.advance_content_checkpoints(chat_id, ("photos",), 850)

    download_dir = tmp_path / "downloads"
    unique_path = download_dir / "delete-room" / "unique.jpg"
    partial_path = unique_path.with_name(f"{unique_path.name}.part")
    shared_path = download_dir / "shared" / "shared.jpg"
    outside_path = tmp_path / "outside.jpg"
    missing_path = download_dir / "delete-room" / "missing.jpg"
    for path, payload in (
        (unique_path, b"unique"),
        (partial_path, b"partial"),
        (shared_path, b"shared"),
        (outside_path, b"outside"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    async def archived_media(
        message_id: int,
        media_path: Path,
        *,
        target_chat_id: int = chat_id,
    ) -> int:
        record, _ = await archive.upsert_message(
            make_message(
                telegram_chat_id=target_chat_id,
                telegram_message_id=message_id,
                message_date=datetime(2026, 8, 13, message_id % 23, tzinfo=UTC),
                mime_type="image/jpeg",
                original_filename=media_path.name,
                extension=".jpg",
            )
        )
        await archive.mark_download_completed(record.id, media_path, 10)
        return record.id

    unique_id = await archived_media(101, unique_path)
    shared_id = await archived_media(102, shared_path)
    outside_id = await archived_media(103, outside_path)
    missing_id = await archived_media(104, missing_path)
    text_record, _ = await archive.upsert_message(
        make_no_media_message(
            telegram_chat_id=chat_id,
            telegram_message_id=105,
            text="local conversation text",
        )
    )
    retained_id = await archived_media(201, shared_path, target_chat_id=other_chat_id)

    result = await ChatArchiveDeletionService(archive).delete(chat_id, download_dir)

    assert result is not None
    assert result.telegram_chat_id == chat_id
    assert result.messages_deleted == 5
    assert result.files_deleted == 2
    assert result.bytes_deleted == len(b"unique") + len(b"partial")
    assert result.files_missing == 1
    assert result.files_skipped == 1
    assert result.files_failed == 0
    assert not result.cleanup_complete
    assert not unique_path.exists()
    assert not partial_path.exists()
    assert shared_path.is_file()
    assert outside_path.is_file()

    for message_id in (unique_id, shared_id, outside_id, missing_id, text_record.id):
        assert await dashboard.message(message_id) is None
    assert await dashboard.message(retained_id) is not None
    assert await archive.get_checkpoint(chat_id) == 900
    assert await archive.get_content_checkpoints(chat_id, ("photos",)) == {"photos": 850}
    assert (await dashboard.chat_summary(chat_id)).message_count == 0
    assert chat_id not in {
        chat.telegram_chat_id for chat in (await dashboard.archived_chat_summaries()).items
    }
    assert await ChatArchiveDeletionService(archive).delete(chat_id, download_dir) is None
