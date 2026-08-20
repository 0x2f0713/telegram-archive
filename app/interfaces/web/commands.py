"""Composition of allowlisted long-running workflows for the web operator.

Each command is a use case executed inside an ``OperationContext``: sync
historical history, run the real-time listener, retry failed media, and run
the doctor. They are the only commands a browser can start, and none of them
accepts a shell command.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from telethon import events

from app.application.archive import ArchiveService, RetryProgress
from app.application.chat_selection import ChatSelectionService
from app.application.listener import ListenerProgress, RealtimeListener
from app.application.operations import (
    OperationContext,
    OperationExecutionError,
    OperationExecutor,
    OperationManager,
)
from app.application.sync import SyncProgress, sync_history
from app.config import ConfigurationError, Settings
from app.domain import ALL_CONTENT_TYPES, ContentType, normalize_content_types
from app.domain.content import canonical_content_type_list
from app.infrastructure.download import MediaDownloader
from app.infrastructure.ffmpeg import extract_poster, probe_capabilities, remux_faststart
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.persistence.selection import ChatSelectionRepository
from app.infrastructure.telegram.client import (
    accessible_dialogs,
    connect_authorized,
    create_readonly_client,
    flood_wait_seconds,
    is_transient_telegram_error,
    resolve_accessible_chats,
)
from app.infrastructure.telegram.translation import content_types_of, message_data
from app.infrastructure.terabox import TeraBoxClient, TeraBoxUploader, create_terabox_client
from app.infrastructure.transcode import POSTER_SUFFIX, is_faststart
from app.utils.logging import format_bytes

logger = logging.getLogger(__name__)


def _resolve_media_path(raw_path: str) -> Path:
    return Path(raw_path).expanduser().resolve()


class OperationCommands:
    """Executors bound to one operation manager."""

    def __init__(
        self,
        manager: OperationManager,
        database: Database,
        readonly_client_factory: Callable[[Settings], Any] = create_readonly_client,
        video_cache: Any | None = None,
    ) -> None:
        self.manager = manager
        self.database = database
        self.video_cache = video_cache
        #: Short-lived operations share the account without writing the session
        #: file, so they never lock the archiver's Telethon SQLite session.
        self.readonly_client_factory = readonly_client_factory

    @property
    def settings(self) -> Settings:
        return self.manager.settings

    def executors(self) -> dict[str, OperationExecutor]:
        return {
            "sync": self.sync,
            "listen": self.listen,
            "retry-failed": self.retry_failed,
            "doctor": self.doctor,
            "optimize-media": self.optimize_media,
        }

    def _chat_selection_service(self, repository: ArchiveRepository) -> ChatSelectionService:
        return ChatSelectionService(
            self.settings.configured_chat_ids,
            repository,
            ChatSelectionRepository(repository.database),
            accessible_dialogs,
            resolve_accessible_chats,
        )

    def _terabox_client(self) -> TeraBoxClient:
        """Raw TeraBox client for health checks (doctor, quota)."""

        return create_terabox_client(self.settings)

    def _terabox_uploader(self) -> TeraBoxUploader | None:
        """Build the remote-storage adapter when TeraBox mode is enabled."""

        if not self.settings.terabox_enabled:
            return None
        return TeraBoxUploader(self.settings, create_terabox_client(self.settings))

    @staticmethod
    def _content_types(parameters: Mapping[str, Any]) -> frozenset[ContentType] | None:
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
        content_types: frozenset[ContentType] | None = None,
        download_progress: Callable[[str, int, int], None] | None = None,
    ) -> tuple[ArchiveRepository, ArchiveService]:
        """Compose workers against the web process's single SQLite engine."""

        repository = ArchiveRepository(self.database)
        downloader = MediaDownloader(
            self.settings, repository, self._terabox_uploader(), self.video_cache
        )
        return (
            repository,
            ArchiveService(
                self.settings,
                repository,
                downloader,
                message_data,
                content_types_of,
                flood_wait_seconds,
                is_transient_telegram_error,
                content_types,
                download_progress,
            ),
        )

    @staticmethod
    def _download_reporter(
        context: OperationContext,
    ) -> Callable[[str, int, int], None]:
        """Bridge Telethon's synchronous callback into throttled operation updates."""
        last_report: dict[str, float] = {}
        states: dict[str, tuple[int, float, float]] = {}
        tasks: dict[str, dict[str, Any]] = {}
        pending_update: dict[str, Any] | None = None
        pending_force = False
        reporter_task: asyncio.Task[None] | None = None

        async def publish() -> None:
            nonlocal pending_force, pending_update
            while pending_update is not None:
                update = pending_update
                force = pending_force
                pending_update = None
                pending_force = False
                await context.progress(force=force, **update)

        def publish_done(completed: asyncio.Task[None]) -> None:
            if completed.cancelled():
                return
            if error := completed.exception():
                logger.error(
                    "Could not publish download progress: %s",
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        def report(filename: str, current: int, total: int, phase: str = "downloading") -> None:
            nonlocal pending_force, pending_update, reporter_task
            now = monotonic()
            # Throttle per file AND per phase so a download→upload transition is
            # never suppressed by the immediately preceding download report.
            report_key = f"{phase}:{filename}"
            previous_report = last_report.get(report_key, 0.0)
            if current < total and now - previous_report < 0.2:
                return
            last_report[report_key] = now
            last_current, last_time, previous_speed = states.get(report_key, (0, now, 0.0))
            elapsed = max(0.001, now - last_time)
            instantaneous_speed = max(0, current - last_current) / elapsed
            speed = (
                instantaneous_speed
                if not previous_speed
                else (previous_speed * 0.7 + instantaneous_speed * 0.3)
            )
            states[report_key] = (current, now, speed)
            percent = round(current / total * 100, 1) if total else None

            # Keep the same task row through the download→upload transition.
            # A final upload callback is allowed to become completed, but an
            # in-flight upload must remain orange even when the download had
            # already reported current == total.
            if phase == "uploading" and (not total or current < total):
                status = "uploading"
            elif phase == "preparing":
                status = "preparing"
            elif total and current >= total:
                status = "completed"
            else:
                status = "downloading"

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
                if tasks[oldest]["status"] in ("downloading", "uploading", "preparing"):
                    break
                tasks.pop(oldest)
                for phase_name in ("downloading", "uploading", "preparing"):
                    states.pop(f"{phase_name}:{oldest}", None)
                    last_report.pop(f"{phase_name}:{oldest}", None)
            downloading = [task for task in tasks.values() if task["status"] == "downloading"]
            uploading = [task for task in tasks.values() if task["status"] == "uploading"]
            preparing = [task for task in tasks.values() if task["status"] == "preparing"]
            aggregate_download = round(sum(task["speed"] for task in downloading))
            aggregate_upload = round(sum(task["speed"] for task in uploading))
            segments: list[str] = []
            if downloading:
                segments.append(
                    f"Downloading {len(downloading)} file{'s' if len(downloading) != 1 else ''} "
                    f"at {format_bytes(aggregate_download)}/s"
                )
            if preparing:
                segments.append(
                    f"Optimizing {len(preparing)} file{'s' if len(preparing) != 1 else ''}"
                )
            if uploading:
                segments.append(
                    f"Uploading {len(uploading)} file{'s' if len(uploading) != 1 else ''} "
                    f"at {format_bytes(aggregate_upload)}/s"
                )
            if segments:
                detail = " · ".join(segments)
            else:
                detail = (
                    f"Downloading {filename} ({percent or 0:.1f}%, {format_bytes(round(speed))}/s)"
                )
            if downloading:
                active_phase = "downloading"
            elif preparing:
                active_phase = "preparing"
            elif uploading:
                active_phase = "uploading"
            else:
                active_phase = "idle"
            pending_update = {
                "phase": active_phase,
                "detail": detail,
                "download_filename": filename,
                "download_current": current,
                "download_total": total,
                "download_percent": percent,
                "download_speed": aggregate_download,
                "upload_speed": aggregate_upload,
                "transfer_speed": aggregate_download + aggregate_upload,
                "download_tasks": list(tasks.values()),
            }
            pending_force = pending_force or current >= total
            if reporter_task is None or reporter_task.done():
                reporter_task = asyncio.create_task(
                    publish(),
                    name="operation-download-progress",
                )
                reporter_task.add_done_callback(publish_done)

        return report

    async def _selected_chats(self, client: Any, repository: ArchiveRepository) -> dict[int, Any]:
        chats = await self._chat_selection_service(repository).resolve_with_client(client)
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

    async def sync(self, context: OperationContext) -> None:
        content_types = self._content_types(context.parameters)
        repository, archive = self._archive_stack(content_types, self._download_reporter(context))
        client = self.readonly_client_factory(self.settings)
        try:
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
                    chat_id=progress.chat_id,
                    chat_title=progress.chat_title,
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
                    chat_id=progress.chat_id,
                    chat_title=progress.chat_title,
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
                content_classifier=content_types_of,
                rate_limit_delay=flood_wait_seconds,
                is_transient_error=is_transient_telegram_error,
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

    async def retry_failed(self, context: OperationContext) -> None:
        content_types = self._content_types(context.parameters)
        repository, archive = self._archive_stack(content_types, self._download_reporter(context))
        client = self.readonly_client_factory(self.settings)
        try:
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
                    chat_id=progress.chat_id,
                    chat_title=progress.chat_title,
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

    async def listen(self, context: OperationContext) -> None:
        content_types = self._content_types(context.parameters)
        repository, archive = self._archive_stack(content_types, self._download_reporter(context))
        client = self.readonly_client_factory(self.settings)
        try:
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
                if progress.stage == "completed":
                    await context.increment(
                        messages_processed=1,
                        downloads_completed=int(progress.downloaded),
                    )
                noun = "edit" if progress.edited else "message"
                action = "Archived" if progress.stage == "completed" else "Archiving"
                await context.progress(
                    phase="listening",
                    detail=f"{action} {noun} {progress.message_id} from {progress.chat_title}",
                    chat_id=progress.chat_id,
                    chat_title=progress.chat_title,
                )

            listener = RealtimeListener(
                client,
                chats,
                archive,
                self.settings,
                event_builders=lambda entities: (
                    events.NewMessage(chats=entities),
                    events.MessageEdited(chats=entities),
                ),
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
                    chat_id=progress.chat_id,
                    chat_title=progress.chat_title,
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

    async def doctor(self, context: OperationContext) -> None:
        checks_total = 5 if self.settings.terabox_enabled else 4
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

        current_check = 3
        if self.settings.terabox_enabled:
            current_check += 1
            terabox_client = self._terabox_client()
            try:
                await terabox_client.login_check()
                await terabox_client.ensure_remote_dir(terabox_client.remote_root)
                total, used = await terabox_client.quota()
                await context.log(f"PASS · TeraBox authenticated; {used} / {total} bytes used")
            except Exception as exc:
                failed = True
                await context.log(f"FAIL · TeraBox: {type(exc).__name__}: {exc}", "ERROR")
            finally:
                await terabox_client.aclose()
            await context.progress(
                progress_current=current_check,
                detail="Validated TeraBox storage",
            )
            if context.stop_event.is_set():
                return

        client = self.readonly_client_factory(self.settings)
        repository, _archive = self._archive_stack()
        try:
            await connect_authorized(client)
            chats = await self._chat_selection_service(repository).resolve_with_client(client)
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
        await context.progress(
            force=True,
            progress_current=current_check + 1,
            detail="Doctor checks finished",
        )
        if failed:
            raise OperationExecutionError("One or more doctor checks failed")

    async def optimize_media(self, context: OperationContext) -> None:
        """Make completed videos play instantly: faststart remux and posters."""
        if self.settings.terabox_enabled:
            raise OperationExecutionError(
                "Media optimization is disabled in TeraBox storage mode: the remote "
                "drive keeps pristine originals and cannot be remuxed in place"
            )
        repository = ArchiveRepository(self.database)
        capabilities = await probe_capabilities(self.settings)
        if not capabilities.available:
            raise OperationExecutionError(
                "ffmpeg is not available in this environment; media optimization is disabled"
            )
        candidates = await repository.completed_video_paths()
        if not candidates:
            await context.progress(
                force=True,
                phase="optimizing",
                detail="No completed videos to optimize",
            )
            return
        faststarted = 0
        posters = 0
        total = len(candidates)
        for index, (raw_path, _size) in enumerate(candidates, start=1):
            if context.stop_event.is_set():
                return
            media_path = await asyncio.to_thread(_resolve_media_path, raw_path)
            if not await asyncio.to_thread(media_path.is_file):
                continue
            needs_faststart = not await is_faststart(media_path)
            poster_target = media_path.with_name(f"{media_path.stem}{POSTER_SUFFIX}")
            needs_poster = self.settings.media_variants and not await asyncio.to_thread(
                poster_target.is_file
            )
            if not needs_faststart and not needs_poster:
                continue
            actions = "faststart" if needs_faststart else ""
            if needs_poster:
                actions = "faststart + poster" if actions else "poster"
            await context.progress(
                force=(index == 1),
                phase="optimizing",
                detail=f"Optimizing {media_path.name} ({index}/{total}): {actions}",
                progress_current=index - 1,
                progress_total=total,
            )
            try:
                if needs_faststart:
                    await remux_faststart(self.settings, capabilities, media_path)
                    faststarted += 1
                if needs_poster:
                    await extract_poster(self.settings, capabilities, media_path, poster_target)
                    posters += 1
            except Exception as exc:
                logger.error("Could not optimize %s: %s", media_path, exc)
        await context.progress(
            force=True,
            progress_current=total,
            progress_total=total,
            phase="optimizing",
            detail=(
                f"Optimized {total} completed videos: "
                f"{faststarted} faststarted, {posters} posters written"
            ),
        )
