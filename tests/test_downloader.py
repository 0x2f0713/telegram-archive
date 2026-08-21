import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.infrastructure.download import MIN_SPEED_FILE_BYTES, MediaDownloader
from app.infrastructure.ffmpeg import FfmpegCapabilities
from app.infrastructure.terabox import TeraBoxError, UploadReceipt


class RecordingRepository:
    def __init__(self) -> None:
        self.started = 0
        self.completed = 0
        self.failed: str | None = None

    async def mark_download_start(self, _message_id: int, _path: Path) -> None:
        self.started += 1

    async def mark_download_completed(
        self, _message_id: int, _path: Path, _size: int, _variant_local_path: str | None = None,
        **_kwargs: object,
    ) -> None:
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


class ChunkedClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def download_file(self, input_location: object, *, file: str, **kwargs) -> None:
        self.calls.append({"input_location": input_location, "file": file, **kwargs})
        await asyncio.to_thread(Path(file).write_bytes, b"complete")
        callback = kwargs.get("progress_callback")
        if callback is not None:
            callback(8, kwargs.get("file_size", 8))


class ChunkedMessage:
    def __init__(self) -> None:
        self._client = ChunkedClient()

    async def download_media(self, **_kwargs: object) -> str:
        raise AssertionError("explicit part-size downloads should use the client adapter")


class ProgressMessage:
    async def download_media(self, *, file: str, progress_callback) -> str:
        await asyncio.to_thread(Path(file).write_bytes, b"complete")
        progress_callback(2, 8)
        progress_callback(8, 8)
        return file


class LargeProgressMessage:
    async def download_media(self, *, file: str, progress_callback) -> str:
        await asyncio.to_thread(Path(file).write_bytes, b"x" * MIN_SPEED_FILE_BYTES)
        progress_callback(MIN_SPEED_FILE_BYTES, MIN_SPEED_FILE_BYTES)
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


async def test_explicit_part_size_uses_public_telethon_download_file(
    tmp_path: Path,
) -> None:
    repository = RecordingRepository()
    settings = Settings(
        _env_file=None,
        download_retries=1,
        telegram_download_part_size_kb=512,
    )
    downloader = MediaDownloader(settings, repository)  # type: ignore[arg-type]
    message = ChunkedMessage()
    target = tmp_path / "42_report.pdf"
    record = type(
        "Record",
        (),
        {"id": 1, "telegram_chat_id": 12345, "media_size": len(b"complete")},
    )()

    result = await downloader.download(record, message, target)  # type: ignore[arg-type]

    assert result.completed
    assert message._client.calls[0]["part_size_kb"] == 512
    assert message._client.calls[0]["file_size"] == len(b"complete")
    assert message._client.calls[0]["input_location"] is message



async def test_direct_large_download_does_not_apply_proxy_speed_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = RecordingRepository()
    settings = Settings(_env_file=None, download_retries=1)
    downloader = MediaDownloader(settings, repository)  # type: ignore[arg-type]
    target = tmp_path / "42_large.bin"
    record = type(
        "Record",
        (),
        {"id": 1, "telegram_chat_id": 12345, "media_size": MIN_SPEED_FILE_BYTES},
    )()

    def unexpected_guard(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("direct downloads must not install the proxy speed guard")

    monkeypatch.setattr(
        "app.infrastructure.download.DownloadRateGuard.observe", unexpected_guard
    )
    result = await downloader.download(
        record,
        LargeProgressMessage(),
        target,
        lambda _current, _total: None,
    )  # type: ignore[arg-type]

    assert result.completed
    assert target.is_file()


async def test_proxy_large_download_keeps_speed_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = RecordingRepository()
    settings = Settings(_env_file=None, download_retries=1)

    class ProxyManager:
        def begin_transfer(self) -> None:
            return None

        async def rotate(self) -> bool:
            return False

    downloader = MediaDownloader(
        settings,
        repository,  # type: ignore[arg-type]
        proxy_manager=ProxyManager(),  # type: ignore[arg-type]
    )
    observed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.infrastructure.download.DownloadRateGuard.observe",
        lambda _guard, current, total: observed.append((current, total)),
    )
    target = tmp_path / "42_large.bin"
    record = type(
        "Record",
        (),
        {"id": 1, "telegram_chat_id": 12345, "media_size": MIN_SPEED_FILE_BYTES},
    )()

    result = await downloader.download(
        record,
        LargeProgressMessage(),
        target,
        lambda _current, _total: None,
    )  # type: ignore[arg-type]

    assert result.completed
    assert observed == [(MIN_SPEED_FILE_BYTES, MIN_SPEED_FILE_BYTES)]


