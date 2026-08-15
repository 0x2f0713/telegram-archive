import Plyr from "plyr";

const videoPlayers = new WeakMap();

function formatBytes(value) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
  const amount = value / 1024 ** index;
  return `${amount >= 100 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function formatSpeed(bytesPerSecond) {
  return `${formatBytes(bytesPerSecond)}/s`;
}

function requestNativeFullscreen(host, video) {
  if (typeof video.webkitEnterFullscreen === "function") {
    try {
      video.webkitEnterFullscreen();
      return true;
    } catch {
      return false;
    }
  }

  const request = host.requestFullscreen || host.webkitRequestFullscreen;
  if (typeof request !== "function") return false;
  try {
    const result = request.call(host);
    result?.catch?.(() => {});
    return true;
  } catch {
    return false;
  }
}

function setupVideoPlayer(video) {
  const existing = videoPlayers.get(video);
  if (existing) return existing.player;

  const host = video.closest("[data-video-player]");
  if (!host) return null;
  const badge = host.querySelector("[data-video-speed]");
  const size = Number.parseInt(host.dataset.mediaSize || "0", 10);
  const PROBE_BYTES = 4 * 1024 * 1024;
  const BUFFER_AHEAD_SECONDS = 20;
  let probe = null;
  let probeSpeed = 0;

  const config = {
    controls: [
      "play-large",
      "play",
      "progress",
      "current-time",
      "duration",
      "mute",
      "volume",
      "settings",
      "pip",
      "fullscreen",
    ],
    settings: ["speed"],
    speed: { selected: 1, options: [0.5, 0.75, 1, 1.25, 1.5, 2] },
    tooltips: { controls: true, seek: true },
    storage: { enabled: false },
    seekTime: 10,
    iconUrl: "/static/plyr.svg",
    fullscreen: { enabled: true, fallback: "force", iosNative: true, container: "[data-video-player]" },
  };
  if (!host.hasAttribute("data-video-fill")) config.ratio = "16:9";

  let player;
  try {
    player = new Plyr(video, config);
  } catch {
    video.controls = true;
    return null;
  }
  video.dataset.playerReady = "true";

  player.on("ready", () => {
    const fullscreenControl = host.querySelector('[data-plyr="fullscreen"]');
    if (!fullscreenControl || player.fullscreen?.supported) return;
    fullscreenControl.addEventListener("click", (event) => {
      event.preventDefault();
      requestNativeFullscreen(host, video);
    });
  });

  const bufferedAhead = () => {
    const current = video.currentTime || 0;
    for (let i = 0; i < video.buffered.length; i += 1) {
      if (video.buffered.start(i) <= current && current < video.buffered.end(i)) {
        return video.buffered.end(i) - current;
      }
    }
    return 0;
  };
  const stopProbe = () => {
    if (probe) probe.abort();
    probe = null;
    if (badge) badge.hidden = true;
  };
  const startProbe = () => {
    if (probe || !size) return;
    if (bufferedAhead() >= BUFFER_AHEAD_SECONDS) return;
    const offset = Math.min(
      size - 1,
      Math.floor((video.currentTime / Math.max(video.duration, 1)) * size),
    );
    const end = Math.min(size - 1, offset + PROBE_BYTES - 1);
    let loadedBytes = 0;
    let startedAt = 0;
    probe = new XMLHttpRequest();
    probe.open("GET", video.currentSrc, true);
    probe.setRequestHeader("Range", `bytes=${offset}-${end}`);
    probe.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      loadedBytes = event.loaded;
      const elapsed = (performance.now() - startedAt) / 1000;
      if (elapsed < 0.15) return;
      const instantaneous = loadedBytes / elapsed;
      probeSpeed = probeSpeed ? probeSpeed * 0.7 + instantaneous * 0.3 : instantaneous;
      if (badge) {
        badge.hidden = false;
        badge.textContent = `Speed ${formatSpeed(probeSpeed)} · ${formatBytes(loadedBytes)} loaded`;
      }
    });
    probe.addEventListener("loadstart", () => {
      startedAt = performance.now();
    });
    probe.addEventListener("loadend", () => {
      probe = null;
      if (badge) badge.hidden = true;
    });
    probe.send();
  };
  const stopProbeWhenBuffered = () => {
    if (bufferedAhead() >= BUFFER_AHEAD_SECONDS) stopProbe();
  };

  video.addEventListener("play", startProbe);
  video.addEventListener("waiting", startProbe);
  video.addEventListener("seeked", startProbe);
  video.addEventListener("canplaythrough", stopProbe);
  video.addEventListener("playing", stopProbeWhenBuffered);
  videoPlayers.set(video, {
    player,
    stopProbe,
    removeListeners: () => {
      video.removeEventListener("play", startProbe);
      video.removeEventListener("waiting", startProbe);
      video.removeEventListener("seeked", startProbe);
      video.removeEventListener("canplaythrough", stopProbe);
      video.removeEventListener("playing", stopProbeWhenBuffered);
    },
  });
  return player;
}

function destroyVideoPlayer(video) {
  const state = videoPlayers.get(video);
  video.pause();
  if (!state) return;
  state.stopProbe();
  state.removeListeners();
  state.player.destroy();
  videoPlayers.delete(video);
  delete video.dataset.playerReady;
}

function setupVideoPlayers() {
  document.querySelectorAll("[data-video-player] video").forEach((video) => {
    setupVideoPlayer(video);
  });
}

export { destroyVideoPlayer, setupVideoPlayer, setupVideoPlayers };
