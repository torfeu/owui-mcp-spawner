import { apiFetch, applyEditMode, esc, fetchVenvs, setToken, showAlert, state } from "./common.js";

// All three token fields get the same controls — one wiring for all of them,
// so they cannot drift apart.
const TOKEN_FIELDS = ["mcp", "read", "agent"];
const TOKEN_LABELS = { mcp: "MCP token", read: "Read token", agent: "Agent token" };

for (const kind of TOKEN_FIELDS) {
  const input = document.getElementById(`settings-${kind}-token`);
  const clear = document.getElementById(`settings-${kind}-clear`);

  document.getElementById(`settings-${kind}-toggle`).addEventListener("click", () => {
    input.type = input.type === "password" ? "text" : "password";
  });

  document.getElementById(`settings-${kind}-generate`).addEventListener("click", () => {
    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    const arr = new Uint8Array(32);
    crypto.getRandomValues(arr);
    input.value = Array.from(arr, b => chars[b % chars.length]).join("");
    input.type = "text";   // show the generated token so user can copy it
    clear.checked = false;
    input.disabled = false;
  });

  clear.addEventListener("change", e => {
    input.disabled = e.target.checked;
    if (e.target.checked) input.value = "";
  });
}

document.getElementById("settings-shared-enable").addEventListener("change", e => {
  document.getElementById("settings-shared-port").disabled = !e.target.checked;
});

// The user-JWT secret has no reveal and no generator, unlike the three tokens
// above: there is no route that hands it back, and the value is not ours to
// invent — it has to match what OpenWebUI signs with.
document.getElementById("settings-identity-clear").addEventListener("change", e => {
  const input = document.getElementById("settings-identity-secret");
  input.disabled = e.target.checked;
  if (e.target.checked) input.value = "";
});

document.getElementById("settings-btn").addEventListener("click", openSettings);
document.getElementById("settings-cancel").addEventListener("click", closeSettings);
document.getElementById("settings-backdrop").addEventListener("click", closeSettings);
document.getElementById("settings-save").addEventListener("click", saveSettings);
document.getElementById("settings-restart").addEventListener("click", restartManager);
document.getElementById("settings-venv-create").addEventListener("click", createVenv);
document.getElementById("settings-update-now").addEventListener("click", runUpdateCheck);

// Guards saveSettings: saving before the current values arrive would silently
// reset edit mode and disable the shared port from the pristine form state.
let settingsLoaded = false;

function openSettings() {
  settingsLoaded = false;
  document.getElementById("settings-modal").classList.remove("hidden");
  loadSettingsData();
}

function closeSettings() {
  document.getElementById("settings-modal").classList.add("hidden");
  document.getElementById("settings-pw-current").value = "";
  document.getElementById("settings-pw1").value = "";
  document.getElementById("settings-pw2").value = "";
  for (const kind of TOKEN_FIELDS) {
    const input = document.getElementById(`settings-${kind}-token`);
    input.value = "";
    input.type = "password";
    input.disabled = false;
    document.getElementById(`settings-${kind}-clear`).checked = false;
  }
  const identitySecret = document.getElementById("settings-identity-secret");
  identitySecret.value = "";
  identitySecret.disabled = false;
  document.getElementById("settings-identity-clear").checked = false;
}

