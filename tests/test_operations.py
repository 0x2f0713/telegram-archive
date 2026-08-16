from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.application.operations import (
    OperationConflictError,
    OperationContext,
    OperationManager,
    operation_action,
)
from app.config import Settings
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.operations import OperationRepository
from app.interfaces.web.commands import OperationCommands


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
            chat_id=-1001234567890,
            chat_title="Test Community",
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

    manager = OperationManager(
        settings,
        OperationRepository(database),
        executors={"sync": fake_sync},
    )
    await manager.startup()
    started = await manager.start_job("sync", {"limit": 100})
    running = await _wait_for_phase(manager, int(started["id"]), "syncing")

    assert running["phase"] == "syncing"
    assert running["progress_percent"] == 50.0
    assert running["messages_processed"] == 25
    assert running["chat_id"] == -1001234567890
    assert running["chat_title"] == "Test Community"
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

    reporter = OperationCommands._download_reporter(Context())  # type: ignore[arg-type]
    reporter("one.bin", 512, 1024)
    await asyncio.sleep(0)
    reporter("two.bin", 1024, 2048)
    await asyncio.sleep(0)

    assert updates
    tasks = updates[-1]["download_tasks"]
    assert isinstance(tasks, list)
    assert {task["filename"] for task in tasks} == {"one.bin", "two.bin"}
    assert all("speed" in task and "percent" in task for task in tasks)
    assert " B/s" not in str(updates[-1]["detail"])


async def test_download_reporter_aggregates_speed_across_active_files(
    tmp_path: Path,
) -> None:
    del tmp_path
    updates: list[dict[str, object]] = []

    class Context:
        async def progress(self, **values: object) -> None:
            updates.append(values)

    reporter = OperationCommands._download_reporter(Context())  # type: ignore[arg-type]
    reporter("one.bin", 512, 1024)
    await asyncio.sleep(0)
    reporter("two.bin", 1024, 2048)
    await asyncio.sleep(0)

    update = updates[-1]
    tasks = update["download_tasks"]
    assert isinstance(tasks, list)
    assert update["download_speed"] == sum(task["speed"] for task in tasks)
    assert "2 files" in str(update["detail"]) and "total" in str(update["detail"])
    # A single active file keeps the per-file detail instead of the aggregate.
    reporter("two.bin", 2048, 2048)
    reporter("one.bin", 1024, 1024)
    await asyncio.sleep(0)
    assert updates[-1]["download_speed"] == 0


async def test_download_reporter_coalesces_callback_bursts(tmp_path: Path) -> None:
    del tmp_path
    updates: list[dict[str, object]] = []
    first_update = asyncio.Event()
    release = asyncio.Event()

    class Context:
        async def progress(self, **values: object) -> None:
            updates.append(values)
            if len(updates) == 1:
                first_update.set()
                await release.wait()

    reporter = OperationCommands._download_reporter(Context())  # type: ignore[arg-type]
    reporter("one.bin", 256, 1024)
    await first_update.wait()
    reporter("two.bin", 2048, 2048)
    reporter("three.bin", 768, 1024)
    reporter("four.bin", 512, 1024)
    release.set()
    for _ in range(20):
        if len(updates) == 2:
            break
        await asyncio.sleep(0)

    assert len(updates) == 2
    assert updates[-1]["download_filename"] == "four.bin"
    assert updates[-1]["force"] is True
    tasks = updates[-1]["download_tasks"]
    assert isinstance(tasks, list)
    assert {task["filename"] for task in tasks} == {
        "one.bin",
        "two.bin",
        "three.bin",
        "four.bin",
    }


