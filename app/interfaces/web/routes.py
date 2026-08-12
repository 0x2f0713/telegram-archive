"""HTML pages, JSON endpoints, and guarded media delivery."""

from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from telethon.errors import RPCError

from app.application.chat_selection import ChatDiscovery, ChatSelectionService
from app.application.operations import (
    OPERATION_COMMANDS,
    OperationConflictError,
    OperationManager,
    OperationNotFoundError,
)
from app.application.runtime_settings import load_runtime_settings
from app.config import (
    RUNTIME_OVERRIDE_FIELDS,
    ConfigurationError,
    Settings,
    apply_runtime_overrides,
    decode_overrides,
    encode_overrides,
    merge_runtime_form_values,
    runtime_form_values,
)
from app.domain.content import (
    ALL_CONTENT_TYPES,
    CONTENT_TYPE_OPTIONS,
    ContentTypeSelectionError,
    canonical_content_type_list,
    normalize_content_types,
)
from app.infrastructure.persistence.read_models import (
    DashboardRepository,
    DashboardService,
    MessageQuery,
)
from app.infrastructure.persistence.selection import ChatSelectionRepository
from app.infrastructure.persistence.settings import RuntimeSettingsRepository
from app.infrastructure.telegram.client import TelegramAccessError
from app.infrastructure.telegram.session_account import read_session_account_id
from app.interfaces.web.auth import TelegramQrAuthManager
from app.interfaces.web.session import TelegramWebSession
from app.interfaces.web.system import inspect_storage
from app.utils.logging import format_bytes

TEMPLATES_ROOT = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_ROOT)
INLINE_IMAGE_TYPES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})
logger = logging.getLogger(__name__)


def _format_datetime(value: datetime | str | None) -> str:
    if value is None:
        return "Never"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


templates.env.filters["bytes"] = lambda value: format_bytes(int(value or 0))
templates.env.filters["datetime"] = _format_datetime


