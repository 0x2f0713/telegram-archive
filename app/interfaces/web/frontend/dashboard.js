import "@fontsource-variable/geist";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import Plyr from "plyr";
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
  const count = form.querySelector("[data-chat-filter-count]");
  const empty = form.querySelector("[data-chat-filter-empty]");
  const selectVisible = form.querySelector("[data-select-visible]");
  const clearVisible = form.querySelector("[data-clear-visible]");
  const save = form.querySelector("[data-save-selection]");
  let selectedChoice = null;
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
    if (count) {
      count.hidden = !query || visibleChoices().length === 0;
      if (!count.hidden) count.textContent = `${visibleChoices().length} of ${choices.length} dialogs match`;
    }
    setHighlight(query ? 0 : -1);
  };
  const setHighlight = (index) => {
    const visible = visibleChoices();
    if (!visible.length) {
      selectedChoice = null;
      return;
    }
    if (index < 0) {
      selectedChoice = null;
      choices.forEach((choice) => choice.classList.remove("is-highlighted"));
      return;
    }
    selectedChoice = visible[index % visible.length];
    choices.forEach((choice) => choice.classList.remove("is-highlighted"));
    selectedChoice.classList.add("is-highlighted");
    selectedChoice.scrollIntoView({ block: "nearest" });
  };
  const openChat = (choice) => {
    const conversation = choice?.querySelector('a[href^="/chats/"]');
    if (conversation) window.location.href = conversation.href;
  };
  const moveHighlight = (offset) => {
    const visible = visibleChoices();
    if (!visible.length) return;
    const currentIndex = visible.indexOf(selectedChoice);
    setHighlight(
      currentIndex === -1 ? (offset > 0 ? 0 : visible.length - 1) : currentIndex + offset,
    );
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
  search?.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveHighlight(event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (selectedChoice) openChat(selectedChoice);
    } else if (event.key === "Escape") {
      event.preventDefault();
      if (search.value) {
        search.value = "";
        filter();
      } else {
        search.blur();
      }
    }
  });
  document.addEventListener("keydown", (event) => {
    if (
      event.key === "/"
      && !event.metaKey
      && !event.ctrlKey
      && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)
    ) {
      const chatFilter = form.querySelector("[data-chat-filter]");
      if (chatFilter) {
        event.preventDefault();
        chatFilter.focus();
      }
    }
  });
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
  const downloadDetails = monitor.querySelector("[data-operation-download-details]");
  const downloadList = monitor.querySelector("[data-operation-download-list]");
  const downloadSummary = monitor.querySelector("[data-operation-download-summary]");
  const logs = monitor.querySelector("[data-operation-logs]");
  const actionContainer = monitor.querySelector("[data-operation-actions]");
  const commands = document.querySelector("[data-operation-commands]");
  const csrfToken = document.querySelector('input[name="csrf_token"]')?.value || "";
  let timer = 0;

  const statusClasses = [
    "status-queued", "status-running", "status-stopping", "status-completed",
    "status-failed", "status-cancelled", "status-interrupted",
  ];
  const formatted = (value) => new Intl.NumberFormat().format(value || 0);
  const humanBytes = (value) => {
    let size = Number(value) || 0;
    const units = ["B", "KB", "MB", "GB", "TB"];
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return `${size.toFixed(1)} ${units[unit]}`;
  };
  const renderDownloadTasks = (tasks) => {
    if (!downloadList) return;
    downloadList.replaceChildren();
    const entries = Array.isArray(tasks) ? tasks : [];
    const active = entries.filter((task) => task.status !== "completed").length;
    if (downloadSummary) {
      downloadSummary.textContent = `${active} active · ${entries.length} shown`;
    }
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "operation-download-empty";
      empty.textContent = "No media transfer is active.";
      downloadList.append(empty);
      return;
    }
    entries.forEach((task) => {
      const current = Number(task.current) || 0;
      const total = Number(task.total) || 0;
      const percent = Number(task.percent) || (total ? current / total * 100 : 0);
      const row = document.createElement("article");
      row.className = "operation-download-progress";
      const head = document.createElement("div");
      head.className = "operation-download-progress-head";
      const filename = document.createElement("strong");
      filename.textContent = task.filename || "Unnamed media";
      const percentage = document.createElement("span");
      percentage.textContent = `${percent.toFixed(1)}%`;
      head.append(filename, percentage);
      const bar = document.createElement("progress");
      bar.value = current;
      bar.max = total || 1;
      bar.setAttribute("aria-label", `Download progress for ${task.filename || "media"}`);
      bar.textContent = `${percent.toFixed(1)}%`;
      const stats = document.createElement("small");
      stats.textContent = `${humanBytes(current)} / ${humanBytes(total)} · ${humanBytes(task.speed || 0)}/s · ${task.status || "downloading"}`;
      row.append(head, bar, stats);
      downloadList.append(row);
    });
  };
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
  const renderAction = (operation) => {
    if (!actionContainer) return;
    const action = operation.action || { kind: "none", label: "", enabled: false };
    const blockedByOther = Boolean(
      commands?.dataset.activeOperation
      && commands.dataset.activeOperation !== String(operation.id)
      && ["resume", "retry"].includes(action.kind),
    );
    const enabled = action.enabled !== false && !blockedByOther;
    actionContainer.dataset.operationActionKind = action.kind;
    actionContainer.replaceChildren();
    if (action.kind === "none") return;
    const form = document.createElement("form");
    form.method = "post";
    form.action = `/operations/${operation.id}/${action.kind === "resume" ? "resume" : action.kind === "retry" ? "retry" : "stop"}`;
    form.dataset.operationActionForm = "";
    form.dataset.operationActionKind = action.kind;
    const token = document.createElement("input");
    token.type = "hidden";
    token.name = "csrf_token";
    token.value = csrfToken;
    const button = document.createElement("button");
    button.className = `button ${action.kind === "stop" ? "danger-button" : "primary"}`;
    button.type = "submit";
    button.textContent = action.label;
    button.disabled = !enabled;
    if (!enabled) button.setAttribute("aria-disabled", "true");
    form.append(token, button);
    actionContainer.append(form);
  };
  const updateCommandButtons = (operation) => {
    if (!commands) return;
    if (operation.active) {
      commands.dataset.activeOperation = String(operation.id);
    } else if (commands.dataset.activeOperation === String(operation.id)) {
      commands.dataset.activeOperation = "";
    }
    const anotherOperationIsActive = Boolean(
      commands.dataset.activeOperation
      && commands.dataset.activeOperation !== String(operation.id),
    );
    document.querySelectorAll("[data-operation-start-form] button[type='submit']").forEach((button) => {
      const needsSelection = button.closest("form")?.querySelector('[name="command"]')?.value !== "doctor";
      const selectedCount = Number.parseInt(commands.dataset.selectedChats || "0", 10);
      const disabled = operation.active || anotherOperationIsActive || (needsSelection && selectedCount <= 0);
      button.disabled = disabled;
      if (disabled) button.setAttribute("aria-disabled", "true");
      else button.removeAttribute("aria-disabled");
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
    renderDownloadTasks(operation.download_tasks);
    renderLogs(payload.logs || []);
    updateCommandButtons(operation);
    renderAction(operation);
    if (operation.terminal) window.clearInterval(timer);
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
  document.querySelectorAll("[data-content-type-picker]").forEach((picker) => {
    const checkboxes = [...picker.querySelectorAll('input[name="content_type"]')];
    const error = picker.querySelector("[data-content-type-error]");
    const disclosure = picker.closest("[data-content-disclosure]");
    const update = () => {
      const checked = checkboxes.filter((input) => input.checked);
      picker.classList.toggle("is-invalid", checked.length === 0);
      if (error && checked.length > 0) error.hidden = true;
      if (disclosure) {
        const summary = disclosure.querySelector("summary");
        const label = disclosure.dataset.contentLabel || "Content types";
        if (summary) {
          summary.textContent = checked.length === checkboxes.length
            ? `${label} · all selected`
            : `${label} · ${checked.length} of ${checkboxes.length} selected`;
        }
      }
    };
    picker.querySelectorAll("[data-content-select]").forEach((button) => {
      button.addEventListener("click", () => {
        const mode = button.dataset.contentSelect;
        checkboxes.forEach((input) => {
          input.checked = mode === "all"
            || (mode === "media" && input.dataset.downloadable === "true")
            || (mode === "text" && input.value === "text");
        });
        update();
      });
    });
    checkboxes.forEach((input) => input.addEventListener("change", update));
    update();
  });

  document.querySelectorAll("[data-operation-start-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      const emptyPicker = [...form.querySelectorAll("[data-content-type-picker]")].find(
        (picker) => !picker.querySelector('input[name="content_type"]:checked'),
      );
      if (emptyPicker) {
        event.preventDefault();
        emptyPicker.classList.add("is-invalid");
        const error = emptyPicker.querySelector("[data-content-type-error]");
        if (error) error.hidden = false;
        const disclosure = emptyPicker.closest("details");
        if (disclosure) disclosure.open = true;
        emptyPicker.querySelector('input[name="content_type"]')?.focus();
        return;
      }
      const button = form.querySelector("button[type='submit']");
      if (!button) return;
      button.disabled = true;
      button.textContent = "Starting…";
    });
  });
  document.querySelector("[data-operation-actions]")?.addEventListener("submit", (event) => {
    const button = event.currentTarget.querySelector("button[type='submit']");
    if (!button) return;
    const kind = event.target.closest("[data-operation-action-form]")?.dataset.operationActionKind;
    button.disabled = true;
    button.textContent = kind === "stop" ? "Requesting safe stop…" : kind === "resume" ? "Resuming…" : "Retrying…";
  });
}

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

function setupVideoPlayers() {
  document.querySelectorAll("[data-video-player] video").forEach((video) => {
    const host = video.closest("[data-video-player]");
    const badge = host?.querySelector("[data-video-speed]");
    const size = Number.parseInt(host?.dataset.mediaSize || "0", 10);
    const PROBE_BYTES = 4 * 1024 * 1024;
    const BUFFER_AHEAD_SECONDS = 20;
    let probe = null;
    let probeSpeed = 0;

    new Plyr(video, {
      controls: [
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
      if (!probe) return;
      probe.abort();
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
          badge.textContent = `⚡ ${formatSpeed(probeSpeed)} · ${formatBytes(loadedBytes)} loaded`;
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

    video.addEventListener("play", startProbe);
    video.addEventListener("waiting", startProbe);
    video.addEventListener("seeked", startProbe);
    video.addEventListener("canplaythrough", stopProbe);
    video.addEventListener("playing", () => {
      if (bufferedAhead() >= BUFFER_AHEAD_SECONDS) stopProbe();
    });
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
  setupVideoPlayers();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initialize, { once: true });
} else {
  initialize();
}
