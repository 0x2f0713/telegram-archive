import asyncio
from pathlib import Path

from app.config import Settings
from app.services.downloader import MediaDownloader


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


async def test_interrupted_download_leaves_part_and_never_final(tmp_path: Path) -> None:
    repository = RecordingRepository()
    settings = Settings(_env_file=None, download_retries=1)
    downloader = MediaDownloader(settings, repository)  # type: ignore[arg-type]
    target = tmp_path / "archive" / "42_report.pdf"
    record = type("Record", (), {"id": 1})()

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
    record = type("Record", (), {"id": 1})()

    result = await downloader.download(record, SuccessfulMessage(), target)  # type: ignore[arg-type]

    assert result.completed
    assert target.read_bytes() == b"complete"
    assert not target.with_name(f"{target.name}.part").exists()
    assert repository.completed == 1
