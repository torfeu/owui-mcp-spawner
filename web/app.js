import { applyEditMode, clearToken, getToken, hideLoginModal, setToken, showLoginModal, state } from "./common.js";
import { bindEdit, openEdit } from "./config.js";
import { openEditorForInstance } from "./editor.js";
import { bindFilter, configureInstanceActions, loadInstances } from "./instances.js";
import { bindInfo, openInfo } from "./info.js";
import { bindLogs, openLogs } from "./logs.js";
import { bindPermissions } from "./permissions.js";
import { bindStats } from "./stats.js";
import { bindUpload } from "./upload.js";
import { refreshUpdateBadge } from "./settings.js";

configureInstanceActions({ openEdit, openEditorForInstance, openLogs, openInfo });

document.getElementById("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const password = document.getElementById("login-password").value;
  const response = await fetch("/api/auth-check", { headers: { "Authorization": `Bearer ${password}` } });
  if (response.ok) {
    setToken(password);
    state.guestMode = false;
    hideLoginModal();
    applyEditMode();
    loadInstances();
    refreshUpdateBadge();
    startPolling();
  } else {
    document.getElementById("login-error").classList.remove("hidden");
  }
});

document.getElementById("login-btn").addEventListener("click", showLoginModal);
document.getElementById("login-cancel").addEventListener("click", () => {
  hideLoginModal();
  if (state.authEnabled && !getToken() && !state.guestMode) {
    state.guestMode = true;
    applyEditMode();
    loadInstances();
  }
});
document.getElementById("logout-btn").addEventListener("click", () => {
  clearToken();
  location.reload();
});

document.addEventListener("DOMContentLoaded", async () => {
  bindUpload();
  bindEdit();
  bindLogs();
  bindInfo();
  bindStats();
  bindPermissions();
  bindFilter();

  const statusResponse = await fetch("/api/auth-status");
  const status = await statusResponse.json();
  state.editMode = status.edit_mode || "full";
  state.authEnabled = !!status.auth_enabled;
  if (status.version) document.getElementById("app-version").textContent = `v${status.version}`;

  state.guestMode = state.authEnabled && !getToken();
  applyEditMode();
  loadInstances();
  refreshUpdateBadge();
  startPolling();
});

function startPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(loadInstances, 4000);
}
