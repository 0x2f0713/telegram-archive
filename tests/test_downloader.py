import asyncio
from pathlib import Path

from app.config import Settings
from app.infrastructure.download import MediaDownloader
from app.infrastructure.terabox import TeraBoxError, UploadReceipt


class RecordingRepository:
    def __init__(self) -> None:
        self.started = 0
        self.completed = 0
        self.failed: str | None = None

    async def mark_download_start(self, _message_id: int, _path: Path) -> None:
        self.started += 1

    async def mark_download_completed(self, _message_id: int, _path: Path, _size: int) -> None:
        self.completed += 1

    async def mark_download_failed(self, _message_id: int, error: str) -> None:
        self.failed = error


class InterruptedMessage:
    async def download_media(self, *, file: str) -> str:
        await asyncio.to_thread(Path(file).write_bytes, b"partial bytes")
        raise OSError("connection reset")


class SuccessfulMessage:
    async def download_media(self, *, file: str) -> str:
        await asyncio.to_thread(Path(file).write_bytes, b"complete")
        return file


class ProgressMessage:
    async def download_media(self, *, file: str, progress_callback) -> str:
        await asyncio.to_thread(Path(file).write_bytes, b"complete")
        progress_callback(2, 8)
        progress_callback(8, 8)
        return file


async def test_interrupted_download_leaves_part_and_never_final(tmp_path: Path) -> None:
    repository = RecordingRepository()
    settings = Settings(_env_file=None, download_retries=1)
    downloader = MediaDownloader(settings, repository)  # type: ignore[arg-type]
    target = tmp_path / "archive" / "42_report.pdf"
    record = type("Record", (), {"id": 1, "telegram_chat_id": 12345})()

    result = await downloader.download(record, InterruptedMessage(), target)  # type: ignore[arg-type]

    assert not result.completed
    assert not target.exists()
    assert target.with_name(f"{target.name}.part").read_bytes() == b"partial bytes"
    assert repository.completed == 0
    assert repository.failed and "connection reset" in repository.failed


async def test_successful_download_is_atomically_renamed(tmp_path: Path) -> None:
    repository = RecordingRepository()
    settings = Settings(_env_file=None, download_retries=1)
    downloader = MediaDownloader(settings, repository)  # type: ignore[arg-type]
    target = tmp_path / "42_report.pdf"
    record = type("Record", (), {"id": 1, "telegram_chat_id": 12345})()

    result = await downloader.download(record, SuccessfulMessage(), target)  # type: ignore[arg-type]

    assert result.completed
    assert target.read_bytes() == b"complete"
    assert not target.with_name(f"{target.name}.part").exists()
    assert repository.completed == 1


async def test_download_forwards_per_file_progress(tmp_path: Path) -> None:
    repository = RecordingRepository()
    downloader = MediaDownloader(Settings(_env_file=None, download_retries=1), repository)  # type: ignore[arg-type]
    updates: list[tuple[int, int]] = []
    target = tmp_path / "42_report.pdf"

    def collect(current: int, total: int) -> None:
        updates.append((current, total))

    result = await downloader.download(
        type("Record", (), {"id": 1, "telegram_chat_id": 12345})(),
        ProgressMessage(),
        target,
        collect,
    )  # type: ignore[arg-type]

    assert result.completed
    assert updates == [(2, 8), (8, 8)]


class FakeUploader:
    def __init__(self, mount_dir: Path, *, error: str | None = None) -> None:
        self.mount_dir = mount_dir
        self.error = error
        self.uploaded: list[Path] = []

    async def upload(self, target: Path, progress=None) -> UploadReceipt:
        if self.error:
            raise TeraBoxError(self.error)
        size = await asyncio.to_thread(lambda: target.stat().st_size)
        self.uploaded.append(target)
        mount_path = self.mount_dir / target.name
        return UploadReceipt(
            remote_path=f"/Telegram Archive/{target.name}",
            mount_path=mount_path,
            size=size,
            md5="d" * 32,
        )


def _terabox_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        download_retries=1,
        storage_mode="terabox",
        terabox_ndus="t",
        terabox_profile=None,
        download_dir=tmp_path / "downloads",
        terabox_mount_dir=tmp_path / "mnt",
        terabox_remote_dir="/Telegram Archive",
    )


async def test_download_uploads_to_terabox_and_removes_local_copy(tmp_path: Path) -> None:
    repository = RecordingRepository()
    settings = _terabox_settings(tmp_path)
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    uploader = FakeUploader(settings.terabox_mount_dir)
    downloader = MediaDownloader(settings, repository, uploader)  # type: ignore[arg-type]
    target = settings.download_dir / "42_report.pdf"
    record = type("Record", (), {"id": 1, "telegram_chat_id": 12345})()

    result = await downloader.download(record, SuccessfulMessage(), target)  # type: ignore[arg-type]

    assert result.completed
    assert uploader.uploaded == [target]
    # Local buffer removed after a verified upload.
    assert not target.exists()
    assert result.path == settings.terabox_mount_dir / "42_report.pdf"
    assert repository.completed == 1


async def test_download_keeps_local_copy_when_upload_fails(tmp_path: Path) -> None:
    repository = RecordingRepository()
    settings = _terabox_settings(tmp_path)
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    uploader = FakeUploader(settings.terabox_mount_dir, error="boom")
    downloader = MediaDownloader(settings, repository, uploader)  # type: ignore[arg-type]
    target = settings.download_dir / "42_report.pdf"
    record = type("Record", (), {"id": 1, "telegram_chat_id": 12345})()

    result = await downloader.download(record, SuccessfulMessage(), target)  # type: ignore[arg-type]

    assert not result.completed
    assert "boom" in (result.error or "")
    # Buffer survives so retry can re-upload without re-downloading.
    assert target.is_file()
    assert repository.completed == 0
    assert repository.failed is not None


async def test_download_records_completion_before_removing_buffer(tmp_path: Path) -> None:
    settings = _terabox_settings(tmp_path)
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    target = settings.download_dir / "42_report.pdf"

    class OrderRepository(RecordingRepository):
        def __init__(self) -> None:
            super().__init__()
            self.buffer_present_at_completion: bool | None = None

        async def mark_download_completed(self, _message_id, _path, _size) -> None:
            self.buffer_present_at_completion = target.is_file()
            await super().mark_download_completed(_message_id, _path, _size)

    repository = OrderRepository()
    downloader = MediaDownloader(settings, repository, FakeUploader(settings.terabox_mount_dir))  # type: ignore[arg-type]

    result = await downloader.download(
        type("Record", (), {"id": 1, "telegram_chat_id": 12345})(), SuccessfulMessage(), target
    )  # type: ignore[arg-type]

    assert result.completed
    assert repository.buffer_present_at_completion is True
    assert not target.exists()


async def test_publish_buffered_returns_none_in_local_mode(tmp_path: Path) -> None:
    repository = RecordingRepository()
    settings = Settings(_env_file=None, download_retries=1)
    downloader = MediaDownloader(settings, repository)  # type: ignore[arg-type]

    assert await downloader.publish_buffered(1, tmp_path / "file.bin") is None
