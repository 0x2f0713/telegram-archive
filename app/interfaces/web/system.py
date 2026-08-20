"""Safe filesystem health inspection for the local dashboard."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import Settings


@dataclass(frozen=True, slots=True)
class StorageHealth:
    database_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    partial_files: int
    partial_bytes: int
    missing_completed_files: int

    @property
    def disk_used_percent(self) -> float:
        if not self.disk_total_bytes:
            return 0.0
        return (self.disk_total_bytes - self.disk_free_bytes) / self.disk_total_bytes * 100


def inspect_storage(
    settings: Settings,
    completed_paths: tuple[str, ...],
    remote_quota: tuple[int, int] | None = None,
) -> StorageHealth:
    """Inspect only configured archive paths and return redaction-safe totals.

    With a ``remote_quota`` (TeraBox ``total, used`` bytes) the free-space
    figures describe the cloud volume instead of the local disk. Missing-file
    detection checks only local media buffers; remote-only TeraBox objects are
    validated through the API health check instead.
    """

    roots = settings.media_storage_roots()
    download_root = roots[0]

    if remote_quota is not None:
        disk_total_bytes, disk_used_bytes = remote_quota
        disk_free_bytes = max(0, disk_total_bytes - disk_used_bytes)
    else:
        disk_probe = download_root
        while not disk_probe.exists() and disk_probe != disk_probe.parent:
            disk_probe = disk_probe.parent
        disk = shutil.disk_usage(disk_probe)
        disk_total_bytes, disk_free_bytes = disk.total, disk.free

    database_name = make_url(settings.database_url).database
    database_path = Path(database_name).expanduser().resolve() if database_name else None
    database_bytes = (
        database_path.stat().st_size if database_path and database_path.is_file() else 0
    )

    partial_files = 0
    partial_bytes = 0
    if download_root.is_dir():
        for partial in download_root.rglob("*.part"):
            if partial.is_file():
                partial_files += 1
                try:
                    partial_bytes += partial.stat().st_size
                except OSError:
                    continue

    missing = 0
    for raw_path in completed_paths:
        media_path = Path(raw_path).expanduser().resolve()
        contained = any(root == media_path or root in media_path.parents for root in roots)
        if not contained:
            missing += 1
        elif not media_path.is_file():
            missing += 1

    return StorageHealth(
        database_bytes=database_bytes,
        disk_total_bytes=disk_total_bytes,
        disk_free_bytes=disk_free_bytes,
        partial_files=partial_files,
        partial_bytes=partial_bytes,
        missing_completed_files=missing,
    )
