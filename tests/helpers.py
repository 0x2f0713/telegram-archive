from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.telegram.entities import ChatInfo, MessageData


def make_chat(**overrides: Any) -> ChatInfo:
    values: dict[str, Any] = {
        "telegram_chat_id": -1001234567890,
        "title": "Test Community",
        "username": "test_community",
        "type": "supergroup",
        "entity": object(),
    }
    values.update(overrides)
    return ChatInfo(**values)


def make_message(**overrides: Any) -> MessageData:
    values: dict[str, Any] = {
        "telegram_chat_id": -1001234567890,
        "telegram_message_id": 42,
        "sender_id": 100,
        "sender_name": "Test Sender",
        "text": "release notes",
        "message_date": datetime(2026, 8, 9, 10, 30, tzinfo=UTC),
        "edit_date": None,
        "reply_to_message_id": None,
        "grouped_id": None,
        "has_media": True,
        "media_type": "document",
        "media_size": 1024,
        "telegram_document_id": 999,
        "mime_type": "application/pdf",
        "original_filename": "report.pdf",
        "extension": ".pdf",
        "raw_message": object(),
    }
    values.update(overrides)
    return MessageData(**values)
