const API = "/api/instances";
let pollTimer = null;
let currentEditId = null;
let currentLogsId = null;
let currentLogsTab = "install";

// "full" | "upload" | "readonly"  — set from /api/auth-status at startup
let editMode = "full";

function applyEditMode() {
  // "full":     all buttons visible
  // "upload":   upload + config edit + delete OK; hide New Tool + Edit Code
  // "readonly": hide upload, New Tool, Edit Code, config edit, delete
  const hideUpload   = editMode === "readonly";
  const hideNewTool  = editMode !== "full";
  document.getElementById("upload-btn").classList.toggle("hidden", hideUpload);
  document.getElementById("editor-btn").classList.toggle("hidden", hideNewTool);
}

// ── Auth ──────────────────────────────────────────────────────────────────────

function getToken() { return sessionStorage.getItem("mcp_token") || ""; }
function setToken(t) { sessionStorage.setItem("mcp_token", t); }
function clearToken() { sessionStorage.removeItem("mcp_token"); }

function authHeaders() {
  const t = getToken();
  return t ? { "Authorization": `Bearer ${t}` } : {};
}

function showLoginModal() {
  document.getElementById("login-modal").classList.remove("hidden");
  document.getElementById("login-password").focus();
}
function hideLoginModal() {
  document.getElementById("login-modal").classList.add("hidden");
  document.getElementById("login-password").value = "";
  document.getElementById("login-error").classList.add("hidden");
}

document.getElementById("login-form").addEventListener("submit", async e => {
  e.preventDefault();
  const pw = document.getElementById("login-password").value;
  const res = await fetch("/api/auth-check", { headers: { "Authorization": `Bearer ${pw}` } });
  if (res.ok) {
    setToken(pw);
    hideLoginModal();
    applyEditMode();
    loadInstances();
    startPolling();
  } else {
    document.getElementById("login-error").classList.remove("hidden");
  }
});

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", async () => {
  // Bind handlers once — they rely on sessionStorage token at call time, not at bind time
  bindUpload();
  bindEdit();
  bindLogs();

  const statusRes = await fetch("/api/auth-status");
  const statusData = await statusRes.json();
  editMode = statusData.edit_mode || "full";
  applyEditMode();

  if (statusData.auth_enabled && !getToken()) {
    showLoginModal();
    return;
  }
  loadInstances();
  startPolling();
});

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(loadInstances, 4000);
}

// ── API helpers ──────────────────────────────────────────────────────────────

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", ...authHeaders(), ...opts.headers },
    ...opts,
  });
  if (res.status === 401) {
    clearToken();
    showLoginModal();
    throw new Error("Session expired — please log in again");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || res.statusText);
  }
  return res.json().catch(() => ({}));
}

// ── Venvs ──────────────────────────────────────────────────────────────────────

async function fetchVenvs() {
  try {
    return await apiFetch("/api/venvs");
  } catch {
    return [{ name: "default", is_default: true, instances: 0, exists: true }];
  }
}

function fillVenvSelect(sel, venvs, selected) {
  sel.innerHTML = venvs.map(v =>
    `<option value="${esc(v.name)}"${v.name === selected ? " selected" : ""}>${esc(v.name)}${v.is_default ? " (default)" : ""}</option>`
  ).join("");
  // Keep the instance's current venv selectable even if it no longer exists
  if (selected && !venvs.some(v => v.name === selected)) {
    const o = document.createElement("option");
    o.value = selected;
    o.textContent = `${selected} (current)`;
    o.selected = true;
    sel.appendChild(o);
  }
}

// ── Instances table ──────────────────────────────────────────────────────────

async function loadInstances() {
  try {
    const instances = await apiFetch(API);
    renderTable(instances);
  } catch (e) {
    // silently skip poll errors
  }
}

