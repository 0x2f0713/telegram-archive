from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.application.commands import Commands
from app.application.operations import (
    OperationConflictError,
    OperationContext,
    OperationManager,
)
from app.config import Settings
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.operations import OperationRepository


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
            download_tasks=[
                {
                    "filename": "clip.mp4",
                    "current": 512,
                    "total": 1024,
                    "percent": 50.0,
                    "speed": 256,
                    "status": "downloading",
                }
            ],
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
    assert running["download_tasks"] == [
        {
            "filename": "clip.mp4",
            "current": 512,
            "total": 1024,
            "percent": 50.0,
            "speed": 256,
            "status": "downloading",
        }
    ]
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


async def test_download_reporter_tracks_each_task_with_speed(tmp_path: Path) -> None:
    updates: list[dict[str, object]] = []

    class Context:
        async def progress(self, **values: object) -> None:
            updates.append(values)

    reporter = Commands._download_reporter(Context())  # type: ignore[arg-type]
    reporter("one.bin", 512, 1024)
    await asyncio.sleep(0)
    reporter("two.bin", 1024, 2048)
    await asyncio.sleep(0)

    assert updates
    tasks = updates[-1]["download_tasks"]
    assert isinstance(tasks, list)
    assert {task["filename"] for task in tasks} == {"one.bin", "two.bin"}
    assert all("speed" in task and "percent" in task for task in tasks)
    assert "/s)" in str(updates[-1]["detail"])
    assert " B/s" not in str(updates[-1]["detail"])


async def test_resume_reactivates_the_original_sync_operation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize()
    runs = 0

    async def fake_sync(context: OperationContext) -> None:
        nonlocal runs
        runs += 1
        await context.progress(force=True, phase="syncing", detail=f"Run {runs}")

    manager = OperationManager(settings, database, executors={"sync": fake_sync})
    await manager.startup()
    started = await manager.start_job("sync", {"chat": -1001234567890, "limit": 50})
    original_id = int(started["id"])
    await _wait_for_status(manager, original_id, {"completed"})
    await manager._progress(
        original_id,
        force=True,
        status="interrupted",
        phase="interrupted",
        detail="Web process exited before this operation finished",
    )

    resumed = await manager.resume_job(original_id)
    completed = await _wait_for_status(manager, original_id, {"completed"})
    logs = await manager.logs(original_id)

    assert resumed["id"] == original_id
    assert completed["parameters"] == {"chat": -1001234567890, "limit": 50}
    assert runs == 2
    assert any("Resuming from durable checkpoints" in str(log["message"]) for log in logs)
    assert len(await manager.recent()) == 1
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


def test_operation_content_type_parameters_are_canonical_and_all_is_unfiltered(
    tmp_path: Path,
) -> None:
    del tmp_path  # content parsing is pure; no database needed

    assert Commands._content_types({"content_types": ["images", "voice-messages"]}) == (
        frozenset({"photo", "voice"})
    )
    assert (
        Commands._content_types(
            {
                "content_types": [
                    "text",
                    "photo",
                    "video",
                    "video_note",
                    "voice",
                    "audio",
                    "animation",
                    "sticker",
                    "document",
                    "other",
                ]
            }
        )
        is None
    )