async function loadSettingsData() {
  try {
    const data = await apiFetch("/api/settings");
    const authEl = document.getElementById("settings-auth-status");
    authEl.textContent = data.auth_enabled
      ? "Authentication enabled — password is set"
      : "Authentication disabled — set a password to protect this instance";
    authEl.className = "settings-status " + (data.auth_enabled ? "settings-status-ok" : "settings-status-warn");

    // Show "Current Password" field only when a password is already set
    const showCurrent = data.auth_enabled;
    document.getElementById("settings-current-pw-label").classList.toggle("hidden", !showCurrent);
    document.getElementById("settings-pw-current").classList.toggle("hidden", !showCurrent);

    const modeSel = document.getElementById("settings-edit-mode");
    modeSel.value = data.edit_mode || "full";
    // Mode set via CLI flag (--no-edit / --no-code-edit) cannot be changed at runtime
    modeSel.disabled = !!data.edit_mode_locked;
    modeSel.title = data.edit_mode_locked
      ? "Fixed by a CLI flag (--no-edit / --no-code-edit) — restart without the flag to change it"
      : "";

    await renderTokenSection("mcp", {
      isSet: data.mcp_token_set,
      locked: !data.token_edit_enabled,
      endpoint: "/api/settings/mcp-token",
      statusSet: ["MCP endpoints protected — Bearer token is set", "settings-status-ok"],
      // No token means every MCP endpoint is open — that is a warning.
      statusUnset: ["MCP endpoints open — no authentication required", "settings-status-warn"],
    });

    await renderTokenSection("read", {
      isSet: data.read_token_set,
      locked: !data.token_edit_enabled,
      endpoint: "/api/settings/read-token",
      statusSet: ["Read token active — GET requests accept it instead of the password", "settings-status-ok"],
      // Not set is the default, not a defect: reading then needs the password.
      statusUnset: ["Not set — reading tools have to use the admin password", ""],
    });

    await renderTokenSection("agent", {
      isSet: data.agent_token_set,
      locked: !data.token_edit_enabled,
      endpoint: "/api/settings/agent-token",
      statusSet: ["Agent token active — full API access except password and tokens", "settings-status-ok"],
      statusUnset: ["Not set — writing tools have to use the admin password", ""],
    });

    const identityStatus = document.getElementById("settings-identity-status");
    if (data.user_jwt_secret_set) {
      identityStatus.textContent = "Secret set — instances with User identity on verify the forwarded user";
      identityStatus.className = "settings-status settings-status-ok";
    } else if (data.user_trust_headers) {
      // Working, but on a weaker footing — say which one is in force.
      identityStatus.textContent = "No secret — users are taken from OpenWebUI's plain headers, unverified";
      identityStatus.className = "settings-status settings-status-warn";
    } else {
      identityStatus.textContent = "Not set — no instance can identify a user, whatever its mode says";
      identityStatus.className = "settings-status";
    }
    document.getElementById("settings-identity-trust-headers").checked = !!data.user_trust_headers;
    const identitySecret = document.getElementById("settings-identity-secret");
    identitySecret.placeholder = data.user_jwt_secret_set ? "●●●●●●●● (leave empty to keep)" : "Not set";
    identitySecret.value = "";
    identitySecret.disabled = false;
    document.getElementById("settings-identity-clear").checked = false;
    // Same lock as the tokens: --no-token-edit exists so credentials cannot be
    // changed through the web UI at all, and this is one.
    const identityLocked = !data.token_edit_enabled;
    document.getElementById("settings-identity-fields").classList.toggle("hidden", identityLocked);
    document.getElementById("settings-identity-locked").classList.toggle("hidden", !identityLocked);

    // Shared MCP port
    const sharedOn = data.shared_port != null;
    document.getElementById("settings-shared-enable").checked = sharedOn;
    const sharedPortInp = document.getElementById("settings-shared-port");
    sharedPortInp.value = sharedOn ? data.shared_port : "";
    sharedPortInp.disabled = !sharedOn;
    const sharedStatus = document.getElementById("settings-shared-status");
    sharedStatus.textContent = sharedOn
      ? (data.shared_proxy_running
          ? `Shared port active — all MCPs reachable on :${data.shared_port}/mcp/<id>`
          : `Shared port ${data.shared_port} configured but not running — check the port and restart`)
      : "Disabled — each MCP is exposed on its own port";
    sharedStatus.className = "settings-status " +
      (sharedOn ? (data.shared_proxy_running ? "settings-status-ok" : "settings-status-warn") : "settings-status-warn");

    const retention = document.getElementById("settings-retention");
    const stored = String(data.usage_retention_days ?? 30);
    // A value set by hand in the settings file must not be silently reset to
    // the nearest preset on the next save.
    if (![...retention.options].some(option => option.value === stored)) {
      const custom = document.createElement("option");
      custom.value = stored;
      custom.textContent = `${stored} days`;
      retention.appendChild(custom);
    }
    retention.value = stored;

    renderUpdateSection(data.update);

    document.getElementById("settings-host").textContent = data.host || "—";
    document.getElementById("settings-port").textContent = data.port || "—";
    const hints = {
      "0.0.0.0": "All network interfaces — reachable from other machines",
      "127.0.0.1": "Localhost only — not reachable from other machines",
      "localhost":  "Localhost only — not reachable from other machines",
    };
    const hint = document.getElementById("settings-bind-hint");
    hint.textContent = hints[data.host] || "";
    hint.style.display = hint.textContent ? "" : "none";

    await renderVenvSettings();
    settingsLoaded = true;
  } catch (e) {
    showAlert("error", "Could not load settings: " + e.message);
  }
}

