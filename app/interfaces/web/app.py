"""FastAPI application factory with local-first security defaults."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.application.chat_selection import ChatSelectionService
from app.application.operations import OperationManager
from app.config import (
    ConfigurationError,
    Settings,
    apply_runtime_overrides,
)
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.read_models import DashboardRepository, DashboardService
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.persistence.selection import ChatSelectionRepository
from app.infrastructure.persistence.settings import RuntimeSettingsRepository
from app.infrastructure.telegram.session_account import read_session_account_id
from app.interfaces.web.auth import TelegramQrAuthManager
from app.interfaces.web.routes import create_router, templates

PACKAGE_ROOT = Path(__file__).resolve().parent


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_web_security(settings: Settings) -> None:
    """Refuse an unauthenticated dashboard on a remotely reachable bind."""

    password = settings.web_password.get_secret_value() if settings.web_password else ""
    if not _is_loopback_host(settings.web_host) and not password:
        raise ConfigurationError(
            "WEB_PASSWORD is required when WEB_HOST is not localhost or a loopback address"
        )


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, username: str, password: str) -> None:
        super().__init__(app)
        self.username = username
        self.password = password

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        authorization = request.headers.get("Authorization", "")
        authenticated = False
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(
                    authorization.removeprefix("Basic "), validate=True
                ).decode("utf-8")
                username, password = decoded.split(":", 1)
                authenticated = secrets.compare_digest(
                    username, self.username
                ) and secrets.compare_digest(password, self.password)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                authenticated = False
        if not authenticated:
            return PlainTextResponse(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Telegram Archive"'},
            )
        return await call_next(request)


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


def create_web_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    validate_web_security(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings.database_url)
        await database.initialize()
        runtime_settings = RuntimeSettingsRepository(database)
        overridden = apply_runtime_overrides(settings, await runtime_settings.overrides())
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
    password = settings.web_password.get_secret_value() if settings.web_password else ""
    if password:
        application.add_middleware(
            BasicAuthMiddleware,
            username=settings.web_username,
            password=password,
        )
    # Registered last so the headers also cover Basic Auth rejections.
    application.add_middleware(SecurityHeadersMiddleware)
    application.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    application.include_router(create_router(settings))
    return application
