/** Show or hide one element by id. */
function show(id, visible) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle("hidden", !visible);
}

/** A port field is there when its checkbox is ticked, and gone otherwise.
 *
 * Not merely disabled: a greyed-out field still asks to be read, and a manager
 * that serves nothing on a port of its own has nothing to say about one. The
 * category URL segment goes the same way — it only describes a path that
 * something answers on.
 */
function syncEndpointRows() {
  const ticked = id => {
    const box = document.getElementById(id);
    return !!box && box.checked;
  };
  show("settings-shared-port-label", ticked("settings-shared-enable"));
  show("settings-shared-port", ticked("settings-shared-enable"));

  const categoryOwnPort = ticked("settings-category-enable");
  show("settings-category-port-label", categoryOwnPort);
  show("settings-category-port", categoryOwnPort);

  const categoriesServed = categoryOwnPort || ticked("settings-category-endpoints");
  show("settings-category-segment-label", categoriesServed);
  show("settings-category-segment", categoriesServed);
}

function fillPortRow({ checkbox, input, port }) {
  const on = port != null;
  document.getElementById(checkbox).checked = on;
  const field = document.getElementById(input);
  field.value = on ? port : "";
  field.disabled = !on;
}

/** The one line above a section: where this kind of endpoint answers.
 *
 * Both ways in, in one sentence. It described the port alone until 03.09.,
 * which read as "not served" to somebody who had just switched the manager
 * port on — the line said nothing about the way that was actually working.
 * It reports what is *saved*, not what is ticked: it is about reality.
 */
function fillSectionStatus({ status, path, managerPort, onManagerPort, port, running, nothing }) {
  const line = document.getElementById(status);
  if (!line) return;
  const where = [];
  if (onManagerPort) where.push(`on the manager port :${managerPort}`);
  if (port != null) where.push(running ? `on port ${port}` : `on port ${port} (configured, not listening)`);

  line.textContent = where.length
    ? `Served ${where.join(" and ")} — ${path}`
    : nothing;
  const bad = port != null && !running;
  line.className = "settings-status " +
    (where.length && !bad ? "settings-status-ok" : "settings-status-warn");
}

import { renderCategorySettingsList } from "./categories.js";
import { apiFetch, applyEditMode, copyText, downloadBlob, esc, fetchVenvs, formatBytes, setToken, showAlert, state } from "./common.js";

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

for (const [box, field] of [["settings-shared-enable", "settings-shared-port"],
                            ["settings-category-enable", "settings-category-port"]]) {
  document.getElementById(box).addEventListener("change", e => {
    document.getElementById(field).disabled = !e.target.checked;
    syncEndpointRows();
  });
}
// The manager-port box owns no field of its own, but the URL segment follows
// it: unticking both ways in leaves nothing for a segment to describe.
document.getElementById("settings-category-endpoints")
        .addEventListener("change", syncEndpointRows);

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
document.getElementById("settings-agent-create").addEventListener("click", createAgentIdentity);
document.getElementById("settings-agent-token-copy").addEventListener("click", copyIssuedToken);
document.getElementById("settings-update-now").addEventListener("click", runUpdateCheck);
document.getElementById("settings-content-clear").addEventListener("click", clearAllContent);
document.getElementById("backup-download").addEventListener("click", downloadBackup);
document.getElementById("backup-check").addEventListener("click", () => runRestore(true));
document.getElementById("backup-restore").addEventListener("click", () => runRestore(false));
document.getElementById("backup-secrets").addEventListener("change", markBackupKind);
markBackupKind();

// Tabs are presentation only: every panel stays in the DOM and keeps its
// fields, so loading and saving reach all of them regardless of what is shown.
// Saving is one action for the whole dialog, not per tab.
const TAB_STORAGE_KEY = "settings-tab";

function showSettingsTab(key) {
  for (const tab of document.querySelectorAll("[data-settings-tab]")) {
    tab.classList.toggle("settings-tab-active", tab.dataset.settingsTab === key);
  }
  for (const panel of document.querySelectorAll("[data-settings-panel]")) {
    panel.classList.toggle("hidden", panel.dataset.settingsPanel !== key);
  }
  try {
    localStorage.setItem(TAB_STORAGE_KEY, key);
  } catch {
    // Private mode or blocked storage: remembering the tab is a convenience.
  }
}

