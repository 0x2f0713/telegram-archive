"""Bounded form parsing and CSRF checks shared by web feature routers."""

from __future__ import annotations

import secrets
from urllib.parse import parse_qs

from fastapi import HTTPException, Request


async def form_values(
    request: Request, *, max_bytes: int = 2048, max_fields: int = 100
) -> dict[str, list[str]]:
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


def require_csrf(request: Request, values: dict[str, list[str]]) -> None:
    submitted = values.get("csrf_token", [""])[0]
    if not secrets.compare_digest(submitted, request.app.state.csrf_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
