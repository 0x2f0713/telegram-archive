"""Translation from Telethon objects into stable domain objects.

This is the only adapter that may touch Telethon message/entity internals.
Everything below the ``message_data`` / ``chat_info`` boundary is pure domain.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telethon import utils
from telethon.tl.types import Channel, ChannelForbidden, Chat, ChatForbidden, User

from app.domain.artifacts import MediaArtifact, MediaType
from app.domain.chats import ChatInfo, ChatType, display_chat_title
from app.domain.content import ContentType, classify_content
from app.domain.messages import MessageData


def entity_type(entity: Any) -> ChatType:
    if isinstance(entity, (Channel, ChannelForbidden)):
        return ChatType.SUPERGROUP if getattr(entity, "megagroup", False) else ChatType.CHANNEL
    if isinstance(entity, (Chat, ChatForbidden)):
        return ChatType.GROUP
    if isinstance(entity, User):
        return ChatType.PRIVATE
    return ChatType.UNKNOWN


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


def classify_media(message: Any) -> MediaType | None:
    if getattr(message, "photo", None):
        return MediaType.PHOTO
    if getattr(message, "voice", None):
        return MediaType.VOICE
    if getattr(message, "audio", None):
        return MediaType.AUDIO
    if getattr(message, "gif", None):
        return MediaType.ANIMATION
    if getattr(message, "video_note", None):
        return MediaType.VIDEO_NOTE
    if getattr(message, "video", None):
        return MediaType.VIDEO
    if getattr(message, "sticker", None):
        return MediaType.STICKER
    if getattr(message, "document", None):
        return MediaType.DOCUMENT
    if getattr(message, "media", None):
        return MediaType.UNSUPPORTED
    return None


def content_types_of(message: Any) -> frozenset[ContentType]:
    """Classify one raw Telethon message without building a full domain object."""

    return classify_content(str(getattr(message, "message", "") or ""), classify_media(message))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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
    artifact = None
    if media_type is not None:
        artifact = MediaArtifact(
            media_type=media_type,
            media_size=getattr(file_info, "size", None) if file_info else None,
            telegram_document_id=getattr(document, "id", None),
            mime_type=getattr(file_info, "mime_type", None) if file_info else None,
            original_filename=original_filename,
            extension=(extension or "").casefold(),
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
        artifact=artifact,
    )
