"""Telegram browser-authentication routes."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.infrastructure.telegram.session_account import read_session_account_id
from app.interfaces.web.auth import TelegramQrAuthManager
from app.interfaces.web.forms import form_values, require_csrf
from app.interfaces.web.presentation import navigation_context, templates
from app.interfaces.web.session import TelegramWebSession


def _safe_next_path(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _browser_authenticated(request: Request) -> bool:
    session: TelegramWebSession | None = getattr(request.app.state, "web_session", None)
    account_id = getattr(request.app.state, "account_user_id", None)
    return bool(session and session.valid(request.cookies.get(session.cookie_name), account_id))


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


def create_auth_router() -> APIRouter:
    router = APIRouter()

    @router.get("/auth/telegram", response_class=HTMLResponse)
    async def telegram_auth_page(request: Request) -> HTMLResponse:
        manager: TelegramQrAuthManager = request.app.state.telegram_auth
        account_id = getattr(request.app.state, "account_user_id", None)
        use_existing_session = getattr(manager, "use_existing_session", None)
        snapshot = (
            use_existing_session(account_id)
            if account_id is not None and callable(use_existing_session)
            else await manager.inspect_session()
        )
        next_path = _safe_next_path(request.query_params.get("next"))
        context = navigation_context(request, "account")
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
        values = await form_values(request)
        require_csrf(request, values)
        manager: TelegramQrAuthManager = request.app.state.telegram_auth
        await manager.start()
        next_path = _safe_next_path(values.get("next", [""])[0])
        return RedirectResponse(
            f"/auth/telegram?{urlencode({'next': next_path})}",
            status_code=303,
        )

    @router.post("/auth/telegram/continue", response_class=HTMLResponse)
    async def continue_telegram_auth(request: Request) -> RedirectResponse:
        values = await form_values(request)
        require_csrf(request, values)
        manager: TelegramQrAuthManager = request.app.state.telegram_auth
        account_id = getattr(request.app.state, "account_user_id", None)
        use_existing_session = getattr(manager, "use_existing_session", None)
        if account_id is not None and callable(use_existing_session):
            snapshot = use_existing_session(account_id)
        else:
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
        values = await form_values(request)
        require_csrf(request, values)
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
        account_id = getattr(request.app.state, "account_user_id", None)
        use_existing_session = getattr(manager, "use_existing_session", None)
        if account_id is not None and callable(use_existing_session):
            use_existing_session(account_id)
        return manager.snapshot.public_dict()

    @router.get("/login", include_in_schema=False)
    @router.get("/register", include_in_schema=False)
    async def telegram_auth_alias() -> RedirectResponse:
        return RedirectResponse("/auth/telegram", status_code=307)

    return router
