from __future__ import annotations

from pathlib import Path

from app.application.archive import ArchiveService
from app.application.archive_records import DownloadResult, MessageSnapshot
from app.application.filenames import output_path
from app.config import Settings
from app.infrastructure.telegram.translation import content_types_of
from tests.helpers import make_chat, make_message


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "storage_mode": "terabox",
        "terabox_ndus": "t",
        "download_dir": tmp_path / "downloads",
        "terabox_remote_dir": "/Telegram Archive",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


class RecordingRepository:
    def __init__(self, record: MessageSnapshot) -> None:
        self._record = record
        self.completed: list[tuple[Path, int]] = []
        self.failed: list[str] = []

    async def upsert_message(self, _data: object) -> tuple[MessageSnapshot, bool]:
        return self._record, True

    async def mark_download_start(self, _message_id: int, _path: Path) -> None:
        return None

    async def mark_download_completed(
        self, _message_id: int, path: Path, size: int, _variant_local_path: str | None = None,
        **_kwargs: object,
    ) -> None:
        self.completed.append((path, size))

    async def mark_download_skipped(self, _message_id: int, reason: str) -> None:
        return None

    async def mark_download_failed(self, _message_id: int, error: str) -> None:
        self.failed.append(error)


class PublishingDownloader:
    def __init__(
        self,
        repository: RecordingRepository,
        remote_root: str,
        publish_error: str | None = None,
    ) -> None:
        self.repository = repository
        self.remote_root = remote_root.rstrip("/")
        self.publish_error = publish_error
        self.published: list[Path] = []
        self.downloaded: list[Path] = []

    async def download(
        self,
        record,
        raw_message,
        target,
        progress=None,
        upload_progress=None,
        prepare_progress=None,
    ) -> DownloadResult:
        self.downloaded.append(target)
        return DownloadResult(True, target, 1)

    async def publish_buffered(self, message_id, buffered_path, progress=None):
        self.published.append(buffered_path)
        if self.publish_error:
            self.repository.failed.append(self.publish_error)
            return DownloadResult(False, None, None, self.publish_error)
        self.repository.completed.append((buffered_path, buffered_path.stat().st_size))
        return DownloadResult(True, buffered_path, buffered_path.stat().st_size)


def _archive(settings: Settings, repository: RecordingRepository, downloader) -> ArchiveService:
    return ArchiveService(
        settings,
        repository,  # type: ignore[arg-type]
        downloader,  # type: ignore[arg-type]
        lambda _raw, _chat: make_message(),
        content_types_of,
        lambda _error: None,
        lambda _error: False,
    )


def _record(telegram_chat_id: int) -> MessageSnapshot:
    return MessageSnapshot(
        id=1,
        telegram_chat_id=telegram_chat_id,
        telegram_message_id=42,
        has_media=True,
        media_path=None,
        media_variant_path=None,
        media_size=None,
        download_status="failed",
        download_attempts=0,
    )


async def test_terabox_mode_publishes_buffered_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    chat = make_chat()
    repository = RecordingRepository(_record(chat.telegram_chat_id))
    downloader = PublishingDownloader(repository, settings.terabox_remote_root)
    archive = _archive(settings, repository, downloader)

    target = output_path(settings.download_dir, chat, make_message())
    target.parent.mkdir(parents=True)
    target.write_bytes(b"buffered video")

    result = await archive.process_message(object(), chat)

    assert result.downloaded is True
    assert downloader.published == [target]
    assert downloader.downloaded == []
    assert repository.completed and repository.completed[0][0] is not None


async def test_terabox_mode_publish_failure_records_failure(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    chat = make_chat()
    repository = RecordingRepository(_record(chat.telegram_chat_id))
    downloader = PublishingDownloader(
        repository, settings.terabox_remote_root, publish_error="upload refused"
    )
    archive = _archive(settings, repository, downloader)

    target = output_path(settings.download_dir, chat, make_message())
    target.parent.mkdir(parents=True)
    target.write_bytes(b"buffered video")

    result = await archive.process_message(object(), chat)

    assert result.downloaded is False
    assert result.skipped is False
    assert repository.failed == ["upload refused"]
    assert repository.completed == []


async def test_terabox_mode_without_buffer_downloads_normally(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    chat = make_chat()
    repository = RecordingRepository(_record(chat.telegram_chat_id))
    downloader = PublishingDownloader(repository, settings.terabox_remote_root)
    archive = _archive(settings, repository, downloader)

    result = await archive.process_message(object(), chat)

    assert result.downloaded is True
    assert downloader.published == []
    assert len(downloader.downloaded) == 1


async def test_local_mode_treats_buffered_file_as_archived(tmp_path: Path) -> None:
    settings = _settings(tmp_path, storage_mode="local")
    chat = make_chat()
    repository = RecordingRepository(_record(chat.telegram_chat_id))
    downloader = PublishingDownloader(repository, settings.terabox_remote_root)
    archive = _archive(settings, repository, downloader)

    target = output_path(settings.download_dir, chat, make_message())
    target.parent.mkdir(parents=True)
    target.write_bytes(b"already here")

    result = await archive.process_message(object(), chat)

    assert result.skipped is True
    assert result.downloaded is False
    assert downloader.published == []
    assert downloader.downloaded == []