function renderTable(instances) {
  const tbody = document.getElementById("instances-body");

  if (!instances.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No MCP instances yet. Upload a JSON to get started.</td></tr>';
    return;
  }

  tbody.innerHTML = instances.map(inst => `
    <tr data-id="${inst.id}">
      <td class="id-cell">${esc(inst.id)}</td>
      <td>${esc(inst.name)}${inst.version ? ` <span class="version-badge">${esc(inst.version)}</span>` : ''}</td>
      <td>${statusBadge(inst.status, inst.error)}</td>
      <td>${inst.port}</td>
      <td><span class="venv-badge">${esc(inst.venv || 'default')}</span></td>
      <td class="url-cell"><a href="${esc(inst.url)}" target="_blank">${esc(inst.url)}</a></td>
      <td class="actions">${actionButtons(inst)}</td>
    </tr>
  `).join("");

  // Bind action buttons
  tbody.querySelectorAll("[data-action]").forEach(btn => {
    btn.addEventListener("click", handleAction);
  });
}

function statusBadge(status, error = "") {
  const title = error ? ` title="${esc(error)}"` : "";
  return `<span class="badge badge-${status}"${title}>${status}</span>`;
}

function actionButtons(inst) {
  const s = inst.status;
  const running = s === "running";
  const stopped = ["stopped", "installed", "failed", "dependency_error"].includes(s);
  const busy = ["starting", "stopping", "installing"].includes(s);
  const locked = !!inst.locked;

  const canUploadOrEdit = editMode !== "readonly" && !locked;
  const canCodeEdit     = editMode === "full" && !locked;
  const canRestart      = !locked;
  const canReinstall    = !locked;

  const btns = [];
  if (stopped)  btns.push(ab("start",    inst.id, "Start",    "btn-success btn-sm", busy));
  if (running && !locked) btns.push(ab("stop", inst.id, "Stop", "btn-danger btn-sm", busy));
  if ((running || stopped) && canRestart) btns.push(ab("restart", inst.id, "Restart", "btn-secondary btn-sm", busy));
  if (canUploadOrEdit) btns.push(ab("edit",     inst.id, "Edit",      "btn-secondary btn-sm", busy));
  if (canCodeEdit)     btns.push(ab("editcode", inst.id, "Edit Code", "btn-secondary btn-sm", busy));
  btns.push(ab("logs",   inst.id, "Logs",   "btn-secondary btn-sm"));
  btns.push(ab("export", inst.id, "Export", "btn-secondary btn-sm"));
  if (canReinstall && editMode !== "readonly") btns.push(ab("reinstall", inst.id, "Reinstall", "btn-warning btn-sm", busy));
  if (canUploadOrEdit) btns.push(ab("delete", inst.id, "Delete", "btn-danger btn-sm", running));
  btns.push(ab(locked ? "unlock" : "lock", inst.id, locked ? "🔓" : "🔒", "btn-lock btn-sm", busy));
  return btns.join("");
}

function ab(action, id, label, cls, disabled = false) {
  return `<button class="btn ${cls}" data-action="${action}" data-id="${id}" ${disabled ? "disabled" : ""}>${label}</button>`;
}

async function handleAction(e) {
  const action = e.target.dataset.action;
  const id = e.target.dataset.id;

  try {
    switch (action) {
      case "start":
        await apiFetch(`${API}/${id}/start`, { method: "POST" });
        showAlert("success", `Starting ${id}…`);
        break;
      case "stop":
        await apiFetch(`${API}/${id}/stop`, { method: "POST" });
        showAlert("success", `Stopping ${id}…`);
        break;
      case "restart":
        await apiFetch(`${API}/${id}/restart`, { method: "POST" });
        showAlert("success", `Restarting ${id}…`);
        break;
      case "reinstall":
        if (!confirm(`Reinstall dependencies for ${id}?`)) return;
        await apiFetch(`${API}/${id}/reinstall`, { method: "POST" });
        showAlert("success", "Reinstall started.");
        break;
      case "delete":
        if (!confirm(`Delete ${id}? This cannot be undone.`)) return;
        await apiFetch(`${API}/${id}`, { method: "DELETE" });
        showAlert("success", `${id} deleted.`);
        break;
      case "export":
        await exportInstance(id);
        return;
      case "lock":
        await apiFetch(`${API}/${id}/lock`, { method: "POST" });
        showAlert("info", `${id} locked — only Start/Stop allowed.`);
        break;
      case "unlock":
        await apiFetch(`${API}/${id}/unlock`, { method: "POST" });
        showAlert("success", `${id} unlocked.`);
        break;
      case "edit":
        await openEdit(id);
        return;
      case "editcode":
        await openEditorForInstance(id);
        return;
      case "logs":
        await openLogs(id);
        return;
    }
    loadInstances();
  } catch (err) {
    showAlert("error", err.message);
  }
}

