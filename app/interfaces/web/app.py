"""FastAPI application factory with local-first security defaults."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.application.chat_selection import ChatSelectionService
from app.application.dashboard import DashboardService
from app.application.media_variants import MediaVariantService
from app.application.operations import OperationManager
from app.application.runtime_settings import load_runtime_settings
from app.config import ConfigurationError, Settings
from app.infrastructure.ffmpeg import probe_capabilities
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.operations import OperationRepository
from app.infrastructure.persistence.read_models import DashboardRepository
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.persistence.selection import ChatSelectionRepository
from app.infrastructure.persistence.settings import RuntimeSettingsRepository
from app.infrastructure.prefetch import TeraBoxPrefetcher
from app.infrastructure.telegram.client import (
    accessible_dialogs,
    connect_authorized,
    create_readonly_client,
    resolve_accessible_chats,
)
from app.infrastructure.telegram.session_account import read_session_account_id
from app.infrastructure.terabox import (
    TeraBoxMediaDeleter,
    create_terabox_client,
)
from app.infrastructure.transcode import VariantManager
from app.infrastructure.video_cache import VideoRangeCache
from app.interfaces.web.auth import TelegramQrAuthManager
from app.interfaces.web.commands import OperationCommands
from app.interfaces.web.presentation import templates
from app.interfaces.web.routes import create_router
from app.interfaces.web.session import TelegramWebSession
from app.utils.logging import configure_logging

PACKAGE_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_web_security(settings: Settings) -> None:
    """Refuse a remotely reachable dashboard without signed browser sessions."""

    session_secret = _configured_session_secret(settings)
    if not _is_loopback_host(settings.web_host) and not session_secret:
        raise ConfigurationError(
            "WEB_SESSION_SECRET is required when WEB_HOST is not localhost or a loopback address"
        )


class TelegramSessionMiddleware(BaseHTTPMiddleware):
    """Protect the dashboard with a cookie bound to the local Telegram account."""

    public_paths = frozenset(
        {
            "/auth/telegram",
            "/auth/telegram/start",
            "/auth/telegram/continue",
            "/auth/telegram/logout",
            "/auth/telegram/qr.svg",
            "/api/v1/auth/telegram",
            "/login",
            "/register",
        }
    )

    def __init__(self, app: Any, *, session: TelegramWebSession) -> None:
        super().__init__(app)
        self.session = session

    @classmethod
    def _is_public(cls, path: str) -> bool:
        return path in cls.public_paths or path.startswith("/static/")

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._is_public(request.url.path):
            return await call_next(request)

        account_id = getattr(request.app.state, "account_user_id", None)
        if account_id is None:
            account_id = await asyncio.to_thread(
                read_session_account_id,
                request.app.state.settings.tg_session_name,
            )
            request.app.state.account_user_id = account_id
        if self.session.valid(request.cookies.get(self.session.cookie_name), account_id):
            return await call_next(request)

        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        login_url = f"/auth/telegram?next={quote(target, safe='/')}"
        return RedirectResponse(login_url, status_code=303)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; media-src 'self' https://dm-d.terabox.com https://*.terabox.com; "
            "frame-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path in {
            "/static/assets/dashboard.css",
            "/static/assets/dashboard.js",
        }:
            response.headers["Cache-Control"] = "private, no-cache"
        elif request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "private, max-age=3600"
        elif (
            request.url.path.startswith("/media/")
            and not request.url.path.endswith("/variant-status")
            and response.status_code in {200, 206}
        ):
            # Completed archive media is immutable: the file is finalized once
            # and never rewritten, and each message id maps to a unique file.
            # Private immutable caching makes back/forward navigation and
            # gallery reopens instant without re-downloading multi-GB videos.
            response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-store"
        return response


def _session_secret(settings: Settings) -> str:
    return _configured_session_secret(settings) or secrets.token_urlsafe(32)


def _configured_session_secret(settings: Settings) -> str:
    return settings.web_session_secret.get_secret_value() if settings.web_session_secret else ""


def create_web_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    validate_web_security(settings)
    web_session = TelegramWebSession(_session_secret(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings.database_url)
        await database.initialize()
        runtime_settings = RuntimeSettingsRepository(database)
        resolution = await load_runtime_settings(settings, runtime_settings)
        overridden = resolution.settings.with_terabox_policy()
        configure_logging(overridden.log_level)
        logger.info("SQLite connection pool active: %s", database.engine.sync_engine.pool.status())
        app.state.base_settings = settings
        app.state.settings = overridden
        app.state.database = database
        app.state.dashboard = DashboardRepository(database)
        app.state.archive = ArchiveRepository(database)
        app.state.chat_selections = ChatSelectionRepository(database)
        app.state.runtime_settings = runtime_settings
        app.state.chat_selection_service = ChatSelectionService(
            overridden.configured_chat_ids,
            app.state.archive,
            app.state.chat_selections,
            accessible_dialogs,
            resolve_accessible_chats,
            client_factory=lambda: create_readonly_client(overridden),
            client_connector=connect_authorized,
        )
        app.state.dashboard_service = DashboardService(
            app.state.archive,
            app.state.dashboard,
            app.state.chat_selections,
            overridden.configured_chat_ids,
        )
        capabilities = await probe_capabilities(overridden)
        app.state.ffmpeg_capabilities = capabilities
        app.state.variant_manager = VariantManager(overridden, capabilities)
        app.state.media_variants = MediaVariantService(
            enabled=overridden.media_variants,
            ports=app.state.variant_manager,
        )
        app.state.terabox_client = None
        app.state.media_remote_deleter = None
        if overridden.terabox_enabled:
            try:
                app.state.terabox_client = create_terabox_client(overridden)
                app.state.media_remote_deleter = TeraBoxMediaDeleter(
                    overridden, app.state.terabox_client
                )
            except ConfigurationError as exc:
                logger.error("TeraBox storage unavailable: %s", exc)

        # Initialize video range cache for TeraBox mode seeking optimization
        video_cache_dir = overridden.video_cache_dir
        video_cache_max_size = overridden.video_cache_max_size_gb * 1024 * 1024 * 1024
        video_cache_max_age = overridden.video_cache_max_age_days * 24 * 3600
        app.state.video_cache = VideoRangeCache(
            video_cache_dir, video_cache_max_size, video_cache_max_age
        )
        await app.state.video_cache.initialize()

        # Background CDN→disk prefetcher: fills the video cache continuously
        # while a user watches, so playback reads local disk instead of the
        # bandwidth-capped TeraBox CDN.
        app.state.terabox_prefetcher = None
        if app.state.terabox_client is not None:
            app.state.terabox_prefetcher = TeraBoxPrefetcher(
                app.state.terabox_client, app.state.video_cache
            )

        app.state.csrf_token = secrets.token_urlsafe(32)
        app.state.telegram_auth = TelegramQrAuthManager(overridden)
        app.state.web_session = web_session
        operations = OperationManager(
            overridden,
            OperationRepository(database),
            executors={},
        )
        operations.configure_executors(
            OperationCommands(operations, database, video_cache=app.state.video_cache).executors()
        )
        app.state.operations = operations
        app.state.account_user_id = read_session_account_id(overridden.tg_session_name)
        templates.env.globals["refresh_seconds"] = overridden.web_refresh_seconds
        await app.state.operations.startup()
        try:
            yield
        finally:
            await app.state.operations.shutdown()
            await app.state.variant_manager.shutdown()
            await app.state.telegram_auth.close()
            if app.state.terabox_client is not None:
                await app.state.terabox_client.aclose()
            await database.close()

    application = FastAPI(
        title="Telegram Archiver",
        description="Local archive dashboard and operator controls",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    if _configured_session_secret(settings) or not _is_loopback_host(settings.web_host):
        application.add_middleware(
            TelegramSessionMiddleware,
            session=web_session,
        )
    # Registered last so the headers also cover authentication redirects.
    application.add_middleware(SecurityHeadersMiddleware)
    application.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    application.include_router(create_router(settings))
    return application
