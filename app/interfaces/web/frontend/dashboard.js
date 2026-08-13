import "@fontsource-variable/geist";
import "../static/dashboard.css";

import {
  setupAutoRefresh,
  setupCommandMenu,
  setupCopyActions,
  setupMobileMenu,
  setupRouteProgress,
  setupTelegramAuth,
  setupThemes,
} from "./core.js";
import { setupChatSelection } from "./chat-selection.js";
import { setupOperationForms, setupOperationMonitor } from "./operations.js";
import { setupQuickChats } from "./quick-chats.js";
import { setupVideoPlayers } from "./media-player.js";

function initialize() {
  setupThemes();
  setupCopyActions();
  setupCommandMenu();
  setupRouteProgress();
  setupAutoRefresh();
  setupMobileMenu();
  setupTelegramAuth();
  setupChatSelection();
  setupOperationMonitor();
  setupOperationForms();
  setupQuickChats();
  setupVideoPlayers();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initialize, { once: true });
} else {
  initialize();
}
