from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import httpx
import pytest

from app.application.media_variants import MediaVariantService, VariantStatus
from app.config import Settings
from app.infrastructure.ffmpeg import (
    FASTSTART_LIMIT,
    FfmpegCapabilities,
    extract_poster,
    hw_decode_enabled,
    moov_offset,
    probe_video_codec,
    remux_faststart,
)
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.repository import ArchiveRepository
from app.infrastructure.transcode import (
    POSTER_SUFFIX,
    VARIANT_SUFFIX,
    VariantManager,
    is_faststart,
)
from app.interfaces.web.app import create_web_app
from tests.helpers import make_chat, make_message


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": f"sqlite:///{tmp_path / 'web.db'}",
        "download_dir": tmp_path / "downloads",
        "tg_session_name": tmp_path / "telegram_session",
        "target_chats": "-1001234567890",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def _seed_video(
    settings: Settings,
    *,
    mime_type: str = "video/mp4",
    media_type: str = "video",
    filename: str = "clip.mp4",
    completed: bool = True,
    telegram_message_id: int = 42,
) -> int:
    database = Database(settings.database_url)
    await database.initialize()
    archive = ArchiveRepository(database)
    await archive.upsert_chat(make_chat(title="Video Room"))
    record, _ = await archive.upsert_message(
        make_message(
            text="video clip",
            media_type=media_type,
            mime_type=mime_type,
            original_filename=filename,
            extension=Path(filename).suffix,
            telegram_message_id=telegram_message_id,
        )
    )
    media_path = settings.download_dir / "video" / filename
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"archived video")
    if completed:
        await archive.mark_download_completed(record.id, media_path, media_path.stat().st_size)
    await database.close()
    return record.id


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _mp4(*, moov_at_front: bool) -> bytes:
    ftyp = _box(b"ftyp", b"isom" + b"\x00" * 8)
    mdat = _box(b"mdat", b"\x00" * (3 * 1024 * 1024))
    moov = _box(b"moov", b"\x00" * 16)
    if moov_at_front:
        return ftyp + moov + mdat
    return ftyp + mdat + moov


