import { applyEditMode, clearToken, getToken, hideLoginModal, setToken, showLoginModal, state } from "./common.js";
import { bindEdit, openEdit } from "./config.js";
import { openEditorForInstance } from "./editor.js";
import { bindFilter, bindPagination, bindSorting, configureInstanceActions, loadInstances } from "./instances.js";
import { bindCategories, loadCategories } from "./categories.js";
import { loadSystemStats } from "./system.js";
import { bindInfo, openInfo } from "./info.js";
import { bindLogs, openLogs } from "./logs.js";
import { bindPermissions } from "./permissions.js";
import { bindStats } from "./stats.js";
import { bindUpload } from "./upload.js";
import { refreshUpdateBadge } from "./settings.js";

configureInstanceActions({ openEdit, openEditorForInstance, openLogs, openInfo });

// Counts the lockout down in the login dialog. Without it a 429 looks like a
// wrong password that stays wrong, and the next thing anyone tries is another
// guess — which is exactly what extends the block.
let lockoutTimer = null;

function showLockout(seconds) {
  const error = document.getElementById("login-error");
  const submit = document.querySelector("#login-form button[type=submit]");
  clearInterval(lockoutTimer);
  error.classList.remove("hidden");
  submit.disabled = true;

  const tick = () => {
    if (seconds <= 0) {
      clearInterval(lockoutTimer);
      submit.disabled = false;
      error.textContent = "Wrong password";
      error.classList.add("hidden");
      return;
    }
    error.textContent = `Too many attempts — wait ${seconds} s`;
    seconds -= 1;
  };
  tick();
  lockoutTimer = setInterval(tick, 1000);
}

document.getElementById("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const password = document.getElementById("login-password").value;
  const response = await fetch("/api/auth-check", { headers: { "Authorization": `Bearer ${password}` } });
  if (response.ok) {
    clearInterval(lockoutTimer);
    setToken(password);
    state.guestMode = false;
    hideLoginModal();
    applyEditMode();
    loadInstances();
    refreshUpdateBadge();
    startPolling();
  } else if (response.status === 429) {
    showLockout(Number(response.headers.get("Retry-After")) || 60);
  } else {
    document.getElementById("login-error").textContent = "Wrong password";
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

// The version number in the header is the About trigger. Available to guests
// too — a copyright notice that only logged-in users can see would miss the
// point of shipping it in the first place.
const aboutModal = document.getElementById("about-modal");
const toggleAbout = show => aboutModal.classList.toggle("hidden", !show);
document.getElementById("app-version").addEventListener("click", () => toggleAbout(true));
document.getElementById("about-close").addEventListener("click", () => toggleAbout(false));
document.getElementById("about-backdrop").addEventListener("click", () => toggleAbout(false));

document.addEventListener("DOMContentLoaded", async () => {
  bindUpload();
  bindEdit();
  bindLogs();
  bindInfo();
  bindStats();
  bindPermissions();
  bindFilter();
  bindSorting();
  bindPagination();
  bindCategories();

  const statusResponse = await fetch("/api/auth-status");
  const status = await statusResponse.json();
  state.editMode = status.edit_mode || "full";
  state.authEnabled = !!status.auth_enabled;
  if (status.version) {
    document.getElementById("app-version").textContent = `v${status.version}`;
    document.getElementById("about-version").textContent = `v${status.version}`;
  }

  state.guestMode = state.authEnabled && !getToken();
  applyEditMode();
  poll();
  refreshUpdateBadge();
  startPolling();
});

// The system stats are fetched first so the table's RAM column and the tiles
// come out of the same round — a column lagging one poll behind the row it
// sits in is the kind of small lie a monitor should not tell.
async function poll() {
  await loadSystemStats();
  await loadInstances();
  // After the instances: the bar is only shown for a category the filter
  // offers, and the filter's options are rebuilt in loadInstances().
  await loadCategories();
}

function startPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(poll, 4000);
}
