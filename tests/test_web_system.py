from pathlib import Path

from app.config import Settings
from app.web.system import inspect_storage


def test_storage_health_counts_partial_and_missing_files(tmp_path: Path) -> None:
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    partial = download_dir / "chat" / "42_video.mp4.part"
    partial.parent.mkdir()
    partial.write_bytes(b"partial")
    completed = download_dir / "chat" / "43_photo.jpg"
    completed.write_bytes(b"complete")
    missing = download_dir / "chat" / "44_report.pdf"
    outside = tmp_path / "outside.pdf"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'archive.db'}",
        download_dir=download_dir,
    )
    (tmp_path / "archive.db").write_bytes(b"sqlite")

    health = inspect_storage(
        settings,
        (str(completed), str(missing), str(outside)),
    )

    assert health.database_bytes == 6
    assert health.partial_files == 1
    assert health.partial_bytes == 7
    assert health.missing_completed_files == 2
    assert health.disk_total_bytes > health.disk_free_bytes