async def test_media_cache_headers_and_head_support(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    message_id = await _seed_video(settings)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            media = await client.get(f"/media/{message_id}")
            head = await client.head(f"/media/{message_id}")
            missing = await client.get("/media/999999")

    assert media.status_code == 200
    assert media.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert media.headers["content-disposition"].startswith("inline")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"


async def test_media_non_ascii_filename_serves_without_500(tmp_path: Path) -> None:
    """Chinese filenames must not crash the server (latin-1 header encoding).

    Regression: Content-Disposition carried the raw UTF-8 filename, so every
    request for a Chinese-named TeraBox file raised UnicodeEncodeError and
    returned 500, which made video playback impossible.
    """
    settings = _settings(tmp_path)
    message_id = await _seed_video(settings, filename="视频测试 (1).mp4")
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            media = await client.get(f"/media/{message_id}")
            ranged = await client.get(
                f"/media/{message_id}",
                headers={"Range": "bytes=0-3"},
            )

    assert media.status_code == 200
    assert ranged.status_code == 206
    disposition = media.headers["content-disposition"]
    assert disposition.startswith("inline")
    assert "filename*=UTF-8''" in disposition
    assert "%E8%A7%86%E9%A2%91" in disposition  # percent-encoded 视频, not raw UTF-8
    assert ranged.content == b"arch"


async def test_media_quicktime_served_as_mp4_when_ftyp_is_mp4(tmp_path: Path) -> None:
    """MOV files that are really MP4 containers must play in Chromium.

    Telegram reports .MOV videos as ``video/quicktime``, which Chromium
    refuses (canPlayType returns ""), so the original video could never
    play inline. The brand box says mp42/isom, so the media route must
    serve ``video/mp4`` instead.
    """
    settings = _settings(tmp_path)
    message_id = await _seed_video(settings, mime_type="video/quicktime", filename="clip.MOV")
    application = create_web_app(settings)

    # Rewrite the stored media file to a real MP4 container (brand mp42).
    media_path = settings.download_dir / "video" / "clip.MOV"
    media_path.write_bytes(_box(b"ftyp", b"mp42" + b"\x00" * 8) + _box(b"moov", b"\x00" * 8))

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            media = await client.get(f"/media/{message_id}")

    assert media.status_code == 200
    assert media.headers["content-type"] == "video/mp4"


async def test_media_quicktime_kept_for_actual_qt_files(tmp_path: Path) -> None:
    """Non-MP4 MOV files keep their original MIME type."""
    settings = _settings(tmp_path)
    message_id = await _seed_video(settings, mime_type="video/quicktime", filename="clip.MOV")
    application = create_web_app(settings)

    media_path = settings.download_dir / "video" / "clip.MOV"
    media_path.write_bytes(b"\x00" * 16 + b"not an mp4 container")

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            media = await client.get(f"/media/{message_id}")

    assert media.status_code == 200
    assert media.headers["content-type"] == "video/quicktime"


class _FakeVariantPorts:
    def __init__(self, playable: Path | None, poster: Path | None) -> None:
        self.playable = playable
        self.poster = poster

    async def playable_path(self, path: Path) -> Path | None:
        return self.playable

    def status(self, path: Path) -> VariantStatus:
        return VariantStatus(
            enabled=True,
            ready=self.playable is not None,
            transcoding=self.playable is None,
            codec="hevc",
        )

    async def poster_path(self, path: Path) -> Path | None:
        return self.poster


async def test_variant_and_poster_routes_serve_cached_artifacts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    message_id = await _seed_video(settings)
    application = create_web_app(settings)
    media_path = settings.download_dir / "video" / "clip.mp4"
    variant = media_path.with_name(f"{media_path.stem}{VARIANT_SUFFIX}")
    poster = media_path.with_name(f"{media_path.stem}{POSTER_SUFFIX}")

    async with application.router.lifespan_context(application):
        application.state.media_variants = MediaVariantService(
            enabled=True,
            ports=_FakeVariantPorts(playable=None, poster=None),
        )
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            pending_variant = await client.get(f"/media/{message_id}/variant")
            pending_status = await client.get(f"/media/{message_id}/variant-status")
            pending_poster = await client.get(f"/media/{message_id}/poster")

        variant.write_bytes(b"cached h264 variant")
        poster.write_bytes(b"cached jpeg poster")
        application.state.media_variants = MediaVariantService(
            enabled=True,
            ports=_FakeVariantPorts(playable=variant, poster=poster),
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            served_variant = await client.get(f"/media/{message_id}/variant")
            served_status = await client.get(f"/media/{message_id}/variant-status")
            served_poster = await client.get(f"/media/{message_id}/poster")

    assert pending_variant.status_code == 404
    assert pending_status.status_code == 200
    assert pending_status.headers["cache-control"] == "no-store"
    assert pending_status.json()["transcoding"] is True
    assert pending_status.json()["ready"] is False
    assert pending_poster.status_code == 404
    assert served_variant.status_code == 200
    assert served_variant.content == b"cached h264 variant"
    assert served_variant.headers["content-type"] == "video/mp4"
    assert served_status.json()["ready"] is True
    assert served_poster.status_code == 200
    assert served_poster.content == b"cached jpeg poster"
    assert served_poster.headers["content-type"] == "image/jpeg"


async def test_variant_routes_degrade_when_feature_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, media_variants=False)
    message_id = await _seed_video(settings)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status = await client.get(f"/media/{message_id}/variant-status")
            variant = await client.get(f"/media/{message_id}/variant")
            poster = await client.get(f"/media/{message_id}/poster")

    assert status.status_code == 200
    assert status.json() == {
        "enabled": False,
        "ready": True,
        "transcoding": False,
        "codec": None,
        "progress": None,
        "source_size": 0,
        "variant_size": 0,
        "started_at": None,
    }
    assert variant.status_code == 404
    assert poster.status_code == 404


async def test_terabox_variant_served_from_predictable_sibling_without_db_path(
    tmp_path: Path,
) -> None:
    """Rows completed via the re-publish path have no media_variant_path, but
    the H.264 variant sits at a predictable sibling path on the mount. The
    variant route must serve it and the status route must report it ready."""
    settings = _settings(
        tmp_path,
        storage_mode="terabox",
        terabox_ndus="t",
        terabox_profile=None,
        terabox_mount_dir=tmp_path / "mnt",
        terabox_remote_dir="/Telegram Archive",
    )
    message_id = await _seed_video(settings)
    media_path = settings.download_dir / "video" / "clip.mp4"
    sibling = media_path.with_name(f"{media_path.stem}.h264.mp4")
    sibling.write_bytes(b"cached h264 variant")
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            variant = await client.get(f"/media/{message_id}/variant")
            status = await client.get(f"/media/{message_id}/variant-status")

    assert variant.status_code == 200
    assert variant.content == b"cached h264 variant"
    assert variant.headers["content-type"] == "video/mp4"
    payload = status.json()
    assert payload["enabled"] is True
    assert payload["ready"] is True
    assert payload["transcoding"] is False


async def test_terabox_variant_status_reports_disabled_without_sibling(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        storage_mode="terabox",
        terabox_ndus="t",
        terabox_profile=None,
        terabox_mount_dir=tmp_path / "mnt",
        terabox_remote_dir="/Telegram Archive",
    )
    message_id = await _seed_video(settings)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            variant = await client.get(f"/media/{message_id}/variant")
            status = await client.get(f"/media/{message_id}/variant-status")

    assert variant.status_code == 404
    payload = status.json()
    assert payload["enabled"] is False
    assert payload["ready"] is True


class FakeTeraboxSourceClient:
    """Stand-in for TeraBoxClient behind the /media/{id}/source route."""

    def __init__(self, links: dict[str, tuple[str, int, bool]], *, fail: bool = False) -> None:
        self.links = links
        self.fail = fail
        self.calls: list[str] = []

    async def direct_download_link(self, remote_path: str) -> tuple[str, int, bool] | None:
        self.calls.append(remote_path)
        if self.fail:
            from app.infrastructure.terabox import TeraBoxError

            raise TeraBoxError("dlink refused")
        return self.links.get(remote_path)

    async def aclose(self) -> None:
        return None


async def _seed_mount_video(
    settings: Settings,
    *,
    variant: bool = False,
    completed: bool = True,
) -> int:
    database = Database(settings.database_url)
    await database.initialize()
    archive = ArchiveRepository(database)
    await archive.upsert_chat(make_chat(title="Video Room"))
    record, _ = await archive.upsert_message(
        make_message(
            text="video clip",
            media_type="video",
            mime_type="video/mp4",
            original_filename="clip.mp4",
            extension=".mp4",
            telegram_message_id=42,
        )
    )
    mount_dir = settings.terabox_mount_dir / "Telegram Archive"
    media_path = mount_dir / "clip.mp4"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"remote video")
    if completed:
        variant_path = str(mount_dir / "clip.h264.mp4") if variant else None
        await archive.mark_download_completed(
            record.id, media_path, media_path.stat().st_size, variant_mount_path=variant_path
        )
    await database.close()
    return record.id


