"""Domain value object for an archived Telegram chat."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ChatType(StrEnum):
    """Canonical Telegram chat taxonomy used everywhere in the domain."""

    PRIVATE = "private chat"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ChatInfo:
    """An archive target resolved from the account's accessible dialog list.

    ``entity`` is the raw Telethon entity used by the download adapters; it is
    intentionally typed as ``Any`` because the domain itself never touches it.
    """

    telegram_chat_id: int
    title: str
    username: str | None
    type: ChatType
    entity: Any


def display_chat_title(
    telegram_chat_id: int,
    title: str | None,
    username: str | None,
    chat_type: ChatType | str,
    *,
    deleted: bool = False,
    is_self: bool = False,
    is_bot: bool = False,
) -> str:
    """Return a useful title without ever promoting a numeric ID to the name."""

    chat_type = chat_type if isinstance(chat_type, ChatType) else ChatType(chat_type)
    candidate = (title or "").strip()
    numeric_fallback = candidate == str(telegram_chat_id)
    if numeric_fallback:
        candidate = ""
    if candidate:
        return candidate
    clean_username = (username or "").strip().lstrip("@")
    if clean_username:
        return f"@{clean_username}"
    if chat_type == ChatType.PRIVATE:
        if is_self:
            return "Saved Messages"
        if is_bot:
            return "Telegram bot"
        return "Deleted account" if deleted or numeric_fallback else "Private chat"
    if deleted or numeric_fallback:
        return {
            ChatType.CHANNEL: "Deleted channel",
            ChatType.SUPERGROUP: "Deleted community",
            ChatType.GROUP: "Deleted group",
        }.get(chat_type, "Deleted chat")
    return {
        ChatType.CHANNEL: "Untitled channel",
        ChatType.SUPERGROUP: "Untitled community",
        ChatType.GROUP: "Untitled group",
    }.get(chat_type, "Telegram chat")