async function renderTokenSection(kind, { isSet, locked, endpoint, statusSet, statusUnset }) {
  const [text, cls] = isSet ? statusSet : statusUnset;
  const status = document.getElementById(`settings-${kind}-status`);
  status.textContent = text;
  status.className = ("settings-status " + cls).trim();

  document.getElementById(`settings-${kind}-fields`).classList.toggle("hidden", locked);
  document.getElementById(`settings-${kind}-locked`).classList.toggle("hidden", !locked);
  document.getElementById(`settings-${kind}-clear`).checked = false;

  // Load the existing token into the field (masked)
  const input = document.getElementById(`settings-${kind}-token`);
  input.type = "password";
  input.disabled = false;
  input.value = "";
  if (isSet && !locked) {
    try {
      const data = await apiFetch(endpoint);
      input.value = data.token || "";
    } catch (_) {
      // Server refused to hand it out — leave the field empty rather than
      // showing a stale value the save would then write back.
    }
  }
}

// One wording for both the cached status line and the Check-now result: while
// developing, the running build is ahead of the published release, and
// "you have the latest release" would be wrong there.
function upToDateText(data) {
  const same = (data.latest_version || "").replace(/^v/i, "") === data.current_version;
  return same
    ? `Up to date — ${data.current_version} is the latest release`
    : `Up to date — latest release is ${data.latest_version || "unknown"}, you are running ${data.current_version}`;
}

function renderUpdateSection(update = {}) {
  document.getElementById("settings-update-enable").checked = !!update.enabled;
  const status = document.getElementById("settings-update-status");
  if (!update.enabled) {
    status.textContent = "Disabled — this installation never contacts GitHub";
    status.className = "settings-status settings-status-warn";
  } else if (update.update_available) {
    status.textContent = `Version ${update.latest_version} is available (running ${update.current_version})`;
    status.className = "settings-status settings-status-warn";
  } else if (update.last_checked) {
    const when = new Date(update.last_checked * 1000).toLocaleString();
    status.textContent = `${upToDateText(update)} · last checked ${when}`;
    status.className = "settings-status settings-status-ok";
  } else {
    // Enabled but never answered: offline, GitHub down or the check is pending.
    status.textContent = "Enabled — no result yet";
    status.className = "settings-status settings-status-warn";
  }
  document.getElementById("settings-update-result").textContent = "";
  document.getElementById("settings-update-result").className = "update-check-result";
  applyUpdateBadge(update);
}

async function runUpdateCheck() {
  const button = document.getElementById("settings-update-now");
  const result = document.getElementById("settings-update-result");
  button.disabled = true;
  result.className = "update-check-result";
  result.textContent = "Asking GitHub…";
  try {
    const data = await apiFetch("/api/settings/update-check", { method: "POST" });
    if (!data.ok) {
      // Explicitly asked for, so a failed request must say so — reporting
      // "up to date" when nothing was reached would be a lie.
      result.className = "update-check-result update-check-error";
      result.textContent = `Check failed — GitHub not reachable (${data.error || "unknown error"})`;
      return;
    }
    if (data.update_available) {
      result.className = "update-check-result update-check-new";
      result.textContent = `Version ${data.latest_version} is available — you are running ${data.current_version}`;
      showAlert("info", `Update available: ${data.latest_version}`);
      if (data.enabled) applyUpdateBadge(data);
    } else {
      result.className = "update-check-result update-check-ok";
      result.textContent = upToDateText(data);
    }
  } catch (e) {
    result.className = "update-check-result update-check-error";
    result.textContent = `Check failed: ${e.message}`;
  } finally {
    button.disabled = false;
  }
}

