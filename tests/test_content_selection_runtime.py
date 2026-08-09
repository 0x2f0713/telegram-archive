import asyncio
from types import SimpleNamespace

from app.config import Settings
from app.database.repository import RetryCandidate
from app.services.archive import ArchiveService, ProcessResult
from app.telegram.listener import RealtimeListener
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
    listener = RealtimeListener(
        SimpleNamespace(),
        {chat.telegram_chat_id: chat},
        archive,  # type: ignore[arg-type]
        Settings(_env_file=None),
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
        frozenset({"voice"}),
    )
    client = RecordingClient()
    chat = make_chat()

    attempted, completed = await archive.retry_candidates(
        client,
        {chat.telegram_chat_id: chat},
        failed_only=True,
    )

    assert attempted == 0
    assert completed == 0
    assert client.calls == 0
