"""TeraBox Web API client used to publish archived media to a TeraBox drive.

The unidisk FUSE mount exposing the drive is read-only, so uploads use the
same Web API protocol as the ``terabox-api`` library bundled with unidisk:

1. Hash the local file (full MD5, first-slice MD5, CRC32, per-chunk MD5s).
2. Attempt a server-side ``rapidupload`` dedupe for files >= 256 KB.
3. Otherwise ``precreate`` the file, upload chunks to the ``superfile2``
   endpoint on the upload host from ``locateupload``, then ``create`` the
   final entry with the chunk MD5 list.
4. Verify the returned etag and the remote size.

Reads stay on the FUSE mount (``terabox_mount_dir``), which caches blocks on
the local hard drive, so the web dashboard can stream archived media back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

SLICE_SIZE = 256 * 1024
USER_AGENT = "terabox;1.40.0.132;PC;PC-Windows;10.0.26100;WindowsTeraBox"
APP_PARAMS = {"app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0"}
DEFAULT_HOST = "https://www.terabox.com"
QUOTA_CACHE_SECONDS = 300.0
_MIN_RETRY_DELAY = 0.5
_CHUNK_MAX_TRIES = 4

_TEMPLATE_DATA = re.compile(r"<script>var templateData = (.*);</script>", re.DOTALL)
_JS_TOKEN_VALUE = re.compile(r"%28%22(.*)%22%29")
_JS_TOKEN_FALLBACK = re.compile(r"window.jsToken%20%3D%20a%7D%3Bfn%28%22(.*)%22%29")


class TeraBoxError(RuntimeError):
    """A TeraBox API failure that the caller should record and stop on."""


class TeraBoxTransientError(TeraBoxError):
    """A failure worth retrying: transport errors and HTTP 5xx responses."""


class TeraBoxAuthError(TeraBoxError):
    """The ndus cookie is missing, expired, or requires interactive login."""


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    """Result of a verified upload: where the bytes live once published."""

    remote_path: str
    mount_path: Path
    size: int
    md5: str


@dataclass(frozen=True, slots=True)
class FileHashes:
    """Hashes required by the TeraBox upload protocol."""

    size: int
    file_md5: str
    slice_md5: str
    crc32: int
    chunk_md5s: tuple[str, ...]

    @property
    def etag(self) -> str:
        if len(self.chunk_md5s) <= 1:
            return self.file_md5
        payload = json.dumps(list(self.chunk_md5s), separators=(",", ":"))
        return f"{hashlib.md5(payload.encode('utf-8')).hexdigest()}-{len(self.chunk_md5s)}"

    @property
    def commit_md5_candidates(self) -> frozenset[str]:
        """Digests TeraBox may return after ``/api/create``.

        Single-chunk uploads echo the content MD5; multi-chunk uploads echo
        the MD5 of the chunk-list JSON (the etag base) instead. Either value
        proves the committed block list matches what we uploaded.
        """
        if len(self.chunk_md5s) <= 1:
            return frozenset({self.file_md5})
        payload = json.dumps(list(self.chunk_md5s), separators=(",", ":"))
        return frozenset({self.file_md5, hashlib.md5(payload.encode("utf-8")).hexdigest()})


def hash_file(path: Path, chunk_size: int) -> FileHashes:
    """Compute every hash the upload protocol needs in one streaming pass."""

    file_hash = hashlib.md5()
    slice_hash = hashlib.md5()
    chunk_hashes: list[str] = []
    chunk_hash = hashlib.md5()
    crc = 0
    total = 0
    in_chunk = 0

    with Path(path).open("rb") as handle:
        while True:
            buffer = handle.read(1024 * 1024)
            if not buffer:
                break
            file_hash.update(buffer)
            crc = zlib.crc32(buffer, crc)
            if total < SLICE_SIZE:
                slice_hash.update(buffer[: SLICE_SIZE - total])
            offset = 0
            while offset < len(buffer):
                take = min(len(buffer) - offset, chunk_size - in_chunk)
                chunk_hash.update(buffer[offset : offset + take])
                in_chunk += take
                offset += take
                total += take
                if in_chunk >= chunk_size:
                    chunk_hashes.append(chunk_hash.digest().hex())
                    chunk_hash = hashlib.md5()
                    in_chunk = 0
    if in_chunk > 0:
        chunk_hashes.append(chunk_hash.digest().hex())

    return FileHashes(
        size=total,
        file_md5=file_hash.digest().hex(),
        slice_md5=slice_hash.digest().hex(),
        crc32=crc & 0xFFFFFFFF,
        chunk_md5s=tuple(chunk_hashes),
    )


def decode_etag(value: str) -> str:
    """Invert TeraBox's scrambled etag into the plain MD5 hex digest.

    Mirrors ``DecodeMd5`` from the terabox-api helper: restore the offset
    character at position 9, XOR each nibble with its index, then swap the
    four byte quartets. The transformation is its own inverse.
    """

    if len(value) != 32:
        return value
    restored = format(ord(value[9]) - ord("g"), "x")
    restored_str = value[:9] + restored + value[10:]
    xored = "".join(
        format(int(char, 16) ^ (index & 15), "x") for index, char in enumerate(restored_str)
    )
    return xored[8:16] + xored[0:8] + xored[24:32] + xored[16:24]


def sanitize_remote_component(name: str) -> str:
    """Return a TeraBox-safe single path component.

    TeraBox rejects (errno -7) any component containing characters outside the
    BMP (emoji, some CJK extensions) and a few ASCII symbols. We normalize,
    strip those, and collapse the rest so the component still reads naturally.
    """

    normalized = unicodedata.normalize("NFKC", name)
    cleaned = "".join(
        "_" if ord(char) > 0xFFFF or char in '<>:"/\\|?*' or ord(char) < 0x20 else char
        for char in normalized
    )
    cleaned = re.sub(r"__+", "_", cleaned)
    cleaned = cleaned.strip(" ._")
    return cleaned or "unnamed"


def sanitize_remote_path(path: str) -> str:
    """Sanitize every component of a remote path, preserving the leading slash."""

    parts = PurePosixPath(path).parts
    cleaned = "/".join(sanitize_remote_component(part) for part in parts if part != "/")
    return f"/{cleaned}" if cleaned else "/"


def remote_path_for(base_dir: Path, root: str, media_path: Path) -> str:
    """Translate a buffered download path into a TeraBox remote path."""

    relative = Path(media_path).relative_to(base_dir)
    parts = tuple(sanitize_remote_component(part) for part in relative.parts)
    return str(PurePosixPath(sanitize_remote_path(root), *parts))


def _form_body(data: dict[str, Any]) -> str:
    """application/x-www-form-urlencoded body matching URLSearchParams rules.

    URLSearchParams encodes spaces as ``+`` and then TeraBox's library
    rewrites ``+`` to ``%20``; literal ``+`` values were already ``%2B``.
    """

    return urlencode({key: str(value) for key, value in data.items()}).replace("+", "%20")


def _query_params(params: dict[str, Any]) -> str:
    return urlencode({key: str(value) for key, value in params.items()}).replace("+", "%20")


@dataclass(slots=True)
class TeraBoxUpload:
    remote_path: str
    hashes: FileHashes
    uploaded: bool = False
    upload_id: str | None = None
    chunk_failures: dict[int, int] = field(default_factory=dict)


class TeraBoxClient:
    """Async TeraBox session with lazy bootstrap and resumable uploads."""

    def __init__(
        self,
        settings: Settings,
        ndus: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._host = DEFAULT_HOST
        self._upload_host: str | None = None
        self._js_token = ""
        self._cookies = f"lang=en; ndus={ndus}"
        self._boot_lock = asyncio.Lock()
        self._bootstrapped = False
        self._quota_cache: tuple[float, tuple[int, int]] | None = None
        self._http = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(connect=45.0, read=180.0, write=900.0, pool=45.0),
            transport=transport,
        )

    @property
    def chunk_size(self) -> int:
        return self._settings.terabox_chunk_size

    @property
    def remote_root(self) -> str:
        return sanitize_remote_path(self._settings.terabox_remote_root)

    @property
    def mount_dir(self) -> Path:
        return self._settings.terabox_mount_dir

    def mount_path(self, remote_path: str) -> Path:
        """Local FUSE path where the upload is readable once published."""

        return self.mount_dir / PurePosixPath(remote_path).relative_to("/")

    def remote_path(self, media_path: Path) -> str:
        base = self._settings.download_dir.expanduser().resolve()
        target = Path(media_path).expanduser().resolve()
        return remote_path_for(base, self.remote_root, target)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- Session bootstrap -------------------------------------------------

    async def _ensure_bootstrapped(self) -> None:
        if self._bootstrapped:
            return
        async with self._boot_lock:
            if not self._bootstrapped:
                await self._refresh_app_data()
                self._bootstrapped = True

    async def _refresh_app_data(self) -> None:
        """Fetch the console page to learn the jsToken and canonical host."""

        url = f"{self._host}/main"
        for _hop in range(5):
            try:
                response = await self._http.get(url, headers=self._headers())
            except httpx.HTTPError as exc:
                raise TeraBoxError(f"TeraBox bootstrap request failed: {exc}") from exc
            if response.status_code == 302:
                location = response.headers.get("location", "")
                if not location or location.rstrip("/").endswith("login"):
                    raise TeraBoxAuthError(
                        "TeraBox rejected the ndus cookie; refresh it from the browser "
                        "or unidisk profile"
                    )
                target = urlparse(location)
                origin = f"{target.scheme}://{target.netloc}"
                if target.netloc and origin != self._host:
                    logger.info("TeraBox host changed to %s", origin)
                    self._host = origin
                url = f"{self._host}{target.path or '/main'}"
                if target.query:
                    url = f"{url}?{target.query}"
                continue
            if response.status_code != 200:
                raise TeraBoxError(f"TeraBox bootstrap returned HTTP {response.status_code}")
            self._merge_cookies(response)
            if not self._extract_js_token(response.text):
                logger.warning("TeraBox bootstrap page did not expose a jsToken; continuing")
            return
        raise TeraBoxError("TeraBox bootstrap followed too many redirects")

    def _extract_js_token(self, html: str) -> bool:
        js_token = ""
        match = _TEMPLATE_DATA.search(html)
        if match:
            try:
                template = json.loads(match.group(1).split(";</script>")[0])
            except json.JSONDecodeError:
                template = {}
            raw = template.get("jsToken") or ""
            token_match = _JS_TOKEN_VALUE.search(raw)
            if token_match:
                js_token = token_match.group(1)
        if not js_token:
            fallback = _JS_TOKEN_FALLBACK.search(html)
            if fallback:
                js_token = fallback.group(1)
        if js_token:
            self._js_token = js_token
            return True
        return False

    def _merge_cookies(self, response: httpx.Response) -> None:
        jar: dict[str, str] = {}
        for cookie in self._cookies.split(";"):
            cookie = cookie.strip()
            if "=" in cookie:
                name, _, value = cookie.partition("=")
                jar[name.strip()] = value.strip()
        for key, value in response.headers.multi_items():
            if key.casefold() != "set-cookie":
                continue
            pair = value.split(";", 1)[0]
            if "=" in pair:
                name, _, pair_value = pair.partition("=")
                jar[name.strip()] = pair_value.strip()
        self._cookies = "; ".join(f"{name}={value}" for name, value in jar.items())

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Cookie": self._cookies}
        if extra:
            headers.update(extra)
        return headers

    # ---- Low-level request helpers -----------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        await self._ensure_bootstrapped()
        extra_headers = kwargs.pop("headers", None)
        delay = _MIN_RETRY_DELAY
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._http.request(
                    method, url, headers=self._headers(extra_headers), **kwargs
                )
            except httpx.HTTPError as exc:
                last_error = TeraBoxTransientError(f"TeraBox transport error: {exc}")
            else:
                if response.status_code < 500:
                    self._merge_cookies(response)
                    if response.status_code != 200:
                        raise TeraBoxError(
                            f"TeraBox API {urlparse(url).path} returned HTTP {response.status_code}"
                        )
                    return response
                last_error = TeraBoxTransientError(
                    f"TeraBox API {urlparse(url).path} returned HTTP {response.status_code}"
                )
            if attempt < 2:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 4.0)
        raise last_error or TeraBoxError("TeraBox request failed after retries")

    async def _api_json(self, path: str, form: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._host}{path}"
        if form is None:
            response = await self._request("GET", url)
        else:
            response = await self._request(
                "POST",
                url,
                content=_form_body(form),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise TeraBoxError(f"TeraBox API {path} returned invalid JSON") from exc

    def _token_params(self) -> dict[str, str]:
        return {**APP_PARAMS, "jsToken": self._js_token}

    # ---- Filesystem operations ----------------------------------------------

    async def login_check(self) -> None:
        """Verify the ndus cookie authorizes the account."""

        for attempt in range(2):
            data = await self._api_json("/api/check/login")
            errno = data.get("errno")
            if errno in (0, None):
                return
            if attempt == 0:
                # A fresh bootstrap can race with session settlement (errno -6);
                # refresh the console page once and retry before giving up.
                await self._refresh_app_data()
        raise TeraBoxAuthError("TeraBox login check failed; refresh the ndus cookie")

    async def quota(self) -> tuple[int, int]:
        """Return ``(total_bytes, used_bytes)`` for the account."""

        data = await self._api_json("/api/quota?checkexpire=1&checkfree=1")
        if data.get("errno") not in (0, None):
            raise TeraBoxError(f"TeraBox quota failed with errno {data.get('errno')}")
        return int(data.get("total", 0)), int(data.get("used", 0))

    async def cached_quota(self, max_age_seconds: float = QUOTA_CACHE_SECONDS) -> tuple[int, int]:
        """Return the quota, reusing a recent result to avoid per-page API hits."""

        now = time.monotonic()
        if self._quota_cache is not None:
            fetched_at, cached = self._quota_cache
            if now - fetched_at < max_age_seconds:
                return cached
        result = await self.quota()
        self._quota_cache = (now, result)
        return result

    async def file_meta(self, remote_path: str) -> dict[str, Any] | None:
        """Return the list entry for one remote path, or None when absent."""

        posix = PurePosixPath(remote_path)
        if len(posix.parts) <= 1:
            return {"path": "/", "isdir": 1, "size": 0}
        parent = str(posix.parent) if len(posix.parts) > 2 else "/"
        page = 1
        while True:
            data = await self._api_json(
                "/api/list",
                {
                    "order": "name",
                    "desc": 0,
                    "dir": parent,
                    "num": 20000,
                    "page": page,
                    "showempty": 0,
                },
            )
            if data.get("errno") not in (0, None):
                return None
            for entry in data.get("list") or []:
                if entry.get("path") == remote_path:
                    return entry
            if not data.get("has_more"):
                return None
            page += 1

    async def ensure_remote_dir(self, remote_dir: str) -> None:
        """Create a remote directory including any missing parents."""

        parts = [part for part in PurePosixPath(remote_dir).parts if part != "/"]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            data = await self._api_json(
                "/api/create?a=commit", {"path": current, "isdir": 1, "block_list": "[]"}
            )
            errno = data.get("errno", 0)
            if errno == 0:
                continue
            existing = await self.file_meta(current)
            if existing is not None and existing.get("isdir") == 1:
                continue
            raise TeraBoxError(f"TeraBox mkdir {current} failed with errno {errno}")

    async def delete(self, remote_path: str) -> bool:
        """Delete one remote file or folder; return False when it is missing."""

        query = _query_params({**self._token_params(), "onnest": "fail", "opera": "delete"})
        body = {"filelist": json.dumps([remote_path], separators=(",", ":"))}
        data = await self._api_json(f"/api/filemanager?{query}", body)
        if data.get("errno") == 450016:
            await self._refresh_app_data()
            query = _query_params({**self._token_params(), "onnest": "fail", "opera": "delete"})
            data = await self._api_json(f"/api/filemanager?{query}", body)
        errno = data.get("errno", 0)
        if errno == 0:
            return True
        if errno in (-7, 31066):
            return False
        raise TeraBoxError(f"TeraBox delete {remote_path} failed with errno {errno}")

    # ---- Upload pipeline ----------------------------------------------------

    async def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> FileHashes:
        """Upload one local file to TeraBox and verify the published entry."""

        hashes = await asyncio.to_thread(hash_file, Path(local_path), self.chunk_size)
        if hashes.size == 0:
            raise TeraBoxError("Cannot upload an empty file")
        upload = TeraBoxUpload(remote_path=remote_path, hashes=hashes)
        self._report(progress, 0, hashes.size)
        if hashes.size >= SLICE_SIZE and await self._rapid_upload(upload):
            logger.info("TeraBox dedupe hit: %s already existed remotely", remote_path)
            self._report(progress, hashes.size, hashes.size)
            await self._verify_published(upload)
            return hashes
        await self._chunked_upload(Path(local_path), upload, progress)
        await self._verify_published(upload)
        return hashes

    @staticmethod
    def _report(progress: Callable[[int, int], None] | None, current: int, total: int) -> None:
        if progress is None:
            return
        try:
            progress(current, total)
        except Exception:
            logger.debug("Upload progress callback failed", exc_info=True)

    async def _rapid_upload(self, upload: TeraBoxUpload) -> bool:
        hashes = upload.hashes
        data = await self._api_json(
            "/api/rapidupload",
            {
                "path": upload.remote_path,
                "content-length": hashes.size,
                "content-md5": hashes.file_md5,
                "slice-md5": hashes.slice_md5,
                "content-crc32": hashes.crc32,
                "block_list": json.dumps(list(hashes.chunk_md5s), separators=(",", ":")),
                "rtype": 2,
                "mode": 1,
            },
        )
        upload.uploaded = data.get("errno") in (0, 2)
        return upload.uploaded

    async def _locate_upload_host(self) -> str:
        if self._upload_host:
            return self._upload_host
        response = await self._request("GET", f"{self._host}/rest/2.0/pcs/file?method=locateupload")
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise TeraBoxError("TeraBox locateupload returned invalid JSON") from exc
        host = data.get("host")
        if not host:
            raise TeraBoxError("TeraBox locateupload did not return an upload host")
        self._upload_host = f"https://{host}"
        return self._upload_host

    def invalidate_upload_host(self) -> None:
        """Drop the cached upload host so the next chunk upload re-locates.

        ``locateupload`` can hand back a host in a region that does not match
        the account's data center; those hosts reject the superfile2 request
        with HTTP 403. Re-locating picks a usable host for the retry.
        """

        if self._upload_host:
            logger.info("TeraBox upload host invalidated: %s", self._upload_host)
        self._upload_host = None

    async def _precreate(self, upload: TeraBoxUpload) -> None:
        hashes = upload.hashes
        base_form: dict[str, Any] = {
            "path": upload.remote_path,
            "autoinit": 1,
            "size": hashes.size,
            "file_limit_switch_v34": "true",
            "block_list": json.dumps(list(hashes.chunk_md5s), separators=(",", ":")),
            "rtype": 2,
            "content-md5": hashes.file_md5,
            "slice-md5": hashes.slice_md5,
            "content-crc32": hashes.crc32,
        }
        for _attempt in range(2):
            form = dict(base_form)
            if upload.upload_id:
                form["uploadid"] = upload.upload_id
            data = await self._api_json(
                f"/api/precreate?{_query_params(self._token_params())}", form
            )
            errno = data.get("errno", -1)
            if errno == 4000023:
                logger.info("TeraBox precreate asked for re-verification; refreshing session")
                await self._refresh_app_data()
                continue
            if errno != 0:
                raise TeraBoxError(f"TeraBox precreate failed with errno {errno}")
            if data.get("return_type") == 2:
                upload.uploaded = True
                return
            upload_id = data.get("uploadid")
            if not upload_id:
                raise TeraBoxError("TeraBox precreate did not return an uploadid")
            upload.upload_id = str(upload_id)
            return
        raise TeraBoxError("TeraBox precreate repeatedly asked for verification")

    async def _chunked_upload(
        self,
        local_path: Path,
        upload: TeraBoxUpload,
        progress: Callable[[int, int], None] | None,
    ) -> None:
        hashes = upload.hashes
        upload_host = await self._locate_upload_host()
        await self.ensure_remote_dir(str(PurePosixPath(upload.remote_path).parent))
        await self._precreate(upload)
        if upload.uploaded:
            logger.info("TeraBox precreate reported %s is already stored", upload.remote_path)
            self._report(progress, hashes.size, hashes.size)
            return

        sent = 0
        with local_path.open("rb") as handle:
            for partseq, expected_md5 in enumerate(hashes.chunk_md5s):
                payload = await asyncio.to_thread(handle.read, self.chunk_size)
                if not payload:
                    break
                # Re-resolve each chunk: a 403 re-locates a fresh host, and we
                # must not send the remaining chunks to the stale one.
                upload_host = await self._locate_upload_host()
                await self._upload_chunk(upload_host, upload, partseq, payload, expected_md5)
                sent += len(payload)
                self._report(progress, sent, hashes.size)
        if sent != hashes.size:
            raise TeraBoxError(
                f"TeraBox upload sent {sent} bytes but the file is {hashes.size} bytes"
            )
        await self._commit(upload)
        upload.uploaded = True

    async def _upload_chunk(
        self,
        upload_host: str,
        upload: TeraBoxUpload,
        partseq: int,
        payload: bytes,
        expected_md5: str,
    ) -> None:
        failures = upload.chunk_failures
        for attempt in range(1, _CHUNK_MAX_TRIES + 1):
            query = _query_params(
                {
                    "method": "upload",
                    **APP_PARAMS,
                    "path": upload.remote_path,
                    "uploadid": upload.upload_id,
                    "partseq": partseq,
                }
            )
            files = {"file": ("blob", payload, "application/octet-stream")}
            backoff = min(2.0 * 2 ** (attempt - 1), 15.0)
            try:
                response = await self._http.post(
                    f"{upload_host}/rest/2.0/pcs/superfile2?{query}",
                    files=files,
                    headers=self._headers(),
                )
            except httpx.HTTPError as exc:
                failures[partseq] = failures.get(partseq, 0) + 1
                logger.warning(
                    "TeraBox chunk %s transport failure (attempt %s/%s): %s",
                    partseq,
                    attempt,
                    _CHUNK_MAX_TRIES,
                    exc,
                )
                if attempt < _CHUNK_MAX_TRIES:
                    await asyncio.sleep(backoff)
                continue
            self._merge_cookies(response)
            if response.status_code != 200:
                failures[partseq] = failures.get(partseq, 0) + 1
                if response.status_code == 403:
                    # 403 from an upload host means the located host does not
                    # serve this account's region. Drop it and re-locate so the
                    # next attempt targets a fresh, working host.
                    logger.warning(
                        "TeraBox chunk %s HTTP 403 from %s; re-locating upload host (attempt %s/%s)",
                        partseq,
                        upload_host,
                        attempt,
                        _CHUNK_MAX_TRIES,
                    )
                    self.invalidate_upload_host()
                    upload_host = await self._locate_upload_host()
                    await asyncio.sleep(backoff)
                    continue
                logger.warning(
                    "TeraBox chunk %s returned HTTP %s (attempt %s/%s)",
                    partseq,
                    response.status_code,
                    attempt,
                    _CHUNK_MAX_TRIES,
                )
                if attempt < _CHUNK_MAX_TRIES:
                    await asyncio.sleep(backoff)
                continue
            try:
                data = response.json()
            except json.JSONDecodeError:
                data = {}
            if data.get("error_code"):
                failures[partseq] = failures.get(partseq, 0) + 1
                logger.warning(
                    "TeraBox chunk %s failed with error_code %s (attempt %s/%s)",
                    partseq,
                    data.get("error_code"),
                    attempt,
                    _CHUNK_MAX_TRIES,
                )
                if attempt < _CHUNK_MAX_TRIES:
                    await asyncio.sleep(backoff)
                continue
            actual = str(data.get("md5") or "").casefold()
            if actual and actual != expected_md5:
                # The server stored a corrupted copy of this chunk; a retry
                # re-precreates and uploads the whole file again.
                raise TeraBoxTransientError(
                    f"TeraBox chunk {partseq} MD5 mismatch (expected {expected_md5}, got {actual})"
                )
            return
        raise TeraBoxError(
            f"TeraBox chunk {partseq} failed after {failures.get(partseq, 0)} attempts"
        )

    async def _commit(self, upload: TeraBoxUpload) -> None:
        hashes = upload.hashes
        data = await self._api_json(
            "/api/create",
            {
                "path": upload.remote_path,
                "isdir": 0,
                "size": hashes.size,
                "content-md5": hashes.file_md5,
                "slice-md5": hashes.slice_md5,
                "content-crc32": hashes.crc32,
                "block_list": json.dumps(list(hashes.chunk_md5s), separators=(",", ":")),
                "uploadid": upload.upload_id,
                "rtype": 2,
            },
        )
        errno = data.get("errno", -1)
        if errno != 0:
            raise TeraBoxError(f"TeraBox create (commit) failed with errno {errno}")
        actual_md5 = decode_etag(str(data.get("md5", "")))
        if actual_md5 and actual_md5 not in hashes.commit_md5_candidates:
            # Corrupted server-side assembly; a retry restarts the upload.
            raise TeraBoxTransientError(
                f"TeraBox commit MD5 mismatch (expected one of "
                f"{sorted(hashes.commit_md5_candidates)}, got {actual_md5})"
            )

    async def _verify_published(self, upload: TeraBoxUpload) -> None:
        # TeraBox list responses can lag a committed upload for a few seconds,
        # so poll briefly before deciding the published entry is wrong.
        entry: dict[str, Any] | None = None
        for attempt in range(4):
            await asyncio.sleep(min(1.0 * attempt, 3.0))
            entry = await self.file_meta(upload.remote_path)
            if entry is None:
                continue
            if int(entry.get("size", -1)) == upload.hashes.size:
                return
        if entry is None:
            raise TeraBoxTransientError(
                f"TeraBox verification failed: {upload.remote_path} not listed"
            )
        # The listed size does not match what we uploaded. The commit MD5 for
        # multi-chunk uploads only verifies the block-list etag, not content
        # integrity, so we cannot assume the published bytes are complete.
        # Remove the wrong entry (often a stale partially-committed file) so a
        # retry re-uploads into a clean path rather than seeing the same error.
        size = entry.get("size")
        try:
            await self.delete(upload.remote_path)
        except TeraBoxError:
            logger.debug("Could not delete mismatched remote entry before retry", exc_info=True)
        raise TeraBoxTransientError(
            f"TeraBox verification failed: remote size {size} does not "
            f"match {upload.hashes.size}; removed the mismatched entry for retry"
        )


class TeraBoxMediaDeleter:
    """Deleter adapter for chat-archive deletion of files served from the mount.

    Translates a mount path back into a TeraBox remote path and removes it.
    Returns True when the remote entry no longer exists afterwards.
    """

    def __init__(self, settings: Settings, client: TeraBoxClient) -> None:
        self.settings = settings
        self.client = client

    @staticmethod
    def _to_remote_path(mount_root: Path, target: Path, root: str) -> str | None:
        try:
            relative = target.relative_to(mount_root)
        except ValueError:
            return None
        return str(PurePosixPath(root, *relative.parts))

    async def __call__(self, mount_path: Path) -> bool:
        mount_root = self.settings.terabox_mount_dir.expanduser().resolve()
        remote_path = await asyncio.to_thread(
            self._to_remote_path,
            mount_root,
            Path(mount_path),
            self.client.remote_root,
        )
        if remote_path is None:
            return False
        return await self.client.delete(remote_path)


def create_terabox_client(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> TeraBoxClient:
    """Build a TeraBox client from settings, resolving the ndus cookie."""

    return TeraBoxClient(settings, settings.require_terabox_ndus(), transport=transport)


class TeraBoxUploader:
    """Adapter exposing :class:`MediaUploader` over a TeraBox client.

    Translates the buffered download path into a remote path under the
    configured archive folder, uploads with retries, and hands back a receipt
    pointing at the read-only FUSE mount path used for serving.
    """

    RETRIES = 3

    def __init__(self, settings: Settings, client: TeraBoxClient) -> None:
        self.settings = settings
        self.client = client

    async def upload(
        self, target: Path, progress: Callable[[int, int], None] | None = None
    ) -> UploadReceipt:
        remote_path = self.client.remote_path(target)
        hashes = await self._upload_with_retries(target, remote_path, progress)
        return UploadReceipt(
            remote_path=remote_path,
            mount_path=self.client.mount_path(remote_path),
            size=hashes.size,
            md5=hashes.file_md5,
        )

    async def _upload_with_retries(
        self,
        target: Path,
        remote_path: str,
        progress: Callable[[int, int], None] | None,
    ) -> FileHashes:
        delay = 2.0
        for attempt in range(1, self.RETRIES + 1):
            try:
                return await self.client.upload_file(target, remote_path, progress=progress)
            except TeraBoxTransientError as exc:
                if attempt == self.RETRIES:
                    raise
                logger.warning(
                    "TeraBox upload attempt %s/%s failed; retrying in %ss: %s",
                    attempt,
                    self.RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise TeraBoxError("TeraBox upload retry loop ended unexpectedly")