async function exportInstance(id) {
  try {
    const res = await fetch(`${API}/${id}/export`, { headers: authHeaders() });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showAlert("error", body.detail || "Export failed");
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    showAlert("error", "Export failed: " + e.message);
  }
}

// ── Upload Modal ─────────────────────────────────────────────────────────────

function bindUpload() {
  const modal    = document.getElementById("upload-modal");
  const backdrop = document.getElementById("upload-backdrop");
  const openBtn  = document.getElementById("upload-btn");
  const cancelBtn = document.getElementById("upload-cancel");
  const submitBtn = document.getElementById("upload-submit");
  const fileInput = document.getElementById("file-input");
  const fileLabel = document.getElementById("file-label");
  const fileDrop  = document.getElementById("file-drop");
  const progress  = document.getElementById("upload-progress");
  const statusTxt = document.getElementById("upload-status-text");

  let selectedFile = null;

  openBtn.addEventListener("click", async () => {
    selectedFile = null; resetUpload(); modal.classList.remove("hidden");
    const uploadVenv = document.getElementById("upload-venv");
    fillVenvSelect(uploadVenv, await fetchVenvs(), "");
    uploadVenv.prepend(new Option("from JSON / default", ""));
    uploadVenv.value = "";
  });
  cancelBtn.addEventListener("click", closeUpload);
  backdrop.addEventListener("click", closeUpload);

  fileInput.addEventListener("change", () => {
    selectedFile = fileInput.files[0];
    fileLabel.textContent = selectedFile ? selectedFile.name : "Drop file here or click to select";
    submitBtn.disabled = !selectedFile;
  });

  fileDrop.addEventListener("dragover", e => { e.preventDefault(); fileDrop.classList.add("drag-over"); });
  fileDrop.addEventListener("dragleave", () => fileDrop.classList.remove("drag-over"));
  fileDrop.addEventListener("drop", e => {
    e.preventDefault();
    fileDrop.classList.remove("drag-over");
    const f = e.dataTransfer.files[0];
    if (f) { selectedFile = f; fileLabel.textContent = f.name; submitBtn.disabled = false; }
  });

  submitBtn.addEventListener("click", async () => {
    if (!selectedFile) return;
    submitBtn.disabled = true;
    progress.classList.remove("hidden");
    statusTxt.textContent = "Uploading & installing…";

    try {
      const form = new FormData();
      form.append("file", selectedFile);
      // Only send venv when the user picked one, so a venv set in an uploaded
      // MCP config stays the default instead of being overwritten with "default".
      const venvVal = document.getElementById("upload-venv").value.trim();
      if (venvVal) form.append("venv", venvVal);
      const portVal = document.getElementById("upload-port").value;
      if (portVal) form.append("port", portVal);
      const res = await fetch(`${API}/upload`, { method: "POST", headers: authHeaders(), body: form });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || res.statusText);
      }
      const data = await res.json();
      closeUpload();
      showAlert("success", `Installed: ${data.id} on port ${data.port}`);
      loadInstances();
    } catch (err) {
      statusTxt.textContent = "";
      progress.classList.add("hidden");
      submitBtn.disabled = false;
      showAlert("error", err.message);
    }
  });

  function closeUpload() { modal.classList.add("hidden"); }
  function resetUpload() {
    fileInput.value = "";
    fileLabel.textContent = "Drop file here or click to select";
    document.getElementById("upload-port").value = "";
    submitBtn.disabled = true;
    progress.classList.add("hidden");
  }
}

