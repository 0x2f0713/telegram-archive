from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.infrastructure.terabox import (
    FileHashes,
    TeraBoxAuthError,
    TeraBoxClient,
    TeraBoxMediaDeleter,
    TeraBoxUploader,
    create_terabox_client,
    decode_etag,
    hash_file,
    remote_path_for,
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
