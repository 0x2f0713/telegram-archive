from pathlib import Path

from app.services.filenames import media_filename, output_path, sanitize_filename
from tests.helpers import make_chat, make_message


def test_sanitize_filename_removes_traversal_and_cross_platform_characters() -> None:
    sanitized = sanitize_filename("../CON:<bad>|name?.pdf")

    assert "/" not in sanitized
    assert "\\" not in sanitized
    assert not sanitized.startswith(".")
    assert sanitized.endswith(".pdf")


def test_sanitize_windows_reserved_name() -> None:
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("NUL.txt") == "_NUL.txt"
    assert sanitize_filename("CON .txt") == "_CON.txt"


def test_sanitize_filename_limits_utf8_bytes() -> None:
    sanitized = sanitize_filename(f"{'😀' * 100}.jpg", max_length=180)

    assert len(sanitized.encode("utf-8")) <= 180
    assert sanitized.endswith(".jpg")


def test_generated_media_filename_is_deterministic() -> None:
    message = make_message(original_filename=None, media_type="photo", extension=".jpg")

    assert media_filename(message) == "42_photo.jpg"


def test_output_path_uses_chat_and_telegram_date(tmp_path: Path) -> None:
    path = output_path(tmp_path, make_chat(title="News / Updates"), make_message())

    assert path.relative_to(tmp_path) == Path(
        "-1001234567890_News _ Updates/2026/08/09/42_report.pdf"
    )


def test_original_filename_cannot_escape_download_root(tmp_path: Path) -> None:
    path = output_path(
        tmp_path,
        make_chat(title="../../outside"),
        make_message(original_filename="../../secret.txt", extension=".txt"),
    )

    assert path.resolve().is_relative_to(tmp_path.resolve())
    assert path.name == "42_secret.txt"
