"""Typer command implementations."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from app.application.archive import ArchiveService
from app.application.chat_selection import ChatSelectionService
from app.application.listener import RealtimeListener
from app.application.runtime_settings import load_runtime_settings
from app.application.sync import sync_history
from app.config import (
    ConfigurationError,
    Settings,
)
from app.domain import ContentType
from app.domain.content import (
    ALL_CONTENT_TYPES,
    normalize_content_types,
)
from app.infrastructure.download import MediaDownloader
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.persistence.selection import ChatSelectionRepository
from app.infrastructure.telegram.client import (
    TelegramAccessError,
    connect_authorized,
    create_client,
    login,
)
from app.utils.logging import configure_logging, format_bytes

console = Console()
app = typer.Typer(
    name="telegram-archiver",
    no_args_is_help=True,
    help="Archive accessible Telegram chats and media using your own user account.",
)
logger = logging.getLogger(__name__)


def _settings() -> Settings:
    settings = Settings()
    configure_logging(settings.log_level)
    return settings


async def _effective_settings(settings: Settings, database: Database) -> Settings:
    """Return settings with durable web overrides applied, or the originals."""
    effective = (await load_runtime_settings(settings, database)).settings
    if effective is not settings:
        configure_logging(effective.log_level)
    return effective


async def _effective_web_settings(settings: Settings) -> Settings:
    """Resolve the web log level before handing control to Uvicorn."""
    database = Database(settings.database_url)
    await database.initialize()
    try:
        return await _effective_settings(settings, database)
    finally:
        await database.close()


def _run(coroutine: Coroutine[Any, Any, Any]) -> None:
    try:
        asyncio.run(coroutine)
    except (ConfigurationError, TelegramAccessError, ValidationError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    except typer.Exit:
        raise
    except Exception as exc:
        logger.error("Command failed: %s: %s", type(exc).__name__, exc)
        raise typer.Exit(code=1) from exc


def _parse_day(value: str | None, option: str, *, end_exclusive: bool = False) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ConfigurationError(f"{option} must use YYYY-MM-DD format") from exc
    return parsed + timedelta(days=1) if end_exclusive else parsed


def _parse_content_types(value: str | None) -> frozenset[ContentType] | None:
    if value is None:
        return None
    selected = normalize_content_types((value,))
    return None if selected == ALL_CONTENT_TYPES else selected


async def _archive_stack(
    settings: Settings,
    content_types: frozenset[ContentType] | None = None,
) -> tuple[Database, ArchiveRepository, ArchiveService, Settings]:
    database = Database(settings.database_url)
    await database.initialize()
    repository = ArchiveRepository(database)
    effective = await _effective_settings(settings, database)
    downloader = MediaDownloader(effective, repository)
    return (
        database,
        repository,
        ArchiveService(
            effective,
            repository,
            downloader,
            content_types,
        ),
        effective,
    )


@app.command("login")
def login_command() -> None:
    """Interactively authenticate and save the local Telethon session."""

    async def command() -> None:
        settings = _settings()
        client = create_client(settings)
        try:
            await login(client)
        finally:
            await client.disconnect()

    _run(command())


@app.command("chats")
def chats_command() -> None:
    """List dialogs accessible to the authenticated Telegram account."""

    async def command() -> None:
        settings = _settings()
        database = Database(settings.database_url)
        repository = ArchiveRepository(database)
        selection_service = ChatSelectionService(settings, repository)
        client = create_client(settings)
        try:
            await database.initialize()
            await connect_authorized(client)
            discovery = await selection_service.discover_with_client(client)
            configured = set(discovery.effective_chat_ids)
            table = Table(title=f"Accessible Telegram chats · selection: {discovery.policy.mode}")
            table.add_column("ID", justify="right", no_wrap=True)
            table.add_column("TYPE")
            table.add_column("TITLE")
            table.add_column("USERNAME")
            table.add_column("ARCHIVING", justify="center")
            for dialog in discovery.dialogs:
                table.add_row(
                    str(dialog.telegram_chat_id),
                    dialog.type,
                    dialog.title,
                    f"@{dialog.username}" if dialog.username else "",
                    "yes" if dialog.telegram_chat_id in configured else "",
                )
            console.print(table)
        finally:
            await client.disconnect()
            await database.close()

    _run(command())


@app.command("sync")
def sync_command(
    chat: int | None = typer.Option(None, "--chat", help="Sync one configured Telegram chat ID."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum messages per chat."),
    since: str | None = typer.Option(None, "--since", help="Include messages on/after YYYY-MM-DD."),
    until: str | None = typer.Option(None, "--until", help="Include messages through YYYY-MM-DD."),
    content_types: str | None = typer.Option(
        None,
        "--types",
        help=(
            "Comma-separated content types: text, photo, video, video_note, voice, "
            "audio, animation, sticker, document, other. Defaults to all."
        ),
    ),
) -> None:
    """Incrementally archive historical messages from configured chats."""

    async def command() -> None:
        settings = _settings()
        since_date = _parse_day(since, "--since")
        until_date = _parse_day(until, "--until", end_exclusive=True)
        if since_date and until_date and since_date >= until_date:
            raise ConfigurationError("--since must be on or before --until")
        selected_types = _parse_content_types(content_types)

        database, repository, archive, effective = await _archive_stack(settings, selected_types)
        client = create_client(effective)
        try:
            await connect_authorized(client)
            all_chats = await ChatSelectionService(effective, repository).resolve_with_client(
                client
            )
            if not all_chats:
                raise ConfigurationError(
                    "No chats are selected. Choose chats in the web dashboard or TUI, "
                    "or configure TARGET_CHATS/CONFIG_FILE."
                )
            if chat is not None and chat not in all_chats:
                raise ConfigurationError(f"Chat {chat} is not selected for archiving")
            selected = {chat: all_chats[chat]} if chat is not None else all_chats

            attempted, repaired = await archive.retry_candidates(client, selected)
            if attempted:
                logger.info(
                    "Incomplete media repair: %s attempted, %s downloaded", attempted, repaired
                )
            result = await sync_history(
                client,
                selected,
                archive,
                repository,
                limit=limit,
                since=since_date,
                until=until_date,
                concurrency=effective.download_concurrency,
                content_types=selected_types,
            )
            console.print(
                f"[green]Sync complete:[/green] {result.messages} messages processed, "
                f"{result.downloads + repaired} files downloaded across {result.chats} chats."
            )
        finally:
            await client.disconnect()
            await database.close()

    _run(command())


@app.command("listen")
def listen_command(
    content_types: str | None = typer.Option(
        None,
        "--types",
        help="Comma-separated Telegram content types to archive. Defaults to all.",
    ),
) -> None:
    """Monitor configured chats and archive new messages and edits."""

    async def command() -> None:
        settings = _settings()
        selected_types = _parse_content_types(content_types)
        database, repository, archive, effective = await _archive_stack(settings, selected_types)
        client = create_client(effective)
        try:
            await connect_authorized(client)
            chats = await ChatSelectionService(effective, repository).resolve_with_client(client)
            if not chats:
                raise ConfigurationError(
                    "No chats are selected. Choose chats in the web dashboard or TUI, "
                    "or configure TARGET_CHATS/CONFIG_FILE."
                )
            listener = RealtimeListener(client, chats, archive, effective)
            # Install update handlers before repair so messages arriving during
            # startup recovery are still accepted.
            listener.install_handlers()
            repair_task = asyncio.create_task(
                archive.retry_candidates(client, chats, stop_event=listener.stop_event)
            )
            stopping = asyncio.create_task(listener.stop_event.wait())
            done, _pending = await asyncio.wait(
                {repair_task, stopping}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopping in done and not repair_task.done():
                repair_task.cancel()
            else:
                stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
            repair_result = await asyncio.gather(repair_task, return_exceptions=True)
            attempted, repaired = (0, 0)
            if repair_result:
                outcome = repair_result[0]
                if isinstance(outcome, asyncio.CancelledError):
                    if not listener.stop_event.is_set():
                        raise outcome
                elif isinstance(outcome, BaseException):
                    raise outcome
                else:
                    attempted, repaired = outcome
            if attempted:
                logger.info(
                    "Listener startup repair: %s attempted, %s downloaded", attempted, repaired
                )
            await listener.run()
        finally:
            await client.disconnect()
            await database.close()

    _run(command())


@app.command("web")
def web_command(
    host: str | None = typer.Option(
        None, "--host", help="Bind address. Defaults to WEB_HOST (127.0.0.1)."
    ),
    port: int | None = typer.Option(
        None, "--port", min=1, max=65535, help="TCP port. Defaults to WEB_PORT (8686)."
    ),
) -> None:
    """Run the private archive dashboard and account connection screen."""

    import uvicorn

    from app.interfaces.web.app import create_web_app

    settings = _settings()
    updates: dict[str, object] = {}
    if host is not None:
        updates["web_host"] = host
    if port is not None:
        updates["web_port"] = port
    if updates:
        settings = settings.model_copy(update=updates)
    try:
        effective = asyncio.run(_effective_web_settings(settings))
        web_app = create_web_app(settings)
    except (ConfigurationError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    auth_note = (
        " with Telegram browser auth"
        if settings.web_session_secret and settings.web_session_secret.get_secret_value()
        else " on loopback only"
    )
    logger.info(
        "Starting web dashboard at http://%s:%s%s",
        settings.web_host,
        settings.web_port,
        auth_note,
    )
    uvicorn.run(
        web_app,
        host=settings.web_host,
        port=settings.web_port,
        log_level=effective.log_level.casefold(),
        access_log=True,
    )


@app.command("tui")
def tui_command() -> None:
    """Open the keyboard-first terminal dashboard."""

    from app.interfaces.tui import ArchiveTui

    settings = _settings()
    ArchiveTui(settings).run()


@app.command("stats")
def stats_command() -> None:
    """Show archive counts, byte totals, failures, and per-chat freshness."""

    async def command() -> None:
        settings = _settings()
        database = Database(settings.database_url)
        repository = ArchiveRepository(database)
        try:
            await database.initialize()
            stats = await repository.stats()
            selected_ids = await ChatSelectionRepository(database).effective_known_ids(
                settings.configured_chat_ids
            )
            summary = Table(title="Archive statistics", show_header=False)
            summary.add_column("Metric")
            summary.add_column("Value", justify="right")
            summary.add_row("Selected chats", str(len(selected_ids)))
            summary.add_row("Archived messages", str(stats.total_messages))
            summary.add_row("Downloaded files", str(stats.downloaded_files))
            summary.add_row("Downloaded bytes", format_bytes(stats.downloaded_bytes))
            summary.add_row("Failed downloads", str(stats.failed_downloads))
            summary.add_row("Skipped downloads", str(stats.skipped_downloads))
            console.print(summary)

            newest = Table(title="Newest archived message per known chat")
            newest.add_column("CHAT ID", justify="right")
            newest.add_column("TITLE")
            newest.add_column("MESSAGE ID", justify="right")
            newest.add_column("DATE")
            for row in stats.newest_by_chat:
                newest.add_row(
                    str(row.telegram_chat_id),
                    row.title,
                    str(row.message_id or ""),
                    row.message_date.isoformat() if row.message_date else "",
                )
            console.print(newest)
        finally:
            await database.close()

    _run(command())


@app.command("retry-failed")
def retry_failed_command(
    content_types: str | None = typer.Option(
        None,
        "--types",
        help="Retry only these comma-separated Telegram media types. Defaults to all.",
    ),
) -> None:
    """Retry media downloads currently marked failed."""

    async def command() -> None:
        settings = _settings()
        selected_types = _parse_content_types(content_types)
        database, repository, archive, effective = await _archive_stack(settings, selected_types)
        client = create_client(effective)
        try:
            await connect_authorized(client)
            chats = await ChatSelectionService(effective, repository).resolve_with_client(client)
            if not chats:
                raise ConfigurationError(
                    "No chats are selected. Choose chats in the web dashboard or TUI, "
                    "or configure TARGET_CHATS/CONFIG_FILE."
                )
            attempted, completed = await archive.retry_candidates(client, chats, failed_only=True)
            console.print(
                f"[green]Retry complete:[/green] {attempted} attempted, {completed} downloaded."
            )
        finally:
            await client.disconnect()
            await database.close()

    _run(command())


@app.command("doctor")
def doctor_command() -> None:
    """Validate credentials, storage, database, session, and chat access."""

    async def command() -> None:
        settings = _settings()
        checks: list[tuple[str, str, str]] = []
        failed = False

        try:
            settings.require_telegram_credentials()
            checks.append(("Environment", "PASS", "TG_API_ID and TG_API_HASH are set"))
        except ConfigurationError as exc:
            failed = True
            checks.append(("Environment", "FAIL", str(exc)))

        database: Database | None = None
        repository: ArchiveRepository | None = None
        try:
            database = Database(settings.database_url)
            await database.initialize()
            await database.healthcheck()
            repository = ArchiveRepository(database)
            checks.append(("Database", "PASS", settings.database_url))
        except Exception as exc:
            failed = True
            checks.append(("Database", "FAIL", f"{type(exc).__name__}: {exc}"))

        try:
            settings.download_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=settings.download_dir, prefix=".doctor-", delete=True
            ):
                pass
            checks.append(("Downloads", "PASS", str(settings.download_dir)))
        except OSError as exc:
            failed = True
            checks.append(("Downloads", "FAIL", f"{type(exc).__name__}: {exc}"))

        if settings.tg_api_id and settings.tg_api_hash:
            client = None
            try:
                client = create_client(settings)
                await connect_authorized(client)
                checks.append(("Authentication", "PASS", "Telethon session is authorized"))
                if repository:
                    service = ChatSelectionService(settings, repository)
                    chats = await service.resolve_with_client(client)
                    policy = await service.selections.policy()
                    if chats:
                        checks.append(
                            (
                                "Chat access",
                                "PASS",
                                f"{len(chats)} selected chats accessible ({policy.mode} mode)",
                            )
                        )
                    else:
                        checks.append(
                            (
                                "Chat access",
                                "WARN",
                                f"No chats selected ({policy.mode} mode)",
                            )
                        )
                else:
                    checks.append(("Chat access", "WARN", "Database unavailable"))
            except Exception as exc:
                failed = True
                checks.append(("Telegram", "FAIL", str(exc)))
            finally:
                if client:
                    await client.disconnect()

        if database:
            await database.close()

        table = Table(title="Telegram Archiver doctor")
        table.add_column("CHECK")
        table.add_column("STATUS")
        table.add_column("DETAIL")
        colors = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
        for name, status, detail in checks:
            table.add_row(name, f"[{colors[status]}]{status}[/{colors[status]}]", detail)
        console.print(table)
        if failed:
            raise typer.Exit(code=1)

    _run(command())
