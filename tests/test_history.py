from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

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


class BlockingArchive:
    """Archive double that proves multiple messages are in flight together."""

    def __init__(self, expected_starts: int = 2) -> None:
        self.expected_starts = expected_starts
        self.started: list[int] = []
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def process_message(self, message: SimpleNamespace, _chat: object) -> ProcessResult:
        self.started.append(message.id)
        if len(self.started) >= self.expected_starts:
            self.all_started.set()
        await self.release.wait()
        return ProcessResult(created=True, downloaded=True, skipped=False)


class OrderedArchive:
    """Allow newer work to finish first while the checkpoint stays ordered."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.started_ids: set[int] = set()
        self.releases = {1: asyncio.Event(), 2: asyncio.Event()}

    async def process_message(self, message: SimpleNamespace, _chat: object) -> ProcessResult:
        self.started_ids.add(message.id)
        if self.started_ids == {1, 2}:
            self.started.set()
        await self.releases[message.id].wait()
        return ProcessResult(created=True, downloaded=True, skipped=False)


class FailingArchive:
    """Fail the oldest task and record cancellation of newer in-flight work."""

    def __init__(self) -> None:
        self.started_ids: set[int] = set()
        self.all_started = asyncio.Event()
        self.newer_cancelled = asyncio.Event()

    async def process_message(self, message: SimpleNamespace, _chat: object) -> ProcessResult:
        self.started_ids.add(message.id)
        if self.started_ids == {1, 2}:
            self.all_started.set()
        if message.id == 1:
            await self.all_started.wait()
            raise RuntimeError("database unavailable")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.newer_cancelled.set()
            raise


class FakeRepository:
    def __init__(self) -> None:
        self.checkpoint: int | None = None
        self.advanced: list[int] = []
        self.content_checkpoints: dict[str, int | None] = {}
        self.content_advanced: list[tuple[tuple[str, ...], int]] = []

    async def upsert_chat(self, _chat: object) -> None:
        pass

    async def get_checkpoint(self, _chat_id: int) -> int | None:
        return self.checkpoint

    async def advance_checkpoint(self, _chat_id: int, message_id: int) -> None:
        self.checkpoint = message_id
        self.advanced.append(message_id)

    async def get_content_checkpoints(
        self, _chat_id: int, content_types: tuple[str, ...]
    ) -> dict[str, int | None]:
        return {
            content_type: self.content_checkpoints.get(content_type)
            for content_type in content_types
        }

    async def advance_content_checkpoints(
        self,
        _chat_id: int,
        content_types: tuple[str, ...],
        message_id: int,
    ) -> None:
        self.content_advanced.append((content_types, message_id))
        for content_type in content_types:
            current = self.content_checkpoints.get(content_type) or 0
            self.content_checkpoints[content_type] = max(current, message_id)


def messages() -> list[SimpleNamespace]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        SimpleNamespace(id=value, date=start + timedelta(days=value - 1)) for value in range(1, 5)
    ]


def typed_messages() -> list[SimpleNamespace]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        SimpleNamespace(id=1, date=start, message="text only"),
        SimpleNamespace(id=2, date=start + timedelta(days=1), photo=object()),
        SimpleNamespace(id=3, date=start + timedelta(days=2), video=object()),
        SimpleNamespace(id=4, date=start + timedelta(days=3), message="more text"),
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


async def test_sync_processes_messages_with_bounded_concurrency() -> None:
    client = FakeClient(messages())
    archive = BlockingArchive()
    repository = FakeRepository()
    chat = make_chat()

    sync_task = asyncio.create_task(
        sync_history(
            client,
            {chat.telegram_chat_id: chat},
            archive,  # type: ignore[arg-type]
            repository,  # type: ignore[arg-type]
            limit=2,
            concurrency=2,
        )
    )
    await asyncio.wait_for(archive.all_started.wait(), timeout=1)

    assert archive.started == [1, 2]
    assert repository.advanced == []

    archive.release.set()
    result = await asyncio.wait_for(sync_task, timeout=1)

    assert result.messages == 2
    assert result.downloads == 2
    assert repository.advanced == [1, 2]


async def test_sync_advances_checkpoint_in_source_order() -> None:
    client = FakeClient(messages()[:2])
    archive = OrderedArchive()
    repository = FakeRepository()
    chat = make_chat()

    sync_task = asyncio.create_task(
        sync_history(
            client,
            {chat.telegram_chat_id: chat},
            archive,  # type: ignore[arg-type]
            repository,  # type: ignore[arg-type]
            concurrency=2,
        )
    )
    await asyncio.wait_for(archive.started.wait(), timeout=1)
    archive.releases[2].set()
    await asyncio.sleep(0)

    assert repository.advanced == []

    archive.releases[1].set()
    result = await asyncio.wait_for(sync_task, timeout=1)

    assert result.messages == 2
    assert repository.advanced == [1, 2]


async def test_sync_concurrency_does_not_overshoot_limit() -> None:
    client = FakeClient(messages())
    archive = BlockingArchive(expected_starts=2)
    repository = FakeRepository()
    chat = make_chat()

    sync_task = asyncio.create_task(
        sync_history(
            client,
            {chat.telegram_chat_id: chat},
            archive,  # type: ignore[arg-type]
            repository,  # type: ignore[arg-type]
            limit=2,
            concurrency=4,
        )
    )
    await asyncio.wait_for(archive.all_started.wait(), timeout=1)
    archive.release.set()
    result = await asyncio.wait_for(sync_task, timeout=1)

    assert result.messages == 2
    assert archive.started == [1, 2]
    assert repository.advanced == [1, 2]


async def test_sync_cancels_newer_tasks_when_oldest_fails() -> None:
    client = FakeClient(messages()[:2])
    archive = FailingArchive()
    repository = FakeRepository()
    chat = make_chat()

    with pytest.raises(RuntimeError, match="database unavailable"):
        await sync_history(
            client,
            {chat.telegram_chat_id: chat},
            archive,  # type: ignore[arg-type]
            repository,  # type: ignore[arg-type]
            concurrency=2,
        )

    assert archive.newer_cancelled.is_set()
    assert repository.advanced == []


async def test_filtered_sync_uses_independent_content_checkpoints() -> None:
    client = FakeClient(typed_messages())
    archive = FakeArchive()
    repository = FakeRepository()
    chat = make_chat()

    photos = await sync_history(
        client,
        {chat.telegram_chat_id: chat},
        archive,
        repository,  # type: ignore[arg-type]
        content_types=frozenset({"photo"}),
    )
    photos_again = await sync_history(
        client,
        {chat.telegram_chat_id: chat},
        archive,
        repository,  # type: ignore[arg-type]
        content_types=frozenset({"photo"}),
    )
    videos = await sync_history(
        client,
        {chat.telegram_chat_id: chat},
        archive,
        repository,  # type: ignore[arg-type]
        content_types=frozenset({"video"}),
    )

    assert photos.messages == 1
    assert photos_again.messages == 0
    assert videos.messages == 1
    assert archive.processed == [2, 3]
    assert repository.content_checkpoints == {"photo": 4, "video": 4}
    assert repository.advanced == []


async def test_filtered_sync_matches_caption_as_text_content() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    client = FakeClient([SimpleNamespace(id=1, date=start, message="caption", video=object())])
    archive = FakeArchive()
    repository = FakeRepository()
    chat = make_chat()

    result = await sync_history(
        client,
        {chat.telegram_chat_id: chat},
        archive,
        repository,  # type: ignore[arg-type]
        content_types=frozenset({"text"}),
    )

    assert result.messages == 1
    assert archive.processed == [1]
    assert repository.content_checkpoints == {"text": 1}
