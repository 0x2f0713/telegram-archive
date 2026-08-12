from __future__ import annotations

from pathlib import Path

from app.application.sync import SyncResult
from app.config import Settings
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.settings import RuntimeSettingsRepository
from app.interfaces import cli


def test_sync_command_passes_effective_concurrency(monkeypatch, tmp_path: Path) -> None:
    base = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'cli.db'}")
    observed: list[int] = []

    class FakeDatabase:
        async def close(self) -> None:
            return None

    class FakeArchive:
        async def retry_candidates(self, client, selected):
            return 0, 0

    class FakeClient:
        async def disconnect(self) -> None:
            return None

    class FakeSelection:
        def __init__(self, settings, repository) -> None:
            return None

        async def resolve_with_client(self, client):
            return {123: object()}

    async def fake_stack(settings, content_types):
        return (
            FakeDatabase(),
            object(),
            FakeArchive(),
            settings.model_copy(update={"download_concurrency": 7}),
        )

    async def fake_sync(*args, concurrency, **kwargs):
        observed.append(concurrency)
        return SyncResult(chats=1)

    monkeypatch.setattr(cli, "_settings", lambda: base)
    monkeypatch.setattr(cli, "_archive_stack", fake_stack)
    monkeypatch.setattr(cli, "create_client", lambda settings: FakeClient())
    monkeypatch.setattr(cli, "connect_authorized", lambda client: _completed())
    monkeypatch.setattr(cli, "ChatSelectionService", FakeSelection)
    monkeypatch.setattr(cli, "sync_history", fake_sync)

    cli.sync_command(chat=None, limit=None, since=None, until=None, content_types=None)

    assert observed == [7]


async def _completed() -> None:
    return None


async def test_web_effective_settings_reads_persisted_log_level(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'web.db'}")
    database = Database(settings.database_url)
    await database.initialize()
    await RuntimeSettingsRepository(database).set_values({"log_level": "DEBUG"})
    await database.close()

    effective = await cli._effective_web_settings(settings)

    assert effective.log_level == "DEBUG"