def _terabox_settings(tmp_path: Path) -> Settings:
    return _settings(
        tmp_path,
        storage_mode="terabox",
        terabox_ndus="t",
        terabox_profile=None,
        terabox_mount_dir=tmp_path / "mnt",
        terabox_remote_dir="/Telegram Archive",
    )


async def test_media_source_local_mode_returns_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    message_id = await _seed_video(settings)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = (await client.get(f"/media/{message_id}/source")).json()

    assert payload == {"source": "proxy", "url": f"/media/{message_id}"}


async def test_media_source_terabox_returns_direct_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _terabox_settings(tmp_path)
    fake = FakeTeraboxSourceClient(
        {"/Telegram Archive/clip.mp4": ("https://dm-d.terabox.com/file?fid=1", 1234, False)}
    )
    monkeypatch.setattr("app.interfaces.web.app.create_terabox_client", lambda _s: fake)
    message_id = await _seed_mount_video(settings)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = (await client.get(f"/media/{message_id}/source")).json()

    assert payload == {
        "source": "terabox",
        "url": "https://dm-d.terabox.com/file?fid=1",
        "direct": False,
        "media": "original",
        "mime": "video/mp4",
        "size": 1234,
    }
    # The unrecorded sibling is probed first, then falls through to the original.
    assert fake.calls == ["/Telegram Archive/clip.h264.mp4", "/Telegram Archive/clip.mp4"]


