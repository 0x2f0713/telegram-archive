from __future__ import annotations

import logging

from app.utils.logging import configure_logging


def test_configure_logging_applies_application_and_uvicorn_levels() -> None:
    configure_logging("DEBUG")

    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("telethon").level == logging.WARNING
    assert logging.getLogger("uvicorn.error").level == logging.DEBUG
    assert logging.getLogger("uvicorn.access").level == logging.DEBUG

    configure_logging("INFO")
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("uvicorn.error").level == logging.INFO
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
