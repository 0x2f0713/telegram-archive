"""The media download policy: global switches, size, extensions, keywords.

This is a pure application rule set over the domain model. It decides whether
an artifact is eligible for transfer; it never performs the transfer itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.domain import ContentType, MediaType, MessageData
from app.domain.content import CONTENT_TO_MEDIA, MEDIA_CONTENT_TYPES


@dataclass(frozen=True, slots=True)
class FilterDecision:
    allowed: bool
    reason: str | None = None


class MediaFilter:
    def __init__(
        self,
        settings: Settings,
        selected_content_types: frozenset[ContentType] | None = None,
    ) -> None:
        self.settings = settings
        self.selected_media_types = (
            frozenset(
                CONTENT_TO_MEDIA[content_type]
                for content_type in selected_content_types & MEDIA_CONTENT_TYPES
            )
            if selected_content_types is not None
            else None
        )

    def media_type_selected(self, media_type: str | None) -> bool:
        """Return whether an operation-specific selection permits this media type."""

        if self.selected_media_types is None:
            return True
        if not media_type:
            return False
        try:
            return MediaType(media_type) in self.selected_media_types
        except ValueError:
            return False

    def evaluate(self, message: MessageData) -> FilterDecision:
        artifact = message.artifact
        media_type = artifact.media_type if artifact else None
        if media_type is None or media_type == MediaType.UNSUPPORTED:
            return FilterDecision(False, "Unsupported media type")
        if not self.media_type_selected(media_type.value):
            return FilterDecision(False, f"{media_type.display_name} not selected")
        enabled = {
            MediaType.PHOTO: self.settings.download_photos,
            MediaType.VIDEO: self.settings.download_videos,
            MediaType.VIDEO_NOTE: self.settings.download_videos,
            MediaType.ANIMATION: self.settings.download_videos,
            MediaType.DOCUMENT: self.settings.download_documents,
            MediaType.STICKER: self.settings.download_documents,
            MediaType.AUDIO: self.settings.download_audio,
            MediaType.VOICE: self.settings.download_audio,
        }.get(media_type, False)
        if not enabled:
            return FilterDecision(False, f"{media_type.display_name} downloads are disabled")

        if (
            message.media_size is not None
            and message.media_size > self.settings.max_file_size_bytes
        ):
            return FilterDecision(
                False,
                f"File exceeds configured maximum size of {self.settings.max_file_size_mb} MB",
            )

        extension = message.extension.casefold()
        if extension and extension in self.settings.ignored_extension_set:
            return FilterDecision(False, f"Extension {extension} is ignored")
        if (
            self.settings.allowed_extension_set
            and extension not in self.settings.allowed_extension_set
        ):
            return FilterDecision(False, f"Extension {extension or '(unknown)'} is not allowed")

        if self.settings.keyword_set:
            text = (message.text or "").casefold()
            if not any(keyword in text for keyword in self.settings.keyword_set):
                return FilterDecision(False, "Message does not match configured keywords")

        return FilterDecision(True)
