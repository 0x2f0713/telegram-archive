"""Durable lifecycle for long-running archiver workflows started from the web.

This is the application controller: it owns job state transitions, the
in-memory progress overlay, stop coordination, and process-shutdown recovery.
The actual command bodies (sync, listen, retry-failed, doctor) live in
``app.application.commands`` and are injected as allowlisted executors.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from app.config import Settings
from app.domain import OperationCommand, OperationStatus
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.operations import (
    ACTIVE_OPERATION_STATUSES,
    OperationRecord,
    OperationRepository,
)
from app.infrastructure.telegram.client import create_client

logger = logging.getLogger(__name__)
OPERATION_COMMANDS = frozenset(command.value for command in OperationCommand)
PERSISTED_PROGRESS_FIELDS = frozenset(
    {
        "status",
        "phase",
        "detail",
        "progress_current",
        "progress_total",
        "chats_completed",
        "chats_total",
        "messages_processed",
        "downloads_completed",
        "retry_attempted",
        "retry_completed",
        "stop_requested",
        "error",
        "started_at",
        "finished_at",
    }
)
RUNTIME_PROGRESS_FIELDS = frozenset(
    {
        "download_filename",
        "download_current",
        "download_total",
        "download_percent",
        "download_speed",
        "download_tasks",
    }
)


class OperationConflictError(RuntimeError):
    """Raised when another Telegram operation is already active."""


class OperationNotFoundError(LookupError):
    """Raised when a requested operation ID does not exist."""


class OperationExecutionError(RuntimeError):
    """Operator-safe workflow failure."""


ClientFactory = Callable[[Settings], Any]
OperationExecutor = Callable[["OperationContext"], Awaitable[None]]


def operation_action(command: str, status: str, *, enabled: bool = True) -> dict[str, Any]:
    """Return the canonical operator action for one command/status pair."""
    try:
        operation_command = OperationCommand(command)
        operation_status = OperationStatus(status)
    except ValueError:
        return {"kind": "none", "label": "", "enabled": False}

    if operation_status is OperationStatus.STOPPING:
        return {"kind": "stop", "label": "Stopping safely…", "enabled": False}
    if operation_status.is_active:
        return {"kind": "stop", "label": "Stop safely", "enabled": enabled}
    if not operation_status.is_recoverable:
        return {"kind": "none", "label": "", "enabled": False}
    if operation_command is OperationCommand.SYNC:
        return {"kind": "resume", "label": "Resume sync", "enabled": enabled}
    labels = {
        OperationCommand.LISTEN: "Restart listener",
        OperationCommand.RETRY_FAILED: "Retry failed media",
        OperationCommand.DOCTOR: "Run diagnostics",
    }
    return {"kind": "retry", "label": labels[operation_command], "enabled": enabled}


def default_executors(manager: OperationManager) -> Mapping[str, OperationExecutor]:
    """Build the production executors; imported lazily to avoid an import cycle."""
    from app.application.commands import Commands

    return Commands(manager).executors()


@dataclass(slots=True)
class _Runtime:
    job_id: int
    command: str
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    overlay: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    last_flush: float = 0.0


@dataclass(frozen=True, slots=True)
class OperationContext:
    """Narrow capability passed to an allowlisted operation executor."""

    manager: OperationManager
    job_id: int
    command: str
    parameters: dict[str, Any]
    stop_event: asyncio.Event

    async def progress(self, *, force: bool = False, **values: Any) -> None:
        await self.manager._progress(self.job_id, force=force, **values)

    async def increment(self, **values: int) -> None:
        await self.manager._increment(self.job_id, **values)

    async def log(self, message: str, level: str = "INFO") -> None:
        await self.manager.repository.add_log(self.job_id, level, message)


class OperationManager:
    """Runs one allowlisted Telegram workflow and exposes reload-safe progress."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        client_factory: ClientFactory = create_client,
        executors: Mapping[str, OperationExecutor] | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.repository = OperationRepository(database)
        self.client_factory = client_factory
        self._lock = asyncio.Lock()
        self._runtime: dict[int, _Runtime] = {}
        self._executors: Mapping[str, OperationExecutor] = (
            executors if executors is not None else default_executors(self)
        )

    async def startup(self) -> None:
        interrupted = await self.repository.mark_active_interrupted()
        if interrupted:
            logger.warning("Recovered %s interrupted web operation(s)", interrupted)

    async def start_job(
        self, command: str, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        normalized = command.strip().casefold()
        if normalized not in OPERATION_COMMANDS or normalized not in self._executors:
            raise ValueError(f"Unsupported web operation: {command}")
        safe_parameters = dict(parameters or {})
        async with self._lock:
            active = await self.active()
            if active:
                raise OperationConflictError(
                    f"{active['command']} operation {active['id']} is already active"
                )
            record = await self._start_job_locked(normalized, safe_parameters)
        return await self.get(record.id)

    async def _start_job_locked(
        self, command: str, parameters: dict[str, Any]
    ) -> OperationRecord:
        """Create and schedule a job while ``self._lock`` is held."""
        record = await self.repository.create(command, parameters)
        runtime = _Runtime(job_id=record.id, command=command)
        runtime.overlay.update(record.public_dict())
        self._runtime[record.id] = runtime
        runtime.task = asyncio.create_task(
            self._execute(record.id, command, parameters),
            name=f"web-operation-{record.id}-{command}",
        )
        return record

    async def resume_job(self, job_id: int) -> dict[str, Any]:
        """Reactivate one interrupted sync under its original operation ID.

        Telegram progress is checkpointed separately from the operation row, so
        a resumed task continues from durable checkpoints without creating a
        second operation history or re-running completed downloads.
        """

        async with self._lock:
            active = await self.active()
            if active:
                raise OperationConflictError(
                    f"{active['command']} operation {active['id']} is already active"
                )
            record = await self.repository.get(job_id)
            if record is None:
                raise OperationNotFoundError(f"Operation {job_id} does not exist")
            if record.command != OperationCommand.SYNC.value:
                raise ValueError("Only historical sync operations can resume")
            if operation_action(record.command, record.status)["kind"] != "resume":
                raise ValueError("Only unfinished sync operations can resume")

            resumed = await self.repository.update(
                job_id,
                status=OperationStatus.QUEUED.value,
                phase=OperationStatus.QUEUED.value,
                detail="Resuming from durable checkpoints",
                stop_requested=False,
                error=None,
                finished_at=None,
            )
            if resumed is None:
                raise OperationNotFoundError(f"Operation {job_id} does not exist")
            runtime = _Runtime(job_id=job_id, command=record.command)
            runtime.overlay.update(resumed.public_dict())
            self._runtime[job_id] = runtime
            runtime.task = asyncio.create_task(
                self._execute(job_id, record.command, record.parameters),
                name=f"web-operation-{job_id}-{record.command}-resume",
            )
        await self.repository.add_log(job_id, "INFO", "Resuming from durable checkpoints")
        return await self.get(job_id)

    async def retry_job(self, job_id: int) -> dict[str, Any]:
        """Start a new operation using a retryable job's original parameters."""
        async with self._lock:
            active = await self.active()
            if active:
                raise OperationConflictError(
                    f"{active['command']} operation {active['id']} is already active"
                )
            record = await self.repository.get(job_id)
            if record is None:
                raise OperationNotFoundError(f"Operation {job_id} does not exist")
            action = operation_action(record.command, record.status)
            if action["kind"] != "retry":
                raise ValueError("Only unfinished non-sync operations can retry")
            if record.command not in self._executors:
                raise ValueError(f"Unsupported web operation: {record.command}")
            retried = await self._start_job_locked(record.command, dict(record.parameters))
        await self.repository.add_log(
            retried.id,
            "INFO",
            f"Retrying operation #{job_id} ({record.command})",
        )
        await self.repository.add_log(
            job_id,
            "INFO",
            f"Retry started as operation #{retried.id}",
        )
        return await self.get(retried.id)

    async def request_stop(self, job_id: int) -> dict[str, Any]:
        record = await self.repository.get(job_id)
        if record is None:
            raise OperationNotFoundError(f"Operation {job_id} does not exist")
        runtime = self._runtime.get(job_id)
        if record.status not in ACTIVE_OPERATION_STATUSES or runtime is None:
            return record.public_dict()
        runtime.stop_event.set()
        await self._progress(
            job_id,
            force=True,
            status=OperationStatus.STOPPING.value,
            phase=OperationStatus.STOPPING.value,
            detail="Finishing current database or file operation before stopping",
            stop_requested=True,
        )
        await self.repository.add_log(job_id, "WARNING", "Stop requested by the web operator")
        return await self.get(job_id)

    async def active(self) -> dict[str, Any] | None:
        for job_id, runtime in reversed(tuple(self._runtime.items())):
            if runtime.task and not runtime.task.done():
                candidate = await self.get(job_id)
                if candidate["active"]:
                    return candidate
        record = await self.repository.active()
        return self._public(record) if record else None

    async def get(self, job_id: int) -> dict[str, Any]:
        record = await self.repository.get(job_id)
        if record is None:
            raise OperationNotFoundError(f"Operation {job_id} does not exist")
        return self._public(record)

    async def recent(self, limit: int = 20) -> tuple[dict[str, Any], ...]:
        records = await self.repository.recent(limit)
        return tuple(self._public(record) for record in records)

    async def logs(self, job_id: int, limit: int = 100) -> tuple[dict[str, Any], ...]:
        if await self.repository.get(job_id) is None:
            raise OperationNotFoundError(f"Operation {job_id} does not exist")
        return tuple(log.public_dict() for log in await self.repository.logs(job_id, limit))

    async def shutdown(self) -> None:
        active = tuple(
            runtime
            for runtime in self._runtime.values()
            if runtime.task and not runtime.task.done()
        )
        if not active:
            return
        for runtime in active:
            runtime.stop_event.set()
            await self._progress(
                runtime.job_id,
                force=True,
                status=OperationStatus.STOPPING.value,
                phase=OperationStatus.STOPPING.value,
                detail="Web process is shutting down",
                stop_requested=True,
            )
        tasks = tuple(runtime.task for runtime in active if runtime.task)
        done, pending = await asyncio.wait(
            tasks,
            timeout=self.settings.shutdown_timeout_seconds,
        )
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done, return_exceptions=True)

    def _public(self, record: OperationRecord) -> dict[str, Any]:
        payload = record.public_dict()
        runtime = self._runtime.get(record.id)
        if runtime:
            for key, value in runtime.overlay.items():
                if key in PERSISTED_PROGRESS_FIELDS or key in RUNTIME_PROGRESS_FIELDS:
                    payload[key] = value
        payload["terminal"] = payload["status"] not in ACTIVE_OPERATION_STATUSES
        payload["active"] = payload["status"] in ACTIVE_OPERATION_STATUSES
        payload["action"] = operation_action(record.command, payload["status"])
        total = payload.get("progress_total")
        current = payload.get("progress_current", 0)
        payload["progress_percent"] = (
            min(100, round(current / total * 100, 1)) if total and total > 0 else None
        )
        return payload

    async def _execute(self, job_id: int, command: str, parameters: dict[str, Any]) -> None:
        runtime = self._runtime[job_id]
        context = OperationContext(
            manager=self,
            job_id=job_id,
            command=command,
            parameters=parameters,
            stop_event=runtime.stop_event,
        )
        started_at = datetime.now(UTC)
        try:
            await self._progress(
                job_id,
                force=True,
                status=OperationStatus.RUNNING.value,
                phase="starting",
                detail=f"Starting {command}",
                started_at=started_at,
            )
            await context.log(f"Started {command} operation")
            await self._executors[command](context)
            if runtime.stop_event.is_set():
                await self._finish(
                    job_id,
                    status=OperationStatus.CANCELLED.value,
                    phase=OperationStatus.CANCELLED.value,
                    detail="Stopped safely by the operator",
                    level="WARNING",
                )
            else:
                completion_detail = str(
                    runtime.overlay.get("detail") or f"{command} completed successfully"
                )
                await self._finish(
                    job_id,
                    status=OperationStatus.COMPLETED.value,
                    phase=OperationStatus.COMPLETED.value,
                    detail=completion_detail,
                )
        except asyncio.CancelledError:
            await self._finish(
                job_id,
                status=OperationStatus.CANCELLED.value,
                phase=OperationStatus.CANCELLED.value,
                detail="Cancelled during web process shutdown",
                level="WARNING",
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:4000]
            logger.error("Web operation %s failed: %s", job_id, error)
            await self._progress(
                job_id,
                force=True,
                status=OperationStatus.FAILED.value,
                phase=OperationStatus.FAILED.value,
                detail="Operation failed; review the log and retry after correcting the cause",
                error=error,
                finished_at=datetime.now(UTC),
            )
            await self.repository.add_log(job_id, "ERROR", error)

    async def _finish(
        self,
        job_id: int,
        *,
        status: str,
        phase: str,
        detail: str,
        level: str = "INFO",
    ) -> None:
        await self._progress(
            job_id,
            force=True,
            status=status,
            phase=phase,
            detail=detail,
            finished_at=datetime.now(UTC),
        )
        await self.repository.add_log(job_id, level, detail)

    async def _progress(self, job_id: int, *, force: bool = False, **values: Any) -> None:
        runtime = self._runtime.get(job_id)
        if runtime is None:
            return
        previous_phase = runtime.overlay.get("phase")
        runtime.overlay.update(
            {
                key: value
                for key, value in values.items()
                if key in PERSISTED_PROGRESS_FIELDS or key in RUNTIME_PROGRESS_FIELDS
            }
        )
        now = monotonic()
        should_flush = force or previous_phase != runtime.overlay.get("phase")
        should_flush = should_flush or now - runtime.last_flush >= 0.5
        if should_flush:
            persistent = {
                key: value
                for key, value in runtime.overlay.items()
                if key in PERSISTED_PROGRESS_FIELDS
            }
            await self.repository.update(job_id, **persistent)
            runtime.last_flush = now

    async def _increment(self, job_id: int, **values: int) -> None:
        runtime = self._runtime.get(job_id)
        if runtime is None:
            return
        updates = {
            key: int(runtime.overlay.get(key, 0) or 0) + increment
            for key, increment in values.items()
            if key in PERSISTED_PROGRESS_FIELDS
        }
        await self._progress(job_id, **updates)
