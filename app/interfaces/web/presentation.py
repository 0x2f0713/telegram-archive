"""Shared Jinja environment and page-level presentation context."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.utils.logging import format_bytes

TEMPLATES_ROOT = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_ROOT)


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


def _month_label(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%B %Y")


def navigation_context(request: Request, section: str) -> dict[str, object]:
    return {
        "request": request,
        "section": section,
        "generated_at": datetime.now(UTC),
        "csrf_token": request.app.state.csrf_token,
        "media_variants": request.app.state.settings.media_variants,
        "terabox_enabled": request.app.state.settings.terabox_enabled,
    }


templates.env.filters["bytes"] = lambda value: format_bytes(int(value or 0))
templates.env.filters["datetime"] = _format_datetime
templates.env.filters["day_label"] = _day_label
templates.env.filters["month_label"] = _month_label
