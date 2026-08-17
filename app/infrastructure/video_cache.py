"""Local LRU cache for video byte ranges (TeraBox mode seeking optimization)."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_INDEX_NAME = "index.json"
CHUNK_SIZE = 256 * 1024  # 256KB chunks


class VideoRangeCache:
    """Local LRU cache for video byte ranges.

    Stores video segments as individual files with an LRU index for eviction.
    Designed to accelerate seeking in TeraBox mode by caching hot byte ranges locally.
    """

    def __init__(
        self,
        cache_dir: Path,
        max_size_bytes: int,
        max_age_seconds: int,
    ) -> None:
        self.cache_dir = cache_dir.expanduser().resolve()
        self.max_size_bytes = max_size_bytes
        self.max_age_seconds = max_age_seconds
        self._lock = asyncio.Lock()
        self._index: dict[
            int, dict[str, object]
        ] = {}  # message_id -> {chunk_start: (path, size, last_access)}
        self._total_size = 0
        self._initialized = False
        self._last_save = 0.0

    async def initialize(self) -> None:
        """Load index from disk."""
        async with self._lock:
            if self._initialized:
                return
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            index_path = self.cache_dir / CACHE_INDEX_NAME
            if await asyncio.to_thread(index_path.is_file):
                try:
                    import json

                    data = await asyncio.to_thread(index_path.read_text)
                    loaded = json.loads(data)
                    for msg_id_str, chunks in loaded.items():
                        msg_id = int(msg_id_str)
                        self._index[msg_id] = {}
                        for chunk_start_str, info in chunks.items():
                            chunk_start = int(chunk_start_str)
                            chunk_path = self.cache_dir / info["path"]
                            if await asyncio.to_thread(chunk_path.is_file):
                                self._index[msg_id][chunk_start] = (
                                    chunk_path,
                                    info["size"],
                                    info["last_access"],
                                )
                                self._total_size += info["size"]
                except Exception as exc:
                    logger.warning("Failed to load video cache index: %s", exc)
            self._initialized = True

    async def _save_index(self, force: bool = False) -> None:
        """Persist index to disk, at most once per second unless forced."""
        now = time.time()
        if not force and now - self._last_save < 1.0:
            return
        self._last_save = now
        try:
            import json

            serializable = {}
            for msg_id, chunks in self._index.items():
                serializable[str(msg_id)] = {}
                for chunk_start, (path, size, last_access) in chunks.items():
                    serializable[str(msg_id)][str(chunk_start)] = {
                        "path": path.name,
                        "size": size,
                        "last_access": last_access,
                    }
            index_path = self.cache_dir / CACHE_INDEX_NAME
            await asyncio.to_thread(index_path.write_text, json.dumps(serializable))
        except Exception as exc:
            logger.warning("Failed to save video cache index: %s", exc)

    async def get_range(self, message_id: int, start: int, end: int) -> bytes | None:
        """Return cached bytes for the range, or None if not fully cached."""
        await self.initialize()
        async with self._lock:
            if message_id not in self._index:
                return None
            chunks = self._index[message_id]
            chunk_start = (start // CHUNK_SIZE) * CHUNK_SIZE
            needed_chunks = []
            while chunk_start <= end:
                if chunk_start in chunks:
                    needed_chunks.append(chunk_start)
                else:
                    return None  # Not fully cached
                chunk_start += CHUNK_SIZE

            # All chunks present - read and concatenate, trimming to the
            # requested range (a cached chunk may hold more bytes than the
            # caller asked for, e.g. a 1MiB store served later as 4KiB).
            result = bytearray()
            now = time.time()
            for cs in needed_chunks:
                path, size, _ = chunks[cs]
                # Update last access time
                chunks[cs] = (path, size, now)
                data = await asyncio.to_thread(path.read_bytes)
                chunk_offset = cs
                if chunk_offset < start:
                    data = data[start - chunk_offset :]
                if chunk_offset + len(data) > end + 1:
                    data = data[: end + 1 - chunk_offset]
                result.extend(data)
            await self._save_index()
            return bytes(result)

    async def store_range(self, message_id: int, start: int, data: bytes) -> None:
        """Store a byte range in the cache."""
        await self.initialize()
        async with self._lock:
            if message_id not in self._index:
                self._index[message_id] = {}

            chunk_start = (start // CHUNK_SIZE) * CHUNK_SIZE
            offset = start - chunk_start
            remaining = data
            while remaining:
                chunk_data = remaining[: CHUNK_SIZE - offset]
                chunk_path = self.cache_dir / f"{message_id}_{chunk_start}.cache"
                if offset == 0 and len(chunk_data) == CHUNK_SIZE:
                    # Full chunk - write directly
                    await asyncio.to_thread(chunk_path.write_bytes, chunk_data)
                    size = len(chunk_data)
                    self._index[message_id][chunk_start] = (chunk_path, size, time.time())
                    self._total_size += size
                else:
                    # Partial chunk - read existing, modify, write back
                    existing = b""
                    if chunk_start in self._index[message_id]:
                        existing_path, _, _ = self._index[message_id][chunk_start]
                        if await asyncio.to_thread(existing_path.is_file):
                            existing = await asyncio.to_thread(existing_path.read_bytes)
                    merged = bytearray(existing)
                    if len(merged) < offset + len(chunk_data):
                        merged.extend(b"\x00" * (offset + len(chunk_data) - len(merged)))
                    merged[offset : offset + len(chunk_data)] = chunk_data
                    await asyncio.to_thread(chunk_path.write_bytes, bytes(merged))
                    size = len(merged)
                    self._index[message_id][chunk_start] = (chunk_path, size, time.time())
                    self._total_size += size

                remaining = remaining[len(chunk_data) :]
                chunk_start += CHUNK_SIZE
                offset = 0

            await self._save_index()
            await self._evict_if_needed()

    async def _evict_if_needed(self) -> None:
        """Evict LRU entries if cache exceeds max size."""
        while self._total_size > self.max_size_bytes:
            await self._evict_one()

    async def _evict_one(self) -> bool:
        """Evict the least recently used chunk. Returns True if evicted."""
        oldest_time = float("inf")
        oldest_msg_id = None
        oldest_chunk_start = None

        for msg_id, chunks in self._index.items():
            for chunk_start, (_, _, last_access) in chunks.items():
                if last_access < oldest_time:
                    oldest_time = last_access
                    oldest_msg_id = msg_id
                    oldest_chunk_start = chunk_start

        if oldest_msg_id is None:
            return False

        chunks = self._index[oldest_msg_id]
        path, size, _ = chunks.pop(oldest_chunk_start)
        try:
            await asyncio.to_thread(path.unlink, True)
        except OSError:
            pass
        self._total_size -= size
        if not chunks:
            del self._index[oldest_msg_id]
        await self._save_index()
        return True

    async def evict_message(self, message_id: int) -> None:
        """Remove all cached chunks for a specific message."""
        async with self._lock:
            if message_id in self._index:
                for _, size, _ in self._index[message_id].values():
                    self._total_size -= size
                # Delete chunk files
                for _chunk_start, (path, _, _) in self._index[message_id].items():
                    try:
                        await asyncio.to_thread(path.unlink, True)
                    except OSError:
                        pass
                del self._index[message_id]
                await self._save_index()

    async def evict_old(self) -> int:
        """Evict entries older than max_age_seconds. Returns count evicted."""
        await self.initialize()
        async with self._lock:
            cutoff = time.time() - self.max_age_seconds
            evicted = 0
            for msg_id in list(self._index.keys()):
                chunks = self._index[msg_id]
                for chunk_start in list(chunks.keys()):
                    _, _, last_access = chunks[chunk_start]
                    if last_access < cutoff:
                        path, size, _ = chunks.pop(chunk_start)
                        try:
                            await asyncio.to_thread(path.unlink, True)
                        except OSError:
                            pass
                        self._total_size -= size
                        evicted += 1
                if not chunks:
                    del self._index[msg_id]
            if evicted:
                await self._save_index()
            return evicted

    async def clear(self) -> None:
        """Clear all cached data."""
        async with self._lock:
            for msg_id in list(self._index.keys()):
                for _chunk_start, (path, _, _) in self._index[msg_id].items():
                    try:
                        await asyncio.to_thread(path.unlink, True)
                    except OSError:
                        pass
            self._index.clear()
            self._total_size = 0
            await self._save_index()

    async def stats(self) -> dict[str, object]:
        """Return cache statistics."""
        await self.initialize()
        return {
            "total_size_bytes": self._total_size,
            "max_size_bytes": self.max_size_bytes,
            "cached_messages": len(self._index),
            "cached_chunks": sum(len(c) for c in self._index.values()),
        }