class FakeUploader:
    def __init__(self, remote_root: str, *, error: str | None = None) -> None:
        self.remote_root = remote_root.rstrip("/")
        self.error = error
        self.uploaded: list[Path] = []

    async def upload(self, target: Path, progress=None) -> UploadReceipt:
        if self.error:
            raise TeraBoxError(self.error)
        size = await asyncio.to_thread(lambda: target.stat().st_size)
        self.uploaded.append(target)
        return UploadReceipt(
            remote_path=f"{self.remote_root}/{target.name}",
            size=size,
            md5="d" * 32,
        )


class BlockingUploader(FakeUploader):
    def __init__(self, remote_root: str) -> None:
        super().__init__(remote_root)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.peak_active = 0

    async def upload(self, target: Path, progress=None) -> UploadReceipt:
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        self.started.set()
        try:
            await self.release.wait()
            return await super().upload(target, progress)
        finally:
            self.active -= 1


class TrackingMessage(SuccessfulMessage):
    def __init__(self) -> None:
        self.downloaded = asyncio.Event()

    async def download_media(self, *, file: str) -> str:
        result = await super().download_media(file=file)
        self.downloaded.set()
        return result


def _terabox_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        download_retries=1,
        storage_mode="terabox",
        terabox_ndus="t",
        download_dir=tmp_path / "downloads",
        terabox_remote_dir="/Telegram Archive",
    )


async def test_download_uploads_to_terabox_and_removes_local_copy(tmp_path: Path) -> None:
    repository = RecordingRepository()
    settings = _terabox_settings(tmp_path)
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    uploader = FakeUploader(settings.terabox_remote_root)
    downloader = MediaDownloader(settings, repository, uploader)  # type: ignore[arg-type]
    target = settings.download_dir / "42_report.pdf"
    record = type("Record", (), {"id": 1, "telegram_chat_id": 12345})()

    result = await downloader.download(record, SuccessfulMessage(), target)  # type: ignore[arg-type]

    assert result.completed
    assert uploader.uploaded == [target]
    # Local buffer removed after a verified upload.
    assert not target.exists()
    assert result.path == target
    assert repository.completed == 1


async def test_download_keeps_local_copy_when_upload_fails(tmp_path: Path) -> None:
    repository = RecordingRepository()
    settings = _terabox_settings(tmp_path)
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    uploader = FakeUploader(settings.terabox_remote_root, error="boom")
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

        async def mark_download_completed(
            self, _message_id, _path, _size, _variant_local_path=None, **_kwargs
        ) -> None:
            self.buffer_present_at_completion = target.is_file()
            await super().mark_download_completed(_message_id, _path, _size)

    repository = OrderRepository()
    downloader = MediaDownloader(settings, repository, FakeUploader(settings.terabox_remote_root))  # type: ignore[arg-type]

    result = await downloader.download(
        type("Record", (), {"id": 1, "telegram_chat_id": 12345})(), SuccessfulMessage(), target
    )  # type: ignore[arg-type]

    assert result.completed
    assert repository.buffer_present_at_completion is True
    assert not target.exists()