// ── Edit Modal ────────────────────────────────────────────────────────────────

function bindEdit() {
  document.getElementById("edit-backdrop").addEventListener("click", closeEdit);
  document.getElementById("edit-cancel").addEventListener("click", closeEdit);
  document.getElementById("edit-save").addEventListener("click", () => saveEdit(false));
  document.getElementById("edit-save-restart").addEventListener("click", () => saveEdit(true));
}

async function openEdit(id) {
  currentEditId = id;
  const cfg = await apiFetch(`${API}/${id}/config`);

  document.getElementById("edit-title").textContent = cfg.name;
  document.getElementById("edit-name").value = cfg.name;
  document.getElementById("edit-host").value = cfg.server.host;
  document.getElementById("edit-port").value = cfg.server.port;
  document.getElementById("edit-endpoint").value = cfg.server.endpoint;
  document.getElementById("edit-autostart").checked = cfg.lifecycle?.auto_start ?? false;
  document.getElementById("edit-deps").value = (cfg.install?.dependencies || []).join("\n");
  fillVenvSelect(document.getElementById("edit-venv"), await fetchVenvs(), cfg.venv || "default");

  const container = document.getElementById("edit-values-container");
  if (cfg.values && Object.keys(cfg.values).length > 0) {
    container.innerHTML = '<div class="values-grid">' +
      Object.entries(cfg.values).map(([k, v]) => {
        const isSecret = /key|token|secret|password|auth/i.test(k);
        const type = typeof v === "boolean" ? "checkbox"
                   : typeof v === "number" ? "number" : "text";
        if (type === "checkbox") {
          return `<label>${esc(k)}</label><input type="checkbox" data-val="${esc(k)}" ${v ? "checked" : ""} />`;
        }
        const inputVal = isSecret ? "" : esc(String(v));
        const placeholder = isSecret ? "●●●●●●●●" : "";
        return `<label>${esc(k)}</label><input type="${type}" data-val="${esc(k)}" value="${inputVal}" placeholder="${placeholder}" />`;
      }).join("") + "</div>";
  } else {
    container.innerHTML = "<p style='color:var(--text-muted);font-size:13px'>No configurable values.</p>";
  }

  document.getElementById("edit-modal").classList.remove("hidden");
}

async function saveEdit(restart) {
  const id = currentEditId;
  const values = {};

  document.querySelectorAll("[data-val]").forEach(input => {
    const key = input.dataset.val;
    if (input.type === "checkbox") values[key] = input.checked;
    else if (input.type === "number") { if (input.value !== "") values[key] = Number(input.value); }
    else if (input.value !== "●●●●●●●●" && input.value !== "") values[key] = input.value;
  });

  const deps = document.getElementById("edit-deps").value
    .split("\n").map(s => s.trim()).filter(Boolean);

  const body = {
    name: document.getElementById("edit-name").value,
    server: {
      host: document.getElementById("edit-host").value,
      port: parseInt(document.getElementById("edit-port").value),
      endpoint: document.getElementById("edit-endpoint").value,
    },
    lifecycle: { auto_start: document.getElementById("edit-autostart").checked },
    values,
    install: { dependencies: deps },
    venv: document.getElementById("edit-venv").value || "default",
  };

  try {
    await apiFetch(`${API}/${id}`, { method: "PUT", body: JSON.stringify(body) });
    closeEdit();
    showAlert("success", "Config saved.");
    if (restart) {
      await apiFetch(`${API}/${id}/restart`, { method: "POST" });
      showAlert("info", "Restarting…");
    }
    loadInstances();
  } catch (err) {
    showAlert("error", err.message);
  }
}

function closeEdit() {
  document.getElementById("edit-modal").classList.add("hidden");
  currentEditId = null;
}

// ── Logs Modal ────────────────────────────────────────────────────────────────

