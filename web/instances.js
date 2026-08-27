import { API, apiFetch, downloadBlob, esc, showAlert, state } from "./common.js";

let instances = [];
export async function loadInstances() {
  try {
    instances = await apiFetch(API);
    updateCategoryOptions();
    renderTable(filteredInstances());
  } catch (e) {
    // silently skip poll errors
  }
}

// ── Search & category filter ──────────────────────────────────────────────────

export function bindFilter() {
  document.getElementById("filter-search").addEventListener("input", () => renderTable(filteredInstances()));
  document.getElementById("filter-category").addEventListener("change", () => renderTable(filteredInstances()));
}

let lastCategoryKey = "";
let actionHandlers = {};

export function configureInstanceActions(handlers) { actionHandlers = handlers; }

function updateCategoryOptions() {
  const sel = document.getElementById("filter-category");
  const cats = [...new Set(instances.map(i => (i.category || "").trim()).filter(Boolean))].sort();
  // Only rebuild when the category set actually changed — rebuilding on every
  // poll would snap the dropdown shut while the user is picking an option.
  const key = cats.join("\x00");
  if (key === lastCategoryKey) return;
  lastCategoryKey = key;
  const current = sel.value;
  sel.innerHTML = '<option value="">All categories</option>' +
    cats.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  if (cats.includes(current)) sel.value = current;
}

function filteredInstances() {
  const q = document.getElementById("filter-search").value.trim().toLowerCase();
  const cat = document.getElementById("filter-category").value;
  return instances.filter(inst => {
    if (cat && (inst.category || "").trim() !== cat) return false;
    if (!q) return true;
    return [inst.id, inst.name, inst.description, inst.category]
      .some(f => (f || "").toLowerCase().includes(q));
  });
}