async def test_telegram_download_slot_is_released_before_terabox_upload(
    tmp_path: Path,
) -> None:
    settings = _terabox_settings(tmp_path).model_copy(
        update={"download_concurrency": 1, "terabox_upload_concurrency": 1}
    )
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    repository = RecordingRepository()
    uploader = BlockingUploader(settings.terabox_remote_root)
    downloader = MediaDownloader(settings, repository, uploader)  # type: ignore[arg-type]
    first_message = TrackingMessage()
    second_message = TrackingMessage()
    first_target = settings.download_dir / "first.bin"
    second_target = settings.download_dir / "second.bin"
    first_record = type("Record", (), {"id": 1, "telegram_chat_id": 12345})()
    second_record = type("Record", (), {"id": 2, "telegram_chat_id": 12345})()

    first_task = asyncio.create_task(
        downloader.download(first_record, first_message, first_target)  # type: ignore[arg-type]
    )
    await uploader.started.wait()
    second_task = asyncio.create_task(
        downloader.download(second_record, second_message, second_target)  # type: ignore[arg-type]
    )
    await second_message.downloaded.wait()

    assert not second_task.done()
    assert uploader.peak_active == 1

    uploader.release.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert first_result.completed and second_result.completed
    assert uploader.peak_active == 1


async def test_publish_buffered_returns_none_in_local_mode(tmp_path: Path) -> None:
    repository = RecordingRepository()
    settings = Settings(_env_file=None, download_retries=1)
    downloader = MediaDownloader(settings, repository)  # type: ignore[arg-type]

    assert await downloader.publish_buffered(1, tmp_path / "file.bin") is None


class VariantRecordingRepository(RecordingRepository):
    def __init__(self) -> None:
        super().__init__()
        self.variant_remote_paths: list[str | None] = []

    async def mark_download_completed(
        self, _message_id: int, _path: Path, _size: int, _variant_local_path: str | None = None,
        **kwargs: object,
    ) -> None:
        self.variant_remote_paths.append(kwargs.get("terabox_variant_remote_path"))
        await super().mark_download_completed(_message_id, _path, _size, _variant_local_path, **kwargs)

    async def get_message_by_id(self, _message_id: int) -> object:
        return type("Record", (), {"id": 1, "telegram_chat_id": 12345})()


async def test_publish_buffered_does_not_infer_untracked_variant(tmp_path: Path) -> None:
    """A re-publish only records variants uploaded in the same API operation."""
    repository = VariantRecordingRepository()
    settings = _terabox_settings(tmp_path)
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    uploader = FakeUploader(settings.terabox_remote_root)
    downloader = MediaDownloader(settings, repository, uploader)  # type: ignore[arg-type]
    target = settings.download_dir / "42_report.pdf"
    target.write_bytes(b"complete")
    result = await downloader.publish_buffered(1, target)  # type: ignore[arg-type]

    assert result is not None and result.completed
    assert uploader.uploaded == [target]
    assert repository.variant_remote_paths == [None]


async def test_hevc_transcodes_are_bounded_separately_from_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _terabox_settings(tmp_path).model_copy(
        update={
            "download_concurrency": 5,
            "transcode_concurrency": 1,
            "media_faststart": False,
            "media_variants": False,
            "terabox_generate_posters": False,
        }
    )
    downloader = MediaDownloader(settings, RecordingRepository(), FakeUploader(settings.terabox_remote_root))
    downloader._capabilities = FfmpegCapabilities(
        available=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        h264_encoders=("h264_rkmpp",),
        hevc_decoder=True,
    )
    monkeypatch.setattr("app.infrastructure.download.probe_video_codec", lambda *_: _async_hevc())

    active = 0
    peak = 0
    release = asyncio.Event()

    async def fake_transcode(*_args: object) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return True

    monkeypatch.setattr("app.infrastructure.download.transcode_hevc_to_h264", fake_transcode)
    first = asyncio.create_task(downloader._optimize(tmp_path / "first.mp4"))
    second = asyncio.create_task(downloader._optimize(tmp_path / "second.mp4"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert peak == 1
    release.set()
    await asyncio.gather(first, second)


async def _async_hevc() -> str:
    return "hevc"
