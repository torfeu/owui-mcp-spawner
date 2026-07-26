import { apiFetch, applyEditMode, esc, fetchVenvs, setToken, showAlert, state } from "./common.js";

document.getElementById("settings-mcp-toggle").addEventListener("click", () => {
  const inp = document.getElementById("settings-mcp-token");
  inp.type = inp.type === "password" ? "text" : "password";
});

document.getElementById("settings-mcp-generate").addEventListener("click", () => {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  const arr = new Uint8Array(32);
  crypto.getRandomValues(arr);
  const token = Array.from(arr, b => chars[b % chars.length]).join("");
  const inp = document.getElementById("settings-mcp-token");
  inp.value = token;
  inp.type = "text";   // show the generated token so user can copy it
  document.getElementById("settings-mcp-clear").checked = false;
  inp.disabled = false;
});

document.getElementById("settings-mcp-clear").addEventListener("change", e => {
  document.getElementById("settings-mcp-token").disabled = e.target.checked;
  if (e.target.checked) document.getElementById("settings-mcp-token").value = "";
});

document.getElementById("settings-shared-enable").addEventListener("change", e => {
  document.getElementById("settings-shared-port").disabled = !e.target.checked;
});

document.getElementById("settings-btn").addEventListener("click", openSettings);
document.getElementById("settings-cancel").addEventListener("click", closeSettings);
document.getElementById("settings-backdrop").addEventListener("click", closeSettings);
document.getElementById("settings-save").addEventListener("click", saveSettings);
document.getElementById("settings-restart").addEventListener("click", restartManager);
document.getElementById("settings-venv-create").addEventListener("click", createVenv);

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
  const mcpInp = document.getElementById("settings-mcp-token");
  mcpInp.value = "";
  mcpInp.type = "password";
  mcpInp.disabled = false;
  document.getElementById("settings-mcp-clear").checked = false;
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

    // MCP Auth section
    const mcpStatus = document.getElementById("settings-mcp-status");
    mcpStatus.textContent = data.mcp_token_set
      ? "MCP endpoints protected — Bearer token is set"
      : "MCP endpoints open — no authentication required";
    mcpStatus.className = "settings-status " + (data.mcp_token_set ? "settings-status-ok" : "settings-status-warn");

    const mcpLocked = !data.token_edit_enabled;
    document.getElementById("settings-mcp-fields").classList.toggle("hidden", mcpLocked);
    document.getElementById("settings-mcp-locked").classList.toggle("hidden", !mcpLocked);
    document.getElementById("settings-mcp-clear").checked = false;

    // Load existing token into the field (masked)
    const tokenInput = document.getElementById("settings-mcp-token");
    tokenInput.type = "password";
    tokenInput.disabled = false;
    if (data.mcp_token_set && !mcpLocked) {
      try {
        const td = await apiFetch("/api/settings/mcp-token");
        tokenInput.value = td.token || "";
      } catch (_) {
        tokenInput.value = "";
      }
    } else {
      tokenInput.value = "";
    }

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
  const mcpToken = document.getElementById("settings-mcp-token").value.trim();
  const mcpClear = document.getElementById("settings-mcp-clear").checked;

  if (mcpToken && mcpToken.length < 8) {
    showAlert("error", "MCP token must be at least 8 characters");
    return;
  }

  const body = {};
  if (!modeSel.disabled) body.edit_mode = mode;
  if (pw1) {
    body.password = pw1;
    body.password_confirm = pw2;
    if (currentPw) body.current_password = currentPw;
  }
  if (mcpClear) {
    body.mcp_token_clear = true;
  } else if (mcpToken) {
    body.mcp_token = mcpToken;
  }

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
    const shown = (res.changed || []).filter(c => c !== "instances_restarting");
    const msg = shown.length ? "Saved: " + shown.join(", ") : "Nothing changed";
    showAlert("success", msg);
    if (restarting) showAlert("info", "Running instances are restarting to switch their bind address…");
    if (shown.includes("mcp_token") || shown.includes("mcp_token_cleared")) {
      showAlert("info", "Note: running instances keep the previous MCP token until they are restarted.");
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
