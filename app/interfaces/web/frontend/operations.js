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

export { setupOperationForms, setupOperationMonitor };
