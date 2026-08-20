"""Background continuous buffering of TeraBox media into the local disk cache.

TeraBox caps this account's download bandwidth (measured ~6 KB/s per
connection, ~25 KB/s aggregate even with parallel fresh dlinks), so a
single relayed stream cannot sustain real-time playback of large videos.
This prefetcher keeps pulling the file into :class:`VideoRangeCache` from
the CDN in the background while the user watches; playback reads cached
ranges from local disk at full speed and rewinds/re-watches are instant.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.infrastructure.terabox import TeraBoxClient, TeraBoxError
from app.infrastructure.video_cache import VideoRangeCache

logger = logging.getLogger(__name__)

#: Sub-ranges fetched concurrently inside one prefetch window. TeraBox caps
#: per-connection bandwidth, so a handful of parallel connections roughly
#: multiplies the aggregate; more just re-arms the CDN throttle.
PARALLEL_FETCHES = 4
#: Bytes pulled per prefetch window (4 MiB = 16 cache chunks).
PREFETCH_WINDOW_BYTES = 4 * 1024 * 1024
#: Stop prefetching a message after this long without any stream request.
IDLE_TIMEOUT_SECONDS = 10 * 60
#: Consecutive window failures before giving up (restarted by the next request).
MAX_CONSECUTIVE_FAILURES = 3


class TeraBoxPrefetcher:
    """Runs one background CDN→disk cache fill per watched message."""

    def __init__(self, client: TeraBoxClient, cache: VideoRangeCache) -> None:
        self._client = client
        self._cache = cache
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._last_touch: dict[int, float] = {}

    def ensure(self, message_id: int, remote_path: str, size: int) -> None:
        """Start (or keep alive) the background fill for one message.

        Idempotent: repeated calls while a fill is running only refresh the
        idle clock. After an idle timeout or a failure the task exits, and
        the next call restarts it from where the cache already has chunks.
        """
        self._last_touch[message_id] = time.monotonic()
        if message_id in self._tasks:
            return
        self._tasks[message_id] = asyncio.create_task(
            self._run(message_id, remote_path, size)
        )

    def forget(self, message_id: int) -> None:
        """Stop prefetching a message (e.g. on deletion)."""
        task = self._tasks.pop(message_id, None)
        if task is not None:
            task.cancel()
        self._last_touch.pop(message_id, None)

    def active(self) -> int:
        return len(self._tasks)

    async def _run(self, message_id: int, remote_path: str, size: int) -> None:
        try:
            await self._fill(message_id, remote_path, size)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("TeraBox prefetch failed for message %s: %s", message_id, exc)
        finally:
            self._tasks.pop(message_id, None)

    async def _fill(self, message_id: int, remote_path: str, size: int) -> None:
        failures = 0
        window_start = 0
        while window_start < size:
            last_touch = self._last_touch.get(message_id)
            if last_touch is not None and time.monotonic() - last_touch > IDLE_TIMEOUT_SECONDS:
                logger.info("TeraBox prefetch idle; pausing message %s", message_id)
                return
            window_end = min(size, window_start + PREFETCH_WINDOW_BYTES)
            if await self._cache.is_cached(message_id, window_start, window_end - 1):
                window_start = window_end
                continue
            got = await self._fetch_window(message_id, remote_path, window_start, window_end)
            if got > 0:
                failures = 0
                window_start = window_end
                continue
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    "TeraBox prefetch giving up on message %s after %s failed windows",
                    message_id,
                    failures,
                )
                return
            await asyncio.sleep(min(5.0 * failures, 30.0))
        logger.info("TeraBox prefetch completed for message %s (%s bytes)", message_id, size)

    async def _fetch_window(
        self, message_id: int, remote_path: str, window_start: int, window_end: int
    ) -> int:
        """Fetch one window with parallel sub-range connections; return bytes stored."""
        width = window_end - window_start
        sub_size = max(1, width // PARALLEL_FETCHES)
        offsets = [
            window_start + index * sub_size for index in range(PARALLEL_FETCHES)
        ]
        results = await asyncio.gather(
            *(
                self._fetch_sub(message_id, remote_path, offset, min(offset + sub_size, window_end))
                for offset in offsets
            )
        )
        return sum(results)

    async def _fetch_sub(
        self, message_id: int, remote_path: str, start: int, end: int
    ) -> int:
        """Stream one byte range from the CDN into the cache; return bytes stored."""
        if start >= end:
            return 0
        try:
            response, _size = await self._client.stream_remote(
                remote_path, range_spec=f"bytes={start}-{end - 1}"
            )
        except TeraBoxError as exc:
            logger.info("TeraBox prefetch sub-range %s-%s failed: %s", start, end, exc)
            return 0
        stored = 0
        try:
            async for chunk in response.aiter_bytes(256 * 1024):
                await self._cache.store_range(message_id, start + stored, chunk)
                stored += len(chunk)
        except Exception as exc:
            logger.info("TeraBox prefetch sub-range %s-%s interrupted: %s", start, end, exc)
        finally:
            await response.aclose()
        return stored