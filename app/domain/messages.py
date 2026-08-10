"""Domain value object for an archived Telegram message."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.artifacts import MediaArtifact, MediaType


@dataclass(frozen=True, slots=True, init=False)
class MessageData:
    """Stable application representation of one Telegram message.

    The message content is separate from the optional media artifact it
    carries, mirroring the business hierarchy Chat -> Message -> Artifact.

    The constructor also accepts the flattened legacy media fields for
    ergonomic construction; both forms produce the same value object.
    """

    telegram_chat_id: int
    telegram_message_id: int
    sender_id: int | None
    sender_name: str | None
    text: str | None
    message_date: datetime
    edit_date: datetime | None
    reply_to_message_id: int | None
    grouped_id: int | None
    artifact: MediaArtifact | None

    def __init__(
        self,
        telegram_chat_id: int,
        telegram_message_id: int,
        sender_id: int | None,
        sender_name: str | None,
        text: str | None,
        message_date: datetime,
        edit_date: datetime | None,
        reply_to_message_id: int | None,
        grouped_id: int | None,
        artifact: MediaArtifact | None = None,
        *,
        media_type: str | None = None,
        media_size: int | None = None,
        telegram_document_id: int | None = None,
        mime_type: str | None = None,
        original_filename: str | None = None,
        extension: str = "",
        has_media: bool | None = None,
    ) -> None:
        if media_type is not None:
            # A media type is a complete artifact description: every flat field
            # is authoritative, and absent values mean absent attributes.
            try:
                parsed_media_type = MediaType(media_type)
            except ValueError:
                parsed_media_type = None
            artifact = MediaArtifact(
                media_type=parsed_media_type,
                media_size=media_size,
                telegram_document_id=telegram_document_id,
                mime_type=mime_type,
                original_filename=original_filename,
                extension=extension,
            )
        else:
            flat_override = any(
                (
                    media_size is not None,
                    telegram_document_id is not None,
                    mime_type is not None,
                    original_filename is not None,
                    bool(extension),
                )
            )
            if flat_override:
                # Partial flat overrides merge onto the supplied artifact.
                base_artifact = artifact or MediaArtifact(
                    media_type=None,
                    media_size=None,
                    telegram_document_id=None,
                    mime_type=None,
                    original_filename=None,
                    extension="",
                )
                artifact = MediaArtifact(
                    media_type=base_artifact.media_type,
                    media_size=media_size if media_size is not None else base_artifact.media_size,
                    telegram_document_id=telegram_document_id
                    if telegram_document_id is not None
                    else base_artifact.telegram_document_id,
                    mime_type=mime_type if mime_type is not None else base_artifact.mime_type,
                    original_filename=original_filename
                    if original_filename is not None
                    else base_artifact.original_filename,
                    extension=extension or base_artifact.extension,
                )
        if has_media is False:
            artifact = None
        object.__setattr__(self, "telegram_chat_id", telegram_chat_id)
        object.__setattr__(self, "telegram_message_id", telegram_message_id)
        object.__setattr__(self, "sender_id", sender_id)
        object.__setattr__(self, "sender_name", sender_name)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "message_date", message_date)
        object.__setattr__(self, "edit_date", edit_date)
        object.__setattr__(self, "reply_to_message_id", reply_to_message_id)
        object.__setattr__(self, "grouped_id", grouped_id)
        object.__setattr__(self, "artifact", artifact)

    @property
    def has_media(self) -> bool:
        return self.artifact is not None

    @property
    def media_type(self) -> str | None:
        return (
            self.artifact.media_type.value if self.artifact and self.artifact.media_type else None
        )

    @property
    def media_size(self) -> int | None:
        return self.artifact.media_size if self.artifact else None

    @property
    def telegram_document_id(self) -> int | None:
        return self.artifact.telegram_document_id if self.artifact else None

    @property
    def mime_type(self) -> str | None:
        return self.artifact.mime_type if self.artifact else None

    @property
    def original_filename(self) -> str | None:
        return self.artifact.original_filename if self.artifact else None

    @property
    def extension(self) -> str:
        return self.artifact.extension if self.artifact else ""

    @property
    def raw_message(self) -> Any:
        """Legacy alias used by download adapters; the raw object is never stored."""
        raise AttributeError(
            "MessageData no longer carries the raw Telegram message; "
            "pass it to the download adapter separately."
        )