function bindLogs() {
  document.getElementById("logs-backdrop").addEventListener("click", closeLogs);
  document.getElementById("logs-close").addEventListener("click", closeLogs);
  document.getElementById("logs-refresh").addEventListener("click", () => loadLog(currentLogsId, currentLogsTab));

  document.getElementById("tab-install").addEventListener("click", () => switchTab("install"));
  document.getElementById("tab-runtime").addEventListener("click", () => switchTab("runtime"));
}

async function openLogs(id) {
  currentLogsId = id;
  currentLogsTab = "install";
  document.getElementById("logs-title").textContent = id;
  switchTab("install");
  document.getElementById("logs-modal").classList.remove("hidden");
}

async function switchTab(tab) {
  currentLogsTab = tab;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  await loadLog(currentLogsId, tab);
}

async function loadLog(id, tab) {
  const pre = document.getElementById("log-content");
  pre.textContent = "Loading…";
  try {
    const res = await fetch(`${API}/${id}/logs/${tab}`, { headers: authHeaders() });
    const text = await res.text();
    pre.textContent = text || "(empty)";
    pre.scrollTop = pre.scrollHeight;
  } catch {
    pre.textContent = "(failed to load)";
  }
}

function closeLogs() {
  document.getElementById("logs-modal").classList.add("hidden");
  currentLogsId = null;
}

// ── Alerts ────────────────────────────────────────────────────────────────────

function showAlert(type, msg) {
  const area = document.getElementById("alert-area");
  const el = document.createElement("div");
  el.className = `alert alert-${type}`;
  el.textContent = msg;
  area.prepend(el);
  setTimeout(() => el.remove(), 5000);
}

// ── Utils ─────────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Tool Editor ───────────────────────────────────────────────────────────────

let editorCM = null;
let editorEditId = null;   // null = new tool, string = editing existing instance

document.getElementById("editor-btn").addEventListener("click", () => openEditor());
document.getElementById("editor-cancel").addEventListener("click", closeEditor);
document.getElementById("editor-close").addEventListener("click", closeEditor);
document.getElementById("editor-backdrop").addEventListener("click", closeEditor);
document.getElementById("editor-validate").addEventListener("click", runValidate);
document.getElementById("editor-export").addEventListener("click", runExport);
document.getElementById("editor-install").addEventListener("click", runInstall);

async function openEditorForInstance(id) {
  try {
    const data = await apiFetch(`${API}/${id}/tool-code`);
    await openEditor({ id: data.id, name: data.name, description: data.description, code: data.code, editId: id });
  } catch (e) {
    showAlert("error", "Could not load tool code: " + e.message);
  }
}

async function openEditor(opts = {}) {
  editorEditId = opts.editId || null;
  document.getElementById("editor-modal").classList.remove("hidden");

  // Update header + save button label based on mode
  document.querySelector("#editor-modal .editor-header h2").textContent =
    editorEditId ? "Edit Tool Code" : "New Tool";
  document.getElementById("editor-install").textContent =
    editorEditId ? "Save & Apply" : "Install as MCP";

  // Hide results panel when opening fresh
  document.getElementById("editor-results").classList.add("hidden");

  // Init CodeMirror once
  if (!editorCM) {
    const ta = document.getElementById("editor-code");
    if (typeof CodeMirror !== "undefined") {
      editorCM = CodeMirror.fromTextArea(ta, {
        mode: "python",
        theme: "dracula",
        lineNumbers: true,
        indentUnit: 4,
        tabSize: 4,
        indentWithTabs: false,
        lineWrapping: false,
        autofocus: true,
        extraKeys: { Tab: cm => cm.execCommand("indentMore") },
      });
      editorCM.setSize("100%", "100%");
    }
    // Load starter template
    const resp = await fetch("/api/tools/template", { headers: authHeaders() });
    const tmpl = await resp.text();
    if (editorCM) editorCM.setValue(tmpl);
    else ta.value = tmpl;
  }

  // Populate fields (new tool: from opts or keep current; edit: always overwrite)
  if (opts.code !== undefined) {
    if (editorCM) editorCM.setValue(opts.code);
    else document.getElementById("editor-code").value = opts.code;
  }
  if (opts.id !== undefined) document.getElementById("editor-id").value = opts.id;
  if (opts.name !== undefined) document.getElementById("editor-name").value = opts.name;
  if (opts.description !== undefined) document.getElementById("editor-desc").value = opts.description;

  // Lock ID field when editing (can't rename an existing instance)
  document.getElementById("editor-id").disabled = !!editorEditId;

  // Venv + port only apply when creating a new instance
  document.getElementById("editor-venv-field").classList.toggle("hidden", !!editorEditId);
  document.getElementById("editor-port-field").classList.toggle("hidden", !!editorEditId);
  if (!editorEditId) {
    document.getElementById("editor-port").value = "";
    fillVenvSelect(document.getElementById("editor-venv"), await fetchVenvs(), opts.venv || "default");
  }
}