async def test_media_source_terabox_prefers_h264_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _terabox_settings(tmp_path)
    fake = FakeTeraboxSourceClient(
        {
            "/Telegram Archive/clip.h264.mp4": (
                "https://kul-ddata.terabox.com/file?fid=2&expires=8h",
                5678,
                True,
            )
        }
    )
    monkeypatch.setattr("app.interfaces.web.app.create_terabox_client", lambda _s: fake)
    message_id = await _seed_mount_video(settings, variant=True)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = (await client.get(f"/media/{message_id}/source")).json()

    assert payload["media"] == "h264"
    assert payload["mime"] == "video/mp4"
    assert payload["url"] == "https://kul-ddata.terabox.com/file?fid=2&expires=8h"
    assert payload["direct"] is True
    assert fake.calls[0] == "/Telegram Archive/clip.h264.mp4"


async def test_media_source_terabox_falls_back_to_proxy_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _terabox_settings(tmp_path)
    fake = FakeTeraboxSourceClient({}, fail=True)
    monkeypatch.setattr("app.interfaces.web.app.create_terabox_client", lambda _s: fake)
    message_id = await _seed_mount_video(settings)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = (await client.get(f"/media/{message_id}/source")).json()

    assert payload == {"source": "proxy", "url": f"/media/{message_id}"}


async def test_media_source_terabox_buffered_local_copy_is_proxied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _terabox_settings(tmp_path)
    fake = FakeTeraboxSourceClient({})
    monkeypatch.setattr("app.interfaces.web.app.create_terabox_client", lambda _s: fake)
    message_id = await _seed_video(settings)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = (await client.get(f"/media/{message_id}/source")).json()

    assert payload == {"source": "proxy", "url": f"/media/{message_id}"}
    assert fake.calls == []


async def test_media_source_missing_or_incomplete_returns_404(tmp_path: Path) -> None:
    settings = _terabox_settings(tmp_path)
    message_id = await _seed_mount_video(settings, completed=False)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            incomplete = await client.get(f"/media/{message_id}/source")
            missing = await client.get("/media/999999/source")

    assert incomplete.status_code == 404
    assert missing.status_code == 404


async def test_moov_offset_and_faststart_detection(tmp_path: Path) -> None:
    front = tmp_path / "front.mp4"
    front.write_bytes(_mp4(moov_at_front=True))
    tail = tmp_path / "tail.mp4"
    tail.write_bytes(_mp4(moov_at_front=False))

    assert await moov_offset(front) == 20
    assert await is_faststart(front)
    assert await moov_offset(tail) == tail.stat().st_size - 24
    assert not await is_faststart(tail)
    assert await moov_offset(tmp_path / "missing.mp4") == 0
    assert await moov_offset(front) <= FASTSTART_LIMIT


