"""Modular media download filtering."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.telegram.entities import MessageData


@dataclass(frozen=True, slots=True)
class FilterDecision:
    allowed: bool
    reason: str | None = None


class MediaFilter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, message: MessageData) -> FilterDecision:
        media_type = message.media_type
        if media_type in {None, "unsupported"}:
            return FilterDecision(False, "Unsupported media type")
        enabled = {
            "photo": self.settings.download_photos,
            "video": self.settings.download_videos,
            "animation": self.settings.download_videos,
            "document": self.settings.download_documents,
            "audio": self.settings.download_audio,
        }.get(media_type, False)
        if not enabled:
            return FilterDecision(False, f"{media_type.capitalize()} downloads are disabled")

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
