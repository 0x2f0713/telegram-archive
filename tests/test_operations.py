from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.database.operations import OperationRepository
from app.database.session import Database
from app.services.operations import (
    OperationConflictError,
    OperationContext,
    OperationManager,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'operations.db'}",
        download_dir=tmp_path / "downloads",
        tg_session_name=tmp_path / "telegram_session",
        shutdown_timeout_seconds=1,
    )


async def _wait_for_status(
    manager: OperationManager,
    job_id: int,
    statuses: set[str],
) -> dict[str, object]:
    for _ in range(200):
        operation = await manager.get(job_id)
        if operation["status"] in statuses:
            return operation
        await asyncio.sleep(0.01)
    raise AssertionError(f"Operation {job_id} did not reach {statuses}")


async def _wait_for_phase(
    manager: OperationManager,
    job_id: int,
    phase: str,
) -> dict[str, object]:
    for _ in range(200):
        operation = await manager.get(job_id)
        if operation["phase"] == phase:
            return operation
        await asyncio.sleep(0.01)
    raise AssertionError(f"Operation {job_id} did not reach phase {phase}")


async def test_operation_progress_history_and_single_job_exclusion(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize()
    release = asyncio.Event()

    async def fake_sync(context: OperationContext) -> None:
        await context.progress(
            force=True,
            phase="syncing",
            detail="Processing test history",
            progress_current=1,
            progress_total=2,
            chats_completed=1,
            chats_total=2,
            messages_processed=25,
            downloads_completed=3,
        )
        await context.log("First test chat completed")
        await release.wait()
        await context.progress(
            force=True,
            progress_current=2,
            chats_completed=2,
            messages_processed=40,
            downloads_completed=5,
        )

    manager = OperationManager(settings, database, executors={"sync": fake_sync})
    await manager.startup()
    started = await manager.start_job("sync", {"limit": 100})
    running = await _wait_for_phase(manager, int(started["id"]), "syncing")

    assert running["phase"] == "syncing"
    assert running["progress_percent"] == 50.0
    assert running["messages_processed"] == 25
    with pytest.raises(OperationConflictError, match="already active"):
        await manager.start_job("sync")

    release.set()
    completed = await _wait_for_status(manager, int(started["id"]), {"completed"})
    logs = await manager.logs(int(started["id"]))
    recent = await manager.recent()

    assert completed["progress_percent"] == 100.0
    assert completed["messages_processed"] == 40
    assert completed["downloads_completed"] == 5
    assert any("First test chat" in str(entry["message"]) for entry in logs)
    assert recent[0]["status"] == "completed"
    await manager.shutdown()
    await database.close()


async def test_operation_safe_stop_and_restart_recovery(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize()

    async def fake_listener(context: OperationContext) -> None:
        await context.progress(
            force=True,
            phase="listening",
            detail="Monitoring one test chat",
            chats_total=1,
        )
        await context.stop_event.wait()

    manager = OperationManager(settings, database, executors={"listen": fake_listener})
    await manager.startup()
    started = await manager.start_job("listen")
    await _wait_for_status(manager, int(started["id"]), {"running"})
    stopping = await manager.request_stop(int(started["id"]))
    cancelled = await _wait_for_status(manager, int(started["id"]), {"cancelled"})

    assert stopping["stop_requested"] is True
    assert cancelled["detail"] == "Stopped safely by the operator"

    repository = OperationRepository(database)
    orphan = await repository.create("sync", {})
    await repository.update(orphan.id, status="running", phase="syncing")
    recovered_manager = OperationManager(settings, database, executors={})
    await recovered_manager.startup()
    recovered = await recovered_manager.get(orphan.id)

    assert recovered["status"] == "interrupted"
    assert recovered["terminal"] is True
    await manager.shutdown()
    await database.close()
