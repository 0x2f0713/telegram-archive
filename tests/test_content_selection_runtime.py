import asyncio
from types import SimpleNamespace

from app.application.archive import ArchiveService, ProcessResult, RetryProgress
from app.application.archive_records import (
    DownloadResult,
    MessageSnapshot,
    RetryCandidate,
)
from app.application.listener import ListenerProgress, RealtimeListener
from app.config import Settings
from app.domain import ChatInfo, MessageData
from app.infrastructure.telegram.translation import content_types_of, message_data
from tests.helpers import make_chat, make_message


class SelectiveArchive:
    def __init__(self) -> None:
        self.processed: list[int] = []

    def matches_message(self, message: SimpleNamespace) -> bool:
        return bool(getattr(message, "voice", None))

    async def process_message(
        self,
        message: SimpleNamespace,
        _chat: object,
        *,
        edited: bool = False,
    ) -> ProcessResult:
        assert edited is False
        self.processed.append(message.id)
        return ProcessResult(created=True, downloaded=True, skipped=False)


async def test_listener_schedules_only_selected_content_types() -> None:
    chat = make_chat()
    archive = SelectiveArchive()
    progress: list[ListenerProgress] = []

    async def record_progress(update: ListenerProgress) -> None:
        progress.append(update)

    listener = RealtimeListener(
        SimpleNamespace(),
        {chat.telegram_chat_id: chat},
        archive,  # type: ignore[arg-type]
        Settings(_env_file=None),
        progress=record_progress,
        manage_signals=False,
    )

    await listener._new_message(
        SimpleNamespace(
            chat_id=chat.telegram_chat_id,
            message=SimpleNamespace(id=1, audio=object()),
        )
    )
    await listener._new_message(
        SimpleNamespace(
            chat_id=chat.telegram_chat_id,
            message=SimpleNamespace(id=2, voice=object()),
        )
    )
    tasks = tuple(listener.pending)
    await asyncio.gather(*tasks)

    assert archive.processed == [2]
    assert [update.stage for update in progress] == ["started", "completed"]
    assert all(update.chat_id == chat.telegram_chat_id for update in progress)


class RetryRepository:
    async def iter_retry_candidates(
        self,
        _chat_ids: tuple[int, ...],
        *,
        failed_only: bool,
    ) -> list[RetryCandidate]:
        assert failed_only is True
        return [
            RetryCandidate(
                id=1,
                telegram_chat_id=make_chat().telegram_chat_id,
                telegram_message_id=42,
                media_path=None,
                download_status="failed",
                media_type="audio",
            )
        ]


class RecordingClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_messages(self, _entity: object, *, ids: int) -> object:
        self.calls += 1
        raise AssertionError(f"Unselected message {ids} must not be retrieved")


async def test_retry_does_not_fetch_unselected_media_types() -> None:
    repository = RetryRepository()
    archive = ArchiveService(
        Settings(_env_file=None),
        repository,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        message_data,
        content_types_of,
        lambda _error: None,
        lambda _error: False,
        frozenset({"voice"}),
    )
    client = RecordingClient()
    chat = make_chat()
    progress: list[RetryProgress] = []

    async def record_progress(update: RetryProgress) -> None:
        progress.append(update)

    attempted, completed = await archive.retry_candidates(
        client,
        {chat.telegram_chat_id: chat},
        failed_only=True,
        progress=record_progress,
    )

    assert attempted == 0
    assert completed == 0
    assert client.calls == 0
    assert progress[1].chat_id == chat.telegram_chat_id
    assert progress[1].chat_title == chat.title


class RetryBlockingRepository:
    def __init__(self, candidates: list[RetryCandidate]) -> None:
        self.candidates = candidates
        self.failed: list[tuple[int, str]] = []

    async def iter_retry_candidates(
        self,
        _chat_ids: tuple[int, ...],
        *,
        failed_only: bool,
    ) -> list[RetryCandidate]:
        return self.candidates

    async def upsert_message(self, data: MessageData) -> tuple[MessageSnapshot, bool]:
        return (
            MessageSnapshot(
                id=data.telegram_message_id,
                telegram_chat_id=data.telegram_chat_id,
                telegram_message_id=data.telegram_message_id,
                has_media=data.has_media,
                media_path=None,
                media_variant_path=None,
                media_size=None,
                download_status="pending",
                download_attempts=0,
            ),
            True,
        )

    async def mark_download_start(self, _message_id: int, _target: object) -> None:
        return None

    async def mark_download_completed(
        self, _message_id: int, _media_path: object, _media_size: int
    ) -> None:
        return None

    async def mark_download_skipped(self, _message_id: int, _reason: str) -> None:
        return None

    async def mark_download_failed(self, message_id: int, error: str) -> None:
        self.failed.append((message_id, error))


class BlockingRetryDownloader:
    def __init__(self, expected: int) -> None:
        self.started: list[int] = []
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()
        self._expected = expected

    async def download(
        self,
        record: MessageSnapshot,
        _raw_message: object,
        target: object,
        _progress: object | None = None,
        _upload_progress: object | None = None,
        _prepare_progress: object | None = None,
    ) -> DownloadResult:
        self.started.append(record.telegram_message_id)
        if len(self.started) >= self._expected:
            self.all_started.set()
        await self.release.wait()
        return DownloadResult(True, target, 1024)


class RetryClient:
    async def get_messages(self, _entity: object, *, ids: int) -> SimpleNamespace:
        return SimpleNamespace(id=ids)


def parse_retry_message(raw: SimpleNamespace, chat: ChatInfo) -> MessageData:
    return make_message(
        telegram_chat_id=chat.telegram_chat_id,
        telegram_message_id=raw.id,
    )


async def test_retry_candidates_runs_with_bounded_concurrency() -> None:
    chat = make_chat()
    candidates = [
        RetryCandidate(
            id=1,
            telegram_chat_id=chat.telegram_chat_id,
            telegram_message_id=41,
            media_path=None,
            download_status="failed",
            media_type="document",
        ),
        RetryCandidate(
            id=2,
            telegram_chat_id=chat.telegram_chat_id,
            telegram_message_id=42,
            media_path=None,
            download_status="failed",
            media_type="document",
        ),
    ]
    repository = RetryBlockingRepository(candidates)
    downloader = BlockingRetryDownloader(expected=2)
    archive = ArchiveService(
        Settings(_env_file=None, download_concurrency=2),
        repository,  # type: ignore[arg-type]
        downloader,  # type: ignore[arg-type]
        parse_retry_message,
        content_types_of,
        lambda _error: None,
        lambda _error: False,
    )
    client = RetryClient()
    retry_task = asyncio.create_task(
        archive.retry_candidates(
            client,
            {chat.telegram_chat_id: chat},
            failed_only=True,
        )
    )
    await asyncio.wait_for(downloader.all_started.wait(), timeout=1)

    assert sorted(downloader.started) == [41, 42]

    downloader.release.set()
    attempted, completed = await asyncio.wait_for(retry_task, timeout=1)

    assert attempted == 2
    assert completed == 2
    assert repository.failed == []
