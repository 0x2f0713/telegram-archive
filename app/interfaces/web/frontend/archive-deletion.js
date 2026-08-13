function setupArchiveDeletion() {
  const dialog = document.querySelector("[data-archive-delete-dialog]");
  const form = dialog?.querySelector("[data-archive-delete-form]");
  const confirmation = dialog?.querySelector("[data-archive-delete-confirmation]");
  const submit = dialog?.querySelector("[data-archive-delete-submit]");
  const title = dialog?.querySelector("[data-archive-delete-title]");
  const messages = dialog?.querySelector("[data-archive-delete-messages]");
  const files = dialog?.querySelector("[data-archive-delete-files]");
  if (!dialog || !form || !confirmation || !submit) return;

  const number = new Intl.NumberFormat();
  let returnFocus = null;

  const reset = () => {
    confirmation.value = "";
    submit.disabled = true;
    submit.textContent = "Delete local archive";
  };

  document.querySelectorAll("[data-archive-delete-open]").forEach((trigger) => {
    trigger.addEventListener("click", () => {
      if (trigger.disabled) return;
      returnFocus = trigger;
      form.action = trigger.dataset.deleteAction;
      if (title) title.textContent = trigger.dataset.chatTitle || "Archived chat";
      if (messages) {
        const count = Number.parseInt(trigger.dataset.messageCount || "0", 10);
        messages.textContent = `${number.format(count)} message${count === 1 ? "" : "s"}`;
      }
      if (files) {
        const count = Number.parseInt(trigger.dataset.fileCount || "0", 10);
        files.textContent = `${number.format(count)} file${count === 1 ? "" : "s"}`;
      }
      reset();
      dialog.showModal();
      window.requestAnimationFrame(() => confirmation.focus());
    });
  });

  confirmation.addEventListener("input", () => {
    submit.disabled = confirmation.value.trim() !== "DELETE";
  });
  form.addEventListener("submit", () => {
    submit.disabled = true;
    submit.textContent = "Deleting…";
  });
  dialog.querySelector("[data-archive-delete-cancel]")?.addEventListener("click", () => {
    dialog.close();
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => {
    reset();
    returnFocus?.focus();
  });
}

export { setupArchiveDeletion };