// Header badge. The UI only ever renders the server-side cache; it never talks
// to GitHub itself, so no viewer's IP is exposed and no CORS/CSP is involved.
function applyUpdateBadge(update = {}) {
  const badge = document.getElementById("update-badge");
  const show = !!update.update_available && !state.guestMode;
  badge.classList.toggle("hidden", !show);
  if (!show) return;
  badge.textContent = `Update ${update.latest_version}`;
  badge.title = `Version ${update.latest_version} is available — you are running ${update.current_version}`;
  badge.href = update.html_url || "https://github.com/torfeu/owui-mcp-spawner/releases/latest";
}

export async function refreshUpdateBadge() {
  // Guests have no /api/settings access — and "this instance is outdated" is
  // not a sentence for an unauthenticated visitor either.
  if (state.guestMode) return;
  try {
    const data = await apiFetch("/api/settings");
    applyUpdateBadge(data.update);
  } catch {
    // A failed settings call must not break the dashboard over a badge.
  }
}

async function renderVenvSettings() {
  const list = document.getElementById("settings-venv-list");
  const venvs = await fetchVenvs();
  list.innerHTML = venvs.map(v => {
    const meta = `${v.instances} instance${v.instances === 1 ? "" : "s"}${v.exists ? "" : " · not created yet"}`;
    // Always offer Delete for non-default venvs; the server refuses if any
    // instance config still points at it (running or stopped).
    const del = v.is_default
      ? ""
      : `<button class="btn btn-danger btn-sm" data-venv-del="${esc(v.name)}"${v.instances ? ' title="In use — reassign or delete the instances first"' : ""}>Delete</button>`;
    return `<div class="venv-row">
      <span class="venv-badge">${esc(v.name)}${v.is_default ? " (default)" : ""}</span>
      <span class="venv-meta">${meta}</span>
      ${del}
    </div>`;
  }).join("");
  list.querySelectorAll("[data-venv-del]").forEach(btn => {
    btn.addEventListener("click", () => deleteVenv(btn.dataset.venvDel));
  });
}

async function createVenv() {
  const input = document.getElementById("settings-venv-new");
  const name = input.value.trim();
  if (!name) { showAlert("error", "Enter a venv name"); return; }
  try {
    await apiFetch("/api/venvs", { method: "POST", body: JSON.stringify({ name }) });
    input.value = "";
    showAlert("success", `Venv '${name}' created.`);
    await renderVenvSettings();
  } catch (e) {
    showAlert("error", e.message);
  }
}

async function deleteVenv(name) {
  if (!confirm(`Delete venv '${name}'? This removes its installed packages.`)) return;
  try {
    await apiFetch(`/api/venvs/${encodeURIComponent(name)}`, { method: "DELETE" });
    showAlert("success", `Venv '${name}' deleted.`);
    await renderVenvSettings();
  } catch (e) {
    showAlert("error", e.message);
  }
}

