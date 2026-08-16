from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain import ChatInfo, ChatType, MediaArtifact, MediaType, MessageData


def make_chat(**overrides: Any) -> ChatInfo:
    values: dict[str, Any] = {
        "telegram_chat_id": -1001234567890,
        "title": "Test Community",
        "username": "test_community",
        "type": ChatType.SUPERGROUP,
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
        "artifact": MediaArtifact(
            media_type=MediaType.DOCUMENT,
            media_size=1024,
            telegram_document_id=999,
            mime_type="application/pdf",
            original_filename="report.pdf",
            extension=".pdf",
        ),
    }
    values.update(overrides)
    return MessageData(**values)


def make_no_media_message(**overrides: Any) -> MessageData:
    """A text-only message carrying no media artifact."""
    values: dict[str, Any] = {
        "telegram_chat_id": -1001234567890,
        "telegram_message_id": 43,
        "sender_id": 100,
        "sender_name": "Test Sender",
        "text": "plain text only",
        "message_date": datetime(2026, 8, 9, 10, 30, tzinfo=UTC),
        "edit_date": None,
        "reply_to_message_id": None,
        "grouped_id": None,
        "artifact": None,
    }
    values.update(overrides)
    return MessageData(**values)
