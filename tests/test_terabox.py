from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.infrastructure import terabox as terabox_module
from app.infrastructure.terabox import (
    FileHashes,
    TeraBoxAuthError,
    TeraBoxClient,
    TeraBoxMediaDeleter,
    TeraBoxTransientError,
    TeraBoxUploader,
    create_terabox_client,
    decode_etag,
    hash_file,
    remote_path_for,
    sanitize_remote_component,
)

CHUNK = 4 * 1024 * 1024


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "storage_mode": "terabox",
        "terabox_ndus": "test-token",
        "terabox_profile": None,
        "download_dir": tmp_path / "downloads",
        "terabox_mount_dir": tmp_path / "mnt",
        "terabox_remote_dir": "/Telegram Archive",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _client(settings: Settings, handler) -> TeraBoxClient:
    return TeraBoxClient(settings, "test-token", transport=httpx.MockTransport(handler))


def _json(content: object) -> httpx.Response:
    return httpx.Response(200, json=content)


def _scramble_etag(plain_md5: str) -> str:
    """Mirror TeraBox's scrambled-etag encoding so decode_etag can be tested."""

    swapped = plain_md5[8:16] + plain_md5[0:8] + plain_md5[24:32] + plain_md5[16:24]
    chars: list[str] = []
    for index, char in enumerate(swapped):
        value = int(char, 16) ^ (index & 15)
        chars.append(chr(value + ord("g")) if index == 9 else format(value, "x"))
    return "".join(chars)


def _form(request: httpx.Request) -> dict[str, str]:
    from urllib.parse import unquote_plus

    return {
        key: unquote_plus(value)
        for key, value in (
            pair.split("=", 1) for pair in request.read().decode().split("&") if pair
        )
    }


class UploadRecorder:
    """Routes every TeraBox protocol call; records the full chunked upload."""

    def __init__(self, *, rapid_errno: int = 31079, precreate_return_type: int = 1) -> None:
        self.requests: list[httpx.Request] = []
        self.chunk_bodies: dict[int, bytes] = {}
        self.remote: dict[str, int] = {}
        self.precreate_form: dict[str, str] = {}
        self.commit_form: dict[str, str] = {}
        self._rapid_errno = rapid_errno
        self._precreate_return_type = precreate_return_type

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/main":
            return httpx.Response(
                200,
                text='<script>var templateData = {"jsToken": "%28%22tok123%22%29"};</script>',
            )
        if path == "/rest/2.0/pcs/file":
            return _json({"host": "c-test.terabox.com"})
        if path == "/api/rapidupload":
            return _json({"errno": self._rapid_errno})
        if path == "/api/precreate":
            self.precreate_form = _form(request)
            return _json(
                {"errno": 0, "uploadid": "P1-test", "return_type": self._precreate_return_type}
            )
        if path == "/rest/2.0/pcs/superfile2":
            partseq = int(request.url.params["partseq"])
            body = request.read()
            start = body.find(b"\r\n\r\n") + 4
            end = body.rfind(b"\r\n--")
            payload = body[start:end]
            self.chunk_bodies[partseq] = payload
            return _json({"md5": hashlib.md5(payload).hexdigest()})
        if path == "/api/create":
            if request.url.query == "a=commit":
                return _json({"errno": 0})
            self.commit_form = _form(request)
            return _json(
                {"errno": 0, "md5": _scramble_etag(self.commit_form.get("content-md5", ""))}
            )
        if path == "/api/list":
            listed = [
                {"path": remote_path, "size": size, "isdir": 0}
                for remote_path, size in self.remote.items()
            ]
            return _json({"errno": 0, "list": listed, "has_more": False})
        if path == "/api/check/login":
            return _json({"errno": 0})
        if path == "/api/quota":
            return _json({"errno": 0, "total": 1000, "used": 400})
        if path == "/api/filemanager":
            return _json({"errno": 0})
        return _json({"errno": 0})


