"""On-demand playable variants and poster frames for archived videos.

HEVC (H.265) files cannot be decoded by most browsers. When a video is first
requested for playback, a background task transcodes it to an H.264 MP4 with
the faststart flag and caches the result next to the original. Poster frames
are extracted once and cached as JPEGs. Everything runs as subprocesses of
the host ffmpeg toolchain (see :mod:`app.infrastructure.ffmpeg`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from pathlib import Path

from app.application.media_variants import VariantStatus
from app.config import Settings
from app.infrastructure.ffmpeg import (
    FASTSTART_LIMIT,
    FfmpegCapabilities,
    extract_poster,
    hw_decode_enabled,
    moov_offset,
    probe_capabilities,
    probe_video_codec,
)

logger = logging.getLogger(__name__)

VARIANT_SUFFIX = ".h264.mp4"
POSTER_SUFFIX = ".poster.jpg"
TRANSCODE_CONCURRENCY = 1
POSTER_CONCURRENCY = 2


class VariantManager:
    """Resolve streamable paths, drive background transcodes, cache posters."""

    def __init__(
        self,
        settings: Settings,
        capabilities: FfmpegCapabilities,
    ) -> None:
        self.settings = settings
        self.capabilities = capabilities
        self._transcode_slot = asyncio.Semaphore(TRANSCODE_CONCURRENCY)
        self._poster_slot = asyncio.Semaphore(POSTER_CONCURRENCY)
        self._codec_cache: dict[tuple[str, int, int], str | None] = {}
        self._tasks: dict[Path, asyncio.Task[None]] = {}
        self._poster_tasks: dict[Path, asyncio.Task[None]] = {}
        self._state: dict[Path, dict[str, object]] = {}

    @staticmethod
    def variant_path(path: Path) -> Path:
        return path.with_name(f"{path.stem}{VARIANT_SUFFIX}")

    @staticmethod
    def poster_file_path(path: Path) -> Path:
        return path.with_name(f"{path.stem}{POSTER_SUFFIX}")

    async def playable_path(self, path: Path) -> Path | None:
        """Return the path a player should stream.

        The original is returned for H.264 input. For other codecs the cached
        variant is returned, or a background transcode is started and None is
        returned until it completes.
        """
        if not await asyncio.to_thread(path.is_file):
            return None
        codec = await self._codec(path)
        if codec == "h264":
            return path
        if not self.capabilities.can_transcode_hevc:
            return None
        variant = self.variant_path(path)
        if await asyncio.to_thread(variant.is_file):
            return variant
        self._ensure_transcode(path)
        return None

    def status(self, path: Path) -> VariantStatus:
        codec = self._cached_codec(path)
        variant = self.variant_path(path)
        state = self._state.get(path, {})
        started_at = state.get("started_at")
        return VariantStatus(
            enabled=True,
            ready=variant.is_file(),
            transcoding=path in self._tasks,
            codec=codec,
            progress=state.get("progress"),
            source_size=_safe_size(path),
            variant_size=_safe_size(variant),
            started_at=float(started_at) if started_at is not None else None,
        )

    async def poster_path(self, path: Path) -> Path | None:
        """Return a cached poster JPEG, extracting it on first request."""
        if not await asyncio.to_thread(path.is_file):
            return None
        poster = self.poster_file_path(path)
        if await asyncio.to_thread(poster.is_file):
            return poster
        task = self._poster_tasks.get(path)
        if task is None:
            task = asyncio.create_task(self._extract_poster(path))
            self._poster_tasks[path] = task
        try:
            await task
        except Exception as exc:
            logger.warning("poster task failed for %s: %s", path, exc)
        return poster if poster.is_file() else None

    async def _extract_poster(self, path: Path) -> None:
        try:
            async with self._poster_slot:
                await extract_poster(
                    self.settings,
                    self.capabilities,
                    path,
                    self.poster_file_path(path),
                )
        finally:
            self._poster_tasks.pop(path, None)

    def _ensure_transcode(self, path: Path) -> None:
        if path in self._tasks:
            return
        task = asyncio.create_task(self._transcode(path))
        self._tasks[path] = task
        self._state.setdefault(path, {})["started_at"] = time.monotonic()

    async def shutdown(self) -> None:
        """Cancel in-flight transcodes and poster extractions at shutdown."""
        tasks = list(self._tasks.values()) + list(self._poster_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _transcode(self, path: Path) -> None:
        state = self._state.setdefault(path, {})
        temp = _temp_variant_path(path)
        try:
            async with self._transcode_slot:
                await self._transcode_one(path, state)
        except asyncio.CancelledError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except Exception as exc:
            logger.warning("transcode failed for %s: %s", path, exc)
            state["error"] = str(exc)
        finally:
            self._tasks.pop(path, None)
            state.pop("progress", None)

    async def _transcode_one(self, path: Path, state: dict[str, object]) -> None:
        variant = self.variant_path(path)
        temp = _temp_variant_path(path)
        temp.unlink(missing_ok=True)
        encoder = self.capabilities.preferred_h264_encoder()
        codec_args: list[str]
        if encoder == "h264_rkmpp":
            codec_args = ["-c:v", "h264_rkmpp", "-rc_mode", "VBR", "-qp_init", "26"]
        elif encoder == "libx264":
            codec_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        else:
            codec_args = ["-c:v", encoder, "-b:v", "5000k", "-pix_fmt", "yuv420p"]
        returncode, stderr = await self._run_transcode(path, temp, codec_args, state)
        if returncode != 0 and "audio" in stderr.casefold():
            returncode, stderr = await self._run_transcode(
                path, temp, [*codec_args, "-c:a", "aac", "-b:a", "128k"], state
            )
        if returncode != 0:
            temp.unlink(missing_ok=True)
            raise RuntimeError(stderr[-500:])
        os.replace(temp, variant)
        logger.info("transcoded %s -> %s (%s)", path, variant, encoder)

    async def _run_transcode(
        self,
        source: Path,
        temp: Path,
        codec_args: list[str],
        state: dict[str, object],
    ) -> tuple[int, str]:
        args: list[str] = ["-y", "-hide_banner", "-loglevel", "error"]
        if hw_decode_enabled(self.settings, self.capabilities):
            args += ["-hwaccel", "rkmpp"]
        args += [
            "-i",
            str(source),
            *codec_args,
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            "-progress",
            "pipe:1",
            "-nostats",
            str(temp),
        ]
        env = dict(os.environ)
        remote_host = self.settings.ffmpeg_remote_host.strip()
        if remote_host:
            cmd = self._remote_command(remote_host, args)
            env.pop("LD_LIBRARY_PATH", None)
            returncode, stderr = await self._spawn_and_track(cmd, env, state, source)
            if returncode != 0 and _ssh_unreachable(stderr):
                logger.warning(
                    "remote transcode unavailable (%s), falling back to local",
                    stderr.strip().splitlines()[-1],
                )
                cmd = [self.capabilities.ffmpeg_bin, *args]
                if self.settings.ffmpeg_ld_library_path.strip():
                    env["LD_LIBRARY_PATH"] = self.settings.ffmpeg_ld_library_path.strip()
                returncode, stderr = await self._spawn_and_track(cmd, env, state, source)
            return returncode, stderr
        if self.settings.ffmpeg_ld_library_path.strip():
            env["LD_LIBRARY_PATH"] = self.settings.ffmpeg_ld_library_path.strip()
        return await self._spawn_and_track(
            [self.capabilities.ffmpeg_bin, *args], env, state, source
        )

    def _remote_command(self, remote_host: str, args: list[str]) -> list[str]:
        host_dir = self.settings.host_download_dir.strip()
        if not host_dir:
            raise RuntimeError("HOST_DOWNLOAD_DIR is required when FFMPEG_REMOTE_HOST is set")
        container_dir = str(self.settings.download_dir)
        translated = [
            host_dir + arg[len(container_dir) :] if arg.startswith(container_dir) else arg
            for arg in args
        ]
        if "-hwaccel" not in translated:
            translated = ["-hwaccel", "rkmpp", *translated]
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
        ]
        if self.settings.ffmpeg_remote_identity.strip():
            command += ["-i", self.settings.ffmpeg_remote_identity.strip()]
        if self.settings.ffmpeg_remote_known_hosts.strip():
            command += [
                "-o",
                f"UserKnownHostsFile={self.settings.ffmpeg_remote_known_hosts.strip()}",
            ]
        return [
            *command,
            remote_host,
            self.settings.ffmpeg_remote_bin,
            *(shlex.quote(arg) for arg in translated),
        ]

    async def _spawn_and_track(
        self,
        cmd: list[str],
        env: dict[str, str],
        state: dict[str, object],
        source: Path,
    ) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        duration = await self._duration(source)
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").strip()
            if not line.startswith("out_time_us="):
                continue
            try:
                micros = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if duration and duration > 0:
                state["progress"] = min(1.0, micros / 1_000_000 / duration)
        stderr = (await proc.stderr.read()).decode(errors="replace")
        returncode = await proc.wait()
        return returncode, stderr

    async def _duration(self, path: Path) -> float | None:
        returncode, stdout, _ = await self._ffprobe(
            path,
            ["-show_entries", "format=duration", "-of", "csv=p=0"],
        )
        if returncode != 0:
            return None
        try:
            return float(stdout.strip().splitlines()[0])
        except (ValueError, IndexError):
            return None

    async def _codec(self, path: Path) -> str | None:
        try:
            stat = await asyncio.to_thread(path.stat)
        except OSError:
            return None
        key = (str(path), stat.st_size, int(stat.st_mtime))
        cached = self._codec_cache.get(key)
        if cached is not None or key in self._codec_cache:
            return cached
        codec = await probe_video_codec(self.settings, self.capabilities, path)
        self._codec_cache[key] = codec
        return codec

    def _cached_codec(self, path: Path) -> str | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return self._codec_cache.get((str(path), stat.st_size, int(stat.st_mtime)))

    async def _ffprobe(self, path: Path, entries: list[str]) -> tuple[int, str, str]:
        env = dict(os.environ)
        if self.settings.ffmpeg_ld_library_path.strip():
            env["LD_LIBRARY_PATH"] = self.settings.ffmpeg_ld_library_path.strip()
        proc = await asyncio.create_subprocess_exec(
            self.capabilities.ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            *entries,
            "-of",
            "csv=p=0",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


_SSH_UNREACHABLE = (
    "permission denied",
    "connection refused",
    "connection timed out",
    "timed out",
    "host key verification failed",
    "could not resolve",
    "no route to host",
    "network is unreachable",
)


def _ssh_unreachable(stderr: str) -> bool:
    lowered = stderr.casefold()
    return any(marker in lowered for marker in _SSH_UNREACHABLE)


def _temp_variant_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}{VARIANT_SUFFIX}.part")


async def build_variant_manager(
    settings: Settings,
) -> VariantManager:
    capabilities = await probe_capabilities(settings)
    return VariantManager(settings, capabilities)


async def is_faststart(path: Path) -> bool:
    return await moov_offset(path) <= FASTSTART_LIMIT
