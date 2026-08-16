const root = document.documentElement;
root.classList.add("js");

const storageKey = "telegram-archiver-theme";
const videoPlayers = new WeakMap();

function applyTheme(theme) {
  const normalized = ["dark", "light", "system"].includes(theme) ? theme : "dark";
  root.dataset.theme = normalized;
  document.querySelectorAll("[data-theme-select]").forEach((select) => {
    select.value = normalized;
  });
  const effective = normalized === "system"
    ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : normalized;
  document.querySelector('meta[name="theme-color"]')?.setAttribute(
    "content",
    effective === "dark" ? "#081210" : "#f2f5ef",
  );
}

function setupThemes() {
  applyTheme(localStorage.getItem(storageKey) || "dark");
  document.querySelectorAll("[data-theme-select]").forEach((select) => {
    select.addEventListener("change", () => {
      localStorage.setItem(storageKey, select.value);
      applyTheme(select.value);
    });
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (root.dataset.theme === "system") applyTheme("system");
  });
}

function showToast(message) {
  const toast = document.querySelector("[data-toast]");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.clearTimeout(showToast.timeoutId);
  showToast.timeoutId = window.setTimeout(() => toast.classList.remove("is-visible"), 2400);
}
showToast.timeoutId = 0;

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const field = document.createElement("textarea");
    field.value = value;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    field.select();
    document.execCommand("copy");
    field.remove();
  }
  showToast("Copied to clipboard");
}

function setupCopyActions() {
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-copy-value]");
    if (!trigger) return;
    copyText(trigger.dataset.copyValue || "");
  });
}

function setupCommandMenu() {
  const dialog = document.querySelector("[data-command-dialog]");
  if (!dialog) return;
  const search = dialog.querySelector("[data-command-search]");
  const items = [...dialog.querySelectorAll("[data-command-item]")];
  const empty = dialog.querySelector("[data-command-empty]");
  let selectedIndex = 0;

  const visibleItems = () => items.filter((item) => !item.hidden);
  const setSelection = (index) => {
    const visible = visibleItems();
    if (!visible.length) return;
    selectedIndex = (index + visible.length) % visible.length;
    items.forEach((item) => item.classList.remove("is-selected"));
    visible[selectedIndex].classList.add("is-selected");
    visible[selectedIndex].scrollIntoView({ block: "nearest" });
  };
  const filter = () => {
    const query = search.value.trim().toLocaleLowerCase();
    items.forEach((item) => {
      item.hidden = !item.textContent.toLocaleLowerCase().includes(query);
    });
    empty.hidden = visibleItems().length > 0;
    setSelection(0);
  };
  const open = () => {
    if (!dialog.open) dialog.showModal();
    search.value = "";
    filter();
    window.requestAnimationFrame(() => search.focus());
  };

  document.querySelectorAll("[data-command-open]").forEach((button) => {
    button.addEventListener("click", open);
  });
  dialog.querySelector("[data-command-close]")?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => items.forEach((item) => item.classList.remove("is-selected")));
  search.addEventListener("input", filter);
  search.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setSelection(selectedIndex + (event.key === "ArrowDown" ? 1 : -1));
    }
    if (event.key === "Enter") {
      const selected = visibleItems()[selectedIndex];
      if (selected) {
        event.preventDefault();
        selected.click();
      }
    }
  });
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "k") {
      event.preventDefault();
      dialog.open ? dialog.close() : open();
    }
    if (
      event.key === "/"
      && !event.metaKey
      && !event.ctrlKey
      && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)
    ) {
      const pageSearch = document.querySelector('input[name="q"]');
      if (pageSearch) {
        event.preventDefault();
        pageSearch.focus();
      }
    }
  });
}

function setupRouteProgress() {
  const progress = document.querySelector("[data-route-progress]");
  if (!progress) return;
  const start = () => progress.classList.add("is-loading");
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey) return;
    const target = new URL(link.href, window.location.href);
    if (target.origin === window.location.origin && target.href !== window.location.href) start();
  });
  document.addEventListener("submit", start);
  window.addEventListener("pageshow", () => progress.classList.remove("is-loading"));
}

function setupAutoRefresh() {
  if (document.body.dataset.autoRefresh !== "true") return;
  const seconds = Number.parseInt(document.body.dataset.refreshSeconds || "15", 10);
  let lastInteraction = Date.now();
  ["keydown", "pointerdown", "touchstart", "wheel"].forEach((eventName) => {
    window.addEventListener(eventName, () => { lastInteraction = Date.now(); }, { passive: true });
  });
  window.setInterval(() => {
    const focusIsIdle = [document.body, document.documentElement].includes(document.activeElement);
    const interactionIsIdle = Date.now() - lastInteraction >= seconds * 1000;
    const dialogOpen = Boolean(document.querySelector("dialog[open]"));
    if (document.visibilityState === "visible" && focusIsIdle && interactionIsIdle && !dialogOpen) {
      window.location.reload();
    }
  }, Math.max(5, seconds) * 1000);
}

function setupMobileMenu() {
  const menu = document.querySelector(".mobile-menu");
  if (!menu) return;
  menu.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => {
    menu.open = false;
  }));
}

function setupTelegramAuth() {
  const region = document.querySelector("[data-telegram-auth]");
  if (!region || region.dataset.authStatus !== "pending") return;
  const expiry = region.querySelector("[data-auth-expiry]");
  const expiresAt = expiry?.dateTime ? new Date(expiry.dateTime) : null;
  const updateCountdown = () => {
    if (!expiry || !expiresAt || Number.isNaN(expiresAt.getTime())) return;
    const remaining = Math.max(0, Math.ceil((expiresAt.getTime() - Date.now()) / 1000));
    expiry.textContent = remaining > 0 ? `Expires in ${remaining} seconds` : "Checking code status";
  };
  updateCountdown();
  const countdown = window.setInterval(updateCountdown, 1000);
  const poll = window.setInterval(async () => {
    try {
      const response = await fetch(region.dataset.statusUrl, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) return;
      const state = await response.json();
      if (state.status !== "pending") window.location.reload();
    } catch {
      const detail = region.querySelector("[data-auth-detail]");
      if (detail) detail.textContent = "Connection check paused. Retrying locally.";
    }
  }, 1800);
  window.addEventListener("pagehide", () => {
    window.clearInterval(countdown);
    window.clearInterval(poll);
  }, { once: true });
}

export {
  setupAutoRefresh,
  setupCommandMenu,
  setupCopyActions,
  setupMobileMenu,
  setupRouteProgress,
  setupTelegramAuth,
  setupThemes,
};
