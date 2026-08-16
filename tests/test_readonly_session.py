from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from telethon.crypto import AuthKey
from telethon.sessions.sqlite import SQLiteSession
from telethon.tl.types import PeerUser, User

from app.infrastructure.telegram.client import (
    TelegramAccessError,
    load_readonly_session,
)


def test_load_readonly_session_reads_auth_and_entities_without_writing(tmp_path: Path) -> None:
    session_path = tmp_path / "telegram_session.session"
    original = SQLiteSession(str(session_path))
    original.set_dc(5, "91.108.56.161", 443)
    original.auth_key = AuthKey(bytes(range(256)))
    original.process_entities([User(id=12345, access_hash=678, username="someone")])
    original.save()
    original.close()
    stat_before = session_path.stat()

    session = load_readonly_session(session_path)

    assert session.dc_id == 5
    assert session.server_address == "91.108.56.161"
    assert session.port == 443
    assert session.auth_key is not None
    # Entity cache resolves input entities from the loaded file.
    assert session.get_input_entity(PeerUser(12345)).access_hash == 678

    stat_after = session_path.stat()
    assert (stat_after.st_mtime_ns, stat_after.st_size) == (
        stat_before.st_mtime_ns,
        stat_before.st_size,
    )


def test_load_readonly_session_rejects_unauthenticated_file(tmp_path: Path) -> None:
    session_file = tmp_path / "telegram_session.session"
    connection = sqlite3.connect(session_file)
    connection.execute(
        "CREATE TABLE sessions (dc_id integer primary key, server_address text, "
        "port integer, auth_key blob, takeout_id integer, tmp_auth_key blob)"
    )
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (5, "0.0.0.0", 443, b"", None, b""),
    )
    connection.commit()
    connection.close()

    with pytest.raises(TelegramAccessError):
        load_readonly_session(session_file)


def test_load_readonly_session_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TelegramAccessError):
        load_readonly_session(tmp_path / "missing_session")


def test_readonly_session_never_writes_on_disconnect(tmp_path: Path) -> None:
    session_path = tmp_path / "telegram_session.session"
    original = SQLiteSession(str(session_path))
    original.set_dc(5, "91.108.56.161", 443)
    original.auth_key = AuthKey(bytes(range(256)))
    original.save()
    original.close()
    stat_before = session_path.stat()

    session = load_readonly_session(session_path)
    # Simulate Telethon's lifecycle hooks that normally persist state.
    session.process_entities([User(id=99, access_hash=1, username="other")])
    session.save()
    session.close()

    stat_after = session_path.stat()
    assert (stat_after.st_mtime_ns, stat_after.st_size) == (
        stat_before.st_mtime_ns,
        stat_before.st_size,
    )