function closeEditor() {
  document.getElementById("editor-modal").classList.add("hidden");
}

function getEditorCode() {
  if (editorCM) return editorCM.getValue();
  return document.getElementById("editor-code").value;
}

function getEditorMeta() {
  return {
    id: document.getElementById("editor-id").value.trim(),
    name: document.getElementById("editor-name").value.trim(),
    description: document.getElementById("editor-desc").value.trim(),
  };
}

async function runValidate() {
  const btn = document.getElementById("editor-validate");
  btn.disabled = true;
  btn.textContent = "Validating…";
  try {
    const res = await fetch("/api/tools/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ code: getEditorCode() }),
    });
    const data = await res.json();
    // Normalise: API errors come back as {detail:…} without the expected fields
    showValidationResults({
      valid: data.valid ?? false,
      errors: data.errors ?? (data.detail ? [String(data.detail)] : ["Unknown error"]),
      warnings: data.warnings ?? [],
      tools: data.tools ?? [],
      valves: data.valves ?? {},
    });
  } catch (e) {
    showAlert("error", "Validation request failed: " + e);
  } finally {
    btn.disabled = false;
    btn.textContent = "Validate";
  }
}

function showValidationResults(data) {
  const panel = document.getElementById("editor-results");
  panel.classList.remove("hidden");

  const badge = document.getElementById("results-badge");
  const summary = document.getElementById("results-summary");

  if (data.valid) {
    badge.className = "badge badge-ok";
    badge.textContent = "VALID";
    summary.textContent = `${data.tools.length} tool(s) detected, ${data.warnings.length} warning(s)`;
  } else {
    badge.className = "badge badge-error";
    badge.textContent = "INVALID";
    summary.textContent = `${data.errors.length} error(s)`;
  }

  // Errors
  const errSec = document.getElementById("results-errors");
  const errList = document.getElementById("errors-list");
  if (data.errors.length) {
    errList.innerHTML = data.errors.map(e => `<li class="result-error">${esc(e)}</li>`).join("");
    errSec.classList.remove("hidden");
  } else {
    errSec.classList.add("hidden");
  }

  // Warnings
  const warnSec = document.getElementById("results-warnings");
  const warnList = document.getElementById("warnings-list");
  if (data.warnings.length) {
    warnList.innerHTML = data.warnings.map(w => `<li class="result-warn">${esc(w)}</li>`).join("");
    warnSec.classList.remove("hidden");
  } else {
    warnSec.classList.add("hidden");
  }

  // Tools
  const toolSec = document.getElementById("results-tools");
  const toolList = document.getElementById("tools-list");
  if (data.tools.length) {
    toolList.innerHTML = data.tools.map(t => {
      const params = Object.keys(t.parameters.properties || {}).join(", ");
      return `<li><strong>${esc(t.name)}</strong>(${esc(params)}) — ${esc(t.description)}</li>`;
    }).join("");
    toolSec.classList.remove("hidden");
  } else {
    toolSec.classList.add("hidden");
  }

  // Valves
  const valveSec = document.getElementById("results-valves");
  const valvePre = document.getElementById("valves-pre");
  if (data.valves && Object.keys(data.valves).length) {
    valvePre.textContent = JSON.stringify(data.valves, null, 2);
    valveSec.classList.remove("hidden");
  } else {
    valveSec.classList.add("hidden");
  }
}