async def test_ffmpeg_features_degrade_without_capabilities(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    caps = FfmpegCapabilities(available=False)
    source = tmp_path / "source.mp4"
    source.write_bytes(_mp4(moov_at_front=False))
    poster = tmp_path / "poster.jpg"
    original = source.read_bytes()

    assert await remux_faststart(settings, caps, source) is False
    assert await extract_poster(settings, caps, source, poster) is False
    assert await probe_video_codec(settings, caps, source) is None
    assert source.read_bytes() == original
    assert not poster.exists()


async def test_hw_decode_enabled_follows_video_hwaccel_setting(tmp_path: Path) -> None:
    hw_caps = FfmpegCapabilities(
        available=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        h264_encoders=("h264_rkmpp",),
        hevc_decoder=True,
    )
    sw_caps = FfmpegCapabilities(
        available=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        h264_encoders=("libx264",),
        hevc_decoder=True,
    )
    auto_settings = _settings(tmp_path)
    assert hw_decode_enabled(auto_settings, hw_caps) is True
    assert hw_decode_enabled(auto_settings, sw_caps) is False
    disabled = _settings(tmp_path, video_hwaccel="none")
    assert hw_decode_enabled(disabled, hw_caps) is False


async def test_extract_poster_gates_hwaccel_on_video_hwaccel_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caps = FfmpegCapabilities(
        available=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        h264_encoders=("h264_rkmpp",),
        hevc_decoder=True,
    )
    source = tmp_path / "hevc.mp4"
    source.write_bytes(b"video")
    target = tmp_path / "poster.jpg"
    calls: list[list[str]] = []

    async def fake_run(binary: str, args: list[str], settings: Settings) -> tuple[int, str, str]:
        calls.append(args)
        if str(args[-1]).endswith(".part"):
            await asyncio.to_thread(Path(args[-1]).write_bytes, b"jpeg")
        return 0, "", ""

    monkeypatch.setattr("app.infrastructure.ffmpeg._run", fake_run)

    disabled = _settings(tmp_path, video_hwaccel="none")
    assert await extract_poster(disabled, caps, source, target) is True
    assert "-hwaccel" not in calls[0]
    assert calls[0][-3:-1] == ["-f", "image2"]
    assert target.read_bytes() == b"jpeg"

    calls.clear()
    assert await extract_poster(_settings(tmp_path), caps, source, target) is True
    assert "-hwaccel" in calls[0]


class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def read(self) -> bytes:
        return b"".join(self._lines)


class _FakeProc:
    def __init__(self, *, returncode: int, stdout: list[bytes], stderr: str = "") -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream([stderr.encode()])
        self.returncode = returncode
        self.args: list[str] = []

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return await self.stdout.read(), await self.stderr.read()


async def test_variant_manager_transcode_omits_hwaccel_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, video_hwaccel="none")
    caps = FfmpegCapabilities(
        available=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        h264_encoders=("h264_rkmpp",),
        hevc_decoder=True,
    )
    manager = VariantManager(settings, caps)
    source = tmp_path / "hevc.mp4"
    temp = source.with_name(f"{source.stem}{VARIANT_SUFFIX}.part")
    calls: list[list[str]] = []

    async def fake_exec(binary: str, *args: object, **kwargs: object) -> _FakeProc:
        calls.append([str(arg) for arg in args])
        if "-show_entries" in args:
            return _FakeProc(returncode=0, stdout=[b"10.0\n"])
        return _FakeProc(returncode=0, stdout=[b"out_time_us=1000000\n"])

    monkeypatch.setattr("app.infrastructure.transcode.asyncio.create_subprocess_exec", fake_exec)
    state: dict[str, object] = {}
    returncode, stderr = await manager._run_transcode(
        source, temp, ["-c:v", "h264_rkmpp", "-rc_mode", "VBR", "-qp_init", "26"], state
    )

    assert returncode == 0
    assert stderr == ""
    assert state["progress"] == 0.1
    assert "-hwaccel" not in calls[1]
    assert "-hwaccel" not in calls[0]


async def test_variant_manager_remote_transcode_uses_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(
        tmp_path,
        video_hwaccel="none",
        ffmpeg_remote_host="namhh@192.168.1.2",
        ffmpeg_remote_bin="/usr/local/bin/ffmpeg",
        ffmpeg_remote_identity="/app/.ssh/id_ed25519",
        ffmpeg_remote_known_hosts="/app/.ssh/known_hosts",
        host_download_dir="/mnt/disk2/telegram-archiver/downloads",
    )
    caps = FfmpegCapabilities(
        available=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        h264_encoders=("h264_rkmpp",),
        hevc_decoder=True,
    )
    manager = VariantManager(settings, caps)
    source = settings.download_dir / "video room" / "hevc.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"archived video")
    temp = source.with_name(f"{source.stem}{VARIANT_SUFFIX}.part")
    calls: list[list[str]] = []
    envs: list[dict[str, str]] = []

    async def fake_exec(binary: str, *args: object, **kwargs: object) -> _FakeProc:
        calls.append([binary, *(str(arg) for arg in args)])
        envs.append(kwargs.get("env", {}))
        if "-show_entries" in args:
            return _FakeProc(returncode=0, stdout=[b"10.0\n"])
        return _FakeProc(returncode=0, stdout=[b"out_time_us=5000000\n"])

    monkeypatch.setattr("app.infrastructure.transcode.asyncio.create_subprocess_exec", fake_exec)
    state: dict[str, object] = {}
    returncode, stderr = await manager._run_transcode(
        source, temp, ["-c:v", "h264_rkmpp", "-rc_mode", "VBR", "-qp_init", "26"], state
    )

    assert returncode == 0
    assert stderr == ""
    assert state["progress"] == 0.5
    cmd = calls[0]
    assert cmd[0] == "ssh"
    assert cmd[1] == "-o" and cmd[2] == "BatchMode=yes"
    assert "namhh@192.168.1.2" in cmd
    assert "-i" in cmd and "/app/.ssh/id_ed25519" in cmd
    assert "UserKnownHostsFile=/app/.ssh/known_hosts" in cmd
    host_index = cmd.index("namhh@192.168.1.2")
    assert cmd[host_index + 1] == "/usr/local/bin/ffmpeg"
    assert "-hwaccel" in cmd and "rkmpp" in cmd
    host_source = "/mnt/disk2/telegram-archiver/downloads/video room/hevc.mp4"
    assert "'" + host_source + "'" in cmd
    assert str(temp).startswith(str(settings.download_dir))
    assert "LD_LIBRARY_PATH" not in envs[0]


