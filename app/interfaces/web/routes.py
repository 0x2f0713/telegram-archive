"""HTML pages, JSON endpoints, and guarded media delivery."""

from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import BinaryIO, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import ValidationError
from telethon.errors import RPCError

from app.application.archive_deletion import ChatArchiveDeletionService
from app.application.chat_selection import ChatDiscovery, ChatSelectionService
from app.application.dashboard import (
    GALLERY_IMAGE_MIME_TYPES,
    ChatMediaQuery,
    DashboardService,
    MessageQuery,
)
from app.application.media_variants import MediaVariantService
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
from app.infrastructure.persistence.read_models import DashboardRepository
from app.infrastructure.persistence.selection import ChatSelectionRepository
from app.infrastructure.persistence.settings import RuntimeSettingsRepository
from app.infrastructure.telegram.client import TelegramAccessError
from app.interfaces.web.auth_routes import create_auth_router
from app.interfaces.web.forms import form_values as _form_values
from app.interfaces.web.forms import require_csrf as _require_csrf
from app.interfaces.web.overview_routes import create_overview_router
from app.interfaces.web.presentation import navigation_context, templates
from app.interfaces.web.system import inspect_storage

INLINE_IMAGE_TYPES = frozenset(GALLERY_IMAGE_MIME_TYPES)
logger = logging.getLogger(__name__)


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


def _archived_chat_page_url(search: str, page: int) -> str:
    values: dict[str, str | int] = {}
    if search:
        values["q"] = search
    if page > 1:
        values["page"] = page
    return f"/archive/chats?{urlencode(values)}" if values else "/archive/chats"


def _archived_chat_feedback_url(
    search: str,
    page: int,
    **feedback: str | int,
) -> str:
    values: dict[str, str | int] = {}
    if search:
        values["q"] = search
    if page > 1:
        values["page"] = page
    values.update(feedback)
    return f"/archive/chats?{urlencode(values)}"


def _chat_media_page_url(chat_id: int, kind: str, page: int) -> str:
    values: dict[str, str | int] = {}
    if kind != "all":
        values["kind"] = kind
    if page > 1:
        values["page"] = page
    base = f"/chats/{chat_id}/media"
    return f"{base}?{urlencode(values)}" if values else base


def _operation_chat_id(operation: dict[str, object] | None) -> int | None:
    if not operation or operation.get("chat_id") is None:
        return None
    try:
        return int(operation["chat_id"])
    except (TypeError, ValueError):
        return None


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


def _safe_csv_value(value: object | None) -> str:
    if isinstance(value, int | float):
        return str(value)
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def _resolved_media_paths(roots: tuple[Path, ...], media_path: str) -> tuple[Path, Path]:
    resolved = Path(media_path).expanduser().resolve()
    for root in roots:
        if root == resolved or root in resolved.parents:
            return root, resolved
    return roots[0], resolved


async def _completed_media(request: Request, message_id: int) -> tuple[Path, str | None]:
    """Resolve the archived file for a completed media record.

    Media may live in the download directory (local mode, or the TeraBox
    upload buffer) or under the read-only unidisk FUSE mount (TeraBox mode).
    Returns ``(media_path, mime_type)`` or raises HTTP 404/403.
    """
    repository: DashboardRepository = request.app.state.dashboard
    message = await repository.message(message_id)
    if message is None or message.download_status != "completed" or not message.media_path:
        raise HTTPException(status_code=404, detail="Completed media not found")
    roots = await asyncio.to_thread(request.app.state.settings.media_storage_roots)
    download_root, media_path = await asyncio.to_thread(
        _resolved_media_paths, roots, message.media_path
    )
    if download_root != media_path and download_root not in media_path.parents:
        raise HTTPException(status_code=403, detail="Media path is outside DOWNLOAD_DIR")
    if not await asyncio.to_thread(media_path.is_file):
        raise HTTPException(status_code=404, detail="Media file is missing")
    return media_path, message.mime_type


def _preview_kind(mime_type: str | None) -> str:
    normalized = (mime_type or "").casefold()
    if normalized in INLINE_IMAGE_TYPES:
        return "image"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "audio"
    return "file"