async function runExport() {
  const meta = getEditorMeta();
  if (!meta.id) { showAlert("error", "Please enter a Tool ID first"); return; }
  if (!meta.name) { showAlert("error", "Please enter a Tool Name first"); return; }

  const res = await fetch("/api/tools/export", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ code: getEditorCode(), ...meta }),
  });
  if (!res.ok) {
    const err = await res.json();
    showAlert("error", err.detail || "Export failed");
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${meta.id}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

// Surface a server-side 422 (validation / dependency errors) in the results panel.
function showEditorErrors(data) {
  const detail = (data && data.detail !== undefined) ? data.detail : data;
  let errors = [];
  if (detail && typeof detail === "object") {
    errors = detail.errors || (detail.message ? [detail.message] : []);
  } else if (detail) {
    errors = [String(detail)];
  }
  if (!errors.length) errors = ["Request failed"];
  showValidationResults({ valid: false, errors, warnings: [], tools: [], valves: {} });
  showAlert("error", errors.join("; "));
}

async function runInstall() {
  const meta = getEditorMeta();
  if (!meta.id) { showAlert("error", "Please enter a Tool ID first"); return; }
  if (!meta.name) { showAlert("error", "Please enter a Tool Name first"); return; }

  const btn = document.getElementById("editor-install");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = editorEditId ? "Saving…" : "Installing…";

  try {
    if (editorEditId) {
      // Edit mode: PUT code; the server validates inside the instance venv.
      const res = await fetch(`${API}/${editorEditId}/tool-code`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ code: getEditorCode() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { showEditorErrors(data); return; }
      if (data.warnings?.length) showAlert("warning", "Warnings: " + data.warnings.join("; "));
      showAlert("success", data.restarted
        ? `Tool '${meta.name}' saved and restarted.`
        : `Tool '${meta.name}' saved. Restart to apply changes.`);
    } else {
      // New tool: create_tool installs deps, validates in the venv and saves — one step.
      const body = {
        code: getEditorCode(),
        id: meta.id,
        name: meta.name,
        description: meta.description,
        venv: document.getElementById("editor-venv").value || "default",
      };
      const portVal = document.getElementById("editor-port").value;
      if (portVal) body.port = Number(portVal);
      const res = await fetch("/api/tools/create", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { showEditorErrors(data); return; }
      if (data.warnings?.length) showAlert("warning", "Warnings: " + data.warnings.join("; "));
      showAlert("success", `Tool '${meta.name}' installed on port ${data.port}!`);
    }
    closeEditor();
    loadInstances();
  } catch (e) {
    showAlert("error", "Request failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

// ── Settings ──────────────────────────────────────────────────────────────────

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

document.getElementById("settings-btn").addEventListener("click", openSettings);
document.getElementById("settings-cancel").addEventListener("click", closeSettings);
document.getElementById("settings-backdrop").addEventListener("click", closeSettings);
document.getElementById("settings-save").addEventListener("click", saveSettings);
document.getElementById("settings-restart").addEventListener("click", restartManager);
document.getElementById("settings-venv-create").addEventListener("click", createVenv);

function openSettings() {
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

    document.getElementById("settings-edit-mode").value = data.edit_mode || "full";

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
  const pw1 = document.getElementById("settings-pw1").value.trim();
  const pw2 = document.getElementById("settings-pw2").value.trim();
  const mode = document.getElementById("settings-edit-mode").value;

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

  const body = { edit_mode: mode };
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

  try {
    const res = await apiFetch("/api/settings", { method: "PUT", body: JSON.stringify(body) });

    // Keep session alive with new password
    if (pw1 && res.changed && res.changed.includes("password")) {
      setToken(pw1);
    }

    // Apply new edit mode immediately in the UI
    if (res.changed && res.changed.includes("edit_mode")) {
      editMode = mode;
      applyEditMode();
    }

    const msg = res.changed && res.changed.length
      ? "Saved: " + res.changed.join(", ")
      : "Nothing changed";
    showAlert("success", msg);
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