async def test_decode_etag_inverts_scrambling() -> None:
    plain = hashlib.md5(b"telegram archive").hexdigest()

    assert decode_etag(_scramble_etag(plain)) == plain
    assert decode_etag("short") == "short"


def test_decode_etag_matches_reference_vectors() -> None:
    # Vectors generated with the terabox-api JavaScript DecodeMd5 implementation.
    assert decode_etag("e13dabb7dn1df6548e7988a41a60ef54") == "5eb63bbbe01eeed093cb22bb8f5acdc3"
    assert decode_etag("7964deeb0o6d26829f0fd0b97accf48d") == "81c6eb6d78479b8cf36739629e2c95de"


def test_hash_file_streams_full_slice_chunk_and_crc(tmp_path: Path) -> None:
    chunk_size = 8
    payload = bytes(range(256)) * 3  # 768 bytes → 96 chunks of 8 bytes
    path = tmp_path / "file.bin"
    path.write_bytes(payload)

    hashes = hash_file(path, chunk_size)

    assert hashes.size == 768
    assert hashes.file_md5 == hashlib.md5(payload).hexdigest()
    # The file is smaller than the 256 KiB slice window, so the slice digest
    # covers the whole file.
    assert hashes.slice_md5 == hashlib.md5(payload).hexdigest()
    assert len(hashes.chunk_md5s) == 96
    assert hashes.chunk_md5s[0] == hashlib.md5(payload[:8]).hexdigest()
    assert hashes.chunk_md5s[-1] == hashlib.md5(payload[-8:]).hexdigest()
    expected_chunks_json = json.dumps(list(hashes.chunk_md5s), separators=(",", ":"))
    expected_etag_md5 = hashlib.md5(expected_chunks_json.encode()).hexdigest()
    assert hashes.etag == f"{expected_etag_md5}-96"

    small = tmp_path / "small.bin"
    small.write_bytes(b"a" * 10)
    small_hashes = hash_file(small, chunk_size)
    assert small_hashes.chunk_md5s == (
        hashlib.md5(b"a" * 8).hexdigest(),
        hashlib.md5(b"aa").hexdigest(),
    )
    assert (
        small_hashes.etag
        == hashlib.md5(
            json.dumps(list(small_hashes.chunk_md5s), separators=(",", ":")).encode()
        ).hexdigest()
        + "-2"
    )


def test_hash_file_exact_chunk_boundary(tmp_path: Path) -> None:
    path = tmp_path / "exact.bin"
    path.write_bytes(b"x" * 16)

    hashes = hash_file(path, 8)

    assert hashes.chunk_md5s == (
        hashlib.md5(b"x" * 8).hexdigest(),
        hashlib.md5(b"x" * 8).hexdigest(),
    )


def test_remote_path_for_maps_buffer_layout_to_remote_root(tmp_path: Path) -> None:
    base = tmp_path / "downloads"
    media = base / "-1001_Room" / "2026" / "08" / "16" / "42_clip.mp4"

    remote = remote_path_for(base, "/Telegram Archive", media)

    assert remote == "/Telegram Archive/-1001_Room/2026/08/16/42_clip.mp4"


def test_remote_path_for_strips_terabox_rejected_characters(tmp_path: Path) -> None:
    base = tmp_path / "downloads"
    media = base / '-1001_Room🎀 / has:dots"and?stars*' / "2026" / "08" / "16" / "42_clip.mp4"

    remote = remote_path_for(base, "/Telegram Archive", media)

    assert "🎀" not in remote
    for forbidden in ':?"*':
        assert forbidden not in remote
    assert remote.startswith("/Telegram Archive")
    assert remote.endswith("2026/08/16/42_clip.mp4")


def test_sanitize_remote_component_replaces_invalid_characters() -> None:
    assert sanitize_remote_component('a:b?c"d*e<f>g|h') == "a_b_c_d_e_f_g_h"
    sanitized = sanitize_remote_component("clip🎀.mp4")
    assert "🎀" not in sanitized and sanitized == "clip_.mp4"
    assert sanitize_remote_component("🎀🎀🎀") == "unnamed"


