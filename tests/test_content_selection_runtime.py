import asyncio
from types import SimpleNamespace

from app.application.archive import ArchiveService, ProcessResult, RetryProgress
from app.application.archive_records import RetryCandidate
from app.application.listener import ListenerProgress, RealtimeListener
from app.config import Settings
from app.infrastructure.telegram.translation import content_types_of, message_data
from tests.helpers import make_chat


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