function renderTable(rows) {
  const tbody = document.getElementById("instances-body");

  if (!rows.length) {
    // `instances` is the unfiltered module-level list — if it has entries,
    // the filter (not the empty install) produced the empty view.
    const msg = instances.length ? "No instances match the current filter." : "No MCP instances yet. Upload a JSON to get started.";
    tbody.innerHTML = `<tr class="empty-row"><td colspan="8">${msg}</td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(inst => `
    <tr data-id="${inst.id}">
      <td class="id-cell">${esc(inst.id)}</td>
      <td>${esc(inst.name)}${inst.version ? ` <span class="version-badge">${esc(inst.version)}</span>` : ''}${bundledMark(inst)}</td>
      <td>${inst.category ? `<span class="category-badge">${esc(inst.category)}</span>` : '<span class="cell-muted">—</span>'}</td>
      <td>${statusBadge(inst.status, inst.error)}</td>
      <td class="admin-col">${inst.port ?? ""}</td>
      <td class="admin-col"><span class="venv-badge">${esc(inst.venv || 'default')}</span></td>
      <td class="url-cell admin-col">${inst.url ? `<a href="${esc(inst.url)}" target="_blank">${esc(inst.url)}</a>` : ""}</td>
      <td class="admin-col"><div class="actions">${state.guestMode ? "" : actionButtons(inst)}</div></td>
    </tr>
  `).join("");

  // Bind action buttons
  tbody.querySelectorAll("[data-action]").forEach(btn => {
    btn.addEventListener("click", handleAction);
  });
}

// A tool installed from examples/ that the spawner ships in a newer version.
// Same colours as the header's update badge, but compact: a table row has no
// space for a sentence, and the Info dialog carries the detail and the path.
//
// Clickable unless the instance is locked or the edit mode forbids writing —
// a button that only ever answers 403 is worse than no button.
function bundledMark(inst) {
  if (!inst.bundled_update) return "";
  const canUpdate = state.editMode !== "readonly" && !inst.locked && !state.guestMode;
  const label = `↑ ${esc(inst.bundled_update)}`;
  if (!canUpdate) {
    const why = inst.locked ? "instance is locked" : "read-only mode";
    return ` <span class="update-badge update-badge-sm"
      title="Version ${esc(inst.bundled_update)} ships with this spawner — cannot update here (${why})">${label}</span>`;
  }
  return ` <button class="update-badge update-badge-sm" data-action="updateexample" data-id="${esc(inst.id)}"
    title="Update to ${esc(inst.bundled_update)} from the copy shipped with this spawner">${label}</button>`;
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

  const canUploadOrEdit = state.editMode !== "readonly" && !locked;
  const canCodeEdit     = state.editMode === "full" && !locked;
  const canRestart      = !locked;
  const canReinstall    = !locked;

  const btns = [];
  if (stopped)  btns.push(ab("start",    inst.id, "Start",    "btn-success btn-sm", busy));
  if (running) btns.push(ab("stop", inst.id, "Stop", "btn-danger btn-sm", busy));
  if ((running || stopped) && canRestart) btns.push(ab("restart", inst.id, "Restart", "btn-secondary btn-sm", busy));
  if (canUploadOrEdit) btns.push(ab("edit",     inst.id, "Edit",      "btn-secondary btn-sm", busy));
  if (canCodeEdit)     btns.push(ab("editcode", inst.id, "Edit Code", "btn-secondary btn-sm", busy));
  // Info is pure metadata — available in every edit mode, even when the code
  // editor is locked away.
  btns.push(ab("info",   inst.id, "Info",   "btn-secondary btn-sm"));
  btns.push(ab("logs",   inst.id, "Logs",   "btn-secondary btn-sm"));
  btns.push(ab("export", inst.id, "Export", "btn-secondary btn-sm"));
  // Reinstall stays available in readonly mode (matches the API): it only
  // re-runs pip for the already-pinned dependency list.
  if (canReinstall) btns.push(ab("reinstall", inst.id, "Reinstall", "btn-warning btn-sm", busy));
  if (canUploadOrEdit) btns.push(ab("delete", inst.id, "Delete", "btn-danger btn-sm", running));
  // Lock/unlock modifies the config and is rejected server-side in readonly
  // mode — don't render a button that can only 403.
  if (state.editMode !== "readonly") {
    btns.push(ab(locked ? "unlock" : "lock", inst.id, locked ? "🔓" : "🔒", "btn-lock btn-sm", busy));
  }
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
        await actionHandlers.openEdit(id);
        return;
      case "editcode":
        await actionHandlers.openEditorForInstance(id);
        return;
      case "logs":
        await actionHandlers.openLogs(id);
        return;
      case "info":
        await actionHandlers.openInfo(id, instances.find(i => i.id === id) || null);
        return;
      case "updateexample":
        await updateFromExample(id, instances.find(i => i.id === id) || null);
        return;
    }
    loadInstances();
  } catch (err) {
    showAlert("error", err.message);
  }
}

// Overwrites the instance's code with the shipped copy. The question spells
// out what is replaced and what survives, because "Update?" alone would not
// tell anyone that a hand-adapted tool is about to be replaced.
export async function updateFromExample(id, inst = null) {
  const to = inst?.bundled_update ? ` to ${inst.bundled_update}` : "";
  const from = inst?.version ? ` from ${inst.version}` : "";
  if (!confirm(
    `Update ${id}${from}${to} with the copy shipped in this spawner?\n\n` +
    `This replaces the tool's code. Your Valve values are kept, and the ` +
    `previous version is saved to runtime/history — but any changes you made ` +
    `to the code itself will be gone from the instance.`
  )) return;
  try {
    const res = await apiFetch(`${API}/${id}/update-from-example`, { method: "POST" });
    showAlert("success", `${id} updated to ${res.version} from ${res.source}.`);
    if (res.restarted) showAlert("info", `${id} was restarted.`);
    (res.warnings || []).forEach(w => showAlert("info", w));
  } catch (e) {
    showAlert("error", `Update failed: ${e.message}`);
  }
  loadInstances();
}

async function exportInstance(id) {
  try {
    await downloadBlob(`${API}/${id}/export`, `${id}.json`);
  } catch (e) {
    showAlert("error", "Export failed: " + e.message);
  }
}

