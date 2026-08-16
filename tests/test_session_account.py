from __future__ import annotations

import sqlite3
from pathlib import Path

from app.infrastructure.telegram.session_account import read_session_account_id


def _session_file(path: Path, rows: list[tuple]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE entities (id integer primary key, hash integer not null, "
        "username text, phone integer, name text, date integer)"
    )
    connection.executemany(
        "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()
    return path


def test_read_session_account_id_uses_the_id_zero_marker(tmp_path: Path) -> None:
    session_file = _session_file(
        tmp_path / "telegram_session.session",
        [
            (0, 909580109, None, None, None, 1786334406),
            (909580109, 3205879698573660385, "me", 84911984911, "me", 1786334401),
            (100, 1, None, None, "Alice", 1786334400),
        ],
    )
    assert read_session_account_id(session_file) == 909580109


def test_read_session_account_id_falls_back_to_the_phone_row(tmp_path: Path) -> None:
    session_file = _session_file(
        tmp_path / "telegram_session.session",
        [
            (100, 1, None, None, "Alice", 1786334400),
            (909580109, 3205879698573660385, "me", 84911984911, "me", 1786334401),
        ],
    )
    assert read_session_account_id(session_file) == 909580109


def test_read_session_account_id_returns_none_without_a_session(tmp_path: Path) -> None:
    assert read_session_account_id(tmp_path / "missing_session") is None


def test_read_session_account_id_returns_none_for_an_empty_entities_table(
    tmp_path: Path,
) -> None:
    session_file = _session_file(tmp_path / "telegram_session.session", [])
    assert read_session_account_id(session_file) is None


def test_read_session_account_id_tolerates_a_corrupt_file(tmp_path: Path) -> None:
    session_file = tmp_path / "telegram_session.session"
    session_file.write_bytes(b"not a sqlite database at all")
    assert read_session_account_id(session_file) is None
