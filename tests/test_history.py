from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.archive import ProcessResult
from app.telegram.history import SyncProgress, sync_history
from tests.helpers import make_chat


class FakeClient:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = messages
        self.min_ids: list[int] = []

    async def iter_messages(self, _entity: object, *, reverse: bool, min_id: int):
        assert reverse is True
        self.min_ids.append(min_id)
        for message in self.messages:
            if message.id > min_id:
                yield message


class FakeArchive:
    def __init__(self) -> None:
        self.processed: list[int] = []

    async def process_message(self, message: SimpleNamespace, _chat: object) -> ProcessResult:
        self.processed.append(message.id)
        return ProcessResult(created=True, downloaded=message.id % 2 == 0, skipped=False)


class FakeRepository:
    def __init__(self) -> None:
        self.checkpoint: int | None = None
        self.advanced: list[int] = []

    async def upsert_chat(self, _chat: object) -> None:
        pass

    async def get_checkpoint(self, _chat_id: int) -> int | None:
        return self.checkpoint

    async def advance_checkpoint(self, _chat_id: int, message_id: int) -> None:
        self.checkpoint = message_id
        self.advanced.append(message_id)


def messages() -> list[SimpleNamespace]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        SimpleNamespace(id=value, date=start + timedelta(days=value - 1)) for value in range(1, 5)
    ]


async def test_sync_limit_advances_checkpoint_and_next_run_resumes() -> None:
    client = FakeClient(messages())
    archive = FakeArchive()
    repository = FakeRepository()
    chats = {make_chat().telegram_chat_id: make_chat()}

    first = await sync_history(client, chats, archive, repository, limit=2)  # type: ignore[arg-type]
    second = await sync_history(client, chats, archive, repository)  # type: ignore[arg-type]

    assert first.messages == 2
    assert second.messages == 2
    assert archive.processed == [1, 2, 3, 4]
    assert repository.advanced == [1, 2, 3, 4]
    assert client.min_ids == [0, 2]


async def test_bounded_date_sync_does_not_change_full_sync_checkpoint() -> None:
    client = FakeClient(messages())
    archive = FakeArchive()
    repository = FakeRepository()
    repository.checkpoint = 4
    chat = make_chat()

    result = await sync_history(
        client,
        {chat.telegram_chat_id: chat},
        archive,
        repository,  # type: ignore[arg-type]
        since=datetime(2026, 1, 2, tzinfo=UTC),
        until=datetime(2026, 1, 4, tzinfo=UTC),
    )

    assert result.messages == 2
    assert archive.processed == [2, 3]
    assert repository.checkpoint == 4
    assert repository.advanced == []
    assert client.min_ids == [0]


async def test_sync_reports_live_progress_and_stops_between_messages() -> None:
    client = FakeClient(messages())
    archive = FakeArchive()
    repository = FakeRepository()
    chat = make_chat()
    stop_event = asyncio.Event()
    updates: list[SyncProgress] = []

    async def progress(update: SyncProgress) -> None:
        updates.append(update)
        if update.messages_processed == 2:
            stop_event.set()

    result = await sync_history(
        client,
        {chat.telegram_chat_id: chat},
        archive,
        repository,  # type: ignore[arg-type]
        stop_event=stop_event,
        progress=progress,
    )

    assert result.messages == 2
    assert result.chats == 0
    assert archive.processed == [1, 2]
    assert updates[0].detail == "Syncing Test Community"
    assert updates[-1].messages_processed == 2
