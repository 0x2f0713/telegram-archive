"""Safe in-process command controller for the private web dashboard."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from app.config import ConfigurationError, Settings
from app.database.operations import (
    ACTIVE_OPERATION_STATUSES,
    OperationRecord,
    OperationRepository,
)
from app.database.repository import ArchiveRepository
from app.database.session import Database
from app.services.archive import ArchiveService, RetryProgress
from app.services.chat_selection import ChatSelectionService
from app.services.content_types import (
    ALL_CONTENT_TYPES,
    canonical_content_type_list,
    normalize_content_types,
)
from app.services.downloader import MediaDownloader
from app.telegram.client import connect_authorized, create_client
from app.telegram.history import SyncProgress, sync_history
from app.telegram.listener import ListenerProgress, RealtimeListener
from app.utils.logging import format_bytes

logger = logging.getLogger(__name__)
OPERATION_COMMANDS = frozenset({"sync", "listen", "retry-failed", "doctor"})
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


OperationExecutor = Callable[[OperationContext], Awaitable[None]]


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
        self._executors: Mapping[str, OperationExecutor] = executors or {
            "sync": self._run_sync,
            "listen": self._run_listener,
            "retry-failed": self._run_retry_failed,
            "doctor": self._run_doctor,
        }

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
            record = await self.repository.create(normalized, safe_parameters)
            runtime = _Runtime(job_id=record.id, command=normalized)
            runtime.overlay.update(record.public_dict())
            self._runtime[record.id] = runtime
            runtime.task = asyncio.create_task(
                self._execute(record.id, normalized, safe_parameters),
                name=f"web-operation-{record.id}-{normalized}",
            )
        return await self.get(record.id)

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
            if record.command != "sync":
                raise ValueError("Only historical sync operations can resume")
            if record.status not in {"cancelled", "interrupted", "failed"}:
                raise ValueError("Only unfinished sync operations can resume")

            resumed = await self.repository.update(
                job_id,
                status="queued",
                phase="queued",
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
            status="stopping",
            phase="stopping",
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
                status="stopping",
                phase="stopping",
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
                status="running",
                phase="starting",
                detail=f"Starting {command}",
                started_at=started_at,
            )
            await context.log(f"Started {command} operation")
            await self._executors[command](context)
            if runtime.stop_event.is_set():
                await self._finish(
                    job_id,
                    status="cancelled",
                    phase="cancelled",
                    detail="Stopped safely by the operator",
                    level="WARNING",
                )
            else:
                completion_detail = str(
                    runtime.overlay.get("detail") or f"{command} completed successfully"
                )
                await self._finish(
                    job_id,
                    status="completed",
                    phase="completed",
                    detail=completion_detail,
                )
        except asyncio.CancelledError:
            await self._finish(
                job_id,
                status="cancelled",
                phase="cancelled",
                detail="Cancelled during web process shutdown",
                level="WARNING",
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:4000]
            logger.error("Web operation %s failed: %s", job_id, error)
            await self._progress(
                job_id,
                force=True,
                status="failed",
                phase="failed",
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

    @staticmethod
    def _content_types(parameters: Mapping[str, Any]) -> frozenset[str] | None:
        raw_values = parameters.get("content_types")
        if raw_values is None:
            return None
        if isinstance(raw_values, str):
            values = (raw_values,)
        elif isinstance(raw_values, (list, tuple)):
            values = tuple(str(value) for value in raw_values)
        else:
            raise ConfigurationError("content_types must be a list of Telegram content types")
        selected = normalize_content_types(values)
        return None if selected == ALL_CONTENT_TYPES else selected

    def _archive_stack(
        self,
        content_types: frozenset[str] | None = None,
        download_progress: Callable[[str, int, int], None] | None = None,
    ) -> tuple[Database, ArchiveRepository, ArchiveService]:
        database = Database(self.settings.database_url)
        repository = ArchiveRepository(database)
        downloader = MediaDownloader(self.settings, repository)
        return (
            database,
            repository,
            ArchiveService(
                self.settings,
                repository,
                downloader,
                content_types,
                download_progress,
            ),
        )

    @staticmethod
    def _download_reporter(context: OperationContext) -> Callable[[str, int, int], None]:
        """Bridge Telethon's synchronous callback into throttled operation updates."""
        last_report: dict[str, float] = {}
        states: dict[str, tuple[int, float, float]] = {}
        tasks: dict[str, dict[str, Any]] = {}

        def report(filename: str, current: int, total: int) -> None:
            now = monotonic()
            previous_report = last_report.get(filename, 0.0)
            if current < total and now - previous_report < 0.2:
                return
            last_report[filename] = now
            last_current, last_time, previous_speed = states.get(filename, (0, now, 0.0))
            elapsed = max(0.001, now - last_time)
            instantaneous_speed = max(0, current - last_current) / elapsed
            speed = instantaneous_speed if not previous_speed else (
                previous_speed * 0.7 + instantaneous_speed * 0.3
            )
            states[filename] = (current, now, speed)
            percent = round(current / total * 100, 1) if total else None
            status = "completed" if total and current >= total else "downloading"
            tasks[filename] = {
                "filename": filename,
                "current": current,
                "total": total,
                "percent": percent,
                "speed": round(speed),
                "status": status,
            }
            # Keep the active list useful without allowing a long sync to grow
            # the in-memory operation payload without bound.
            while len(tasks) > 8:
                oldest = next(iter(tasks))
                if tasks[oldest]["status"] == "downloading":
                    break
                tasks.pop(oldest)
            snapshot = list(tasks.values())
            task = asyncio.create_task(
                context.progress(
                    force=current >= total,
                    phase="downloading",
                    detail=(
                        f"Downloading {filename} ({percent or 0:.1f}%, "
                        f"{format_bytes(round(speed))}/s)"
                    ),
                    download_filename=filename,
                    download_current=current,
                    download_total=total,
                    download_percent=percent,
                    download_speed=round(speed),
                    download_tasks=snapshot,
                )
            )
            task.add_done_callback(lambda completed: completed.exception())

        return report

    async def _selected_chats(self, client: Any, repository: ArchiveRepository) -> dict[int, Any]:
        chats = await ChatSelectionService(self.settings, repository).resolve_with_client(client)
        if not chats:
            raise ConfigurationError(
                "No chats are selected. Choose chats on the Chats page before starting a worker."
            )
        return chats

    @staticmethod
    def _parse_sync_dates(parameters: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
        def parsed(name: str) -> datetime | None:
            raw = parameters.get(name)
            if not raw:
                return None
            try:
                return datetime.strptime(str(raw), "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError as exc:
                raise ConfigurationError(f"{name} must use YYYY-MM-DD format") from exc

        since = parsed("since")
        until_day = parsed("until")
        until = until_day + timedelta(days=1) if until_day else None
        if since and until and since >= until:
            raise ConfigurationError("Since date must be on or before until date")
        return since, until

    async def _run_sync(self, context: OperationContext) -> None:
        content_types = self._content_types(context.parameters)
        database, repository, archive = self._archive_stack(
            content_types, self._download_reporter(context)
        )
        client = self.client_factory(self.settings)
        try:
            await database.initialize()
            await context.progress(
                force=True,
                phase="connecting",
                detail="Connecting to the authorized Telegram session",
            )
            await connect_authorized(client)
            chats = await self._selected_chats(client, repository)
            raw_chat = context.parameters.get("chat")
            if raw_chat is not None:
                chat_id = int(raw_chat)
                if chat_id not in chats:
                    raise ConfigurationError(f"Chat {chat_id} is not selected for archiving")
                chats = {chat_id: chats[chat_id]}
            limit_value = context.parameters.get("limit")
            limit = int(limit_value) if limit_value is not None else None
            if limit is not None and limit < 1:
                raise ConfigurationError("Message limit must be at least 1")
            since, until = self._parse_sync_dates(context.parameters)
            if content_types is not None:
                await context.log(
                    "Selected content types: "
                    + ", ".join(canonical_content_type_list(content_types))
                )
            await context.progress(
                force=True,
                phase="repairing",
                detail="Checking incomplete media before history sync",
                progress_current=0,
                progress_total=None,
                chats_total=len(chats),
            )

            async def repair_progress(progress: RetryProgress) -> None:
                await context.progress(
                    phase=progress.phase,
                    detail=progress.detail,
                    progress_current=progress.current,
                    progress_total=progress.total,
                    retry_attempted=progress.attempted,
                    retry_completed=progress.completed,
                )

            attempted, repaired = await archive.retry_candidates(
                client,
                chats,
                stop_event=context.stop_event,
                progress=repair_progress,
            )
            if context.stop_event.is_set():
                return
            if attempted:
                await context.log(
                    f"Startup repair attempted {attempted} media files and downloaded {repaired}"
                )

            async def sync_progress(progress: SyncProgress) -> None:
                await context.progress(
                    phase=progress.phase,
                    detail=progress.detail,
                    progress_current=progress.chats_completed,
                    progress_total=progress.chats_total,
                    chats_completed=progress.chats_completed,
                    chats_total=progress.chats_total,
                    messages_processed=progress.messages_processed,
                    downloads_completed=progress.downloads_completed + repaired,
                )
                if progress.phase == "chat-complete":
                    await context.log(progress.detail)
                elif progress.phase in {"rate-limited", "reconnecting"}:
                    await context.log(progress.detail, "WARNING")

            result = await sync_history(
                client,
                chats,
                archive,
                repository,
                limit=limit,
                since=since,
                until=until,
                concurrency=self.settings.download_concurrency,
                content_types=content_types,
                stop_event=context.stop_event,
                progress=sync_progress,
            )
            await context.progress(
                force=True,
                progress_current=result.chats,
                progress_total=len(chats),
                chats_completed=result.chats,
                chats_total=len(chats),
                messages_processed=result.messages,
                downloads_completed=result.downloads + repaired,
                detail=(
                    f"Processed {result.messages} messages and downloaded "
                    f"{result.downloads + repaired} files"
                ),
            )
        finally:
            await client.disconnect()
            await database.close()

    async def _run_retry_failed(self, context: OperationContext) -> None:
        content_types = self._content_types(context.parameters)
        database, repository, archive = self._archive_stack(
            content_types, self._download_reporter(context)
        )
        client = self.client_factory(self.settings)
        try:
            await database.initialize()
            await context.progress(
                force=True,
                phase="connecting",
                detail="Connecting to Telegram and loading failed media",
            )
            await connect_authorized(client)
            chats = await self._selected_chats(client, repository)

            async def retry_progress(progress: RetryProgress) -> None:
                await context.progress(
                    phase=progress.phase,
                    detail=progress.detail,
                    progress_current=progress.current,
                    progress_total=progress.total,
                    retry_attempted=progress.attempted,
                    retry_completed=progress.completed,
                    downloads_completed=progress.completed,
                    chats_total=len(chats),
                )

            attempted, completed = await archive.retry_candidates(
                client,
                chats,
                failed_only=True,
                stop_event=context.stop_event,
                progress=retry_progress,
            )
            await context.progress(
                force=True,
                retry_attempted=attempted,
                retry_completed=completed,
                downloads_completed=completed,
                detail=f"Retried {attempted} failed media files; {completed} downloaded",
            )
        finally:
            await client.disconnect()
            await database.close()

    async def _run_listener(self, context: OperationContext) -> None:
        content_types = self._content_types(context.parameters)
        database, repository, archive = self._archive_stack(
            content_types, self._download_reporter(context)
        )
        client = self.client_factory(self.settings)
        try:
            await database.initialize()
            await context.progress(
                force=True,
                phase="connecting",
                detail="Connecting the real-time Telegram listener",
            )
            await connect_authorized(client)
            chats = await self._selected_chats(client, repository)
            await context.progress(
                force=True,
                chats_total=len(chats),
                phase="repairing",
                detail="Repairing incomplete media before listening",
            )

            async def listener_progress(progress: ListenerProgress) -> None:
                await context.increment(
                    messages_processed=1,
                    downloads_completed=int(progress.downloaded),
                )
                action = "edit" if progress.edited else "message"
                await context.progress(
                    phase="listening",
                    detail=f"Archived {action} {progress.message_id} from {progress.chat_title}",
                )

            listener = RealtimeListener(
                client,
                chats,
                archive,
                self.settings,
                progress=listener_progress,
                manage_signals=False,
                stop_event=context.stop_event,
            )
            listener.install_handlers()

            async def repair_progress(progress: RetryProgress) -> None:
                await context.progress(
                    phase=progress.phase,
                    detail=progress.detail,
                    progress_current=progress.current,
                    progress_total=progress.total,
                    retry_attempted=progress.attempted,
                    retry_completed=progress.completed,
                )

            attempted, repaired = await archive.retry_candidates(
                client,
                chats,
                stop_event=context.stop_event,
                progress=repair_progress,
            )
            if context.stop_event.is_set():
                return
            await context.log(
                f"Listener startup repair attempted {attempted} media files and downloaded {repaired}"
            )
            await context.progress(
                force=True,
                phase="listening",
                detail=f"Monitoring {len(chats)} selected chats for new messages and edits",
                progress_current=0,
                progress_total=None,
                chats_total=len(chats),
                downloads_completed=repaired,
            )
            await listener.run()
        finally:
            await client.disconnect()
            await database.close()

    async def _run_doctor(self, context: OperationContext) -> None:
        checks_total = 4
        failed = False
        await context.progress(
            force=True,
            phase="checking",
            detail="Validating configuration",
            progress_current=0,
            progress_total=checks_total,
        )
        try:
            self.settings.require_telegram_credentials()
            await context.log("PASS · Telegram API credentials are configured")
        except ConfigurationError as exc:
            failed = True
            await context.log(f"FAIL · Environment: {exc}", "ERROR")
        await context.progress(
            progress_current=1,
            detail="Validated environment configuration",
        )
        if context.stop_event.is_set():
            return

        try:
            await self.database.healthcheck()
            await context.log("PASS · SQLite database is readable and writable")
        except Exception as exc:
            failed = True
            await context.log(f"FAIL · Database: {type(exc).__name__}: {exc}", "ERROR")
        await context.progress(progress_current=2, detail="Validated SQLite database")
        if context.stop_event.is_set():
            return

        def check_download_directory(path: Path) -> None:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path, prefix=".doctor-", delete=True):
                pass

        try:
            await asyncio.to_thread(check_download_directory, self.settings.download_dir)
            await context.log("PASS · Download directory is writable")
        except OSError as exc:
            failed = True
            await context.log(f"FAIL · Downloads: {type(exc).__name__}: {exc}", "ERROR")
        await context.progress(progress_current=3, detail="Validated download storage")
        if context.stop_event.is_set():
            return

        client = self.client_factory(self.settings)
        database, repository, _archive = self._archive_stack()
        try:
            await database.initialize()
            await connect_authorized(client)
            chats = await ChatSelectionService(self.settings, repository).resolve_with_client(
                client
            )
            if chats:
                await context.log(
                    f"PASS · Telegram session authorized; {len(chats)} selected chats accessible"
                )
            else:
                await context.log(
                    "WARN · Telegram session authorized; no chats selected", "WARNING"
                )
        except Exception as exc:
            failed = True
            await context.log(f"FAIL · Telegram: {type(exc).__name__}: {exc}", "ERROR")
        finally:
            await client.disconnect()
            await database.close()
        await context.progress(
            force=True,
            progress_current=4,
            detail="Doctor checks finished",
        )
        if failed:
            raise OperationExecutionError("One or more doctor checks failed")
