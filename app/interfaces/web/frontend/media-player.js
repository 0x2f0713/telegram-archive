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

function isCrossOrigin(url) {
  try {
    return new URL(url, window.location.href).origin !== window.location.origin;
  } catch {
    return true;
  }
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
  const PROBE_START_SECONDS = 5;
  const BUFFER_AHEAD_SECONDS = 20;
  const DIRECT_STALL_TIMEOUT_MS = 10000;
  const SLOW_SPEED_BYTES = 500 * 1024;
  const teraboxEnabled = host.dataset.teraboxEnabled === "true";
  const sourceUrl = host.dataset.sourceUrl || "";
  const fallbackUrl = host.dataset.fallbackUrl || "";
  let probe = null;
  let probeSpeed = 0;
  let variantTimer = null;
  let seekingActive = false;
  let directAttempts = 0;
  let sourceState = null;
  let sourceResolving = null;

  const setStatus = (text, state) => {
    if (!badge || !teraboxEnabled) return;
    badge.hidden = false;
    badge.textContent = text;
    badge.dataset.state = state;
  };
  const clearStatus = () => {
    if (!badge || !teraboxEnabled) return;
    badge.hidden = true;
    delete badge.dataset.state;
  };

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
    if (teraboxEnabled && badge) delete badge.dataset.state;
  };
  const startProbe = () => {
    if (probe || !size) return;
    if (isCrossOrigin(video.currentSrc)) return;
    if (bufferedAhead() >= PROBE_START_SECONDS) return;
    const position = Number.isFinite(video.currentTime) ? video.currentTime : 0;
    const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 1;
    const offset = Math.min(size - 1, Math.floor((position / duration) * size));
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
      if (teraboxEnabled && !seekingActive) {
        if (probeSpeed < SLOW_SPEED_BYTES) {
          setStatus(`Streaming from TeraBox · ${formatSpeed(probeSpeed)} · slow link`, "terabox-slow");
        } else {
          setStatus(`Streaming from TeraBox · ${formatSpeed(probeSpeed)}`, "terabox-playing");
        }
      } else if (badge) {
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
    if (bufferedAhead() >= BUFFER_AHEAD_SECONDS) {
      stopProbe();
      clearStatus();
    }
  };
  const resolveSource = async () => {
    if (sourceState) return sourceState;
    if (sourceResolving) return sourceResolving;
    sourceResolving = fetch(sourceUrl, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    })
      .then((response) => (response.ok ? response.json() : null))
      .catch(() => null);
    sourceResolving.then((state) => {
      sourceState = state;
      sourceResolving = null;
    });
    return sourceResolving;
  };
  const swapToDirect = (url) => {
    video.dataset.directSource = "1";
    delete video.dataset.relaySource;
    video.src = url;
    video.load();
    startDirectStallTimer();
    if (teraboxEnabled) setStatus("Direct from TeraBox CDN", "terabox-playing");
  };
  const swapToRelay = (url) => {
    delete video.dataset.directSource;
    delete video.dataset.directExhausted;
    clearDirectStallTimer();
    video.dataset.relaySource = "1";
    const target = new URL(url, window.location.href).href;
    if (video.currentSrc === target) return;
    const wasPlaying = !video.paused && !video.ended;
    video.src = target;
    video.load();
    if (wasPlaying) {
      const attempt = video.play();
      attempt?.catch?.(() => {});
    }
    if (teraboxEnabled) setStatus("Streaming from TeraBox · CDN relay", "terabox-playing");
  };
  const useFallbackSource = () => {
    delete video.dataset.directSource;
    delete video.dataset.relaySource;
    clearDirectStallTimer();
    video.dataset.directExhausted = "1";
    const target = new URL(sourceState?.stream_url || fallbackUrl, window.location.href).href;
    if (video.currentSrc === target) return;
    const wasPlaying = !video.paused && !video.ended;
    video.src = target;
    video.load();
    if (wasPlaying) {
      const attempt = video.play();
      attempt?.catch?.(() => {});
    }
    if (teraboxEnabled) setStatus("Streaming via server proxy", "terabox-slow");
  };
  const handleDirectSourceError = async () => {
    directAttempts += 1;
    if (directAttempts > 2) {
      useFallbackSource();
      return;
    }
    if (badge) {
      badge.hidden = false;
      badge.textContent = "Reconnecting to TeraBox…";
      badge.dataset.state = "terabox-connecting";
    }
    const state = await resolveSource();
    if (state?.source === "terabox" && state.direct && state.url && state.url !== video.currentSrc) {
      swapToDirect(state.url);
      const attempt = video.play();
      attempt?.catch?.(() => {});
      return;
    }
    useFallbackSource();
  };
  const handleWaiting = () => {
    if (!teraboxEnabled || video.dataset.variantPolling) return;
    if (seekingActive) {
      setStatus("Seeking on TeraBox…", "terabox-seeking");
    } else if (video.readyState < 2 && !video.dataset.firstByteSeen) {
      setStatus("Connecting to TeraBox…", "terabox-connecting");
    } else {
      setStatus("Buffering from TeraBox…", "terabox-buffering");
    }
  };
  const startDirectStallTimer = () => {
    if (video.dataset.directStallTimer) return;
    video.dataset.directStallTimer = window.setTimeout(() => {
      delete video.dataset.directStallTimer;
      if (video.dataset.directSource && video.readyState < 2) {
        useFallbackSource();
      }
    }, DIRECT_STALL_TIMEOUT_MS);
  };
  const clearDirectStallTimer = () => {
    if (video.dataset.directStallTimer) {
      window.clearTimeout(Number(video.dataset.directStallTimer));
      delete video.dataset.directStallTimer;
    }
  };
  const handleSeeking = () => {
    if (!teraboxEnabled) return;
    seekingActive = true;
    setStatus("Seeking on TeraBox…", "terabox-seeking");
  };
  const handleSeekedTerabox = () => {
    seekingActive = false;
  };
  const handleProgressing = () => {
    if (!teraboxEnabled || seekingActive) return;
    video.dataset.firstByteSeen = "true";
    clearDirectStallTimer();
  };
  const handlePlay = () => {
    startProbe();
    if (!teraboxEnabled || !sourceUrl || video.dataset.directSource || video.dataset.directExhausted) return;
    resolveSource().then((state) => {
      if (state?.source !== "terabox" || video.dataset.directSource) return;
      directAttempts = 0;
      if (state.stream_url) {
        swapToRelay(state.stream_url);
      } else if (state.direct && state.url) {
        swapToDirect(state.url);
      }
      const attempt = video.play();
      attempt?.catch?.(() => {});
    });
  };
  const prepareDirectSource = () => {
    if (!teraboxEnabled || !sourceUrl || video.dataset.directSource || video.dataset.directExhausted) return;
    resolveSource().then((state) => {
      if (state?.source !== "terabox" || video.dataset.directSource) return;
      directAttempts = 0;
      if (state.stream_url) {
        swapToRelay(state.stream_url);
      } else if (state.direct && state.url) {
        swapToDirect(state.url);
      }
    });
  };
  const handleWaitingCombined = () => {
    startProbe();
    handleWaiting();
  };
  const handleSeekedCombined = () => {
    startProbe();
    handleSeekedTerabox();
  };
  const handleCanPlayThrough = () => {
    stopProbe();
    clearDirectStallTimer();
    clearStatus();
  };
  const handlePlaying = () => {
    stopProbeWhenBuffered();
    handleProgressing();
    if (teraboxEnabled && !seekingActive && bufferedAhead() >= PROBE_START_SECONDS) clearStatus();
  };
  const stopVariantPoll = () => {
    if (variantTimer) {
      window.clearInterval(variantTimer);
      variantTimer = null;
    }
    delete video.dataset.variantPolling;
  };
  const pollVariant = () => {
    const statusUrl = host.dataset.variantStatusUrl;
    const variantUrl = host.dataset.variantUrl;
    if (!statusUrl || !variantUrl || video.dataset.variantSwapped) return;
    if (video.dataset.variantPolling) return;
    video.dataset.variantPolling = "true";
    if (badge) {
      badge.hidden = false;
      badge.textContent = "Preparing H.264 stream…";
    }
    // Warm the variant: the status endpoint only reports state, the transcode
    // is started by a request to the variant URL itself (404 while pending).
    fetch(variantUrl, { credentials: "same-origin", cache: "no-store" }).catch(() => {});
    variantTimer = window.setInterval(async () => {
      let state;
      try {
        const response = await fetch(statusUrl, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          stopVariantPoll();
          if (badge) badge.hidden = true;
          return;
        }
        state = await response.json();
      } catch {
        return;
      }
      if (!state.enabled) {
        stopVariantPoll();
        if (badge) badge.hidden = true;
        return;
      }
      if (state.ready) {
        stopVariantPoll();
        video.dataset.variantSwapped = "true";
        video.src = variantUrl;
        video.load();
        const attempt = video.play();
        attempt?.catch?.(() => {});
      } else if (badge && state.transcoding) {
        const percent = Number.isFinite(state.progress)
          ? ` (${Math.round(state.progress * 100)}%)`
          : "";
        badge.textContent = `Preparing H.264 stream${percent}…`;
      } else {
        stopVariantPoll();
        if (badge) badge.hidden = true;
      }
    }, 2000);
  };
  const handlePlaybackError = () => {
    stopProbe();
    if (video.dataset.variantSwapped) return;
    if (video.dataset.directSource) {
      handleDirectSourceError();
      return;
    }
    if (video.dataset.relaySource && !video.dataset.directExhausted) {
      useFallbackSource();
      return;
    }
    pollVariant();
  };

  video.addEventListener("play", handlePlay);
  video.addEventListener("waiting", handleWaitingCombined);
  video.addEventListener("seeking", handleSeeking);
  video.addEventListener("seeked", handleSeekedCombined);
  video.addEventListener("pause", stopProbe);
  video.addEventListener("canplaythrough", handleCanPlayThrough);
  video.addEventListener("playing", handlePlaying);
  video.addEventListener("progress", handleProgressing);
  video.addEventListener("error", handlePlaybackError);
  if (teraboxEnabled) host.addEventListener("pointerdown", prepareDirectSource, { once: true });
  videoPlayers.set(video, {
    player,
    stopProbe,
    removeListeners: () => {
      video.removeEventListener("play", handlePlay);
      video.removeEventListener("waiting", handleWaitingCombined);
      video.removeEventListener("seeking", handleSeeking);
      video.removeEventListener("seeked", handleSeekedCombined);
      video.removeEventListener("pause", stopProbe);
      video.removeEventListener("canplaythrough", handleCanPlayThrough);
      video.removeEventListener("playing", handlePlaying);
      video.removeEventListener("progress", handleProgressing);
      video.removeEventListener("error", handlePlaybackError);
      stopVariantPoll();
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
  delete video.dataset.variantSwapped;
  delete video.dataset.variantPolling;
  delete video.dataset.firstByteSeen;
  delete video.dataset.directSource;
  delete video.dataset.relaySource;
  delete video.dataset.directExhausted;
}

function setupVideoPlayers() {
  document.querySelectorAll("[data-video-player] video").forEach((video) => {
    setupVideoPlayer(video);
  });
}

export { destroyVideoPlayer, setupVideoPlayer, setupVideoPlayers };
