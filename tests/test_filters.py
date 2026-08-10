from app.application.media_policy import MediaFilter
from app.config import Settings
from tests.helpers import make_message


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


def test_file_size_limit_is_enforced() -> None:
    decision = MediaFilter(settings(max_file_size_mb=1)).evaluate(
        make_message(media_size=1 * 1024 * 1024 + 1)
    )

    assert not decision.allowed
    assert "maximum size" in (decision.reason or "")


def test_equal_file_size_limit_is_allowed() -> None:
    decision = MediaFilter(settings(max_file_size_mb=1)).evaluate(
        make_message(media_size=1 * 1024 * 1024)
    )

    assert decision.allowed


def test_ignored_extension_wins() -> None:
    decision = MediaFilter(
        settings(allowed_extensions=".pdf,.exe", ignored_extensions=".exe")
    ).evaluate(make_message(extension=".exe"))

    assert not decision.allowed
    assert "ignored" in (decision.reason or "")


def test_allowed_extensions_and_keywords_are_case_insensitive() -> None:
    decision = MediaFilter(
        settings(allowed_extensions="PDF", ignored_extensions="", keywords="Release,Urgent")
    ).evaluate(make_message(extension=".PDF", text="New RELEASE available"))

    assert decision.allowed


def test_keyword_mismatch_skips_only_media() -> None:
    decision = MediaFilter(settings(keywords="urgent")).evaluate(
        make_message(text="ordinary update")
    )

    assert not decision.allowed
    assert "keywords" in (decision.reason or "")


def test_media_category_can_be_disabled() -> None:
    decision = MediaFilter(settings(download_audio=False)).evaluate(
        make_message(media_type="audio", extension=".ogg")
    )

    assert not decision.allowed