def _content_disposition_header(filename: str, mime_type: str | None) -> str:
    """Build a Content-Disposition header that survives non-ASCII filenames.

    ASCII names use the classic ``filename=`` form; anything else uses the
    RFC 5987 ``filename*=UTF-8''...`` form because HTTP header values are
    latin-1 on the wire and raw UTF-8 crashes the server (UnicodeEncodeError).
    """
    disposition = "inline" if _preview_kind(mime_type) != "file" else "attachment"
    try:
        filename.encode("latin-1")
    except UnicodeEncodeError:
        return f"{disposition}; filename*=UTF-8''{quote(filename)}"
    return f'{disposition}; filename="{filename}"'


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
        request.app.state.base_settings, request.app.state.runtime_settings
    )
    effective: Settings = resolution.settings.with_terabox_policy()
    repository: DashboardRepository = request.app.state.dashboard
    selection_repository: ChatSelectionRepository = request.app.state.chat_selections
    session_path = effective.tg_session_name.expanduser()
    if session_path.suffix != ".session":
        session_path = session_path.with_suffix(".session")
    completed_paths = await repository.completed_media_paths()
    selected_ids = await selection_repository.effective_known_ids(effective.configured_chat_ids)
    remote_quota: tuple[int, int] | None = None
    if effective.terabox_enabled:
        terabox_client = getattr(request.app.state, "terabox_client", None)
        if terabox_client is not None:
            try:
                remote_quota = await terabox_client.cached_quota()
            except Exception:
                logger.exception("Could not read TeraBox quota")
    storage = await asyncio.to_thread(inspect_storage, effective, completed_paths, remote_quota)
    form_settings = merge_runtime_form_values(effective, submitted or {})
    safe_settings: dict[str, object] = {
        "Database": effective.database_url,
        "Download directory": str(effective.download_dir),
        "Selected chats": len(selected_ids),
    }
    if effective.terabox_enabled:
        safe_settings["Storage mode"] = "TeraBox (hard drive buffers uploads)"
        safe_settings["TeraBox remote dir"] = effective.terabox_remote_root
        if remote_quota is not None:
            total, used = remote_quota
            safe_settings["TeraBox space"] = f"{used} / {total} bytes used"
    context = navigation_context(request, "system")
    context.update(
        {
            "form_settings": form_settings,
            "overridden_keys": set(resolution.valid_overrides),
            "safe_settings": safe_settings,
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
    router.include_router(create_overview_router())
    router.include_router(create_auth_router())

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
        context = navigation_context(request, "messages")
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
        context = navigation_context(request, "messages")
        context.update(
            {
                "message": message,
                "preview_kind": _preview_kind(message.mime_type),
                "album": album,
                "terabox_enabled": request.app.state.settings.terabox_enabled,
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
        context = navigation_context(request, "chats")
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

    @router.get("/archive/chats", response_class=HTMLResponse)
    async def archived_chats_page(
        request: Request,
        q: str = Query(default="", max_length=100),
        page: int = Query(default=1, ge=1),
        deleted: bool = False,
        deleted_messages: int = Query(default=0, ge=0),
        deleted_files: int = Query(default=0, ge=0),
        cleanup_warnings: int = Query(default=0, ge=0),
        delete_error: Literal["active", "confirmation", "missing"] | None = None,
    ) -> HTMLResponse:
        repository: DashboardRepository = request.app.state.dashboard
        manager: OperationManager = request.app.state.operations
        search = q.strip()
        active_operation = await manager.active()
        active_chat_id = _operation_chat_id(active_operation)
        archived = await repository.archived_chat_summaries(
            search,
            page=page,
            page_size=50,
            include_chat_id=active_chat_id,
        )
        context = navigation_context(request, "archived-chats")
        context.update(
            {
                "archived": archived,
                "search": search,
                "active_chat_id": active_chat_id,
                "active_operation": active_operation,
                "deletion_globally_blocked": bool(
                    active_operation is not None and active_chat_id is None
                ),
                "deletion_success": deleted,
                "deleted_messages": deleted_messages,
                "deleted_files": deleted_files,
                "cleanup_warnings": cleanup_warnings,
                "deletion_error": {
                    "active": (
                        "This chat is currently being archived. Stop or finish that operation "
                        "before deleting its local archive."
                    ),
                    "confirmation": "Type DELETE exactly to confirm archive deletion.",
                    "missing": "That chat no longer has a local archive to delete.",
                }.get(delete_error),
                "previous_url": (
                    _archived_chat_page_url(search, archived.page - 1)
                    if archived.page > 1
                    else None
                ),
                "next_url": (
                    _archived_chat_page_url(search, archived.page + 1)
                    if archived.page < archived.pages
                    else None
                ),
            }
        )
        return templates.TemplateResponse(request, "archived_chats.html", context)

    @router.post("/archive/chats/{telegram_chat_id}/delete")
    async def delete_archived_chat(
        request: Request,
        telegram_chat_id: int,
    ) -> RedirectResponse:
        values = await _form_values(request, max_bytes=4096, max_fields=10)
        _require_csrf(request, values)
        search = values.get("q", [""])[0].strip()[:100]
        try:
            page = max(1, int(values.get("page", ["1"])[0]))
        except ValueError:
            page = 1
        if values.get("confirmation", [""])[0].strip() != "DELETE":
            return RedirectResponse(
                _archived_chat_feedback_url(
                    search,
                    page,
                    delete_error="confirmation",
                ),
                status_code=303,
            )

        manager: OperationManager = request.app.state.operations
        active_operation = await manager.active()
        active_chat_id = _operation_chat_id(active_operation)
        if active_operation and (active_chat_id is None or active_chat_id == telegram_chat_id):
            return RedirectResponse(
                _archived_chat_feedback_url(search, page, delete_error="active"),
                status_code=303,
            )

        service = ChatArchiveDeletionService(request.app.state.archive)
        result = await service.delete(
            telegram_chat_id,
            request.app.state.settings.download_dir,
            remove_remote=getattr(request.app.state, "media_remote_deleter", None),
        )
        if result is None:
            return RedirectResponse(
                _archived_chat_feedback_url(search, page, delete_error="missing"),
                status_code=303,
            )
        return RedirectResponse(
            _archived_chat_feedback_url(
                search,
                page,
                deleted="true",
                deleted_messages=result.messages_deleted,
                deleted_files=result.files_deleted,
                cleanup_warnings=result.files_failed + result.files_skipped,
            ),
            status_code=303,
        )

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
        context = navigation_context(request, "archived-chats")
        context.update(
            {
                "chat": chat,
                "messages": thread.items,
                "has_older": page * page_size < thread.total,
                "older_url": f"/chats/{telegram_chat_id}?page={page + 1}",
                "reply_targets": reply_targets,
                "account_user_id": request.app.state.account_user_id,
                "terabox_enabled": request.app.state.settings.terabox_enabled,
            }
        )
        return templates.TemplateResponse(request, "conversation.html", context)

    @router.get("/chats/{telegram_chat_id}/media", response_class=HTMLResponse)
    async def chat_media_page(
        request: Request,
        telegram_chat_id: int,
        kind: Literal["all", "photos", "videos"] = Query(default="all"),
        page: int = Query(default=1, ge=1),
    ) -> HTMLResponse:
        repository: DashboardRepository = request.app.state.dashboard
        chat = await repository.chat_summary(telegram_chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        media = await repository.chat_media(
            ChatMediaQuery(chat_id=telegram_chat_id, kind=kind, page=page)
        )
        context = navigation_context(request, "archived-chats")
        context.update(
            {
                "chat": chat,
                "media": media,
                "kind": kind,
                "previous_url": (
                    _chat_media_page_url(telegram_chat_id, kind, media.page - 1)
                    if media.page > 1
                    else None
                ),
                "next_url": (
                    _chat_media_page_url(telegram_chat_id, kind, media.page + 1)
                    if media.page < media.pages
                    else None
                ),
            }
        )
        return templates.TemplateResponse(request, "chat_media.html", context)

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
        context = navigation_context(request, "operations")
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

    @router.api_route("/media/{message_id}", methods=["GET", "HEAD"])
    async def media_file(request: Request, message_id: int) -> Response:
        """Serve media file with byte-range caching for TeraBox mode.

        For TeraBox mode with video_cache enabled, serves from local byte-range
        cache when available, otherwise streams from FUSE mount while caching.
        """
        repository: DashboardRepository = request.app.state.dashboard
        message = await repository.message(message_id)
        if message is None or message.download_status != "completed" or not message.media_path:
            raise HTTPException(status_code=404, detail="Completed media not found")

        settings = request.app.state.settings
        roots = await asyncio.to_thread(settings.media_storage_roots)
        download_root, media_path = await asyncio.to_thread(
            _resolved_media_paths, roots, message.media_path
        )
        if download_root != media_path and download_root not in media_path.parents:
            raise HTTPException(status_code=403, detail="Media path is outside DOWNLOAD_DIR")
        if not await asyncio.to_thread(media_path.is_file):
            raise HTTPException(status_code=404, detail="Media file is missing")

        # Get file size and mime type
        file_size = await asyncio.to_thread(lambda: media_path.stat().st_size)
        mime_type = message.mime_type or "application/octet-stream"

        # Parse Range header
        range_header = request.headers.get("range")
        start = 0
        end = file_size - 1
        if range_header:
            try:
                range_part = range_header.replace("bytes=", "")
                range_start, range_end = range_part.split("-")
                start = int(range_start) if range_start else 0
                end = int(range_end) if range_end else file_size - 1
            except (ValueError, AttributeError):
                pass
        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
        content_length = end - start + 1

        # Check if video cache is enabled and this is a video
        is_video = mime_type and mime_type.casefold().startswith("video/")
        video_cache = getattr(request.app.state, "video_cache", None)
        cache_enabled = is_video and video_cache is not None and settings.video_cache_dir

        async def stream_from_cache_or_fuse() -> AsyncGenerator[bytes, None]:
            """Stream bytes from cache or FUSE, caching as we go.

            The file handle stays open for the whole request and reads use
            1 MiB blocks: the previous per-64KiB open/seek/read/close pattern
            round-trips the network FUSE mount tens of thousands of times on a
            multi-GB video and stalls playback.
            """
            read_size = 1024 * 1024

            def _open_file() -> BinaryIO:
                return media_path.open("rb")

            def _read_block(handle: BinaryIO, size: int) -> bytes:
                return handle.read(size)

            def _seek(handle: BinaryIO, offset: int) -> None:
                handle.seek(offset)

            if not cache_enabled:
                # No cache - stream directly from FUSE
                remaining = content_length
                current_pos = start
                handle = await asyncio.to_thread(_open_file)
                try:
                    await asyncio.to_thread(_seek, handle, start)
                    while remaining > 0:
                        chunk = await asyncio.to_thread(
                            _read_block, handle, min(read_size, remaining)
                        )
                        if not chunk:
                            break
                        yield chunk
                        current_pos += len(chunk)
                        remaining -= len(chunk)
                finally:
                    await asyncio.to_thread(handle.close)
                return

            # Try to serve from cache first
            cached = await video_cache.get_range(message_id, start, end)
            if cached is not None:
                # Fully cached - serve from cache
                yield cached
                return

            # Not fully cached - stream from FUSE while caching
            remaining = content_length
            current_pos = start
            handle = await asyncio.to_thread(_open_file)
            try:
                await asyncio.to_thread(_seek, handle, start)
                while remaining > 0:
                    chunk = await asyncio.to_thread(_read_block, handle, min(read_size, remaining))
                    if not chunk:
                        break

                    # Cache this block (split into cache-chunk units)
                    await video_cache.store_range(message_id, current_pos, chunk)

                    yield chunk
                    current_pos += len(chunk)
                    remaining -= len(chunk)
            finally:
                await asyncio.to_thread(handle.close)

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Disposition": _content_disposition_header(media_path.name, mime_type),
        }
        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            status_code = 206
        else:
            status_code = 200

        return StreamingResponse(
            stream_from_cache_or_fuse(),
            status_code=status_code,
            media_type=mime_type,
            headers=headers,
        )

    @router.api_route("/media/{message_id}/variant", methods=["GET", "HEAD"])
    async def media_variant(request: Request, message_id: int) -> FileResponse:
        """Serve the playable H.264 variant of a video.

        In TeraBox mode with HEVC transcoding, the variant is stored at a
        predictable path alongside the original. In local mode, the transcode
        manager handles on-demand transcoding.
        """
        repository: DashboardRepository = request.app.state.dashboard
        message = await repository.message(message_id)
        if message is None or message.download_status != "completed":
            raise HTTPException(status_code=404, detail="Completed media not found")

        # Check for pre-generated variant path (TeraBox mode with HEVC transcode)
        if message.media_variant_path:
            roots = await asyncio.to_thread(request.app.state.settings.media_storage_roots)
            download_root, variant_path = await asyncio.to_thread(
                _resolved_media_paths, roots, message.media_variant_path
            )
            if download_root != variant_path and download_root not in variant_path.parents:
                raise HTTPException(status_code=403, detail="Variant path is outside DOWNLOAD_DIR")
            if await asyncio.to_thread(variant_path.is_file):
                return FileResponse(
                    variant_path,
                    media_type="video/mp4",
                    filename=variant_path.name,
                    content_disposition_type="inline",
                )

        # Fallback: use transcode manager for local mode (on-demand transcode)
        media_path, _ = await _completed_media(request, message_id)
        service: MediaVariantService = request.app.state.media_variants
        playable = await service.playable_path(media_path)
        if playable is None or playable == media_path:
            raise HTTPException(status_code=404, detail="Playable variant not available")
        return FileResponse(
            playable,
            media_type="video/mp4",
            filename=media_path.name,
            content_disposition_type="inline",
        )

    @router.get("/media/{message_id}/variant-status")
    async def media_variant_status(request: Request, message_id: int) -> dict[str, object]:
        media_path, _ = await _completed_media(request, message_id)
        service: MediaVariantService = request.app.state.media_variants
        return service.status(media_path).as_dict()

    @router.get("/media/{message_id}/poster")
    async def media_poster(request: Request, message_id: int) -> FileResponse:
        """Serve a cached JPEG poster frame for a video."""
        media_path, mime_type = await _completed_media(request, message_id)
        if mime_type and not mime_type.casefold().startswith("video/"):
            raise HTTPException(status_code=404, detail="Poster not available")
        service: MediaVariantService = request.app.state.media_variants
        poster = await service.poster_path(media_path)
        if poster is None:
            raise HTTPException(status_code=404, detail="Poster not available")
        return FileResponse(
            poster,
            media_type="image/jpeg",
            content_disposition_type="inline",
        )

    @router.get("/media/{message_id}/thumb")
    async def media_thumbnail(
        request: Request, message_id: int, poster: bool = False
    ) -> FileResponse:
        """Serve a local WebP thumbnail or poster for fast gallery loading.

        In TeraBox mode, thumbnails/posters are generated at download time and cached
        locally. If missing, fall back to the full media file from the FUSE mount.
        """
        repository: DashboardRepository = request.app.state.dashboard
        message = await repository.message(message_id)
        if message is None or message.download_status != "completed":
            raise HTTPException(status_code=404, detail="Completed media not found")

        # Check local thumbnail cache first
        settings = request.app.state.settings
        if settings.thumbnail_cache_dir:
            cache_root = settings.thumbnail_cache_dir.expanduser().resolve() / str(
                message.telegram_chat_id
            )
            if poster:
                thumb_path = cache_root / f"{message_id}.poster.jpg"
            else:
                thumb_path = cache_root / f"{message_id}.jpg"
            if await asyncio.to_thread(thumb_path.is_file):
                return FileResponse(
                    thumb_path,
                    media_type="image/jpeg",
                    content_disposition_type="inline",
                    headers={
                        "Cache-Control": "public, max-age=31536000, immutable",
                        "ETag": f'W/"{thumb_path.stat().st_mtime_ns}-{thumb_path.stat().st_size}"',
                    },
                )

        # Fallback: serve from FUSE mount (full image/video)
        media_path, mime_type = await _completed_media(request, message_id)
        return FileResponse(
            media_path,
            media_type=mime_type or "application/octet-stream",
            filename=media_path.name,
            content_disposition_type=(
                "inline" if _preview_kind(mime_type) != "file" else "attachment"
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
        context = navigation_context(request, "")
        context["unknown_path"] = unknown_path
        return templates.TemplateResponse(
            request,
            "not_found.html",
            context,
            status_code=404,
        )

    return router
