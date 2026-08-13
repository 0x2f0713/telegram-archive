from pathlib import Path

import pytest

from app.application.chat_selection import ChatSelectionService
from app.config import Settings
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.persistence.selection import ChatSelection, ChatSelectionRepository
from app.infrastructure.telegram.client import TelegramAccessError, resolve_accessible_chats
from tests.helpers import make_chat


async def test_selection_policy_persists_specific_all_and_environment(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'selection.db'}")
    await database.initialize()
    archive = ArchiveRepository(database)
    first = make_chat(telegram_chat_id=-1001, title="First")
    second = make_chat(telegram_chat_id=-1002, title="Second")
    await archive.upsert_chats((first, second))
    selections = ChatSelectionRepository(database)

    assert await selections.policy() == ChatSelection("environment")
    assert await selections.effective_known_ids((-1002,)) == (-1002,)

    await selections.set_specific((-1002, -1001, -1002))
    assert await selections.policy() == ChatSelection("specific", (-1002, -1001))
    assert await selections.effective_known_ids(()) == (-1002, -1001)

    await selections.set_specific(())
    assert await selections.policy() == ChatSelection("specific", ())

    await selections.set_all()
    assert await selections.policy() == ChatSelection("all")
    assert set(await selections.effective_known_ids(())) == {-1001, -1002}

    await selections.use_environment()
    assert await selections.policy() == ChatSelection("environment")
    assert await selections.effective_known_ids((-1001,)) == (-1001,)
    await database.close()


async def test_runtime_resolution_only_uses_currently_accessible_dialogs(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'runtime-selection.db'}",
        target_chats="-1001",
    )
    database = Database(settings.database_url)
    await database.initialize()
    archive = ArchiveRepository(database)

    async def fake_accessible_dialogs(_client: object):
        return list(dialogs)

    service = ChatSelectionService(
        settings.configured_chat_ids,
        archive,
        ChatSelectionRepository(database),
        fake_accessible_dialogs,
        resolve_accessible_chats,
    )
    dialogs = (
        make_chat(telegram_chat_id=-1001, title="First"),
        make_chat(telegram_chat_id=-1002, title="Second"),
    )

    environment_targets = await service.resolve_with_client(object())  # type: ignore[arg-type]
    assert tuple(environment_targets) == (-1001,)

    await service.selections.set_all()
    all_targets = await service.resolve_with_client(object())  # type: ignore[arg-type]
    assert tuple(all_targets) == (-1001, -1002)

    await service.selections.set_specific((-9999,))
    with pytest.raises(TelegramAccessError, match="cannot resolve"):
        await service.resolve_with_client(object())  # type: ignore[arg-type]
    await database.close()


async def test_dialog_metadata_upsert_handles_large_accounts(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'large-selection.db'}")
    await database.initialize()
    archive = ArchiveRepository(database)
    dialogs = tuple(
        make_chat(telegram_chat_id=-(index + 1), title=f"Chat {index}") for index in range(1_100)
    )

    await archive.upsert_chats(dialogs)

    assert len(await ChatSelectionRepository(database).known_chat_ids()) == 1_100
    await database.close()
