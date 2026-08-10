"""Read-only access to the persisted Telethon session for account identity."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def read_session_account_id(session_name: Path | str) -> int | None:
    """Return the authenticated account's Telegram user ID, if recoverable.

    Telethon persists the current user's ID in the session file's ``entities``
    table under the synthetic row ``id = 0`` (its ``hash`` column carries the
    real user ID). When that marker is missing, fall back to the only entity
    row that stores a phone number, which is the account owner.

    Returns ``None`` when the file is absent, locked, or unreadable so callers
    can degrade gracefully (e.g. render every message as incoming).
    """

    path = Path(session_name).expanduser()
    if not path.suffix:
        path = Path(str(path) + ".session")
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT hash FROM entities WHERE id = 0 ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if row and isinstance(row[0], int) and row[0] > 0:
            return row[0]
        row = connection.execute(
            "SELECT id FROM entities WHERE phone IS NOT NULL AND phone != 0 "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()
