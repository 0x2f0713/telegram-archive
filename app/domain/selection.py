"""The durable chat-selection policy domain model."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class SelectionMode(StrEnum):
    """Where archive workers read their target chat IDs."""

    ENVIRONMENT = "environment"
    SPECIFIC = "specific"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class ChatSelection:
    """Effective selection state; ``environment`` means no database override."""

    mode: SelectionMode
    selected_chat_ids: tuple[int, ...] = ()

    def effective_ids(
        self,
        *,
        legacy_ids: Iterable[int],
        available_ids: Iterable[int],
    ) -> tuple[int, ...]:
        """Resolve this policy against a known or currently accessible ID set."""

        if self.mode == SelectionMode.ENVIRONMENT:
            source = legacy_ids
        elif self.mode == SelectionMode.SPECIFIC:
            source = self.selected_chat_ids
        else:
            source = available_ids
        return tuple(dict.fromkeys(int(chat_id) for chat_id in source))
