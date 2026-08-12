"""FastAPI application factory with local-first security defaults."""

from __future__ import annotations

import asyncio
import ipaddress
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
from app.application.operations import OperationManager
from app.application.runtime_settings import load_runtime_settings
from app.config import ConfigurationError, Settings
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.read_models import DashboardRepository, DashboardService
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.persistence.selection import ChatSelectionRepository
from app.infrastructure.telegram.session_account import read_session_account_id
from app.interfaces.web.auth import TelegramQrAuthManager
from app.interfaces.web.routes import create_router, templates
from app.interfaces.web.session import TelegramWebSession
from app.utils.logging import configure_logging

PACKAGE_ROOT = Path(__file__).resolve().parent


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
            "img-src 'self' data:; media-src 'self'; frame-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "private, max-age=3600"
        else:
            response.headers["Cache-Control"] = "no-store"
        return response


def _session_secret(settings: Settings) -> str:
    return _configured_session_secret(settings) or secrets.token_urlsafe(32)


def _configured_session_secret(settings: Settings) -> str:
    return (
        settings.web_session_secret.get_secret_value() if settings.web_session_secret else ""
    )


def create_web_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    validate_web_security(settings)
    web_session = TelegramWebSession(_session_secret(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings.database_url)
        await database.initialize()
        resolution = await load_runtime_settings(settings, database)
        overridden = resolution.settings
        configure_logging(overridden.log_level)
        app.state.base_settings = settings
        app.state.settings = overridden
        app.state.database = database
        app.state.dashboard = DashboardRepository(database)
        app.state.archive = ArchiveRepository(database)
        app.state.chat_selections = ChatSelectionRepository(database)
        app.state.chat_selection_service = ChatSelectionService(overridden, app.state.archive)
        app.state.dashboard_service = DashboardService(database, overridden.configured_chat_ids)
        app.state.csrf_token = secrets.token_urlsafe(32)
        app.state.telegram_auth = TelegramQrAuthManager(overridden)
        app.state.web_session = web_session
        app.state.operations = OperationManager(overridden, database)
        app.state.account_user_id = read_session_account_id(overridden.tg_session_name)
        templates.env.globals["refresh_seconds"] = overridden.web_refresh_seconds
        await app.state.operations.startup()
        try:
            yield
        finally:
            await app.state.operations.shutdown()
            await app.state.telegram_auth.close()
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