async def test_upload_file_uses_rapid_dedupe_and_validates(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    recorder = UploadRecorder(rapid_errno=0)
    payload = b"d" * (256 * 1024 + 10)  # >= 256 KiB so rapid upload is attempted
    local = tmp_path / "a.bin"
    local.write_bytes(payload)
    recorder.remote["/Telegram Archive/x.bin"] = len(payload)
    client = _client(settings, recorder)

    hashes = await client.upload_file(local, "/Telegram Archive/x.bin")

    assert hashes.file_md5 == hashlib.md5(payload).hexdigest()
    paths = [request.url.path for request in recorder.requests]
    assert paths == ["/main", "/api/rapidupload", "/api/list"]
    await client.aclose()


async def test_upload_file_rejects_size_mismatch_after_dedupe(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    recorder = UploadRecorder(rapid_errno=0)
    payload = b"d" * (256 * 1024 + 10)
    recorder.remote["/Telegram Archive/x.bin"] = 5  # wrong size
    local = tmp_path / "a.bin"
    local.write_bytes(payload)
    client = _client(settings, recorder)

    with pytest.raises(RuntimeError, match="remote size"):
        await client.upload_file(local, "/Telegram Archive/x.bin")
    await client.aclose()


async def test_upload_file_rejects_empty_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    recorder = UploadRecorder()
    local = tmp_path / "empty.bin"
    local.write_bytes(b"")
    client = _client(settings, recorder)

    with pytest.raises(RuntimeError, match="empty"):
        await client.upload_file(local, "/Telegram Archive/empty.bin")
    await client.aclose()


async def test_upload_file_chunked_flow(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    recorder = UploadRecorder()
    payload = b"A" * 5
    local = tmp_path / "chunk.bin"
    local.write_bytes(payload)
    recorder.remote["/Telegram Archive/chunk.bin"] = len(payload)
    client = _client(settings, recorder)

    hashes = await client.upload_file(local, "/Telegram Archive/chunk.bin")

    assert hashes.size == 5
    paths = [request.url.path for request in recorder.requests]
    assert paths == [
        "/main",
        "/rest/2.0/pcs/file",  # locateupload (file < 256 KiB skips rapidupload)
        "/api/create",  # mkdir parent
        "/api/precreate",
        "/rest/2.0/pcs/superfile2",
        "/api/create",  # commit
        "/api/list",  # verification listing
    ]
    chunk_request = next(
        request for request in recorder.requests if request.url.path == "/rest/2.0/pcs/superfile2"
    )
    assert chunk_request.url.params["uploadid"] == "P1-test"
    assert chunk_request.url.params["partseq"] == "0"
    assert recorder.chunk_bodies[0] == payload
    assert recorder.commit_form["size"] == "5"
    assert recorder.commit_form["uploadid"] == "P1-test"
    assert recorder.commit_form["content-md5"] == hashes.file_md5
    await client.aclose()


class CommitVerificationRecorder(UploadRecorder):
    """Returns errno 4000023 for the first commit, then commits normally."""

    def __init__(self) -> None:
        super().__init__()
        self.commit_attempts = 0
        self.refreshes = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/main":
            self.refreshes += 1
            self.requests.append(request)
            return httpx.Response(
                200,
                text='<script>var templateData = {"jsToken": "%28%22tok123%22%29"};</script>',
            )
        if request.url.path == "/api/create" and request.url.query != b"a=commit":
            self.commit_attempts += 1
            self.requests.append(request)
            self.commit_form = _form(request)
            if self.commit_attempts == 1:
                return _json({"errno": 4000023})
            return _json(
                {"errno": 0, "md5": _scramble_etag(self.commit_form.get("content-md5", ""))}
            )
        return super().__call__(request)


async def test_commit_errno_4000023_refreshes_session_and_is_transient(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    recorder = CommitVerificationRecorder()
    payload = b"C" * 6
    local = tmp_path / "commit.bin"
    local.write_bytes(payload)
    recorder.remote["/Telegram Archive/commit.bin"] = len(payload)
    client = _client(settings, recorder)

    # A 4000023 commit raises a transient error so the upload-level retry loop
    # restarts the file against the refreshed session.
    with pytest.raises(TeraBoxTransientError, match="4000023"):
        await client.upload_file(local, "/Telegram Archive/commit.bin")

    # Bootstrap fetched /main once; the 4000023 handler refreshed it again.
    assert recorder.refreshes == 2
    await client.aclose()


async def test_uploader_retries_commit_errno_4000023_against_refreshed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    recorder = CommitVerificationRecorder()
    payload = b"C" * 6
    local = settings.download_dir / "commit.bin"
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    local.write_bytes(payload)
    recorder.remote["/Telegram Archive/commit.bin"] = len(payload)
    client = _client(settings, recorder)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(terabox_module.asyncio, "sleep", no_sleep)
    uploader = TeraBoxUploader(settings, client)

    receipt = await uploader.upload(local)

    assert recorder.commit_attempts == 2
    assert receipt.size == len(payload)
    assert recorder.commit_form["content-md5"] == hashlib.md5(payload).hexdigest()
    await client.aclose()


async def test_exhausted_chunk_attempts_are_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)

    recorder = UploadRecorder()
    original_handler = recorder.__call__

    def always_fail_chunks(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/2.0/pcs/superfile2":
            recorder.requests.append(request)
            return _json({"error_code": 31168, "error_msg": "injected"})
        return original_handler(request)

    payload = b"A" * 5
    local = tmp_path / "flaky.bin"
    local.write_bytes(payload)
    client = _client(settings, always_fail_chunks)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(terabox_module.asyncio, "sleep", no_sleep)

    with pytest.raises(TeraBoxTransientError, match="failed after"):
        await client.upload_file(local, "/Telegram Archive/flaky.bin")
    chunk_requests = [r for r in recorder.requests if r.url.path == "/rest/2.0/pcs/superfile2"]
    assert len(chunk_requests) == terabox_module._CHUNK_MAX_TRIES
    await client.aclose()


async def test_uploader_retries_whole_upload_after_chunk_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    recorder = UploadRecorder()
    original_handler = recorder.__call__
    state = {"failed_attempts": 0}

    def flaky_chunks(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/2.0/pcs/superfile2" and state["failed_attempts"] < 4:
            recorder.requests.append(request)
            state["failed_attempts"] += 1
            return _json({"error_code": 31168, "error_msg": "injected"})
        return original_handler(request)

    payload = b"A" * 5
    local = settings.download_dir / "flaky.bin"
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    local.write_bytes(payload)
    recorder.remote["/Telegram Archive/flaky.bin"] = len(payload)
    client = _client(settings, flaky_chunks)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(terabox_module.asyncio, "sleep", no_sleep)
    uploader = TeraBoxUploader(settings, client)

    receipt = await uploader.upload(local)

    # Four chunk attempts exhausted -> transient error -> whole-file retry.
    precreates = [r for r in recorder.requests if r.url.path == "/api/precreate"]
    assert len(precreates) == 2
    assert receipt.size == len(payload)
    assert recorder.chunk_bodies[0] == payload
    await client.aclose()


async def test_upload_file_skips_chunks_when_precreate_reports_complete(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    recorder = UploadRecorder(precreate_return_type=2)
    payload = b"B" * 7
    local = tmp_path / "pre.bin"
    local.write_bytes(payload)
    recorder.remote["/Telegram Archive/pre.bin"] = len(payload)
    client = _client(settings, recorder)

    await client.upload_file(local, "/Telegram Archive/pre.bin")

    paths = [request.url.path for request in recorder.requests]
    assert "/rest/2.0/pcs/superfile2" not in paths
    assert paths[-1] == "/api/list"
    await client.aclose()


async def test_bootstrap_requires_login_redirect(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/login"})

    client = _client(settings, handler)

    with pytest.raises(TeraBoxAuthError):
        await client.quota()
    await client.aclose()


async def test_bootstrap_follows_host_redirect(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        if request.url.path == "/main" and request.url.host == "www.terabox.com":
            return httpx.Response(302, headers={"location": "https://dm.terabox.com/main"})
        if request.url.path == "/api/quota":
            return _json({"errno": 0, "total": 1000, "used": 400})
        return httpx.Response(
            200,
            text='<script>var templateData = {"jsToken": "%28%22tok%22%29"};</script>',
        )

    client = _client(settings, handler)

    assert await client.cached_quota() == (1000, 400)
    await client.aclose()


async def test_cached_quota_reuses_recent_result(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    quota_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal quota_calls
        if request.url.path == "/api/quota":
            quota_calls += 1
            return _json({"errno": 0, "total": 10, "used": 3})
        return httpx.Response(
            200,
            text='<script>var templateData = {"jsToken": "%28%22tok%22%29"};</script>',
        )

    client = _client(settings, handler)

    assert await client.cached_quota() == (10, 3)
    assert await client.cached_quota() == (10, 3)
    assert quota_calls == 1
    await client.aclose()


async def test_uploader_deletes_local_copy_and_records_mount_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    buffer = settings.download_dir / "-1_Room" / "2026" / "08" / "16"
    buffer.mkdir(parents=True)
    local = buffer / "42_clip.mp4"
    local.write_bytes(b"video-bytes")

    class FakeClient:
        remote_root = "/Telegram Archive"
        mount_dir = settings.terabox_mount_dir

        def remote_path(self, media_path: Path) -> str:
            relative = media_path.relative_to(settings.download_dir.expanduser().resolve())
            from pathlib import PurePosixPath

            return str(PurePosixPath(self.remote_root, *relative.parts))

        async def upload_file(self, target, remote_path, *, progress=None):
            assert target == local
            return FileHashes(
                size=len(b"video-bytes"),
                file_md5=hashlib.md5(b"video-bytes").hexdigest(),
                slice_md5="",
                crc32=0,
                chunk_md5s=(),
            )

        def mount_path(self, remote_path: str) -> Path:
            return self.mount_dir / Path(remote_path.lstrip("/"))

    fake = FakeClient()
    uploader = TeraBoxUploader(settings, fake)  # type: ignore[arg-type]

    receipt = await uploader.upload(local)

    assert receipt.remote_path == "/Telegram Archive/-1_Room/2026/08/16/42_clip.mp4"
    assert receipt.mount_path == (
        settings.terabox_mount_dir / "Telegram Archive/-1_Room/2026/08/16/42_clip.mp4"
    )
    assert receipt.size == 11


def test_create_terabox_client_resolves_ndus_from_profile(tmp_path: Path) -> None:
    profile = tmp_path / "p.json"
    profile.write_text('{"ndus": "profile-cookie"}', encoding="utf-8")
    settings = _settings(tmp_path, terabox_ndus="", terabox_profile=profile)

    assert create_terabox_client(settings) is not None


async def test_media_deleter_translates_mount_path_to_remote(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    deleted: list[str] = []

    class FakeClient:
        remote_root = "/Telegram Archive"

        async def delete(self, remote_path: str) -> bool:
            deleted.append(remote_path)
            return True

    deleter = TeraBoxMediaDeleter(settings, FakeClient())  # type: ignore[arg-type]
    mount_file = settings.terabox_mount_dir / "Telegram Archive" / "-1_Room" / "clip.mp4"

    assert await deleter(mount_file) is True
    assert deleted == ["/Telegram Archive/Telegram Archive/-1_Room/clip.mp4"]


async def test_media_deleter_rejects_paths_outside_mount(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    class FakeClient:
        remote_root = "/Telegram Archive"

        async def delete(self, remote_path: str) -> bool:  # pragma: no cover
            raise AssertionError("must not delete outside the mount")

    deleter = TeraBoxMediaDeleter(settings, FakeClient())  # type: ignore[arg-type]

    assert await deleter(tmp_path / "elsewhere" / "clip.mp4") is False


# ---- direct dlink download ---------------------------------------------------


class DownloadRecorder:
    """Routes the dlink download protocol calls used by fetch_remote_file."""

    def __init__(
        self,
        payload: bytes,
        *,
        throttle_once: bool = False,
        filemetas: bool = True,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.payload = payload
        self.throttle_once = throttle_once
        self.filemetas = filemetas
        self.range_headers: list[str] = []
        self.download_calls = 0
        self.home_calls = 0
        self.filemetas_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/main":
            return httpx.Response(
                200,
                text='<script>var templateData = {"jsToken": "%28%22tok123%22%29"};</script>',
            )
        if path == "/api/filemetas":
            self.filemetas_calls += 1
            if not self.filemetas:
                return _json({"errno": 31023})
            return _json(
                {
                    "errno": 0,
                    "info": [
                        {
                            "fs_id": 12345,
                            "dlink": "https://dm-d.terabox.com/file?fid=12345&expires=8h",
                        }
                    ],
                }
            )
        if path == "/api/home/info":
            self.home_calls += 1
            return _json(
                {
                    "errno": 0,
                    "data": {"sign1": "s1value", "sign3": "s3value", "timestamp": "1717000000"},
                }
            )
        if path == "/api/download":
            self.download_calls += 1
            return _json(
                {
                    "errno": 0,
                    "dlink": [
                        {"fs_id": 12345, "dlink": "https://dm-d.terabox.com/file?fid=12345"}
                    ],
                }
            )
        if request.url.host in ("dm-d.terabox.com", "kul-ddata.terabox.com"):
            self.range_headers.append(request.headers.get("range", ""))
            if self.throttle_once:
                self.throttle_once = False
                throttle = json.dumps({"errno": 400141, "errmsg": "need verify"}).encode()
                return httpx.Response(
                    200, content=throttle, headers={"content-length": str(len(throttle))}
                )
            # The 0-byte probe used to resolve a cookie-free playback URL walks
            # the redirect chain; real range downloads skip it.
            if (
                request.url.host == "dm-d.terabox.com"
                and request.headers.get("range", "") == "bytes=0-0"
            ):
                return httpx.Response(
                    302,
                    headers={
                        "location": (
                            "https://kul-ddata.terabox.com/file?fid=12345"
                            "&expires=8h&r=final"
                        )
                    },
                )
            body = self.payload
            range_header = request.headers.get("range", "")
            if range_header.startswith("bytes="):
                start, _, end = range_header[len("bytes=") :].partition("-")
                body = self.payload[int(start) : int(end) + 1 if end else None]
            return httpx.Response(
                206, content=body, headers={"content-length": str(len(body))}
            )
        if path == "/api/list":
            return _json(
                {
                    "errno": 0,
                    "list": [
                        {
                            "path": "/Telegram Archive/photo.jpg",
                            "size": len(self.payload),
                            "isdir": 0,
                            "fs_id": 12345,
                        }
                    ],
                    "has_more": False,
                }
            )
        return _json({"errno": 0})


def test_sign_download_matches_js_reference_vectors() -> None:
    # Vectors generated with the terabox-api JavaScript SignDownload implementation.
    from app.infrastructure.terabox import sign_download

    assert sign_download("k1secretkey", "payloadtosign123") == "oL1WBFlrJxvuoH1tUqtpDQ=="
    assert sign_download("k1secretkey", "") == ""
    # JS charCodeAt(index % 0) yields NaN -> Uint8Array 0, i.e. identity key.
    assert sign_download("", "payloadtosign123") == "rnnwLcxWOU7ldXcAOV+gXg=="


async def test_download_url_signs_and_caches_dlink(tmp_path: Path) -> None:
    recorder = DownloadRecorder(b"A" * 1024)
    client = _client(_settings(tmp_path), recorder)

    from app.infrastructure.terabox import sign_download

    link = await client.download_url(12345)
    second = await client.download_url(12345)

    assert link == "https://dm-d.terabox.com/file?fid=12345"
    assert second == link
    assert recorder.download_calls == 1
    assert recorder.home_calls == 1
    form = _form(next(r for r in recorder.requests if r.url.path == "/api/download"))
    assert form["sign"] == sign_download("s3value", "s1value")
    assert form["fidlist"] == "[12345]"
    await client.aclose()


async def test_download_url_for_path_uses_filemetas_and_caches(tmp_path: Path) -> None:
    recorder = DownloadRecorder(b"A" * 1024)
    client = _client(_settings(tmp_path), recorder)

    link = await client.download_url_for_path("/Telegram Archive/photo.jpg")
    second = await client.download_url_for_path("/Telegram Archive/photo.jpg")

    assert link == "https://dm-d.terabox.com/file?fid=12345&expires=8h"
    assert second == link
    assert recorder.filemetas_calls == 1
    assert recorder.download_calls == 0
    assert recorder.home_calls == 0
    request = next(r for r in recorder.requests if r.url.path == "/api/filemetas")
    assert request.url.params["dlink"] == "1"
    assert json.loads(request.url.params["target"]) == ["/Telegram Archive/photo.jpg"]
    await client.aclose()


async def test_direct_download_link_returns_size_and_caches_meta(tmp_path: Path) -> None:
    recorder = DownloadRecorder(b"A" * 1024)
    client = _client(_settings(tmp_path), recorder)

    first = await client.direct_download_link("/Telegram Archive/photo.jpg")
    second = await client.direct_download_link("/Telegram Archive/photo.jpg")

    # The signed final hop serves bytes with the desktop User-Agent (no
    # session cookie needed), so the browser can stream it directly.
    assert first == ("https://kul-ddata.terabox.com/file?fid=12345&expires=8h&r=final", 1024, True)
    assert second == first
    # The second call must reuse the cached meta + final-URL entries, not page
    # the listing again or re-follow the redirect chain.
    list_calls = [r for r in recorder.requests if r.url.path == "/api/list"]
    assert len(list_calls) == 1
    assert recorder.filemetas_calls == 1
    redirect_probes = [
        r
        for r in recorder.requests
        if r.url.host == "dm-d.terabox.com" and r.headers.get("range") == "bytes=0-0"
    ]
    assert len(redirect_probes) == 1
    await client.aclose()


async def test_direct_download_link_falls_back_to_unsigned_dlink_when_cdn_redirect_fails(
    tmp_path: Path,
) -> None:
    recorder = DownloadRecorder(b"A" * 1024, throttle_once=True)
    client = _client(_settings(tmp_path), recorder)

    link, size, direct = await client.direct_download_link("/Telegram Archive/photo.jpg")

    # The throttle body aborts the redirect walk; the unsigned dlink still
    # works from the server side but is unusable by the browser (no cookie),
    # so it is flagged as not-direct and the player proxies instead.
    assert link == "https://dm-d.terabox.com/file?fid=12345&expires=8h"
    assert size == 1024
    assert direct is False
    await client.aclose()


async def test_direct_download_link_returns_none_when_path_missing(tmp_path: Path) -> None:
    recorder = DownloadRecorder(b"A" * 1024)
    client = _client(_settings(tmp_path), recorder)

    assert await client.direct_download_link("/Telegram Archive/missing.mp4") is None
    assert recorder.filemetas_calls == 0
    await client.aclose()


async def test_fetch_remote_file_prefers_filemetas_over_signed_download(tmp_path: Path) -> None:
    payload = b"F" * 1024
    recorder = DownloadRecorder(payload)
    client = _client(_settings(tmp_path), recorder)
    dest = tmp_path / "photo.jpg"

    result = await client.fetch_remote_file(
        "/Telegram Archive/photo.jpg", dest, expected_size=len(payload)
    )

    assert result == dest
    assert dest.read_bytes() == payload
    # The unsigned filemetas route replaces the home/info + RC4 download dance.
    assert recorder.filemetas_calls == 1
    assert recorder.download_calls == 0
    assert recorder.home_calls == 0
    await client.aclose()


async def test_fetch_remote_file_falls_back_to_signed_download_when_filemetas_fails(
    tmp_path: Path,
) -> None:
    payload = b"G" * 1024
    recorder = DownloadRecorder(payload, filemetas=False)
    client = _client(_settings(tmp_path), recorder)
    dest = tmp_path / "photo.jpg"

    result = await client.fetch_remote_file(
        "/Telegram Archive/photo.jpg", dest, expected_size=len(payload)
    )

    assert result == dest
    assert dest.read_bytes() == payload
    assert recorder.filemetas_calls == 1
    assert recorder.download_calls == 1
    assert recorder.home_calls == 1
    await client.aclose()


async def test_fetch_remote_file_writes_file_with_resume_range(tmp_path: Path) -> None:
    payload = b"B" * 2048
    recorder = DownloadRecorder(payload)
    client = _client(_settings(tmp_path), recorder)
    dest = tmp_path / "photo.jpg"

    result = await client.fetch_remote_file(
        "/Telegram Archive/photo.jpg", dest, expected_size=len(payload)
    )

    assert result == dest
    assert dest.read_bytes() == payload
    assert not dest.with_suffix(".jpg.part").exists()
    assert recorder.range_headers == [f"bytes=0-{len(payload) - 1}"]
    await client.aclose()


async def test_fetch_remote_file_limit_bytes_fetches_prefix_only(tmp_path: Path) -> None:
    payload = b"C" * 4096
    recorder = DownloadRecorder(payload)
    client = _client(_settings(tmp_path), recorder)
    dest = tmp_path / "clip.mp4"

    result = await client.fetch_remote_file(
        "/Telegram Archive/photo.jpg", dest, limit_bytes=1024
    )

    assert result == dest
    assert dest.read_bytes() == payload[:1024]
    assert recorder.range_headers == ["bytes=0-1023"]
    await client.aclose()


async def test_fetch_remote_file_throttle_retry_arms_gate_and_refreshes_dlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    async def instant_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    payload = b"D" * 1024
    recorder = DownloadRecorder(payload, throttle_once=True)
    client = _client(_settings(tmp_path), recorder)
    dest = tmp_path / "photo.jpg"

    result = await client.fetch_remote_file("/Telegram Archive/photo.jpg", dest)

    assert result == dest
    assert dest.read_bytes() == payload
    # The throttle response must refresh the dlink (second /api/filemetas
    # call) and make the retry wait out the throttle window.
    assert recorder.filemetas_calls == 2
    # Wait out the ~35s throttle window (monotonic drift keeps it just under 35).
    assert any(sleep >= 34 for sleep in sleeps)
    # The second range attempt succeeds after the gate.
    assert recorder.range_headers == ["bytes=0-1023", "bytes=0-1023"]
    await client.aclose()


async def test_fetch_remote_file_rejects_oversize_and_missing(tmp_path: Path) -> None:
    recorder = DownloadRecorder(b"E" * 128)
    client = _client(_settings(tmp_path), recorder)

    with pytest.raises(terabox_module.TeraBoxError, match="too large"):
        await client.fetch_remote_file(
            "/Telegram Archive/photo.jpg", tmp_path / "photo.jpg", max_bytes=64
        )
    with pytest.raises(terabox_module.TeraBoxError, match="not found"):
        await client.fetch_remote_file("/nowhere/missing.jpg", tmp_path / "missing.jpg")
    await client.aclose()
