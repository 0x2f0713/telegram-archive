"""Translation from Telethon objects into stable application data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telethon import utils
from telethon.tl.types import Channel, ChannelForbidden, Chat, ChatForbidden, User


@dataclass(frozen=True, slots=True)
class ChatInfo:
    telegram_chat_id: int
    title: str
    username: str | None
    type: str
    entity: Any


@dataclass(frozen=True, slots=True)
class MessageData:
    telegram_chat_id: int
    telegram_message_id: int
    sender_id: int | None
    sender_name: str | None
    text: str | None
    message_date: datetime
    edit_date: datetime | None
    reply_to_message_id: int | None
    grouped_id: int | None
    has_media: bool
    media_type: str | None
    media_size: int | None
    telegram_document_id: int | None
    mime_type: str | None
    original_filename: str | None
    extension: str
    raw_message: Any


def entity_type(entity: Any) -> str:
    if isinstance(entity, (Channel, ChannelForbidden)):
        return "supergroup" if getattr(entity, "megagroup", False) else "channel"
    if isinstance(entity, (Chat, ChatForbidden)):
        return "group"
    if isinstance(entity, User):
        return "private chat"
    return "unknown"


def display_chat_title(
    telegram_chat_id: int,
    title: str | None,
    username: str | None,
    chat_type: str,
    *,
    deleted: bool = False,
    is_self: bool = False,
    is_bot: bool = False,
) -> str:
    """Return a useful title without ever promoting a numeric ID to the name."""

    candidate = (title or "").strip()
    numeric_fallback = candidate == str(telegram_chat_id)
    if numeric_fallback:
        candidate = ""
    if candidate:
        return candidate
    clean_username = (username or "").strip().lstrip("@")
    if clean_username:
        return f"@{clean_username}"
    if chat_type == "private chat":
        if is_self:
            return "Saved Messages"
        if is_bot:
            return "Telegram bot"
        return "Deleted account" if deleted or numeric_fallback else "Private chat"
    if deleted or numeric_fallback:
        return {
            "channel": "Deleted channel",
            "supergroup": "Deleted community",
            "group": "Deleted group",
        }.get(chat_type, "Deleted chat")
    return {
        "channel": "Untitled channel",
        "supergroup": "Untitled community",
        "group": "Untitled group",
    }.get(chat_type, "Telegram chat")


def chat_info(entity: Any, *, dialog_id: int | None = None, title: str | None = None) -> ChatInfo:
    telegram_chat_id = dialog_id if dialog_id is not None else utils.get_peer_id(entity)
    chat_type = entity_type(entity)
    username = getattr(entity, "username", None)
    display_title = display_chat_title(
        telegram_chat_id,
        title or utils.get_display_name(entity),
        username,
        chat_type,
        deleted=bool(getattr(entity, "deleted", False) or getattr(entity, "deactivated", False)),
        is_self=bool(getattr(entity, "is_self", False)),
        is_bot=bool(getattr(entity, "bot", False)),
    )
    return ChatInfo(
        telegram_chat_id=telegram_chat_id,
        title=display_title,
        username=username,
        type=chat_type,
        entity=entity,
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def classify_media(message: Any) -> str | None:
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "voice", None):
        return "audio"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "gif", None):
        return "animation"
    if getattr(message, "video", None) or getattr(message, "video_note", None):
        return "video"
    if getattr(message, "document", None):
        return "document"
    if getattr(message, "media", None):
        return "unsupported"
    return None


def message_data(message: Any, chat: ChatInfo) -> MessageData:
    media_type = classify_media(message)
    file_info = getattr(message, "file", None)
    original_filename = getattr(file_info, "name", None) if file_info else None
    extension = getattr(file_info, "ext", "") if file_info else ""
    if original_filename and not extension:
        extension = Path(original_filename).suffix
    sender = getattr(message, "sender", None)
    sender_name = utils.get_display_name(sender) if sender else None
    document = getattr(message, "document", None)
    reply_to = getattr(message, "reply_to", None)
    reply_id = getattr(reply_to, "reply_to_msg_id", None) or getattr(
        message, "reply_to_msg_id", None
    )
    return MessageData(
        telegram_chat_id=chat.telegram_chat_id,
        telegram_message_id=int(message.id),
        sender_id=getattr(message, "sender_id", None),
        sender_name=sender_name or None,
        text=getattr(message, "message", None) or None,
        message_date=_aware(message.date) or datetime.now(UTC),
        edit_date=_aware(getattr(message, "edit_date", None)),
        reply_to_message_id=reply_id,
        grouped_id=getattr(message, "grouped_id", None),
        has_media=media_type is not None,
        media_type=media_type,
        media_size=getattr(file_info, "size", None) if file_info else None,
        telegram_document_id=getattr(document, "id", None),
        mime_type=getattr(file_info, "mime_type", None) if file_info else None,
        original_filename=original_filename,
        extension=(extension or "").casefold(),
        raw_message=message,
    )
