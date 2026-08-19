import { destroyVideoPlayer, setupVideoPlayer } from "./media-player.js";

function setupMediaGallery() {
  const gallery = document.querySelector("[data-media-gallery]");
  const dialog = document.querySelector("[data-media-viewer]");
  if (!gallery || !dialog) return;

  const items = [...gallery.querySelectorAll("[data-gallery-item]")];
  const content = dialog.querySelector("[data-media-viewer-content]");
  const title = dialog.querySelector("[data-media-viewer-title]");
  const meta = dialog.querySelector("[data-media-viewer-meta]");
  const counter = dialog.querySelector("[data-media-viewer-counter]");
  const previous = dialog.querySelector("[data-media-viewer-previous]");
  const next = dialog.querySelector("[data-media-viewer-next]");
  const detail = dialog.querySelector("[data-media-viewer-detail]");
  const download = dialog.querySelector("[data-media-viewer-download]");
  const close = dialog.querySelector("[data-media-viewer-close]");
  if (!items.length || !content || typeof dialog.showModal !== "function") return;

  let activeIndex = 0;
  let returnFocus = null;
  let pointerStart = null;

  const clearMedia = () => {
    content.querySelectorAll("video").forEach((video) => destroyVideoPlayer(video));
    content.replaceChildren();
  };

  const render = (index) => {
    if (index < 0 || index >= items.length) return;
    clearMedia();
    activeIndex = index;
    const item = items[activeIndex];
    const caption = item.dataset.caption || "Archived media";
    dialog.dataset.mediaKind = item.dataset.mediaKind;
    if (item.dataset.mediaKind === "video") {
      const playerHost = document.createElement("div");
      const media = document.createElement("video");
      const speedBadge = document.createElement("span");
      playerHost.className = "media-viewer-player";
      playerHost.dataset.videoPlayer = "";
      playerHost.dataset.videoFill = "";
      playerHost.dataset.mediaSize = item.dataset.mediaSize || "0";
      playerHost.dataset.variantUrl = item.dataset.variantUrl || "";
      playerHost.dataset.variantStatusUrl = item.dataset.variantStatusUrl || "";
      playerHost.dataset.teraboxEnabled = item.dataset.teraboxEnabled || "";
      playerHost.dataset.sourceUrl = item.dataset.sourceUrl || "";
      playerHost.dataset.fallbackUrl = item.dataset.fallbackUrl || "";
      media.src = item.dataset.hasVariant ? item.dataset.variantUrl : item.dataset.mediaSrc;
      media.className = "media-viewer-video";
      media.controls = true;
      media.playsInline = true;
      media.preload = item.dataset.teraboxEnabled ? "none" : "metadata";
      if (item.dataset.variantStatusUrl) {
        media.poster = `${item.dataset.mediaSrc}/poster`;
      }
      speedBadge.className = "video-speed-badge";
      speedBadge.dataset.videoSpeed = "";
      speedBadge.ariaHidden = "true";
      speedBadge.hidden = true;
      playerHost.append(media, speedBadge);
      content.replaceChildren(playerHost);
      setupVideoPlayer(media);
    } else {
      const media = document.createElement("img");
      media.src = item.dataset.mediaSrc;
      media.className = "media-viewer-asset";
      media.alt = caption;
      media.decoding = "async";
      content.replaceChildren(media);
    }
    if (title) title.textContent = caption;
    if (meta) meta.textContent = item.dataset.meta || "";
    if (counter) counter.textContent = `${activeIndex + 1} / ${items.length}`;
    if (detail) detail.href = item.dataset.detailUrl;
    if (download) download.href = item.dataset.downloadUrl;
    if (previous) previous.disabled = activeIndex === 0;
    if (next) next.disabled = activeIndex === items.length - 1;
  };

  const openAt = (index, item) => {
    returnFocus = item;
    render(index);
    dialog.showModal();
    close?.focus();
  };

  items.forEach((item, index) => {
    item.addEventListener("click", (event) => {
      if (
        event.button !== 0
        || event.metaKey
        || event.ctrlKey
        || event.shiftKey
        || event.altKey
      ) return;
      event.preventDefault();
      openAt(index, item);
    });
  });

  previous?.addEventListener("click", () => render(activeIndex - 1));
  next?.addEventListener("click", () => render(activeIndex + 1));
  close?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => {
    clearMedia();
    delete dialog.dataset.mediaKind;
    returnFocus?.focus();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.target instanceof Element && event.target.closest("[data-video-player]")) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      render(activeIndex - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      render(activeIndex + 1);
    }
  });
  dialog.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || event.target.closest("a, button, input, video, [data-video-player]")) return;
    pointerStart = { x: event.clientX, y: event.clientY };
  });
  dialog.addEventListener("pointerup", (event) => {
    if (!pointerStart || !event.isPrimary) return;
    const distanceX = event.clientX - pointerStart.x;
    const distanceY = event.clientY - pointerStart.y;
    pointerStart = null;
    if (Math.abs(distanceX) < 56 || Math.abs(distanceX) < Math.abs(distanceY)) return;
    render(activeIndex + (distanceX < 0 ? 1 : -1));
  });
}

export { setupMediaGallery };
