"""Rich-backed logging without sensitive value interpolation."""

from __future__ import annotations

import logging

from rich.logging import RichHandler


def configure_logging(level: str = "INFO") -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, markup=False, show_path=False)],
        force=True,
    )
    # Telethon's protocol-level logs are noisy and may contain more metadata
    # than an ordinary operator needs. Warnings remain visible.
    logging.getLogger("telethon").setLevel(max(numeric_level, logging.WARNING))
    logging.getLogger("uvicorn.error").setLevel(numeric_level)
    logging.getLogger("uvicorn.access").setLevel(
        logging.DEBUG if numeric_level == logging.DEBUG else logging.WARNING
    )


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
