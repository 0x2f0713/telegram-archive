"""Locate the host ffmpeg toolchain and probe its codec capabilities.

The application never links against ffmpeg: it spawns ``ffmpeg``/``ffprobe``
as subprocesses. Binaries may be installed on the host and bind-mounted into
the container together with their dynamic libraries; ``FFMPEG_LD_LIBRARY_PATH``
is applied only to the child process environment so the app itself is
unaffected. When no usable binary exists every feature degrades gracefully
to the pre-ffmpeg behavior.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

logger = logging.getLogger(__name__)

_FLAGS = re.compile(r"^\s{0,2}[A-Za-z.]{6}\s+([a-z0-9_]+)")

#: moov boxes at or before this offset count as faststart for our purposes.
FASTSTART_LIMIT = 1024 * 1024
#: Only the last slice of a big file is scanned when moov was not up front.
_TAIL_SCAN_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class FfmpegCapabilities:
    available: bool
    ffmpeg_bin: str = ""
    ffprobe_bin: str = ""
    h264_encoders: tuple[str, ...] = ()
    hevc_decoder: bool = False

    @property
    def can_remux(self) -> bool:
        return self.available

    @property
    def hardware_h264_encoder(self) -> bool:
        return "h264_rkmpp" in self.h264_encoders

    @property
    def can_transcode_hevc(self) -> bool:
        return self.hevc_decoder and bool(self.h264_encoders)

    def preferred_h264_encoder(self) -> str:
        if self.hardware_h264_encoder:
            return "h264_rkmpp"
        if "libx264" in self.h264_encoders:
            return "libx264"
        return self.h264_encoders[0] if self.h264_encoders else ""


def hw_decode_enabled(settings: Settings, capabilities: FfmpegCapabilities) -> bool:
    """Whether ffmpeg child processes should use MPP hardware decode.

    Opting out via ``video_hwaccel=none`` keeps the hardware encoder while
    falling back to software decoding. This matters where the MPP userspace
    and the runtime glibc are mismatched (e.g. a host-built mpp inside a
    newer Debian container), which makes hevc_rkmpp fail at runtime.
    """
    mode = settings.video_hwaccel.strip().lower()
    if mode in ("none", "off", "0", "false"):
        return False
    return capabilities.hardware_h264_encoder


def _resolve(binary: str, configured: str) -> str:
    if configured and configured.strip():
        candidate = configured.strip()
        if "/" in candidate or Path(candidate).is_file():
            return candidate
    resolved = shutil.which(binary)
    return resolved or ""


def _child_env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    if settings.ffmpeg_ld_library_path.strip():
        env["LD_LIBRARY_PATH"] = settings.ffmpeg_ld_library_path.strip()
    return env


async def _run(binary: str, args: list[str], settings: Settings) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        binary,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_child_env(settings),
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


async def probe_capabilities(settings: Settings) -> FfmpegCapabilities:
    """Locate ffmpeg/ffprobe and record which video codecs are available."""
    ffmpeg = _resolve("ffmpeg", settings.ffmpeg_bin)
    ffprobe = _resolve("ffprobe", settings.ffprobe_bin)
    if not ffmpeg or not ffprobe:
        logger.warning(
            "ffmpeg/ffprobe unavailable (ffmpeg=%s, ffprobe=%s); "
            "faststart, posters, and HEVC variants are disabled",
            settings.ffmpeg_bin,
            settings.ffprobe_bin,
        )
        return FfmpegCapabilities(available=False)

    encoders = await _capability_names(ffmpeg, settings, "-encoders")
    decoders = await _capability_names(ffmpeg, settings, "-decoders")
    h264_encoders = tuple(name for name in encoders if name.startswith("h264"))
    capabilities = FfmpegCapabilities(
        available=True,
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=ffprobe,
        h264_encoders=h264_encoders,
        hevc_decoder="hevc" in decoders,
    )
    logger.info(
        "ffmpeg ready: %s (h264 encoders=%s, hevc decoder=%s, hw h264=%s, hw decode=%s)",
        ffmpeg,
        h264_encoders,
        capabilities.hevc_decoder,
        capabilities.hardware_h264_encoder,
        hw_decode_enabled(settings, capabilities),
    )
    return capabilities


async def _capability_names(binary: str, settings: Settings, flag: str) -> tuple[str, ...]:
    returncode, stdout, stderr = await _run(binary, ["-hide_banner", flag], settings)
    if returncode != 0:
        logger.debug("ffmpeg %s failed (%s): %s", flag, returncode, stderr)
        return ()
    return tuple(match.group(1) for line in stdout.splitlines() if (match := _FLAGS.match(line)))


async def probe_video_codec(
    settings: Settings, capabilities: FfmpegCapabilities, path: Path
) -> str | None:
    """Return the first video stream's codec name (``h264``, ``hevc``, ...)."""
    if not capabilities.available:
        return None
    returncode, stdout, _ = await _run(
        capabilities.ffprobe_bin,
        [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        settings,
    )
    if returncode != 0:
        return None
    codec = stdout.strip().splitlines()[0] if stdout.strip() else ""
    return codec or None


async def remux_faststart(settings: Settings, capabilities: FfmpegCapabilities, path: Path) -> bool:
    """Rewrite ``path`` so the moov atom sits at the front (no re-encode).

    Stream copy only; the operation is I/O bound. Returns True when the file
    was rewritten successfully, False when skipped (already faststart) or
    failed (an error is logged and the original file is left untouched).
    """
    if not capabilities.available:
        return False
    if await moov_offset(path) <= FASTSTART_LIMIT:
        return False
    temp = path.with_name(f"{path.name}.faststart.part")
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        pass
    returncode, _, stderr = await _run(
        capabilities.ffmpeg_bin,
        [
            "-y",
            "-i",
            str(path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(temp),
        ],
        settings,
    )
    if returncode != 0:
        logger.warning("faststart remux failed for %s: %s", path, stderr[-500:])
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        os.replace(temp, path)
    except OSError as exc:
        logger.warning("faststart replace failed for %s: %s", path, exc)
        return False
    logger.info("faststart remuxed %s", path)
    return True


async def extract_poster(
    settings: Settings,
    capabilities: FfmpegCapabilities,
    source: Path,
    target: Path,
) -> bool:
    """Extract a JPEG poster frame from a video into ``target``.

    The frame is written to a temporary file and atomically replaced so an
    interrupted extraction never leaves a corrupt poster behind.
    """
    if not capabilities.available:
        return False
    temp = target.with_name(f"{target.name}.part")
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        pass
    args: list[str] = []
    if hw_decode_enabled(settings, capabilities):
        args += ["-hwaccel", "rkmpp"]
    args += [
        "-y",
        "-ss",
        "1",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        "scale=min(480\\,iw):-2",
        "-q:v",
        "5",
        "-f",
        "image2",
        str(temp),
    ]
    returncode, _, stderr = await _run(capabilities.ffmpeg_bin, args, settings)
    if returncode != 0:
        logger.warning("poster extraction failed for %s: %s", source, stderr[-500:])
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        os.replace(temp, target)
    except OSError as exc:
        logger.warning("poster replace failed for %s: %s", target, exc)
        return False
    return True


async def extract_thumbnail(
    settings: Settings,
    capabilities: FfmpegCapabilities,
    source: Path,
    target: Path,
    max_dimension: int = 320,
    quality: int = 75,
) -> bool:
    """Extract a JPEG thumbnail from an image or video into ``target``.

    For images: scales down preserving aspect ratio.
    For videos: extracts a frame at 1 second (or first keyframe) and scales.

    The thumbnail is written to a temporary file and atomically replaced.
    """
    if not capabilities.available:
        return False
    temp = target.with_name(f"{target.name}.part")
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        pass
    args: list[str] = []
    if hw_decode_enabled(settings, capabilities):
        args += ["-hwaccel", "rkmpp"]
    # For videos, seek to 1s; for images, -ss has no effect
    # Map quality 1-100 to JPEG q:v 2-31 (lower is better for JPEG)
    jpeg_quality = max(2, min(31, int(31 - (quality / 100) * 29)))
    args += [
        "-y",
        "-ss",
        "1",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale='if(gt(iw,ih),-2,{max_dimension})':'if(gt(iw,ih),{max_dimension},-2)'",
        "-q:v",
        str(jpeg_quality),
        "-f",
        "image2",
        str(temp),
    ]
    returncode, _, stderr = await _run(capabilities.ffmpeg_bin, args, settings)
    if returncode != 0:
        logger.warning("thumbnail extraction failed for %s: %s", source, stderr[-500:])
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        os.replace(temp, target)
    except OSError as exc:
        logger.warning("thumbnail replace failed for %s: %s", target, exc)
        return False
    return True


async def moov_offset(path: Path) -> int:
    """Return the byte offset of the top-level moov box, or the file size.

    Only box headers are read: top-level boxes are skipped by seek, and when
    moov was not seen up front a bounded slice at the tail is scanned.
    """
    return await asyncio.to_thread(_scan_moov, path)


def _scan_moov(path: Path) -> int:
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size < 8:
        return size
    try:
        with path.open("rb") as handle:
            return _walk_top_boxes(handle, size)
    except OSError:
        return size


def _walk_top_boxes(handle: object, size: int) -> int:
    pos = 0
    while pos + 8 <= size:
        handle.seek(pos)  # type: ignore[attr-defined]
        header = handle.read(16)  # type: ignore[attr-defined]
        if len(header) < 8:
            break
        box_size, box_type = struct.unpack(">I4s", header[:8])
        header_len = 8
        if box_size == 1:
            if len(header) < 16:
                break
            box_size = struct.unpack(">Q", header[8:16])[0]
            header_len = 16
        elif box_size == 0:
            box_size = size - pos
        if box_type == b"moov":
            return pos
        next_pos = pos + max(box_size, header_len)
        if next_pos <= pos:
            break
        pos = next_pos
    tail_start = max(0, size - _TAIL_SCAN_BYTES)
    if pos < tail_start:
        handle.seek(tail_start)  # type: ignore[attr-defined]
        tail = handle.read(size - tail_start)  # type: ignore[attr-defined]
        cursor = 0
        while cursor + 8 <= len(tail):
            box_size, box_type = struct.unpack(">I4s", tail[cursor : cursor + 8])
            if box_type == b"moov":
                return tail_start + cursor
            if box_size < 8:
                cursor += 8
            else:
                cursor += box_size
    return size


async def transcode_hevc_to_h264(
    settings: Settings,
    capabilities: FfmpegCapabilities,
    source: Path,
    target: Path,
) -> bool:
    """Transcode an HEVC video to H.264 with faststart.

    Returns True on success, False on failure.
    """
    if not capabilities.available or not capabilities.can_transcode_hevc:
        return False
    temp = target.with_name(f"{target.name}.part")
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        pass
    encoder = capabilities.preferred_h264_encoder()
    if not encoder:
        logger.warning("No H.264 encoder available for HEVC transcode")
        return False
    args: list[str] = []
    if hw_decode_enabled(settings, capabilities):
        args += ["-hwaccel", "rkmpp"]
    args += [
        "-y",
        "-i",
        str(source),
        "-c:v",
        encoder,
        "-preset",
        "medium",
        "-crf",
        "23",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temp),
    ]
    returncode, _, stderr = await _run(capabilities.ffmpeg_bin, args, settings)
    if returncode != 0:
        logger.warning("HEVC to H.264 transcode failed for %s: %s", source, stderr[-500:])
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    try:
        os.replace(temp, target)
    except OSError as exc:
        logger.warning("HEVC transcode replace failed for %s: %s", target, exc)
        return False
    return True
