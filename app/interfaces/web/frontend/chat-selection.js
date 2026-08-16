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

export { setupChatSelection };
