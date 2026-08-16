"""Operator overview page."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.application.dashboard import DashboardService
from app.interfaces.web.presentation import navigation_context, templates


def _chart_points(values: tuple[int, ...], width: int = 1000, height: int = 220) -> str:
    if not values:
        return ""
    highest = max(values) or 1
    last_index = max(1, len(values) - 1)
    return " ".join(
        f"{round(index / last_index * width, 2)},{round(height - value / highest * height, 2)}"
        for index, value in enumerate(values)
    )


def create_overview_router() -> APIRouter:
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
        context = navigation_context(request, "overview")
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

    return router
