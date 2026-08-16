"""Pure business model shared by every archive surface.

The domain package contains the artifact hierarchy (Chat -> Message ->
MediaArtifact), the download-state machine, the content taxonomy, and the
selection policy. It imports no frameworks, so every rule here is testable
without Telegram, SQLAlchemy, or a running server.
"""

from app.domain.artifacts import DownloadState, MediaArtifact, MediaType
from app.domain.chats import ChatInfo, ChatType, display_chat_title
from app.domain.content import (
    ALL_CONTENT_TYPES,
    CONTENT_TYPE_OPTIONS,
    MEDIA_CONTENT_TYPES,
    ContentType,
    ContentTypeOption,
    ContentTypeSelectionError,
    canonical_content_type_list,
    classify_content,
    message_content_types,
    normalize_content_types,
)
from app.domain.messages import MessageData
from app.domain.operations import OperationCommand, OperationStatus
from app.domain.selection import ChatSelection, SelectionMode

__all__ = [
    "ALL_CONTENT_TYPES",
    "CONTENT_TYPE_OPTIONS",
    "MEDIA_CONTENT_TYPES",
    "ChatInfo",
    "ChatSelection",
    "ChatType",
    "ContentType",
    "ContentTypeOption",
    "ContentTypeSelectionError",
    "DownloadState",
    "MediaArtifact",
    "MediaType",
    "MessageData",
    "OperationCommand",
    "OperationStatus",
    "SelectionMode",
    "canonical_content_type_list",
    "classify_content",
    "display_chat_title",
    "message_content_types",
    "normalize_content_types",
]
