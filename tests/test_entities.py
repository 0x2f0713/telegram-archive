from telethon.tl.types import User

from app.domain import display_chat_title
from app.infrastructure.telegram.translation import chat_info


def test_deleted_private_chat_gets_human_title_instead_of_id() -> None:
    entity = User(id=42, deleted=True)

    result = chat_info(entity, dialog_id=42, title="")

    assert result.title == "Deleted account"
    assert result.type == "private chat"


def test_saved_messages_and_username_fallbacks_are_readable() -> None:
    saved = User(id=42, is_self=True)
    username_only = User(id=84, username="release_bot")

    assert chat_info(saved, dialog_id=42, title="").title == "Saved Messages"
    assert chat_info(username_only, dialog_id=84, title="84").title == "@release_bot"


def test_cached_numeric_titles_use_type_aware_fallbacks() -> None:
    assert display_chat_title(42, "42", None, "private chat") == "Deleted account"
    assert display_chat_title(-1001, "-1001", None, "channel") == "Deleted channel"
    assert display_chat_title(-1002, "-1002", None, "supergroup") == "Deleted community"
    assert display_chat_title(-1003, "-1003", None, "group") == "Deleted group"
