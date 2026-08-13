function setupQuickChats() {
  const dialog = document.querySelector("[data-quick-chat-dialog]");
  if (!dialog) return;

  const triggers = [...document.querySelectorAll("[data-quick-chat-open]")];
  const closeButton = dialog.querySelector("[data-quick-chat-close]");
  const search = dialog.querySelector("[data-quick-chat-search]");
  const results = dialog.querySelector("[data-quick-chat-results]");
  const status = dialog.querySelector("[data-quick-chat-status]");
  const refreshSeconds = Math.max(
    5,
    Number.parseInt(document.body.dataset.refreshSeconds || "15", 10) || 15,
  );
  let restoreFocus = null;
  let requestController = null;
  let refreshTimer = 0;
  let searchTimer = 0;

  const setExpanded = (expanded) => {
    triggers.forEach((trigger) => trigger.setAttribute("aria-expanded", String(expanded)));
  };

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const formatCount = (value) => new Intl.NumberFormat().format(Number(value) || 0);
  const formatDate = (value) => {
    if (!value) return "No messages yet";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return "Archive date unavailable";
    return `Latest ${new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    }).format(date)}`;
  };

  const resultLinks = () => [...results.querySelectorAll("a[data-quick-chat-link]")];

  const renderLoading = () => {
    results.replaceChildren();
    results.setAttribute("aria-busy", "true");
    const shell = element("div", "quick-chat-loading");
    for (let index = 0; index < 4; index += 1) {
      const row = element("span", "quick-chat-skeleton");
      row.setAttribute("aria-hidden", "true");
      shell.append(row);
    }
    results.append(shell);
    status.textContent = "Loading archived chats";
  };

  const renderItem = (chat) => {
    const link = element("a", `quick-chat-row${chat.active ? " is-active" : ""}`);
    link.href = chat.href;
    link.dataset.quickChatLink = "";
    link.dataset.chatId = String(chat.telegram_chat_id);

    const identity = element("span", "quick-chat-identity");
    identity.append(element("strong", "", chat.title));
    const handle = chat.username ? `@${chat.username}` : `${chat.type} ${chat.telegram_chat_id}`;
    identity.append(element("small", "", handle));

    const meta = element("span", "quick-chat-meta");
    if (chat.active) {
      const active = element("strong", "quick-chat-live", "Archiving now");
      const phase = String(chat.active_phase || chat.active_command || "running").replaceAll("-", " ");
      active.append(element("small", "", phase));
      meta.append(active);
    } else {
      meta.append(element("small", "", formatDate(chat.newest_message_date)));
    }
    const counts = element(
      "span",
      "quick-chat-counts",
      `${formatCount(chat.message_count)} messages · ${formatCount(chat.completed_count)} files`,
    );
    if (chat.failed_count) {
      counts.append(element("em", "", ` · ${formatCount(chat.failed_count)} failed`));
    }
    meta.append(counts);
    link.append(identity, meta);
    return link;
  };

  const renderGroup = (label, chats, active = false) => {
    if (!chats.length) return null;
    const group = element("section", `quick-chat-group${active ? " active-group" : ""}`);
    group.setAttribute("aria-label", label);
    group.append(element("h3", "", label));
    const list = element("nav", "quick-chat-list");
    list.setAttribute("aria-label", label);
    chats.forEach((chat) => list.append(renderItem(chat)));
    group.append(list);
    return group;
  };

  const updateBadges = (activeCount) => {
    document.querySelectorAll("[data-quick-chat-active-badge]").forEach((badge) => {
      badge.hidden = activeCount === 0;
      badge.textContent = activeCount ? `${activeCount} active` : "";
    });
  };

  const renderChats = (payload, focusedChatId) => {
    const items = Array.isArray(payload.items) ? payload.items : [];
    const active = items.filter((chat) => chat.active);
    const archived = items.filter((chat) => !chat.active);
    results.replaceChildren();
    results.setAttribute("aria-busy", "false");
    const activeGroup = renderGroup("Archiving now", active, true);
    const archiveGroup = renderGroup("Recently archived", archived);
    if (activeGroup) results.append(activeGroup);
    if (archiveGroup) results.append(archiveGroup);
    if (!items.length) {
      const empty = element("div", "quick-chat-empty");
      empty.append(element("strong", "", search.value.trim() ? "No matching chats" : "No archived chats yet"));
      empty.append(element(
        "p",
        "",
        search.value.trim()
          ? "Try a title, username, chat type, or Telegram ID."
          : "Run a sync or listener to make conversations available here.",
      ));
      const action = element("a", "", search.value.trim() ? "Clear search" : "Open operations");
      action.href = search.value.trim() ? "#" : "/operations";
      if (search.value.trim()) {
        action.addEventListener("click", (event) => {
          event.preventDefault();
          search.value = "";
          loadChats();
          search.focus();
        });
      }
      empty.append(action);
      results.append(empty);
    }
    updateBadges(payload.active_chat_id ? 1 : 0);
    status.textContent = items.length
      ? `${items.length} archived chat${items.length === 1 ? "" : "s"} available`
      : "No archived chats available";
    if (focusedChatId) {
      resultLinks().find((link) => link.dataset.chatId === focusedChatId)?.focus();
    }
  };

  const renderError = () => {
    results.replaceChildren();
    results.setAttribute("aria-busy", "false");
    const error = element("div", "quick-chat-empty quick-chat-error");
    error.append(element("strong", "", "Archived chats are unavailable"));
    error.append(element("p", "", "The archive could not be read. Your current page is still available."));
    const retry = element("button", "", "Try again");
    retry.type = "button";
    retry.addEventListener("click", () => loadChats());
    error.append(retry);
    results.append(error);
    status.textContent = "Archived chats could not be loaded";
  };

  async function loadChats({ quiet = false } = {}) {
    requestController?.abort();
    requestController = new AbortController();
    const focusedChatId = document.activeElement?.dataset?.chatId || "";
    if (!quiet) renderLoading();
    const query = new URLSearchParams({ limit: "20" });
    if (search.value.trim()) query.set("q", search.value.trim());
    try {
      const response = await fetch(`/api/v1/chats/quick-access?${query}`, {
        headers: { Accept: "application/json" },
        signal: requestController.signal,
      });
      if (!response.ok) throw new Error(`Quick chat request failed with ${response.status}`);
      renderChats(await response.json(), focusedChatId);
    } catch (error) {
      if (error.name !== "AbortError") renderError();
    }
  }

  const close = () => {
    window.clearInterval(refreshTimer);
    refreshTimer = 0;
    requestController?.abort();
    if (dialog.open && typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
    setExpanded(false);
    restoreFocus?.focus();
  };

  const open = (trigger) => {
    restoreFocus = trigger.closest(".mobile-menu")?.querySelector("summary") || trigger;
    document.querySelector(".mobile-menu")?.removeAttribute("open");
    if (!dialog.open) {
      if (typeof dialog.show === "function") dialog.show();
      else dialog.setAttribute("open", "");
    }
    setExpanded(true);
    loadChats();
    window.clearInterval(refreshTimer);
    refreshTimer = window.setInterval(() => loadChats({ quiet: true }), refreshSeconds * 1000);
    window.requestAnimationFrame(() => search.focus());
  };

  triggers.forEach((trigger) => trigger.addEventListener("click", () => {
    if (dialog.open) close();
    else open(trigger);
  }));
  closeButton.addEventListener("click", close);
  dialog.addEventListener("close", () => {
    window.clearInterval(refreshTimer);
    setExpanded(false);
  });
  search.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => loadChats(), 180);
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const links = resultLinks();
    if (!links.length) return;
    event.preventDefault();
    const current = links.indexOf(document.activeElement);
    let next = event.key === "End" ? links.length - 1 : 0;
    if (event.key === "ArrowDown") next = current < 0 ? 0 : (current + 1) % links.length;
    if (event.key === "ArrowUp") next = current < 0 ? links.length - 1 : (current - 1 + links.length) % links.length;
    links[next].focus();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!dialog.open || dialog.contains(event.target)) return;
    if (event.target.closest("[data-quick-chat-open]")) return;
    close();
  });
  loadChats({ quiet: true });
}

export { setupQuickChats };
