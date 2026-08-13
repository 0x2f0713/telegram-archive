function setupGalleryThumbnails(gallery) {
  const thumbnails = [...gallery.querySelectorAll("video[data-gallery-thumbnail]")];
  if (!thumbnails.length) return;

  const loadThumbnail = (video) => {
    if (video.src || !video.dataset.src) return;
    video.src = video.dataset.src;
    video.preload = "metadata";
    video.addEventListener(
      "loadedmetadata",
      () => {
        if (Number.isFinite(video.duration) && video.duration > 0.1) video.currentTime = 0.1;
      },
      { once: true },
    );
    video.load();
  };

  if (!("IntersectionObserver" in window)) {
    thumbnails.forEach(loadThumbnail);
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        loadThumbnail(entry.target);
        observer.unobserve(entry.target);
      });
    },
    { rootMargin: "320px 0px" },
  );
  thumbnails.forEach((video) => observer.observe(video));
}

function setupMediaGallery() {
  const gallery = document.querySelector("[data-media-gallery]");
  const dialog = document.querySelector("[data-media-viewer]");
  if (!gallery || !dialog) return;

  setupGalleryThumbnails(gallery);
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

  const stopMedia = () => {
    content.querySelectorAll("video").forEach((video) => video.pause());
  };

  const render = (index) => {
    if (index < 0 || index >= items.length) return;
    stopMedia();
    activeIndex = index;
    const item = items[activeIndex];
    const caption = item.dataset.caption || "Archived media";
    const media = document.createElement(item.dataset.mediaKind === "video" ? "video" : "img");
    media.src = item.dataset.mediaSrc;
    media.className = "media-viewer-asset";
    if (media instanceof HTMLVideoElement) {
      media.controls = true;
      media.playsInline = true;
      media.preload = "metadata";
    } else {
      media.alt = caption;
      media.decoding = "async";
    }
    content.replaceChildren(media);
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
    stopMedia();
    content.replaceChildren();
    returnFocus?.focus();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.target instanceof HTMLVideoElement) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      render(activeIndex - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      render(activeIndex + 1);
    }
  });
  dialog.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || event.target.closest("a, button, video")) return;
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