for (const tab of document.querySelectorAll("[data-settings-tab]")) {
  tab.addEventListener("click", () => showSettingsTab(tab.dataset.settingsTab));
}

// Guards saveSettings: saving before the current values arrive would silently
// reset edit mode and disable the shared port from the pristine form state.
let settingsLoaded = false;

function openSettings() {
  settingsLoaded = false;
  let remembered = null;
  try {
    remembered = localStorage.getItem(TAB_STORAGE_KEY);
  } catch {
    // See showSettingsTab: fall back to the first tab.
  }
  const known = remembered && document.querySelector(`[data-settings-panel="${remembered}"]`);
  showSettingsTab(known ? remembered : document.querySelector("[data-settings-tab]").dataset.settingsTab);
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
  // A freshly issued agent token must not still be on screen the next time the
  // dialog opens — it is shown once, and closing the dialog is that once
  // ending.
  document.getElementById("settings-agent-token-box").classList.add("hidden");
  document.getElementById("settings-agent-token-value").value = "";
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
    // Both port rows behave the same, so they are filled by the same code.
    fillPortRow({ checkbox: "settings-shared-enable", input: "settings-shared-port",
                  port: data.shared_port });
    fillPortRow({ checkbox: "settings-category-enable", input: "settings-category-port",
                  port: data.category_port });
    fillSectionStatus({
      status: "settings-shared-status", path: "/mcp/<id>",
      managerPort: data.port, onManagerPort: data.instance_endpoints_enabled === true,
      port: data.shared_port, running: data.shared_proxy_running,
      nothing: "Not served — each instance is reachable on its own port only",
    });
    fillSectionStatus({
      status: "settings-category-status",
      path: `/mcp/${data.category_url_segment || "category"}/<name>`,
      managerPort: data.port, onManagerPort: data.category_endpoints_enabled === true,
      port: data.category_port, running: data.category_proxy_running,
      nothing: "Not served — no category endpoint answers anywhere",
    });

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

    document.getElementById("settings-category-endpoints").checked = data.category_endpoints_enabled === true;
    document.getElementById("settings-instance-endpoints").checked = data.instance_endpoints_enabled === true;
    document.getElementById("settings-category-segment").value = data.category_url_segment || "category";
    syncEndpointRows();
    document.getElementById("settings-health-enabled").checked = data.health_check_enabled !== false;
    document.getElementById("settings-health-autorestart").checked = !!data.health_autorestart;
    document.getElementById("settings-health-failures").value = data.health_failures_before_restart ?? 3;

    renderContentSettings(data);
    await renderContentFiles();

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
    await renderAgentIdentities();
    await renderKnownCallers();
    // Under the switch that turns them on, and only while they are on.
    await renderCategorySettingsList();
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

function renderContentSettings(data) {
  // Plain number fields rather than a list of presets: a quota is a number
  // somebody picks for their disk, and 300 MB is not a stranger choice than
  // 500. The server keeps the authoritative bounds (see PUT /api/settings).
  document.getElementById("settings-content-base-url").value = data.content_base_url || "";
  document.getElementById("settings-content-max").value = data.content_max_mb ?? 200;
  document.getElementById("settings-content-warn").value = data.content_warn_percent ?? 80;
  document.getElementById("settings-content-retention").value = data.content_retention_days ?? 0;
  document.getElementById("settings-content-block").checked = !!data.content_block_when_full;
  document.getElementById("settings-content-mode").value = data.content_retention_mode || "file_age";

  // The one setting that fails silently: without it the links are relative,
  // and a chat client resolves them against itself instead of against us.
  const status = document.getElementById("settings-content-status");
  if (data.content_base_url) {
    status.textContent = `Download links point at ${data.content_base_url}/content/…`;
    status.className = "settings-status settings-status-ok";
  } else {
    status.textContent = "No base URL — download links stay relative and will not resolve from a chat";
    status.className = "settings-status settings-status-warn";
  }
}

async function renderContentFiles() {
  const list = document.getElementById("settings-content-list");
  let data;
  try {
    data = await apiFetch("/api/content");
  } catch (e) {
    list.innerHTML = `<div class="venv-meta">Could not read the storage: ${esc(e.message)}</div>`;
    return;
  }
  if (!data.instances.length) {
    list.innerHTML = '<div class="venv-meta">Nothing stored yet.</div>';
    return;
  }
  // The same threshold the runners warn at — hardcoding 80 here would colour
  // the list at a different point than the warning actually fires.
  const warnAt = data.settings?.content_warn_percent ?? 80;
  list.innerHTML = data.instances.map(entry => {
    const quota = entry.limit_bytes
      ? ` · ${entry.percent} % of ${formatBytes(entry.limit_bytes)}`
      : "";
    const warn = entry.limit_bytes && entry.percent >= warnAt ? " content-over" : "";
    return `<div class="venv-row">
      <span class="venv-badge">${esc(entry.instance)}</span>
      <span class="venv-meta${warn}">${entry.files} file${entry.files === 1 ? "" : "s"} · ${formatBytes(entry.bytes)}${quota}</span>
      <button class="btn btn-secondary btn-sm" data-content-show="${esc(entry.instance)}">Files</button>
      <button class="btn btn-danger btn-sm" data-content-empty="${esc(entry.instance)}">Empty</button>
    </div>
    <div class="content-files hidden" data-content-files="${esc(entry.instance)}"></div>`;
  }).join("");

  list.querySelectorAll("[data-content-show]").forEach(btn => {
    btn.addEventListener("click", () => toggleContentFiles(btn.dataset.contentShow));
  });
  list.querySelectorAll("[data-content-empty]").forEach(btn => {
    btn.addEventListener("click", () => emptyContent(btn.dataset.contentEmpty));
  });
}

async function toggleContentFiles(instance) {
  const box = document.querySelector(`[data-content-files="${CSS.escape(instance)}"]`);
  if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.innerHTML = '<div class="venv-meta">Loading…</div>';
  try {
    const data = await apiFetch(`/api/content/${encodeURIComponent(instance)}`);
    if (!data.items.length) {
      box.innerHTML = '<div class="venv-meta">Empty.</div>';
      return;
    }
    box.innerHTML = data.items.map(item => {
      const when = new Date(item.modified * 1000).toLocaleString();
      // The href is the same tokenised link the chat gets — the admin session
      // has no shortcut past it, because there is none to have.
      return `<div class="content-file-row">
        <a href="${esc(item.url)}" target="_blank" rel="noopener">${esc(item.name)}</a>
        <span class="venv-meta">${formatBytes(item.size)} · ${esc(when)}</span>
        <button class="btn btn-danger btn-sm" data-content-del="${esc(item.name)}">Delete</button>
      </div>`;
    }).join("");
    box.querySelectorAll("[data-content-del]").forEach(btn => {
      btn.addEventListener("click", () => deleteContentFile(instance, btn.dataset.contentDel));
    });
  } catch (e) {
    box.innerHTML = `<div class="venv-meta">${esc(e.message)}</div>`;
  }
}

async function deleteContentFile(instance, name) {
  if (!confirm(`Delete '${name}'? Links to it stop working immediately.`)) return;
  try {
    await apiFetch(`/api/content/${encodeURIComponent(instance)}/${encodeURIComponent(name)}`,
                   { method: "DELETE" });
    await renderContentFiles();
    await toggleContentFiles(instance);
  } catch (e) {
    showAlert("error", e.message);
  }
}

async function emptyContent(instance) {
  if (!confirm(`Delete every stored file of '${instance}'?`)) return;
  try {
    const res = await apiFetch(`/api/content/${encodeURIComponent(instance)}`, { method: "DELETE" });
    showAlert("success", `${res.removed} file(s) deleted.`);
    await renderContentFiles();
  } catch (e) {
    showAlert("error", e.message);
  }
}

async function clearAllContent() {
  if (!confirm("Delete every stored file of every instance? This cannot be undone.")) return;
  try {
    const res = await apiFetch("/api/content", { method: "DELETE" });
    showAlert("success", `${res.removed} file(s) deleted.`);
    await renderContentFiles();
  } catch (e) {
    showAlert("error", e.message);
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

// ── Agent identities ────────────────────────────────────────────────────────
// Applied immediately, like the venv list next to it, not through Save: each
// one is its own route, and issuing a credential is not something to leave
// sitting in a form until somebody presses a button somewhere else.

/** Everyone this manager has seen or been told about, with the way back.
 *
 * The stop button lives here and not in Users & permissions on purpose: that
 * page is for granting, and a destructive button on a screen somebody uses
 * daily gets pressed by accident sooner or later. Same reason the confirmation
 * spells out the consequences instead of asking "are you sure?".
 */
async function renderKnownCallers() {
  const list = document.getElementById("settings-callers-list");
  const status = document.getElementById("settings-callers-status");
  if (!list || !status) return;

  let data;
  try {
    data = await apiFetch("/api/identities");
  } catch (e) {
    list.innerHTML = "";
    status.textContent = "Could not load the callers: " + e.message;
    status.className = "settings-status settings-status-warn";
    return;
  }

  const rows = data.identities || [];
  status.textContent = rows.length
    ? `${rows.length} known caller${rows.length === 1 ? "" : "s"}`
    : "Nobody has called yet, and no rule names anybody";
  status.className = "settings-status " + (rows.length ? "settings-status-ok" : "");

  list.innerHTML = rows.map(row => {
    const label = row.name || row.email || row.sub;
    const bits = [];
    if (row.agent) bits.push("agent identity");
    if (row.has_rules) bits.push("has rules");
    if (row.never_seen) bits.push("never called");
    else if (row.last_seen) bits.push(`last seen ${new Date(row.last_seen * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })}`);
    if (row.last_instance) bits.push(esc(row.last_instance));
    return `<div class="venv-row">
      <span class="venv-badge">${esc(label)}</span>
      <span class="venv-meta">${bits.join(" · ")}</span>
      <button class="btn btn-danger btn-sm" data-caller-reset="${esc(row.sub)}"
        title="Remove this caller: entry, rules and — for an agent — its token">Remove</button>
    </div>`;
  }).join("");

  list.querySelectorAll("[data-caller-reset]").forEach(btn => {
    btn.addEventListener("click", () => resetCaller(btn.dataset.callerReset, rows));
  });
}

async function resetCaller(sub, rows) {
  const row = rows.find(r => r.sub === sub) || {};
  const label = row.name || row.email || sub;
  const loses = ["the entry in this list"];
  if (row.has_rules) loses.push("its access rules");
  if (row.agent) loses.push("its token — the agent stops working at once");
  if (!confirm(`Remove ${label}?\n\nThis deletes ${loses.join(", ")}.\n\n`
      + "Anyone using it is refused from their next request on, and reappears "
      + "here with no rights when they call again.")) return;

  try {
    const result = await apiFetch(`/api/identities/${encodeURIComponent(sub)}/reset`,
                                  { method: "DELETE" });
    const gone = [result.forgotten ? "entry" : "", result.rules_removed ? "rules" : "",
                  result.token_removed ? "token" : ""].filter(Boolean);
    showAlert("success", gone.length ? `Removed ${label}: ${gone.join(", ")}`
                                     : `Nothing left to remove for ${label}`);
  } catch (e) {
    // The one credential this needs is the password; a read or agent token
    // gets a 403 here, and saying so beats "request failed".
    const hint = /403/.test(e.message)
      ? " — this needs the admin password; log in with it rather than a token"
      : "";
    showAlert("error", `Could not remove ${label}: ${e.message}${hint}`);
  }
  await renderKnownCallers();
}

async function renderAgentIdentities() {
  const list = document.getElementById("settings-agent-list");
  const status = document.getElementById("settings-agents-status");
  const envBox = document.getElementById("settings-agent-env");
  let data;
  try {
    data = await apiFetch("/api/agent-identities");
  } catch (e) {
    list.innerHTML = "";
    status.textContent = "Could not load the agent identities: " + e.message;
    status.className = "settings-status settings-status-warn";
    return;
  }

  const agents = data.identities || [];
  status.textContent = agents.length
    ? `${agents.length} agent identit${agents.length === 1 ? "y" : "ies"}`
    : "No agent identities — every agent is the same caller behind the shared token";
  status.className = "settings-status " + (agents.length ? "settings-status-ok" : "");

  // Which of the two delivery paths is in force. Without this line, "I changed
  // the token and nothing happened" is the first question anybody asks: with
  // the environment variable set, the file this dialog writes is never read.
  const editable = data.editable !== false;
  envBox.classList.toggle("hidden", editable);
  if (!editable) {
    envBox.innerHTML = `Agent identities come from <code>${esc(data.env_var)}</code> on this
      server. The environment wins over <code>${esc(data.path)}</code>, so they cannot be
      changed here — unset the variable to manage them from this dialog.`;
  }
  for (const id of ["settings-agent-new-id", "settings-agent-new-name",
                    "settings-agent-new-role", "settings-agent-create"]) {
    document.getElementById(id).disabled = !editable;
  }

  list.innerHTML = agents.map(a => {
    const label = a.name ? `${esc(a.name)} · ${esc(a.sub)}` : esc(a.sub);
    const bits = [a.role ? `role ${esc(a.role)}` : "", a.created_at
      ? `issued ${new Date(a.created_at * 1000).toLocaleDateString()}` : ""].filter(Boolean);
    const buttons = editable
      ? `<button class="btn btn-secondary btn-sm" data-agent-roll="${esc(a.sub)}"
           title="Issue a new token — the current one stops working at once">New token</button>
         <button class="btn btn-danger btn-sm" data-agent-del="${esc(a.sub)}">Revoke</button>`
      : "";
    return `<div class="venv-row">
      <span class="venv-badge">${label}</span>
      <span class="venv-meta">${bits.join(" · ")}</span>
      ${buttons}
    </div>`;
  }).join("");

  list.querySelectorAll("[data-agent-roll]").forEach(btn => {
    btn.addEventListener("click", () => regenerateAgentToken(btn.dataset.agentRoll));
  });
  list.querySelectorAll("[data-agent-del]").forEach(btn => {
    btn.addEventListener("click", () => revokeAgentIdentity(btn.dataset.agentDel));
  });
}

function showIssuedToken(token) {
  const box = document.getElementById("settings-agent-token-box");
  document.getElementById("settings-agent-token-value").value = token;
  box.classList.remove("hidden");
  box.scrollIntoView({ block: "nearest" });
}

async function copyIssuedToken() {
  const input = document.getElementById("settings-agent-token-value");
  // Same secure-context problem as everywhere else, and it bit hardest here:
  // this token is shown exactly once. copyText() takes the legacy path over
  // plain HTTP instead of leaving it to be copied by hand.
  if (await copyText(input.value)) {
    showAlert("success", "Token copied to the clipboard.");
  } else {
    input.select();
    showAlert("error", "Could not reach the clipboard — the token is selected, copy it by hand.");
  }
}

async function createAgentIdentity() {
  const idInput = document.getElementById("settings-agent-new-id");
  const nameInput = document.getElementById("settings-agent-new-name");
  const roleInput = document.getElementById("settings-agent-new-role");
  const sub = idInput.value.trim();
  if (!sub) { showAlert("error", "Enter an id for the agent"); return; }
  try {
    const result = await apiFetch("/api/agent-identities", {
      method: "POST",
      body: JSON.stringify({ sub, name: nameInput.value.trim(), role: roleInput.value.trim() }),
    });
    idInput.value = nameInput.value = roleInput.value = "";
    showIssuedToken(result.token);
    showAlert("success", `Agent identity '${sub}' created.`);
    await renderAgentIdentities();
    await renderKnownCallers();
  } catch (e) {
    showAlert("error", e.message);
  }
}

async function regenerateAgentToken(sub) {
  if (!confirm(`Issue a new token for '${sub}'? The current one stops working immediately.`)) return;
  try {
    const result = await apiFetch(
      `/api/agent-identities/${encodeURIComponent(sub)}/token`, { method: "POST" });
    showIssuedToken(result.token);
    showAlert("success", `New token for '${sub}'.`);
    await renderAgentIdentities();
    await renderKnownCallers();
  } catch (e) {
    showAlert("error", e.message);
  }
}

async function revokeAgentIdentity(sub) {
  if (!confirm(`Revoke '${sub}'? Its token stops working immediately. Any access rules for it stay.`)) return;
  try {
    await apiFetch(`/api/agent-identities/${encodeURIComponent(sub)}`, { method: "DELETE" });
    showAlert("success", `Agent identity '${sub}' revoked.`);
    await renderAgentIdentities();
    await renderKnownCallers();
  } catch (e) {
    showAlert("error", e.message);
  }
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

  const contentBase = document.getElementById("settings-content-base-url").value.trim();
  if (contentBase && !/^https?:\/\//.test(contentBase)) {
    showAlert("error", "The download base URL must start with http:// or https://");
    return;
  }
  body.content_base_url = contentBase;

  // An emptied field is not a zero: 0 means "no limit" for the quota and "keep
  // forever" for the retention, so reading a blank box as 0 would silently
  // switch both of them off. Ask instead. The server checks the ranges again.
  const numberFields = [
    ["settings-content-max", "content_max_mb", 0, 1000000, "the quota in MB (0 = no limit)"],
    ["settings-content-warn", "content_warn_percent", 1, 100, "the warning threshold in percent"],
    ["settings-content-retention", "content_retention_days", 0, 3650, "how many days to keep files (0 = forever)"],
    ["settings-health-failures", "health_failures_before_restart", 1, 20, "how many failed checks before a restart"],
  ];
  for (const [id, key, low, high, what] of numberFields) {
    const raw = document.getElementById(id).value.trim();
    if (raw === "") {
      showAlert("error", `Enter ${what}`);
      return;
    }
    const value = Number(raw);
    if (!Number.isInteger(value) || value < low || value > high) {
      showAlert("error", `${what[0].toUpperCase()}${what.slice(1)} must be a whole number between ${low} and ${high}`);
      return;
    }
    body[key] = value;
  }
  body.category_endpoints_enabled = document.getElementById("settings-category-endpoints").checked;
  body.instance_endpoints_enabled = document.getElementById("settings-instance-endpoints").checked;
  body.category_url_segment = document.getElementById("settings-category-segment").value.trim() || "category";
  body.health_check_enabled = document.getElementById("settings-health-enabled").checked;
  body.health_autorestart = document.getElementById("settings-health-autorestart").checked;

  body.content_block_when_full = document.getElementById("settings-content-block").checked;
  body.content_retention_mode = document.getElementById("settings-content-mode").value;

  body.update_check = document.getElementById("settings-update-enable").checked;
  body.usage_retention_days = Number(document.getElementById("settings-retention").value);

  const sharedOn = document.getElementById("settings-shared-enable").checked;
  const sharedPort = document.getElementById("settings-shared-port").value.trim();
  if (sharedOn) {
    if (!sharedPort) {
      showAlert("error", "Enter a port for the instance endpoints");
      return;
    }
    body.shared_port = Number(sharedPort);
  } else {
    body.shared_port = null;
  }

  const categoryOn = document.getElementById("settings-category-enable").checked;
  const categoryPort = document.getElementById("settings-category-port").value.trim();
  if (categoryOn) {
    if (!categoryPort) {
      showAlert("error", "Enter a port for the category endpoints");
      return;
    }
    body.category_port = Number(categoryPort);
  } else {
    body.category_port = null;
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


// ── Backup & Restore ─────────────────────────────────────────────────────────

/** Say what kind of file the next download would be, before it is downloaded.
 *
 * A backup with credentials in it is a different object from one without —
 * one belongs in a password vault, the other may sit in a cloud folder — and
 * a file whose contents nobody can tell by looking is one that ends up in the
 * wrong place.
 */
function markBackupKind() {
  const withSecrets = document.getElementById("backup-secrets").checked;
  const note = document.getElementById("backup-secrets-note");
  note.className = `settings-status settings-status-${withSecrets ? "warn" : "ok"}`;
  note.textContent = withSecrets
    ? "The file will contain the MCP token, the read and agent tokens, the user-JWT secret, the file key and every API key in the instance valves. Treat it like a password vault — it restores a working server, and it opens this one."
    : "No credentials in the file — safe to keep anywhere. After restoring, every instance that needs an API key has to be given it again, and old download links stay broken.";
}

async function downloadBackup() {
  const withSecrets = document.getElementById("backup-secrets").checked;
  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "").replace(/(\d{8})(\d{6})/, "$1-$2");
  const name = `mcp-spawner-backup-${stamp}${withSecrets ? "-with-secrets" : ""}.json`;
  try {
    await downloadBlob(`/api/backup?secrets=${withSecrets}`, name);
    showAlert("info", withSecrets ? `${name} — keep it like a password.` : `${name} downloaded.`);
  } catch (e) {
    showAlert("error", "Backup failed: " + e.message);
  }
}

async function runRestore(dryRun) {
  const input = document.getElementById("backup-file");
  const output = document.getElementById("backup-result");
  const file = input.files?.[0];
  if (!file) {
    output.innerHTML = '<div class="settings-status settings-status-warn">Pick a backup file first.</div>';
    return;
  }
  // Confirmed for the real run only: a check writes nothing, so asking there
  // would train the habit of clicking the dialog away.
  if (!dryRun && !confirm("Restore from this backup?\n\nNothing existing is overwritten — instances, settings and keys that are already here stay as they are.")) return;

  output.innerHTML = '<div class="settings-status">Reading…</div>';
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch (e) {
    output.innerHTML = `<div class="settings-status settings-status-warn">Not readable as JSON: ${esc(e.message)}</div>`;
    return;
  }
  try {
    const report = await apiFetch("/api/backup/restore", {
      method: "POST",
      body: JSON.stringify({ ...payload, dry_run: dryRun }),
    });
    output.innerHTML = renderRestoreReport(report);
    if (!dryRun) loadSettingsData();
  } catch (e) {
    output.innerHTML = `<div class="settings-status settings-status-warn">${esc(e.message)}</div>`;
  }
}

function renderRestoreReport(report) {
  const lines = [];
  const list = (label, items, render = String) =>
    items.length ? lines.push(`<div class="backup-line"><span class="backup-label">${label}</span>${esc(items.map(render).join(", "))}</div>`) : null;

  list("Instances added", report.instances_restored, r =>
    r.credentials_missing ? `${r.id} (no ${r.credentials_missing.join(", ")})` : r.id);
  list("Already here, untouched", report.instances_skipped, r => r.id);
  list("Refused", report.instances_failed, r => `${r.id}: ${r.reason}`);
  list("Port taken, moved", report.ports_reassigned, r => `${r.id}: ${r.was} → ${r.now}`);
  list("Settings added", report.settings_restored);
  list("Settings kept", report.settings_skipped);
  list("Agents added", report.agents_restored);
  list("Agents kept", report.agents_skipped);
  list("Rules added for", report.policy_users_restored);
  list("Rules kept for", report.policy_users_skipped);
  if (report.content_key) lines.push(`<div class="backup-line"><span class="backup-label">File key</span>${esc(report.content_key)}</div>`);

  if (!lines.length) lines.push('<div class="backup-line">Nothing in this backup, and nothing to do.</div>');

  // "Restored." over a report whose every line says *kept* and *untouched* is
  // the dialog contradicting itself. A run that wrote nothing has to say so —
  // that outcome is the normal one when a backup is replayed onto the server
  // it came from, and it is the proof that a mistaken click costs nothing.
  const wrote = report.instances_restored.length || report.settings_restored.length
    || report.agents_restored.length || report.policy_users_restored.length
    || report.content_key === "restored";
  const head = report.dry_run
    ? '<div class="settings-status">Nothing was written. This is what a restore would do:</div>'
    : wrote
      ? '<div class="settings-status settings-status-ok">Restored.</div>'
      : '<div class="settings-status settings-status-ok">Nothing to restore — everything in this backup is already on this server, and nothing was changed.</div>';
  const tail = (!report.dry_run && report.instances_restored.length)
    ? '<p class="modal-hint">Restored instances are not installed or started yet — their venv is built the normal way from the dashboard.</p>'
    : "";
  const warn = (!report.contains_secrets && report.instances_restored.length)
    ? '<p class="modal-hint">This backup carried no credentials, so any instance that needs an API key has to be given it in <em>Edit → Config</em> before it will run.</p>'
    : "";
  return head + lines.join("") + tail + warn;
}