def _day_label(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone()
    today = datetime.now().astimezone().date()
    if local.date() == today:
        return "Today"
    if local.date() == today - timedelta(days=1):
        return "Yesterday"
    return local.strftime("%A, %d %B %Y")


templates.env.filters["day_label"] = _day_label


def _navigation_context(request: Request, section: str) -> dict[str, object]:
    return {
        "request": request,
        "section": section,
        "generated_at": datetime.now(UTC),
        "csrf_token": request.app.state.csrf_token,
    }


def _safe_next_path(value: str | None) -> str:
    """Keep post-login redirects on this origin."""

    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _browser_authenticated(request: Request) -> bool:
    session: TelegramWebSession | None = getattr(request.app.state, "web_session", None)
    account_id = getattr(request.app.state, "account_user_id", None)
    return bool(
        session
        and session.valid(request.cookies.get(session.cookie_name), account_id)
    )


def _set_browser_cookie(request: Request, response: RedirectResponse, account_id: int) -> None:
    session: TelegramWebSession | None = getattr(request.app.state, "web_session", None)
    if session is None:
        return
    response.set_cookie(
        session.cookie_name,
        session.issue(account_id),
        max_age=session.max_age,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def _query_values(query: MessageQuery, *, include_page: bool = True) -> dict[str, str | int]:
    values: dict[str, str | int] = {"page_size": query.page_size}
    if include_page:
        values["page"] = query.page
    if query.search:
        values["q"] = query.search
    if query.chat_id is not None:
        values["chat"] = query.chat_id
    if query.status:
        values["status"] = query.status
    if query.media_type:
        values["media_type"] = query.media_type
    if query.media_only:
        values["media_only"] = "true"
    if query.since:
        values["since"] = query.since.isoformat()
    if query.until:
        values["until"] = query.until.isoformat()
    if query.sort != "newest":
        values["sort"] = query.sort
    return values


def _page_url(query: MessageQuery, page: int) -> str:
    values = _query_values(replace(query, page=page))
    return f"/messages?{urlencode(values)}"


def _export_url(query: MessageQuery) -> str:
    return f"/exports/messages.csv?{urlencode(_query_values(query, include_page=False))}"


def _message_query(
    *,
    q: str,
    chat: int | None,
    status: str,
    media_type: str,
    media_only: bool,
    since: date | None,
    until: date | None,
    sort: str,
    page: int,
    page_size: int,
) -> MessageQuery:
    if since and until and since > until:
        raise HTTPException(status_code=400, detail="Since date must not be after until date")
    return MessageQuery(
        search=q,
        chat_id=chat,
        status=status,
        media_type=media_type,
        media_only=media_only,
        since=since,
        until=until,
        sort=sort,
        page=page,
        page_size=page_size,
    ).normalized()


def _chart_points(values: tuple[int, ...], width: int = 1000, height: int = 220) -> str:
    if not values:
        return ""
    highest = max(values) or 1
    last_index = max(1, len(values) - 1)
    return " ".join(
        f"{round(index / last_index * width, 2)},{round(height - value / highest * height, 2)}"
        for index, value in enumerate(values)
    )


def _safe_csv_value(value: object | None) -> str:
    if isinstance(value, int | float):
        return str(value)
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def _resolved_media_paths(download_dir: Path, media_path: str) -> tuple[Path, Path]:
    return download_dir.expanduser().resolve(), Path(media_path).expanduser().resolve()


def _preview_kind(mime_type: str | None) -> str:
    normalized = (mime_type or "").casefold()
    if normalized in INLINE_IMAGE_TYPES:
        return "image"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "audio"
    return "file"


templates.env.globals["preview_kind"] = _preview_kind


def _validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "value"
        message = first.get("msg", "invalid value")
        value = first.get("input")
        suffix = f" (got {value!r})" if value is not None else ""
        return f"{field}: {message}{suffix}"
    return str(exc)


async def _system_context(
    request: Request, *, error: str | None = None, submitted: dict[str, str] | None = None
) -> dict[str, object]:
    """Shared context for the System page, based on effective settings."""
    resolution = await load_runtime_settings(
        request.app.state.base_settings, request.app.state.database
    )
    effective: Settings = resolution.settings
    repository: DashboardRepository = request.app.state.dashboard
    selection_repository: ChatSelectionRepository = request.app.state.chat_selections
    session_path = effective.tg_session_name.expanduser()
    if session_path.suffix != ".session":
        session_path = session_path.with_suffix(".session")
    completed_paths = await repository.completed_media_paths()
    selected_ids = await selection_repository.effective_known_ids(effective.configured_chat_ids)
    storage = await asyncio.to_thread(inspect_storage, effective, completed_paths)
    form_settings = merge_runtime_form_values(effective, submitted or {})
    context = _navigation_context(request, "system")
    context.update(
        {
            "form_settings": form_settings,
            "overridden_keys": set(resolution.valid_overrides),
            "safe_settings": {
                "Database": effective.database_url,
                "Download directory": str(effective.download_dir),
                "Selected chats": len(selected_ids),
            },
            "session_exists": await asyncio.to_thread(session_path.is_file),
            "web_auth_enabled": bool(
                effective.web_session_secret and effective.web_session_secret.get_secret_value()
            ),
            "loopback_only": _loopback_host(effective.web_host),
            "storage": storage,
            "settings_error": error,
        }
    )
    return context


def _loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _focus_operation_action(
    operation: dict[str, object], active_operation: dict[str, object] | None
) -> dict[str, object]:
    """Disable recovery actions while a different operation owns the worker."""
    action = dict(operation.get("action") or {})
    if (
        active_operation
        and active_operation.get("id") != operation.get("id")
        and action.get("kind") in {"resume", "retry"}
    ):
        action["enabled"] = False
    operation["action"] = action
    return operation


async def _form_values(
    request: Request, *, max_bytes: int = 2048, max_fields: int = 100
) -> dict[str, list[str]]:
    """Read a bounded URL-encoded form without adding multipart dependencies."""

    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Expected a form submission")
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail="Form submission is too large")
    try:
        return parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=max_fields,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid form submission") from exc


def _require_csrf(request: Request, values: dict[str, list[str]]) -> None:
    submitted = values.get("csrf_token", [""])[0]
    if not secrets.compare_digest(submitted, request.app.state.csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _chat_discovery_error(exc: Exception) -> str:
    if isinstance(exc, (ConfigurationError, TelegramAccessError)):
        return str(exc)
    if isinstance(exc, RPCError):
        return (
            "Telegram could not refresh the dialog list. Try again after the connection recovers."
        )
    if isinstance(exc, (OSError, TimeoutError)):
        return "Telegram is temporarily unreachable. Cached chat records are shown below."
    logger.exception("Unexpected Telegram dialog discovery failure")
    return "Chat discovery failed unexpectedly. Check the application log and try again."


def create_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def dashboard_page(
        request: Request,
        days: Literal[14, 30, 90] = 30,
    ) -> HTMLResponse:
        service: DashboardService = request.app.state.dashboard_service
        overview = await service.overview(days)
        activity_values = tuple(point.count for point in overview.activity)
        status_lookup = dict(overview.status_counts)
        configured = len(overview.configured_chat_ids)
        known_configured = sum(
            chat.telegram_chat_id in overview.configured_chat_ids for chat in overview.chats
        )
        if not configured:
            next_action = (
                "Choose archive targets",
                "Open the Chats page or use discovery with environment defaults.",
                "/chats",
                "Choose chats",
            )
        elif not overview.stats.total_messages:
            next_action = (
                "Run the first archive pass",
                "Fetch existing history before starting the live listener.",
                "/operations",
                "Open operations",
            )
        elif overview.stats.failed_downloads:
            next_action = (
                "Repair failed media",
                "Retry the files that could not complete during an earlier pass.",
                "/operations",
                "Open operations",
            )
        else:
            next_action = (
                "Keep the archive live",
                "Start the listener to store new messages and edits as they arrive.",
                "/operations",
                "Open operations",
            )
        context = _navigation_context(request, "overview")
        context.update(
            {
                "overview": overview,
                "days": days,
                "activity_points": _chart_points(activity_values),
                "activity_total": sum(activity_values),
                "activity_peak": max(activity_values, default=0),
                "configured_coverage": round(known_configured / configured * 100)
                if configured
                else 0,
                "pending_count": status_lookup.get("pending", 0)
                + status_lookup.get("downloading", 0),
                "media_total": sum(count for _, count in overview.media_counts),
                "next_action": next_action,
            }
        )
        return templates.TemplateResponse(request, "dashboard.html", context)

    @router.get("/messages", response_class=HTMLResponse)
    async def messages_page(
        request: Request,
        q: str = Query(default="", max_length=200),
        chat: int | None = None,
        status: str = Query(default="", max_length=32),
        media_type: str = Query(default="", max_length=32),
        media_only: bool = False,
        since: date | None = None,
        until: date | None = None,
        sort: str = Query(default="newest", max_length=32),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=30, ge=10, le=100),
    ) -> HTMLResponse:
        repository: DashboardRepository = request.app.state.dashboard
        query = _message_query(
            q=q,
            chat=chat,
            status=status,
            media_type=media_type,
            media_only=media_only,
            since=since,
            until=until,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        messages = await repository.messages(query)
        chats = await repository.chat_summaries()
        context = _navigation_context(request, "messages")
        context.update(
            {
                "messages": messages,
                "query": query,
                "chats": chats,
                "status_options": await repository.status_counts(),
                "media_options": await repository.media_counts(),
                "previous_url": _page_url(query, page - 1) if page > 1 else None,
                "next_url": _page_url(query, page + 1) if page < messages.pages else None,
                "export_url": _export_url(query),
            }
        )
        return templates.TemplateResponse(request, "messages.html", context)

    @router.get("/messages/{message_id}", response_class=HTMLResponse)
    async def message_detail(request: Request, message_id: int) -> HTMLResponse:
        repository: DashboardRepository = request.app.state.dashboard
        message = await repository.message(message_id)
        if message is None:
            raise HTTPException(status_code=404, detail="Message not found")
        album = (
            await repository.album_messages(message.telegram_chat_id, message.grouped_id)
            if message.grouped_id is not None
            else ()
        )
        context = _navigation_context(request, "messages")
        context.update(
            {
                "message": message,
                "preview_kind": _preview_kind(message.mime_type),
                "album": album,
            }
        )
        return templates.TemplateResponse(request, "message_detail.html", context)

    @router.get("/chats", response_class=HTMLResponse)
    async def chats_page(
        request: Request, saved: bool = False, refresh: bool = False
    ) -> HTMLResponse:
        repository: DashboardRepository = request.app.state.dashboard
        selection_repository: ChatSelectionRepository = request.app.state.chat_selections
        selection_service: ChatSelectionService = request.app.state.chat_selection_service
        discovery: ChatDiscovery | None = None
        discovery_error: str | None = None
        chats = await repository.chat_summaries()
        if refresh or not chats:
            try:
                discovery = await selection_service.discover()
                chats = await repository.chat_summaries()
            except Exception as exc:
                discovery_error = _chat_discovery_error(exc)
        policy = discovery.policy if discovery else await selection_repository.policy()
        if discovery:
            accessible_ids = {dialog.telegram_chat_id for dialog in discovery.dialogs}
            chats = tuple(chat for chat in chats if chat.telegram_chat_id in accessible_ids)
            effective_ids = set(discovery.effective_chat_ids)
        else:
            accessible_ids = set()
            effective_ids = set(
                await selection_repository.effective_known_ids(settings.configured_chat_ids)
            )
        context = _navigation_context(request, "chats")
        context.update(
            {
                "chats": chats,
                "selection": policy,
                "configured_chat_ids": effective_ids,
                "specific_chat_ids": set(policy.selected_chat_ids),
                "environment_chat_ids": set(settings.configured_chat_ids),
                "accessible_chat_ids": accessible_ids,
                "discovery_error": discovery_error,
                "selection_saved": saved,
                "dialogs_refreshed": discovery is not None,
            }
        )
        return templates.TemplateResponse(request, "chats.html", context)

    @router.get("/chats/{telegram_chat_id}", response_class=HTMLResponse)
    async def conversation_page(
        request: Request,
        telegram_chat_id: int,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=10, le=100),
    ) -> HTMLResponse:
        repository: DashboardRepository = request.app.state.dashboard
        chat = await repository.chat_summary(telegram_chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        thread = await repository.chat_thread(telegram_chat_id, page=page, page_size=page_size)
        reply_ids = tuple(
            message.reply_to_message_id
            for message in thread.items
            if message.reply_to_message_id is not None
        )
        reply_targets = (
            await repository.resolve_reply_targets(telegram_chat_id, reply_ids) if reply_ids else {}
        )
        context = _navigation_context(request, "chats")
        context.update(
            {
                "chat": chat,
                "messages": thread.items,
                "has_older": page * page_size < thread.total,
                "older_url": f"/chats/{telegram_chat_id}?page={page + 1}",
                "reply_targets": reply_targets,
                "account_user_id": request.app.state.account_user_id,
            }
        )
        return templates.TemplateResponse(request, "conversation.html", context)

    @router.post("/chats/selection")
    async def save_chat_selection(request: Request) -> RedirectResponse:
        values = await _form_values(request, max_bytes=1_000_000, max_fields=20_000)
        _require_csrf(request, values)
        mode = values.get("mode", [""])[0].strip().casefold()
        if mode not in {"specific", "all", "environment"}:
            raise HTTPException(status_code=400, detail="Invalid chat selection mode")

        selection_service: ChatSelectionService = request.app.state.chat_selection_service
        selection_repository: ChatSelectionRepository = request.app.state.chat_selections
        try:
            discovery = await selection_service.discover()
            if mode == "specific":
                raw_ids = values.get("chat_id", [])
                try:
                    selected_ids = tuple(int(value) for value in raw_ids)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="Invalid Telegram chat ID") from exc
                selected_ids = selection_service.validate_specific(discovery, selected_ids)
                await selection_repository.set_specific(selected_ids)
            elif mode == "all":
                await selection_repository.set_all()
            else:
                selection_service.validate_specific(discovery, settings.configured_chat_ids)
                await selection_repository.use_environment()
        except HTTPException:
            raise
        except (ConfigurationError, TelegramAccessError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (RPCError, OSError, TimeoutError) as exc:
            raise HTTPException(
                status_code=503,
                detail="Telegram must be reachable before chat selection can be changed",
            ) from exc
        return RedirectResponse("/chats?saved=true", status_code=303)

    @router.get("/system", response_class=HTMLResponse)
    async def system_page(
        request: Request, saved: bool = False, reset: bool = False
    ) -> HTMLResponse:
        context = await _system_context(request)
        context.update({"settings_saved": saved, "settings_reset": reset})
        return templates.TemplateResponse(request, "system.html", context)

    @router.post("/system/settings")
    async def save_system_settings(request: Request):
        values = await _form_values(request, max_bytes=8192, max_fields=40)
        _require_csrf(request, values)

        submitted = runtime_form_values(values)
        base_canonical = encode_overrides(request.app.state.base_settings)
        overrides = {
            key: submitted[key]
            for key in RUNTIME_OVERRIDE_FIELDS
            if key in submitted and base_canonical.get(key) != submitted[key]
        }
        try:
            merged = decode_overrides(overrides)
            Settings.model_validate({**request.app.state.settings.model_dump(), **merged})
        except (ValidationError, ValueError) as exc:
            context = await _system_context(
                request, error=_validation_message(exc), submitted=submitted
            )
            return templates.TemplateResponse(request, "system.html", context, status_code=422)
        await RuntimeSettingsRepository(request.app.state.database).replace_values(overrides)
        request.app.state.settings = apply_runtime_overrides(
            request.app.state.base_settings, overrides
        )
        return RedirectResponse("/system?saved=true", status_code=303)

    @router.post("/system/settings/reset")
    async def reset_system_settings(request: Request) -> RedirectResponse:
        values = await _form_values(request, max_bytes=2048, max_fields=10)
        _require_csrf(request, values)
        await RuntimeSettingsRepository(request.app.state.database).clear()
        request.app.state.settings = apply_runtime_overrides(request.app.state.base_settings, {})
        return RedirectResponse("/system?reset=true", status_code=303)

    @router.get("/operations", response_class=HTMLResponse)
    async def operations_page(
        request: Request,
        job: int | None = Query(default=None, ge=1),
        started: bool = False,
        stopped: bool = False,
        error: str | None = Query(default=None, max_length=500),
    ) -> HTMLResponse:
        manager: OperationManager = request.app.state.operations
        repository: DashboardRepository = request.app.state.dashboard
        selection_repository: ChatSelectionRepository = request.app.state.chat_selections
        recent = await manager.recent(20)
        active = await manager.active()
        selected_ids = set(
            await selection_repository.effective_known_ids(settings.configured_chat_ids)
        )
        chats = tuple(
            chat
            for chat in await repository.chat_summaries()
            if chat.telegram_chat_id in selected_ids
        )
        focus: dict[str, object] | None = None
        if job is not None:
            try:
                focus = await manager.get(job)
            except OperationNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        elif active:
            focus = active
        elif recent:
            focus = recent[0]
        if focus:
            focus = _focus_operation_action(focus, active)
        logs = await manager.logs(int(focus["id"])) if focus else ()
        context = _navigation_context(request, "operations")
        context.update(
            {
                "active_operation": active,
                "focus_operation": focus,
                "operation_logs": logs,
                "recent_operations": recent,
                "selected_chats": chats,
                "content_type_options": CONTENT_TYPE_OPTIONS,
                "operation_started": started,
                "operation_stopped": stopped,
                "operation_error_message": error,
            }
        )
        return templates.TemplateResponse(request, "operations.html", context)

    @router.post("/operations/start")
    async def start_operation(request: Request) -> RedirectResponse:
        values = await _form_values(request, max_bytes=8192, max_fields=30)
        _require_csrf(request, values)
        command = values.get("command", [""])[0].strip().casefold()
        if command not in OPERATION_COMMANDS:
            raise HTTPException(status_code=400, detail="Unsupported operation command")
        parameters: dict[str, object] = {}
        if command in {"sync", "listen", "retry-failed"} and values.get("content_types_present"):
            try:
                selected_types = normalize_content_types(values.get("content_type", []))
            except ContentTypeSelectionError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if selected_types != ALL_CONTENT_TYPES:
                parameters["content_types"] = canonical_content_type_list(selected_types)
        if command == "sync":
            raw_chat = values.get("chat", [""])[0].strip()
            raw_limit = values.get("limit", [""])[0].strip()
            since = values.get("since", [""])[0].strip()
            until = values.get("until", [""])[0].strip()
            if raw_chat:
                try:
                    parameters["chat"] = int(raw_chat)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="Invalid Telegram chat ID") from exc
            if raw_limit:
                try:
                    limit = int(raw_limit)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="Invalid message limit") from exc
                if not 1 <= limit <= 1_000_000:
                    raise HTTPException(
                        status_code=400,
                        detail="Message limit must be between 1 and 1,000,000",
                    )
                parameters["limit"] = limit
            parsed_dates: dict[str, date] = {}
            for name, raw_value in (("since", since), ("until", until)):
                if raw_value:
                    try:
                        parsed_dates[name] = date.fromisoformat(raw_value)
                    except ValueError as exc:
                        raise HTTPException(
                            status_code=400,
                            detail=f"{name.capitalize()} date must use YYYY-MM-DD",
                        ) from exc
                    parameters[name] = raw_value
            if parsed_dates.get("since") and parsed_dates.get("until"):
                if parsed_dates["since"] > parsed_dates["until"]:
                    raise HTTPException(
                        status_code=400,
                        detail="Since date must not be after until date",
                    )
        manager: OperationManager = request.app.state.operations
        try:
            operation = await manager.start_job(command, parameters)
        except OperationConflictError as exc:
            # A normal browser submission should return to the Operations page
            # with an actionable explanation instead of an opaque JSON/HTML 409.
            return RedirectResponse(
                f"/operations?{urlencode({'error': str(exc)})}",
                status_code=303,
            )
        return RedirectResponse(
            f"/operations?job={operation['id']}&started=true",
            status_code=303,
        )

    @router.post("/operations/{job_id}/stop")
    async def stop_operation(request: Request, job_id: int) -> RedirectResponse:
        values = await _form_values(request)
        _require_csrf(request, values)
        manager: OperationManager = request.app.state.operations
        try:
            await manager.request_stop(job_id)
        except OperationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return RedirectResponse(f"/operations?job={job_id}&stopped=true", status_code=303)

    @router.post("/operations/{job_id}/resume")
    async def resume_sync_operation(request: Request, job_id: int) -> RedirectResponse:
        """Resume a previous sync with its original safe, validated parameters."""
        values = await _form_values(request)
        _require_csrf(request, values)
        manager: OperationManager = request.app.state.operations
        try:
            previous = await manager.get(job_id)
        except OperationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if previous["action"]["kind"] != "resume":
            raise HTTPException(
                status_code=400, detail="Only historical sync operations can resume"
            )
        if previous["active"]:
            raise HTTPException(status_code=400, detail="This sync operation is already active")
        try:
            operation = await manager.resume_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OperationConflictError as exc:
            return RedirectResponse(
                f"/operations?job={job_id}&{urlencode({'error': str(exc)})}",
                status_code=303,
            )
        return RedirectResponse(
            f"/operations?job={operation['id']}&started=true",
            status_code=303,
        )

    @router.post("/operations/{job_id}/retry")
    async def retry_operation(request: Request, job_id: int) -> RedirectResponse:
        """Retry a failed, cancelled, or interrupted non-sync operation."""
        values = await _form_values(request)
        _require_csrf(request, values)
        manager: OperationManager = request.app.state.operations
        try:
            previous = await manager.get(job_id)
        except OperationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if previous["action"]["kind"] != "retry":
            raise HTTPException(
                status_code=400, detail="Only unfinished non-sync operations can retry"
            )
        try:
            operation = await manager.retry_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OperationConflictError as exc:
            return RedirectResponse(
                f"/operations?job={job_id}&{urlencode({'error': str(exc)})}",
                status_code=303,
            )
        return RedirectResponse(
            f"/operations?job={operation['id']}&started=true",
            status_code=303,
        )

    @router.get("/auth/telegram", response_class=HTMLResponse)
    async def telegram_auth_page(request: Request) -> HTMLResponse:
        manager: TelegramQrAuthManager = request.app.state.telegram_auth
        snapshot = await manager.inspect_session()
        next_path = _safe_next_path(request.query_params.get("next"))
        context = _navigation_context(request, "account")
        context.update(
            {
                "auth": snapshot,
                "auth_status": snapshot.status.value,
                "browser_authenticated": _browser_authenticated(request),
                "next_path": next_path,
                "auth_error": request.query_params.get("error"),
            }
        )
        return templates.TemplateResponse(request, "telegram_auth.html", context)

    @router.post("/auth/telegram/start", response_class=HTMLResponse)
    async def start_telegram_auth(request: Request) -> RedirectResponse:
        values = await _form_values(request)
        _require_csrf(request, values)
        manager: TelegramQrAuthManager = request.app.state.telegram_auth
        await manager.start()
        next_path = _safe_next_path(values.get("next", [""])[0])
        return RedirectResponse(
            f"/auth/telegram?{urlencode({'next': next_path})}",
            status_code=303,
        )

    @router.post("/auth/telegram/continue", response_class=HTMLResponse)
    async def continue_telegram_auth(request: Request) -> RedirectResponse:
        values = await _form_values(request)
        _require_csrf(request, values)
        manager: TelegramQrAuthManager = request.app.state.telegram_auth
        snapshot = await manager.inspect_session()
        account_id = await asyncio.to_thread(
            read_session_account_id,
            request.app.state.settings.tg_session_name,
        )
        if snapshot.status.value != "connected" or account_id is None:
            return RedirectResponse(
                f"/auth/telegram?{urlencode({'error': 'Telegram account is not connected yet'})}",
                status_code=303,
            )
        request.app.state.account_user_id = account_id
        next_path = _safe_next_path(values.get("next", [""])[0])
        response = RedirectResponse(next_path, status_code=303)
        _set_browser_cookie(request, response, account_id)
        return response

    @router.post("/auth/telegram/logout", response_class=HTMLResponse)
    async def logout_telegram_auth(request: Request) -> RedirectResponse:
        values = await _form_values(request)
        _require_csrf(request, values)
        response = RedirectResponse("/auth/telegram", status_code=303)
        session: TelegramWebSession | None = getattr(request.app.state, "web_session", None)
        if session is not None:
            response.delete_cookie(session.cookie_name, path="/")
        return response

    @router.get("/auth/telegram/qr.svg", include_in_schema=False)
    async def telegram_qr_image(request: Request) -> Response:
        manager: TelegramQrAuthManager = request.app.state.telegram_auth
        image = await manager.qr_svg()
        if image is None:
            raise HTTPException(status_code=404, detail="No active QR authorization")
        return Response(
            image,
            media_type="image/svg+xml",
            headers={"Content-Disposition": 'inline; filename="telegram-login.svg"'},
        )

    @router.get("/api/v1/auth/telegram")
    async def telegram_auth_status(request: Request) -> dict[str, str | None]:
        manager: TelegramQrAuthManager = request.app.state.telegram_auth
        return manager.snapshot.public_dict()

    @router.get("/login", include_in_schema=False)
    @router.get("/register", include_in_schema=False)
    async def telegram_auth_alias() -> RedirectResponse:
        return RedirectResponse("/auth/telegram", status_code=307)

    @router.get("/media/{message_id}")
    async def media_file(request: Request, message_id: int) -> FileResponse:
        repository: DashboardRepository = request.app.state.dashboard
        message = await repository.message(message_id)
        if message is None or message.download_status != "completed" or not message.media_path:
            raise HTTPException(status_code=404, detail="Completed media not found")
        download_root, media_path = await asyncio.to_thread(
            _resolved_media_paths, settings.download_dir, message.media_path
        )
        if download_root != media_path and download_root not in media_path.parents:
            raise HTTPException(status_code=403, detail="Media path is outside DOWNLOAD_DIR")
        if not await asyncio.to_thread(media_path.is_file):
            raise HTTPException(status_code=404, detail="Media file is missing")
        return FileResponse(
            media_path,
            media_type=message.mime_type or "application/octet-stream",
            filename=media_path.name,
            content_disposition_type=(
                "inline" if _preview_kind(message.mime_type) != "file" else "attachment"
            ),
        )

    @router.get("/healthz")
    async def health(request: Request) -> dict[str, str]:
        await request.app.state.database.healthcheck()
        return {"status": "ok"}

    @router.get("/api/v1/stats")
    async def api_stats(
        request: Request,
        days: int = Query(default=30, ge=1, le=90),
    ) -> dict[str, object]:
        service: DashboardService = request.app.state.dashboard_service
        overview = await service.overview(days)
        return asdict(overview)

    @router.get("/api/v1/operations")
    async def api_operations(request: Request) -> dict[str, object]:
        manager: OperationManager = request.app.state.operations
        return {
            "active": await manager.active(),
            "operations": await manager.recent(20),
        }

    @router.get("/api/v1/operations/{job_id}")
    async def api_operation(request: Request, job_id: int) -> dict[str, object]:
        manager: OperationManager = request.app.state.operations
        try:
            operation = await manager.get(job_id)
            active = await manager.active()
            return {
                "operation": _focus_operation_action(operation, active),
                "logs": await manager.logs(job_id),
            }
        except OperationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/api/v1/messages")
    async def api_messages(
        request: Request,
        q: str = Query(default="", max_length=200),
        chat: int | None = None,
        status: str = Query(default="", max_length=32),
        media_type: str = Query(default="", max_length=32),
        media_only: bool = False,
        since: date | None = None,
        until: date | None = None,
        sort: str = Query(default="newest", max_length=32),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, object]:
        repository: DashboardRepository = request.app.state.dashboard
        result = await repository.messages(
            _message_query(
                q=q,
                chat=chat,
                status=status,
                media_type=media_type,
                media_only=media_only,
                since=since,
                until=until,
                sort=sort,
                page=page,
                page_size=page_size,
            )
        )
        return asdict(result) | {"pages": result.pages}

    @router.get("/exports/messages.csv")
    async def export_messages(
        request: Request,
        q: str = Query(default="", max_length=200),
        chat: int | None = None,
        status: str = Query(default="", max_length=32),
        media_type: str = Query(default="", max_length=32),
        media_only: bool = False,
        since: date | None = None,
        until: date | None = None,
        sort: str = Query(default="newest", max_length=32),
    ) -> StreamingResponse:
        repository: DashboardRepository = request.app.state.dashboard
        query = _message_query(
            q=q,
            chat=chat,
            status=status,
            media_type=media_type,
            media_only=media_only,
            since=since,
            until=until,
            sort=sort,
            page=1,
            page_size=100,
        )

        async def generate() -> AsyncIterator[str]:
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(
                (
                    "telegram_chat_id",
                    "chat_title",
                    "telegram_message_id",
                    "sender_id",
                    "sender_name",
                    "message_date",
                    "text",
                    "media_type",
                    "mime_type",
                    "filename",
                    "media_size",
                    "download_status",
                    "media_path",
                )
            )
            yield buffer.getvalue()
            page_number = 1
            while True:
                page_result = await repository.messages(replace(query, page=page_number))
                if not page_result.items:
                    break
                buffer.seek(0)
                buffer.truncate(0)
                for message in page_result.items:
                    writer.writerow(
                        tuple(
                            _safe_csv_value(value)
                            for value in (
                                message.telegram_chat_id,
                                message.chat_title,
                                message.telegram_message_id,
                                message.sender_id,
                                message.sender_name,
                                message.message_date.isoformat(),
                                message.text,
                                message.media_type,
                                message.mime_type,
                                message.filename,
                                message.media_size,
                                message.download_status,
                                message.media_path,
                            )
                        )
                    )
                yield buffer.getvalue()
                if page_number >= page_result.pages:
                    break
                page_number += 1

        filename = f"telegram-archive-{datetime.now(UTC):%Y%m%d}.csv"
        return StreamingResponse(
            generate(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/{unknown_path:path}", response_class=HTMLResponse, include_in_schema=False)
    async def not_found(request: Request, unknown_path: str) -> HTMLResponse:
        context = _navigation_context(request, "")
        context["unknown_path"] = unknown_path
        return templates.TemplateResponse(
            request,
            "not_found.html",
            context,
            status_code=404,
        )

    return router
