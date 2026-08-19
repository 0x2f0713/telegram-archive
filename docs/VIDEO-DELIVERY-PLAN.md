# Fast Video Delivery Plan

## Diagnosis (verified against the live server and real archive files)

| # | Issue | Evidence |
|---|-------|----------|
| 1 | **HEVC (~25% of 508 videos)** cannot be decoded by Chrome/Firefox/Edge (esp. Linux) → player stuck in "loading" forever | ffprobe sample: 3/12 files `hevc`; server already returns 206 + `video/mp4` |
| 2 | **No faststart** — the `moov` atom sits at 99.7–99.8% of the file | top-level box scan of real files; 137 videos >500 MB, worst 3.6 GB / 96 min |
| 3 | **`Cache-Control: no-store` on `/media/*`** — every page visit, back/forward, or gallery reopen re-downloads the whole file (ETag/Last-Modified already sent but unusable) | `app/interfaces/web/app.py:141` |
| 4 | Gallery grid + conversation thread render many `<video preload="metadata">` elements at once → N×(head+tail) range fetches per page; black tiles for HEVC | `chat_media.html:48`, `conversation.html:56` |
| 5 | Speed probe fires a 4 MB Range XHR on every `waiting`/`seeked`, competing with playback | `media-player.js:144-146` |

The server itself is fast: `FileResponse` supports 206 Range + ETag, measured ~70 MB/s on loopback.

## Changes

1. **Media HTTP caching** — `Cache-Control: private, max-age=31536000, immutable` for `/media/*` (files immutable once `completed`); HEAD support added to the media route.
2. **Host-ffmpeg infrastructure** — `app/infrastructure/ffmpeg.py` resolves `FFMPEG_BIN`/`FFPROBE_BIN` (PATH lookup, compose default `/usr/bin/ffmpeg`), runs subprocesses with `LD_LIBRARY_PATH=/opt/host-libs` scoped to the child only, and probes hardware capabilities (`h264_rkmpp`, `hevc_rkmpp`, `libx264`). docker-compose mounts host binaries/libs/devices (`/dev/dri`, `/dev/mpp_service`, `/dev/rga`). Features degrade gracefully when ffmpeg is missing.
3. **Faststart on download** — `download.py` remuxes completed files with `ffmpeg -c copy -movflags +faststart` (runtime setting `media_faststart`, default on).
4. **On-demand H.264 variant for HEVC** — `app/infrastructure/transcode.py` + `app/application/media_variants.py`; first view of an HEVC video starts a hardware transcode (`h264_rkmpp`, libx264 fallback) to `{stem}.h264.mp4`, cached; `/media/{id}/variant-status` + `/media/{id}/variant` endpoints; the player polls and swaps sources.
5. **JPEG poster thumbnails** — `extract_poster` in `app/infrastructure/ffmpeg.py` + `VariantManager.poster_path`; `/media/{id}/poster`; grid uses `<img>` posters instead of `<video>` elements; players get `poster=`.
6. **Frontend** — probe only when buffer < 5 s and abort on pause; HEVC variant swap flow in `media-player.js`; viewer keeps the last video element cached in the DOM.
7. **`optimize-media` operation** — allowlisted command (`OperationCommand.OPTIMIZE_MEDIA`) with an executor in `app/interfaces/web/commands.py`; scans completed videos via `ArchiveRepository.completed_video_paths()`, faststarts non-faststart files, pre-warms posters; surfaced as the 5th card on the Operations page.
8. **Tests** — `tests/test_media_delivery.py`: cache headers, HEAD, variant/poster routes, `moov_offset`/faststart detection, degradation without ffmpeg, the optimize operation (happy path + missing ffmpeg); full `pytest` (154) + `ruff` + esbuild all green.
9. **TeraBox direct `dlink` thumbnails** — `TeraBoxClient.fetch_remote_file` (`app/infrastructure/terabox.py`) downloads media straight from the CDN (resume + 4 MB prefix for videos, global 35 s "need verify" throttle gate). Dlinks come from `/api/filemetas?dlink=1&target=[path]` — unsigned, path-based, `expires=8h`, cached 6 h — with the RC4-signed `/api/home/info` + `/api/download` flow (`sign_download`, ~50-min cache) kept as fallback. Verified via Playwright probes of the real web player: both routes hit the same ~1.8 MB/s per-connection CDN ceiling; parallel ranges do not help (SSL-handshake throttling). Also fixed `extract_thumbnail` for still images (`-ss 1` seeks past their only 0.04 s frame → empty output; now retries unseeked with `-update 1`) and the mount→remote path mapping (mount exposes the drive root, no extra `remote_root` prefix) plus `_api_json` bootstrapping before URL construction (avoids `errno -6` on the `www→dm` host redirect).
10. **Browser-direct CDN playback** — `GET /media/{id}/source` (`app/interfaces/web/routes.py`) resolves the fastest playback source in TeraBox mode: a fresh `dlink` for the H.264 variant when one exists (recorded `media_variant_path` or `.h264.mp4` sibling), else the original, via `TeraBoxClient.direct_download_link` (`file_meta` cached 300 s per path to avoid paging the parent listing per play click; dlink caches reused). Responses: `{"source":"terabox","url","media","mime","size"}` or `{"source":"proxy","url"}`. The player (`media-player.js`) fetches the source on first play (or first pointerdown as intent), swaps `video.src` to the CDN link when issued — the browser attaches its own terabox.com session cookie, so a logged-in browser streams straight from the CDN and never touches the throttled FUSE mount. On playback error the player re-resolves once and, failing that, falls back to the byte-range proxy (`/media/{id}`), marking the source exhausted so no retry loop occurs. Cross-origin `currentSrc` skips the speed probe XHR. CSP `media-src` extended with `https://dm-d.terabox.com https://*.terabox.com` (the 302 chain lands on a `*.data.terabox.com` host). Templates emit `data-source-url`/`data-fallback-url`/`data-terabox-enabled` on players and gallery lightbox anchors (`message_detail.html`, `conversation.html`, `chat_media.html`, `media-gallery.js`).

## Status

- Implemented: items 1–7, item 8 (tests), item 9 (dlink thumbnails), and item 10 (browser-direct CDN playback). `pytest` 234 passed, `ruff` clean, `npm run build:web` OK.
- Item 10 verified live with Playwright against message 275 (3.86 GB, no H.264 variant): `/media/275/source` returns a fresh `dm-d.terabox.com` dlink; the player swaps to it on play; without a terabox.com session cookie the CDN request fails fast (ORB) and the player falls back to the proxy with the "Streaming via server proxy" badge. With a logged-in terabox.com browser the CDN link plays directly (cookie attached by the browser).
- Pending deployment: update the real `.env` with `FFMPEG_BIN`/`FFPROBE_BIN`/`FFMPEG_LIB_DIR` (or rely on compose defaults), then `docker compose up -d` to apply mounts/devices/env. The container user (1001) may need read access to `/dev/dri`, `/dev/mpp_service`, `/dev/rga` (host user `namhh` is already in `video`/`render`).

## Out of scope

- No ffmpeg inside the Docker image (host binaries are bind-mounted instead).
- H.264 variants consume ~1x of the HEVC archive (~60 GB on /mnt/disk2).
- Requires a docker-compose restart to pick up new mounts/env.