async def test_variant_manager_remote_transcode_falls_back_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(
        tmp_path,
        video_hwaccel="none",
        ffmpeg_remote_host="namhh@192.168.1.2",
        host_download_dir="/mnt/disk2/telegram-archiver/downloads",
    )
    caps = FfmpegCapabilities(
        available=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        h264_encoders=("h264_rkmpp",),
        hevc_decoder=True,
    )
    manager = VariantManager(settings, caps)
    source = settings.download_dir / "video" / "hevc.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"archived video")
    temp = source.with_name(f"{source.stem}{VARIANT_SUFFIX}.part")
    binaries: list[str] = []

    async def fake_exec(binary: str, *args: object, **kwargs: object) -> _FakeProc:
        binaries.append(binary)
        if "-show_entries" in args:
            return _FakeProc(returncode=0, stdout=[b"10.0\n"])
        if binary == "ssh":
            return _FakeProc(
                returncode=255,
                stdout=[],
                stderr="ssh: connect to host 192.168.1.2 port 22: Connection refused",
            )
        return _FakeProc(returncode=0, stdout=[b"out_time_us=1000000\n"])

    monkeypatch.setattr("app.infrastructure.transcode.asyncio.create_subprocess_exec", fake_exec)
    returncode, _ = await manager._run_transcode(source, temp, ["-c:v", "h264_rkmpp"], {})

    assert binaries.count("ssh") == 1
    assert binaries.count("ffmpeg") == 1
    assert binaries[0] == "ssh"
    assert returncode == 0


async def test_variant_manager_playable_path_keeps_h264_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    caps = FfmpegCapabilities(
        available=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        h264_encoders=("libx264",),
        hevc_decoder=True,
    )
    manager = VariantManager(settings, caps)
    source = tmp_path / "h264.mp4"
    source.write_bytes(b"video")

    async def fake_codec(settings: Settings, caps: FfmpegCapabilities, path: Path) -> str | None:
        return "h264"

    monkeypatch.setattr("app.infrastructure.transcode.probe_video_codec", fake_codec)
    assert await manager.playable_path(source) == source
    assert manager.status(source).enabled is True
    assert manager.status(source).ready is False


async def test_variant_manager_returns_none_while_hevc_transcodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    caps = FfmpegCapabilities(
        available=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        h264_encoders=("libx264",),
        hevc_decoder=True,
    )
    manager = VariantManager(settings, caps)
    source = tmp_path / "hevc.mp4"
    source.write_bytes(b"video")

    async def fake_codec(settings: Settings, caps: FfmpegCapabilities, path: Path) -> str | None:
        return "hevc"

    monkeypatch.setattr("app.infrastructure.transcode.probe_video_codec", fake_codec)
    assert await manager.playable_path(source) is None
    assert source.with_name(f"{source.stem}{VARIANT_SUFFIX}") == manager.variant_path(source)
    await asyncio.gather(*manager._tasks.values(), return_exceptions=True)


