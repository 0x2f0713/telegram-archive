"""Small cross-process advisory locks for shared archive files."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the deployed runtime is POSIX
    fcntl = None  # type: ignore[assignment]


class FileLock:
    """An async-friendly advisory lock backed by a file on the shared volume."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    async def __aenter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = await asyncio.to_thread(self.path.open, "a+")
        try:
            if fcntl is not None:
                await asyncio.to_thread(fcntl.flock, handle.fileno(), fcntl.LOCK_EX)
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if fcntl is not None:
                await asyncio.to_thread(fcntl.flock, handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def media_file_lock(path: Path) -> FileLock:
    """Return a deterministic lock shared by all workers using ``path``.

    A fixed set of lock files avoids creating one permanent lock file per media
    item while still allowing unrelated files to transfer concurrently.
    """

    resolved = path.expanduser().resolve()
    digest = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()
    lock_dir = resolved.parent / ".archiver-locks"
    return FileLock(lock_dir / f"media-{digest[:2]}.lock")
