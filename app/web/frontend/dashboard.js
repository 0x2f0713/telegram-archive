import "@fontsource-variable/geist";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import "../static/dashboard.css";

gsap.registerPlugin(ScrollTrigger);

const root = document.documentElement;
root.classList.add("js");

const storageKey = "telegram-archiver-theme";
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

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

function setupStateAccordion() {
  const controls = [...document.querySelectorAll("[data-state-control]")];
  controls.forEach((control) => {
    control.addEventListener("click", () => {
      controls.forEach((item) => {
        const active = item === control;
        item.setAttribute("aria-expanded", String(active));
        item.closest("[data-state-item]")?.classList.toggle("is-active", active);
      });
    });
  });
}

function setupCarousels() {
  document.querySelectorAll("[data-carousel]").forEach((carousel) => {
    const rail = carousel.querySelector("[data-carousel-rail]");
    if (!rail) return;
    carousel.querySelector("[data-carousel-previous]")?.addEventListener("click", () => {
      rail.scrollBy({ left: -Math.max(280, rail.clientWidth * 0.8), behavior: "smooth" });
    });
    carousel.querySelector("[data-carousel-next]")?.addEventListener("click", () => {
      rail.scrollBy({ left: Math.max(280, rail.clientWidth * 0.8), behavior: "smooth" });
    });
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

function setupMotion() {
  if (reducedMotion.matches) return;
  const context = gsap.context(() => {
    const heroTargets = document.querySelectorAll(".hero-copy > *, .hero-record");
    if (heroTargets.length) {
      gsap.from(heroTargets, {
        autoAlpha: 0,
        y: 24,
        duration: 0.7,
        stagger: 0.06,
        ease: "power3.out",
        clearProps: "all",
      });
    }

    document.querySelectorAll("[data-scrub-text]").forEach((block) => {
      const words = block.querySelectorAll("span");
      if (!words.length) return;
      gsap.to(words, {
        opacity: 1,
        stagger: 0.08,
        ease: "none",
        scrollTrigger: {
          trigger: block,
          start: "top 78%",
          end: "bottom 48%",
          scrub: 1,
        },
      });
    });

    const cards = [...document.querySelectorAll("[data-stack-card]")];
    cards.slice(0, -1).forEach((card, index) => {
      gsap.to(card, {
        scale: 0.96,
        opacity: 0.48,
        ease: "none",
        scrollTrigger: {
          trigger: cards[index + 1],
          start: "top 78%",
          end: "top 30%",
          scrub: 1,
        },
      });
    });
  });
  document.fonts?.ready.then(() => ScrollTrigger.refresh());
  window.addEventListener("pagehide", () => context.revert(), { once: true });
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

function setupChatSelection() {
  const form = document.querySelector("[data-chat-selection]");
  if (!form) return;
  const choices = [...form.querySelectorAll("[data-chat-choice]")];
  const modeInputs = [...form.querySelectorAll('input[name="mode"]')];
  const summary = form.querySelector("[data-selection-summary]");
  const modeLabel = form.querySelector("[data-selection-mode-label]");
  const warning = form.querySelector("[data-all-mode-warning]");
  const search = form.querySelector("[data-chat-filter]");
  const empty = form.querySelector("[data-chat-filter-empty]");
  const selectVisible = form.querySelector("[data-select-visible]");
  const clearVisible = form.querySelector("[data-clear-visible]");
  const save = form.querySelector("[data-save-selection]");
  const specificIds = new Set(
    choices
      .filter((choice) => choice.dataset.specificSelected === "true")
      .map((choice) => choice.querySelector('input[name="chat_id"]').value),
  );

  const activeMode = () => modeInputs.find((input) => input.checked)?.value || "specific";
  const visibleChoices = () => choices.filter((choice) => !choice.hidden);
  const selectedForMode = (choice, mode) => {
    const checkbox = choice.querySelector('input[name="chat_id"]');
    if (mode === "all") return true;
    if (mode === "environment") return choice.dataset.environmentSelected === "true";
    return specificIds.has(checkbox.value);
  };
  const updateSummary = () => {
    const mode = activeMode();
    const count = choices.filter((choice) => selectedForMode(choice, mode)).length;
    const text = mode === "all"
      ? `${choices.length} accessible dialogs will be archived, including new dialogs found at startup`
      : mode === "environment"
        ? `${count} accessible dialogs match the current environment configuration`
        : `${count} of ${choices.length} accessible dialogs selected`;
    if (summary) summary.textContent = text;
    if (modeLabel) {
      modeLabel.textContent = {
        all: "All accessible mode",
        environment: "Environment defaults",
        specific: "Specific chats",
      }[mode];
    }
  };
  const applyMode = () => {
    const mode = activeMode();
    choices.forEach((choice) => {
      const checkbox = choice.querySelector('input[name="chat_id"]');
      const selected = selectedForMode(choice, mode);
      checkbox.checked = selected;
      checkbox.disabled = mode !== "specific";
      choice.classList.toggle("is-selected", selected);
      choice.querySelector(".choice-state").textContent = selected ? "Selected" : "Not selected";
    });
    const specific = mode === "specific";
    [selectVisible, clearVisible].forEach((button) => {
      if (button) button.disabled = !specific;
    });
    if (warning) warning.hidden = mode !== "all";
    updateSummary();
  };
  const filter = () => {
    const query = search?.value.trim().toLocaleLowerCase() || "";
    choices.forEach((choice) => {
      choice.hidden = !choice.dataset.search.toLocaleLowerCase().includes(query);
    });
    if (empty) empty.hidden = visibleChoices().length > 0;
  };
  const setVisible = (selected) => {
    if (activeMode() !== "specific") return;
    visibleChoices().forEach((choice) => {
      const checkbox = choice.querySelector('input[name="chat_id"]');
      checkbox.checked = selected;
      selected ? specificIds.add(checkbox.value) : specificIds.delete(checkbox.value);
    });
    applyMode();
  };

  modeInputs.forEach((input) => input.addEventListener("change", applyMode));
  choices.forEach((choice) => {
    const checkbox = choice.querySelector('input[name="chat_id"]');
    checkbox.addEventListener("change", () => {
      checkbox.checked ? specificIds.add(checkbox.value) : specificIds.delete(checkbox.value);
      applyMode();
    });
  });
  search?.addEventListener("input", filter);
  selectVisible?.addEventListener("click", () => setVisible(true));
  clearVisible?.addEventListener("click", () => setVisible(false));
  form.addEventListener("submit", () => {
    choices.forEach((choice) => {
      choice.querySelector('input[name="chat_id"]').disabled = false;
    });
    if (save) {
      save.disabled = true;
      save.textContent = "Saving…";
    }
  });
  applyMode();
  filter();
}

function setupOperationMonitor() {
  const monitor = document.querySelector("[data-operation-monitor]");
  if (!monitor) return;
  const operationId = monitor.dataset.operationId;
  const status = monitor.querySelector("[data-operation-status]");
  const detail = monitor.querySelector("[data-operation-detail]");
  const phase = monitor.querySelector("[data-operation-phase]");
  const progress = monitor.querySelector("[data-operation-progress]");
  const progressLabel = monitor.querySelector("[data-operation-progress-label]");
  const chats = monitor.querySelector("[data-operation-chats]");
  const messages = monitor.querySelector("[data-operation-messages]");
  const downloads = monitor.querySelector("[data-operation-downloads]");
  const retries = monitor.querySelector("[data-operation-retries]");
  const elapsed = monitor.querySelector("[data-operation-elapsed]");
  const error = monitor.querySelector("[data-operation-error]");
  const logs = monitor.querySelector("[data-operation-logs]");
  const stopForm = monitor.querySelector("[data-operation-stop-form]");
  let timer = 0;

  const statusClasses = [
    "status-queued", "status-running", "status-stopping", "status-completed",
    "status-failed", "status-cancelled", "status-interrupted",
  ];
  const formatted = (value) => new Intl.NumberFormat().format(value || 0);
  const renderLogs = (entries) => {
    if (!logs) return;
    logs.replaceChildren();
    if (!entries.length) {
      const empty = document.createElement("li");
      empty.className = "operation-log-empty";
      empty.textContent = "Waiting for the first operation event.";
      logs.append(empty);
      return;
    }
    entries.forEach((entry) => {
      const item = document.createElement("li");
      item.className = `log-${entry.level.toLocaleLowerCase()}`;
      const time = document.createElement("time");
      time.dateTime = entry.created_at;
      time.textContent = new Date(entry.created_at).toLocaleString();
      const level = document.createElement("strong");
      level.textContent = entry.level;
      const message = document.createElement("span");
      message.textContent = entry.message;
      item.append(time, level, message);
      logs.append(item);
    });
  };
  const render = (payload) => {
    const operation = payload.operation;
    if (status) {
      status.classList.remove(...statusClasses);
      status.classList.add(`status-${operation.status}`);
      status.textContent = operation.status;
    }
    if (detail) detail.textContent = operation.detail || "Operation is active";
    if (phase) phase.textContent = operation.phase.replaceAll("-", " ");
    if (progress) {
      if (operation.progress_total) {
        progress.max = operation.progress_total;
        progress.value = operation.progress_current;
        progress.setAttribute(
          "aria-label",
          `Operation progress: ${operation.progress_current} of ${operation.progress_total}`,
        );
      } else {
        progress.removeAttribute("value");
        progress.removeAttribute("max");
        progress.setAttribute("aria-label", "Operation is active; total work is not known");
      }
    }
    if (progressLabel) {
      progressLabel.textContent = operation.progress_total
        ? `${operation.progress_current} / ${operation.progress_total}`
        : "Live progress";
    }
    if (chats) chats.textContent = `${operation.chats_completed} / ${operation.chats_total || "—"}`;
    if (messages) messages.textContent = formatted(operation.messages_processed);
    if (downloads) downloads.textContent = formatted(operation.downloads_completed);
    if (retries) retries.textContent = `${operation.retry_completed} / ${operation.retry_attempted}`;
    if (elapsed) elapsed.textContent = `${operation.elapsed_seconds}s`;
    if (error) {
      error.hidden = !operation.error;
      if (operation.error) error.textContent = operation.error;
    }
    renderLogs(payload.logs || []);
    if (operation.terminal) {
      window.clearInterval(timer);
      stopForm?.remove();
      const commands = document.querySelector("[data-operation-commands]");
      const anotherOperationIsActive = Boolean(
        commands?.dataset.activeOperation
        && commands.dataset.activeOperation !== String(operation.id),
      );
      document.querySelectorAll("[data-operation-start-form] button[type='submit']").forEach((button) => {
        const needsSelection = button.closest("form")?.querySelector('[name="command"]')?.value !== "doctor";
        const selectedCount = Number.parseInt(
          commands?.dataset.selectedChats || "0",
          10,
        );
        if (!anotherOperationIsActive && (!needsSelection || selectedCount > 0)) {
          button.disabled = false;
          button.removeAttribute("aria-disabled");
        }
      });
    } else if (operation.status === "stopping") {
      const stopButton = stopForm?.querySelector("button");
      if (stopButton) {
        stopButton.disabled = true;
        stopButton.textContent = "Stopping safely…";
      }
    }
  };
  const poll = async () => {
    try {
      const response = await fetch(`/api/v1/operations/${operationId}`, {
        headers: { Accept: "application/json" },
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!response.ok) return;
      render(await response.json());
    } catch {
      if (detail) detail.textContent = "Progress connection paused. Retrying locally.";
    }
  };
  timer = window.setInterval(poll, 1200);
  poll();
  window.addEventListener("pagehide", () => window.clearInterval(timer), { once: true });
}

function setupOperationForms() {
  document.querySelectorAll("[data-operation-start-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type='submit']");
      if (!button) return;
      button.disabled = true;
      button.textContent = "Starting…";
    });
  });
  document.querySelector("[data-operation-stop-form]")?.addEventListener("submit", (event) => {
    const button = event.currentTarget.querySelector("button[type='submit']");
    if (!button) return;
    button.disabled = true;
    button.textContent = "Requesting safe stop…";
  });
}

function initialize() {
  setupThemes();
  setupCopyActions();
  setupCommandMenu();
  setupStateAccordion();
  setupCarousels();
  setupRouteProgress();
  setupAutoRefresh();
  setupMotion();
  setupMobileMenu();
  setupTelegramAuth();
  setupChatSelection();
  setupOperationMonitor();
  setupOperationForms();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initialize, { once: true });
} else {
  initialize();
}