async def test_completed_video_paths_filters_by_type_and_status(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    video_id = await _seed_video(settings, filename="one.mp4")
    database = Database(settings.database_url)
    await database.initialize()
    archive = ArchiveRepository(database)
    await archive.upsert_chat(make_chat(title="More Room"))
    other, _ = await archive.upsert_message(
        make_message(
            telegram_message_id=44,
            media_type="video",
            mime_type="video/mp4",
            original_filename="two.mp4",
            extension=".mp4",
        )
    )
    other_path = settings.download_dir / "video" / "two.mp4"
    other_path.parent.mkdir(parents=True, exist_ok=True)
    other_path.write_bytes(b"video two")
    await archive.mark_download_completed(other.id, other_path, other_path.stat().st_size)
    document, _ = await archive.upsert_message(
        make_message(
            telegram_message_id=45,
            media_type="document",
            mime_type="application/pdf",
            original_filename="paper.pdf",
            extension=".pdf",
        )
    )
    document_path = settings.download_dir / "video" / "paper.pdf"
    document_path.write_bytes(b"pdf")
    await archive.mark_download_completed(document.id, document_path, document_path.stat().st_size)
    failed, _ = await archive.upsert_message(
        make_message(
            telegram_message_id=46,
            media_type="video",
            mime_type="video/mp4",
            original_filename="broken.mp4",
            extension=".mp4",
        )
    )
    await archive.mark_download_failed(failed.id, "network error")
    await database.close()

    paths = await ArchiveRepository(Database(settings.database_url)).completed_video_paths()
    assert len(paths) == 2
    assert "one.mp4" in paths[0][0]
    assert "two.mp4" in paths[1][0]
    assert "paper.pdf" not in paths[0][0]
    assert video_id > 0


async def test_optimize_media_operation_faststarts_and_writes_posters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    message_id = await _seed_video(settings)
    application = create_web_app(settings)
    media_path = settings.download_dir / "video" / "clip.mp4"
    poster_path = media_path.with_name(f"{media_path.stem}{POSTER_SUFFIX}")
    progress_updates: list[dict[str, object]] = []

    caps = FfmpegCapabilities(
        available=True,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        h264_encoders=("libx264",),
        hevc_decoder=True,
    )
    real_probe = "app.interfaces.web.commands.probe_capabilities"

    async def fake_probe(settings: Settings) -> FfmpegCapabilities:
        return caps

    async def fake_faststart(settings: Settings, caps: FfmpegCapabilities, path: Path) -> bool:
        await asyncio.to_thread(_append_faststart, path)
        return True

    async def fake_poster(
        settings: Settings,
        caps: FfmpegCapabilities,
        source: Path,
        target: Path,
    ) -> bool:
        await asyncio.to_thread(target.write_bytes, b"jpeg poster")
        return True

    async def fake_is_faststart(path: Path) -> bool:
        return False

    def _append_faststart(path: Path) -> None:
        path.write_bytes(path.read_bytes() + b"+faststart")

    monkeypatch.setattr(real_probe, fake_probe)
    monkeypatch.setattr("app.interfaces.web.commands.remux_faststart", fake_faststart)
    monkeypatch.setattr("app.interfaces.web.commands.extract_poster", fake_poster)
    monkeypatch.setattr("app.interfaces.web.commands.is_faststart", fake_is_faststart)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            started = await client.post(
                "/operations/start",
                data={
                    "csrf_token": application.state.csrf_token,
                    "command": "optimize-media",
                },
            )
            job_id = int(started.headers["location"].split("job=")[1].split("&")[0])
            for _ in range(100):
                status = await client.get(f"/api/v1/operations/{job_id}")
                if status.json()["operation"]["terminal"]:
                    break
                await asyncio.sleep(0.01)
            operation = status.json()["operation"]
            progress_updates.append(operation)

    assert started.status_code == 303
    assert operation["status"] == "completed"
    assert "1 faststarted, 1 posters written" in operation["detail"]
    assert progress_updates[-1]["progress_current"] == 1
    assert progress_updates[-1]["progress_total"] == 1
    assert b"+faststart" in media_path.read_bytes()
    assert poster_path.read_bytes() == b"jpeg poster"
    assert message_id > 0


async def test_variant_status_endpoint_returns_json(tmp_path: Path) -> None:
    settings = _settings(tmp_path, media_variants=True)
    message_id = await _seed_video(settings)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status = await client.get(f"/media/{message_id}/variant-status")
            variant = await client.get(f"/media/{message_id}/variant")
            poster = await client.get(f"/media/{message_id}/poster")

    assert status.status_code == 200
    payload = status.json()
    assert payload["enabled"] is True
    assert payload["ready"] is False
    assert payload["transcoding"] is False
    assert "codec" in payload
    assert variant.status_code == 404
    assert poster.status_code == 404


async def test_variant_status_endpoint_disabled_setting(tmp_path: Path) -> None:
    settings = _settings(tmp_path, media_variants=False)
    message_id = await _seed_video(settings)
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            status = await client.get(f"/media/{message_id}/variant-status")

    assert status.status_code == 200
    payload = status.json()
    assert payload["enabled"] is False
    assert payload["ready"] is True


async def test_gallery_and_player_templates_guard_variant_artifacts(
    tmp_path: Path,
) -> None:
    enabled_settings = _settings(tmp_path, media_variants=True)
    enabled_id = await _seed_video(enabled_settings)
    image_id = await _seed_video(
        enabled_settings,
        mime_type="image/jpeg",
        media_type="photo",
        filename="snap.jpg",
        telegram_message_id=44,
    )
    disabled_settings = _settings(tmp_path / "disabled", media_variants=False)
    disabled_id = await _seed_video(disabled_settings)
    enabled_app = create_web_app(enabled_settings)
    disabled_app = create_web_app(disabled_settings)

    async with enabled_app.router.lifespan_context(enabled_app):
        transport = httpx.ASGITransport(app=enabled_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            gallery = await client.get(f"/chats/{-1001234567890}/media")
            conversation = await client.get(f"/chats/{-1001234567890}")
            detail = await client.get(f"/messages/{enabled_id}")

    async with disabled_app.router.lifespan_context(disabled_app):
        transport = httpx.ASGITransport(app=disabled_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            disabled_gallery = await client.get(f"/chats/{-1001234567890}/media")
            disabled_conversation = await client.get(f"/chats/{-1001234567890}")
            disabled_detail = await client.get(f"/messages/{disabled_id}")

    assert gallery.status_code == 200
    assert "media-grid-poster" in gallery.text
    assert f'data-variant-url="/media/{enabled_id}/variant"' in gallery.text
    assert f'data-variant-url="/media/{image_id}/variant"' not in gallery.text
    assert f'data-media-kind="image" data-media-src="/media/{image_id}"' in gallery.text
    assert 'poster="/media/' in conversation.text
    assert 'poster="/media/' in detail.text
    assert disabled_gallery.status_code == 200
    # In TeraBox mode, video thumbnails are generated locally even when media_variants is disabled
    assert "media-grid-poster" in disabled_gallery.text
    assert "data-variant-url" not in disabled_gallery.text
    assert "poster=" not in disabled_conversation.text
    assert "poster=" not in disabled_detail.text
    assert "data-variant-url" not in disabled_conversation.text
    assert "data-variant-url" not in disabled_detail.text


async def test_terabox_templates_always_offer_variant_url(tmp_path: Path) -> None:
    """In TeraBox mode the H.264 variant may exist on the mount even when
    media_variants is disabled and media_variant_path is unset (re-published
    rows), so templates must always attach the variant URLs for videos."""
    settings = _settings(
        tmp_path,
        storage_mode="terabox",
        terabox_ndus="t",
        terabox_profile=None,
        terabox_mount_dir=tmp_path / "mnt",
        terabox_remote_dir="/Telegram Archive",
    )
    message_id = await _seed_video(settings)
    image_id = await _seed_video(
        settings,
        mime_type="image/jpeg",
        media_type="photo",
        filename="snap.jpg",
        telegram_message_id=44,
    )
    application = create_web_app(settings)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            gallery = await client.get(f"/chats/{-1001234567890}/media")
            conversation = await client.get(f"/chats/{-1001234567890}")
            detail = await client.get(f"/messages/{message_id}")

    assert f'data-variant-url="/media/{message_id}/variant"' in gallery.text
    assert f'data-variant-url="/media/{image_id}/variant"' not in gallery.text
    assert f'data-variant-url="/media/{message_id}/variant"' in conversation.text
    assert f'data-variant-url="/media/{message_id}/variant"' in detail.text


async def test_optimize_media_operation_fails_without_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    await _seed_video(settings)
    application = create_web_app(settings)

    async def fake_probe(settings: Settings) -> FfmpegCapabilities:
        return FfmpegCapabilities(available=False)

    monkeypatch.setattr("app.interfaces.web.commands.probe_capabilities", fake_probe)

    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            started = await client.post(
                "/operations/start",
                data={
                    "csrf_token": application.state.csrf_token,
                    "command": "optimize-media",
                },
            )
            job_id = int(started.headers["location"].split("job=")[1].split("&")[0])
            for _ in range(100):
                status = await client.get(f"/api/v1/operations/{job_id}")
                if status.json()["operation"]["terminal"]:
                    break
                await asyncio.sleep(0.01)

    assert started.status_code == 303
    assert status.json()["operation"]["status"] == "failed"
    assert "ffmpeg is not available" in status.json()["operation"]["error"]