async function saveSettings() {
  if (!settingsLoaded) {
    showAlert("error", "Settings are still loading — please wait a moment");
    return;
  }
  const pw1 = document.getElementById("settings-pw1").value.trim();
  const pw2 = document.getElementById("settings-pw2").value.trim();
  const modeSel = document.getElementById("settings-edit-mode");
  const mode = modeSel.value;

  if (pw1 && pw1.length < 4) {
    showAlert("error", "Password must be at least 4 characters");
    return;
  }
  if (pw1 && pw1 !== pw2) {
    showAlert("error", "Passwords do not match");
    return;
  }

  const currentPw = document.getElementById("settings-pw-current").value.trim();

  // Same rules for all three tokens; the server checks them again and is the
  // authority — this only saves a round trip on the obvious mistake.
  const tokenFields = {};
  for (const kind of TOKEN_FIELDS) {
    const value = document.getElementById(`settings-${kind}-token`).value.trim();
    if (document.getElementById(`settings-${kind}-clear`).checked) {
      tokenFields[`${kind}_token_clear`] = true;
    } else if (value) {
      if (value.length < 8) {
        showAlert("error", `${TOKEN_LABELS[kind]} must be at least 8 characters`);
        return;
      }
      tokenFields[`${kind}_token`] = value;
    }
  }

  const identitySecret = document.getElementById("settings-identity-secret").value.trim();
  if (document.getElementById("settings-identity-clear").checked) {
    tokenFields.user_jwt_secret_clear = true;
  } else if (identitySecret) {
    if (identitySecret.length < 16) {
      showAlert("error", "The shared secret must be at least 16 characters");
      return;
    }
    tokenFields.user_jwt_secret = identitySecret;
  }

  const body = { ...tokenFields };
  body.user_trust_headers = document.getElementById("settings-identity-trust-headers").checked;
  if (!modeSel.disabled) body.edit_mode = mode;
  if (pw1) {
    body.password = pw1;
    body.password_confirm = pw2;
    if (currentPw) body.current_password = currentPw;
  }

  body.update_check = document.getElementById("settings-update-enable").checked;
  body.usage_retention_days = Number(document.getElementById("settings-retention").value);

  const sharedOn = document.getElementById("settings-shared-enable").checked;
  const sharedPort = document.getElementById("settings-shared-port").value.trim();
  if (sharedOn) {
    if (!sharedPort) {
      showAlert("error", "Enter a port for the shared MCP port");
      return;
    }
    body.shared_port = Number(sharedPort);
  } else {
    body.shared_port = null;
  }

  try {
    const res = await apiFetch("/api/settings", { method: "PUT", body: JSON.stringify(body) });

    // Keep session alive with new password
    if (pw1 && res.changed && res.changed.includes("password")) {
      setToken(pw1);
    }

    // Apply new edit mode immediately in the UI
    if (res.changed && res.changed.includes("edit_mode")) {
      state.editMode = mode;
      applyEditMode();
    }

    const restarting = res.changed && res.changed.includes("instances_restarting");
    const shown = (res.changed || [])
      .filter(c => c !== "instances_restarting" && c !== "restart_instances_to_apply");
    const msg = shown.length ? "Saved: " + shown.join(", ") : "Nothing changed";
    showAlert("success", msg);
    if (restarting) showAlert("info", "Running instances are restarting to switch their bind address…");
    if (shown.includes("mcp_token") || shown.includes("mcp_token_cleared")) {
      showAlert("info", "Note: running instances keep the previous MCP token until they are restarted.");
    }
    if (res.changed && res.changed.includes("restart_instances_to_apply")) {
      // Same trap as the MCP token, and worth its own sentence: a runner reads
      // the secret once at startup, so an unrestarted instance keeps refusing
      // users with a secret that looks correct in this dialog.
      showAlert("info", "Note: the new secret only reaches an instance when it is restarted (locked ones: Stop, then Start).");
    }
    const tokenChanged = ["read_token", "read_token_cleared", "agent_token", "agent_token_cleared"]
      .some(c => shown.includes(c));
    if (tokenChanged) {
      showAlert("info", "Note: tools using an API token (tool router, control tool) need the new value in their auth_token — the spawner does not update them.");
    }
    document.getElementById("settings-pw-current").value = "";
    document.getElementById("settings-pw1").value = "";
    document.getElementById("settings-pw2").value = "";
    loadSettingsData();
  } catch (e) {
    showAlert("error", "Save failed: " + e.message);
  }
}

async function restartManager() {
  if (!confirm("Restart OWUI MCP Spawner?\nThe web UI will be unavailable for a few seconds.")) return;
  try {
    await apiFetch("/api/server/restart", { method: "POST" });
    showAlert("info", "Restarting… reconnecting in 5 s.");
    setTimeout(() => location.reload(), 5000);
  } catch (e) {
    showAlert("error", "Restart failed: " + e.message);
  }
}
