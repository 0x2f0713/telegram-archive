"""Stop-aware async waiting used by every long-running archive workflow."""

from __future__ import annotations

import asyncio


async def wait_or_stop(stop_event: asyncio.Event | None, seconds: int) -> bool:
    """Sleep until a stop is requested; return whether the stop fired."""

    if stop_event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except TimeoutError:
        return False
