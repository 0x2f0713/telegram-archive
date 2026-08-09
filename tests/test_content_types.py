from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.content_types import (
    ALL_CONTENT_TYPES,
    ContentTypeSelectionError,
    canonical_content_type_list,
    message_content_types,
    normalize_content_types,
)
from app.services.filters import MediaFilter
from app.telegram.entities import classify_media
from tests.helpers import make_message


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [
        ("photo", "photo"),
        ("video", "video"),
        ("video_note", "video_note"),
        ("voice", "voice"),
        ("audio", "audio"),
        ("gif", "animation"),
        ("sticker", "sticker"),
        ("document", "document"),
    ],
)
def test_telegram_downloadable_media_categories_are_distinct(attribute: str, expected: str) -> None:
    message = SimpleNamespace(**{attribute: object()})

    assert classify_media(message) == expected
    assert message_content_types(message) == frozenset({expected})


def test_message_can_match_text_and_media_or_other_content() -> None:
    captioned_video = SimpleNamespace(message="A caption", video=object())
    unsupported = SimpleNamespace(message="", media=object())
    service = SimpleNamespace(message="")

    assert message_content_types(captioned_video) == frozenset({"text", "video"})
    assert message_content_types(unsupported) == frozenset({"other"})
    assert message_content_types(service) == frozenset({"other"})


def test_content_type_aliases_normalize_to_stable_order() -> None:
    selected = normalize_content_types(("images, GIF, voice-messages, pdf",))

    assert selected == frozenset({"photo", "animation", "voice", "document"})
    assert canonical_content_type_list(selected) == [
        "photo",
        "voice",
        "animation",
        "document",
    ]
    assert normalize_content_types(ALL_CONTENT_TYPES) == ALL_CONTENT_TYPES


def test_empty_and_unknown_content_selections_are_rejected() -> None:
    with pytest.raises(ContentTypeSelectionError, match="at least one"):
        normalize_content_types(())
    with pytest.raises(ContentTypeSelectionError, match="Unknown content type"):
        normalize_content_types(("executable",))


def test_operation_selection_distinguishes_voice_from_audio() -> None:
    media_filter = MediaFilter(
        Settings(_env_file=None),
        frozenset({"voice"}),
    )

    voice = media_filter.evaluate(make_message(media_type="voice", extension=".ogg"))
    music = media_filter.evaluate(make_message(media_type="audio", extension=".mp3"))

    assert voice.allowed
    assert not music.allowed
    assert "not selected" in (music.reason or "")


def test_text_only_selection_archives_caption_without_its_media() -> None:
    media_filter = MediaFilter(
        Settings(_env_file=None),
        frozenset({"text"}),
    )

    decision = media_filter.evaluate(
        make_message(media_type="video", text="caption", extension=".mp4")
    )

    assert not decision.allowed
    assert "not selected" in (decision.reason or "")


def test_global_category_switches_remain_policy_boundaries() -> None:
    settings = Settings(
        _env_file=None,
        download_videos=False,
        download_audio=False,
        download_documents=False,
    )
    media_filter = MediaFilter(settings)

    assert not media_filter.evaluate(make_message(media_type="video_note")).allowed
    assert not media_filter.evaluate(make_message(media_type="voice")).allowed
    assert not media_filter.evaluate(make_message(media_type="sticker")).allowed
