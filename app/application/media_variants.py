"""Application policy for streaming playable video variants and posters.

The web layer decides which bytes a player receives: H.264 originals stream
directly, HEVC originals are served as transcoded H.264 variants once ready,
and video grids display cached JPEG posters. All ffmpeg work happens through
injected adapters so use-case tests never need a real ffmpeg binary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class VariantStatus:
    enabled: bool
    ready: bool
    transcoding: bool
    codec: str | None = None
    progress: float | None = None
    source_size: int = 0
    variant_size: int = 0
    started_at: float | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class VariantPorts(Protocol):
    """Infrastructure adapters injected into the service (ports and adapters)."""

    async def playable_path(self, path: Path) -> Path | None: ...
    def status(self, path: Path) -> VariantStatus: ...
    async def poster_path(self, path: Path) -> Path | None: ...


class MediaVariantService:
    """Gate variant/poster behavior behind the ``media_variants`` setting."""

    def __init__(
        self,
        *,
        enabled: bool,
        ports: VariantPorts,
    ) -> None:
        self.enabled = enabled
        self.ports = ports

    async def playable_path(self, path: Path) -> Path | None:
        """Path to stream: the original when playable, the variant otherwise.

        Returns None while an H.264 variant is still being produced.
        """
        if not self.enabled:
            return path
        return await self.ports.playable_path(path)

    def status(self, path: Path) -> VariantStatus:
        if not self.enabled:
            return VariantStatus(enabled=False, ready=True, transcoding=False)
        return self.ports.status(path)

    async def poster_path(self, path: Path) -> Path | None:
        """Cached JPEG poster path, generating it on first request."""
        if not self.enabled:
            return None
        return await self.ports.poster_path(path)