async def test_web_worker_archive_stack_reuses_application_database(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize()
    manager = OperationManager(
        settings,
        OperationRepository(database),
        executors={},
    )
    commands = OperationCommands(manager, database)

    repository, archive = commands._archive_stack()

    assert repository.database is database
    assert archive.repository is repository
    assert archive.downloader.repository is repository
    await database.close()


async def test_concurrent_progress_updates_are_coalesced_and_serialized(
    tmp_path: Path,
) -> None:
    class TrackingOperationRepository(OperationRepository):
        def __init__(self, database: Database) -> None:
            super().__init__(database)
            self.active_updates = 0
            self.max_active_updates = 0
            self.update_calls = 0

        async def update(self, job_id: int, **values: Any):  # type: ignore[no-untyped-def]
            self.active_updates += 1
            self.max_active_updates = max(self.max_active_updates, self.active_updates)
            self.update_calls += 1
            try:
                await asyncio.sleep(0.005)
                return await super().update(job_id, **values)
            finally:
                self.active_updates -= 1

    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize()
    repository = TrackingOperationRepository(database)
    release = asyncio.Event()

    async def fake_sync(context: OperationContext) -> None:
        await context.progress(force=True, phase="syncing", detail="Ready")
        await release.wait()

    manager = OperationManager(settings, repository, executors={"sync": fake_sync})
    await manager.startup()
    started = await manager.start_job("sync")
    job_id = int(started["id"])
    await _wait_for_phase(manager, job_id, "syncing")
    repository.update_calls = 0
    repository.max_active_updates = 0

    await asyncio.gather(
        *(manager._progress(job_id, detail=f"Burst {index}") for index in range(40))
    )

    assert repository.update_calls == 0
    assert (await manager.get(job_id))["detail"] == "Burst 39"

    await asyncio.gather(
        *(manager._progress(job_id, force=True, detail=f"Forced {index}") for index in range(20))
    )
    await manager._progress(job_id, force=True, detail="Final progress")
    durable = await repository.get(job_id)

    assert repository.max_active_updates == 1
    assert durable is not None
    assert durable.detail == "Final progress"

    release.set()
    await _wait_for_status(manager, job_id, {"completed"})
    await manager._progress(
        job_id,
        force=True,
        phase="downloading",
        detail="Late download callback",
    )
    completed = await manager.get(job_id)

    assert completed["status"] == "completed"
    assert completed["phase"] == "completed"
    assert completed["detail"] == "Final progress"
    await manager.shutdown()
    await database.close()


async def test_queue_pool_timeout_has_actionable_operation_detail(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize()
    queue_timeout = type("TimeoutError", (Exception,), {})

    async def failing_sync(_context: OperationContext) -> None:
        raise queue_timeout("QueuePool limit reached")

    manager = OperationManager(
        settings,
        OperationRepository(database),
        executors={"sync": failing_sync},
    )
    await manager.startup()
    started = await manager.start_job("sync")
    failed = await _wait_for_status(manager, int(started["id"]), {"failed"})

    assert failed["detail"].startswith("SQLite connection queue timed out")
    assert "Ensure only one archiver service" in failed["detail"]
    await manager.shutdown()
    await database.close()


def test_operation_action_matrix() -> None:
    assert operation_action("sync", "queued") == {
        "kind": "stop",
        "label": "Stop safely",
        "enabled": True,
    }
    assert operation_action("sync", "stopping") == {
        "kind": "stop",
        "label": "Stopping safely…",
        "enabled": False,
    }
    assert operation_action("sync", "failed")["kind"] == "resume"
    assert operation_action("listen", "cancelled") == {
        "kind": "retry",
        "label": "Restart listener",
        "enabled": True,
    }
    assert operation_action("retry-failed", "interrupted")["label"] == "Retry failed media"
    assert operation_action("doctor", "failed")["label"] == "Run diagnostics"
    assert operation_action("sync", "completed") == {
        "kind": "none",
        "label": "",
        "enabled": False,
    }
    assert operation_action("unknown", "failed")["kind"] == "none"


async def test_retry_starts_new_operation_with_original_parameters(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize()

    async def fake_listener(context: OperationContext) -> None:
        await context.stop_event.wait()

    manager = OperationManager(
        settings,
        OperationRepository(database),
        executors={"listen": fake_listener},
    )
    await manager.startup()
    started = await manager.start_job("listen", {"content_types": ["photo"]})
    original_id = int(started["id"])
    await _wait_for_status(manager, original_id, {"running"})
    await manager.request_stop(original_id)
    await _wait_for_status(manager, original_id, {"cancelled"})

    retried = await manager.retry_job(original_id)
    retried_id = int(retried["id"])
    assert retried_id != original_id
    assert retried["command"] == "listen"
    assert retried["parameters"] == {"content_types": ["photo"]}
    assert retried["action"]["kind"] == "stop"

    await manager.request_stop(retried_id)
    await _wait_for_status(manager, retried_id, {"cancelled"})
    original_logs = await manager.logs(original_id)
    retry_logs = await manager.logs(retried_id)
    assert any(
        f"Retry started as operation #{retried_id}" in log["message"] for log in original_logs
    )
    assert any(f"Retrying operation #{original_id}" in log["message"] for log in retry_logs)
    await manager.shutdown()
    await database.close()


async def test_resume_reactivates_the_original_sync_operation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize()
    runs = 0

    async def fake_sync(context: OperationContext) -> None:
        nonlocal runs
        runs += 1
        await context.progress(force=True, phase="syncing", detail=f"Run {runs}")

    manager = OperationManager(
        settings,
        OperationRepository(database),
        executors={"sync": fake_sync},
    )
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

    manager = OperationManager(
        settings,
        OperationRepository(database),
        executors={"listen": fake_listener},
    )
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
    recovered_manager = OperationManager(
        settings,
        OperationRepository(database),
        executors={},
    )
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

    assert OperationCommands._content_types(
        {"content_types": ["images", "voice-messages"]}
    ) == frozenset({"photo", "voice"})
    assert (
        OperationCommands._content_types(
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
