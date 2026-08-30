import { API, apiFetch, downloadBlob, esc, formatBytes, showAlert, state } from "./common.js";
import { instanceStat } from "./system.js";

let instances = [];
export async function loadInstances() {
  try {
    instances = await apiFetch(API);
    updateCategoryOptions();
    updateStatusOptions();
    render();
  } catch (e) {
    // silently skip poll errors
  }
}

// The one path to the table: filter, sort, then hand the whole result to
// renderTable, which cuts the current page out of it. The poll comes through
// here too, so the page the user is on survives a re-render.
function render() {
  renderTable(sortInstances(filteredInstances()));
}

// ── Search & category filter ──────────────────────────────────────────────────

export function bindFilter() {
  // Every filter change can shrink the result under the current page, so all
  // three go back to page 1 — the only page that is always populated.
  const bind = (id, event) => document.getElementById(id).addEventListener(event, () => {
    page = 1;
    render();
  });
  bind("filter-search", "input");
  bind("filter-category", "change");
  bind("filter-status", "change");
}

// ── Column sorting ────────────────────────────────────────────────────────────

// Where a click on a header takes the table. Empty key = the order the server
// sent, which is by config file name and therefore roughly by ID.
const SORT_STORAGE_KEY = "instances-sort";
let sort = { key: "", dir: 1 };

// Status is the one column where alphabetical order would be an accident.
// Sorted along the lifecycle instead: ascending puts what is up first and what
// is broken last, descending answers "what needs me?" in one click.
const STATUS_ORDER = ["running", "starting", "installing", "installed",
                      "stopping", "stopped", "failed", "dependency_error"];

export function bindSorting() {
  try {
    const stored = JSON.parse(localStorage.getItem(SORT_STORAGE_KEY) || "null");
    if (stored && typeof stored.key === "string") sort = { key: stored.key, dir: stored.dir === -1 ? -1 : 1 };
  } catch {
    // Private mode or a garbled value: the default order is a fine fallback.
  }
  for (const th of document.querySelectorAll("th[data-sort]")) {
    th.addEventListener("click", () => {
      // Same column again flips the direction; a new column starts ascending.
      sort = sort.key === th.dataset.sort
        ? { key: sort.key, dir: -sort.dir }
        : { key: th.dataset.sort, dir: 1 };
      try {
        localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify(sort));
      } catch {
        // Remembering the column is a convenience, not a requirement.
      }
      // A new order makes the current page number meaningless — start over.
      page = 1;
      render();
    });
  }
  markSortedHeader();
}

function markSortedHeader() {
  for (const th of document.querySelectorAll("th[data-sort]")) {
    const active = th.dataset.sort === sort.key;
    th.classList.toggle("sorted-asc", active && sort.dir === 1);
    th.classList.toggle("sorted-desc", active && sort.dir === -1);
  }
}

function sortKey(inst, key) {
  if (key === "port") return inst.port ?? -1;
  if (key === "status") {
    const rank = STATUS_ORDER.indexOf(inst.status);
    return rank === -1 ? STATUS_ORDER.length : rank;   // unknown status last
  }
  return String(inst[key] ?? "");
}

function sortInstances(rows) {
  markSortedHeader();
  if (!sort.key) return rows;
  // A guest never sees port, venv or URL — a sort remembered from an admin
  // session must not silently reorder by a field that is not there.
  if (!rows.some(inst => inst[sort.key] !== undefined)) return rows;
  return [...rows].sort((a, b) => {
    const x = sortKey(a, sort.key), y = sortKey(b, sort.key);
    let cmp;
    if (typeof x === "number") {
      cmp = x - y;
    } else {
      // numeric: true so mcp2 comes before mcp10; base so case is ignored.
      cmp = x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" });
    }
    // Tie-break by ID, always ascending and with the same collation the ID
    // column uses: the table re-renders on every poll, and equal keys must not
    // shuffle rows under the pointer.
    if (cmp !== 0) return cmp * sort.dir;
    return String(a.id).localeCompare(String(b.id), undefined, { numeric: true, sensitivity: "base" });
  });
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

let lastStatusKey = "";

// Built from the statuses that are actually present, in lifecycle order rather
// than alphabetically (same reasoning as the Status column sort), so the
// dropdown never offers a choice that can only come back empty. Guarded
// against needless rebuilds like the categories are, for the same reason.
function updateStatusOptions() {
  const sel = document.getElementById("filter-status");
  const rank = s => {
    const i = STATUS_ORDER.indexOf(s);
    return i === -1 ? STATUS_ORDER.length : i;
  };
  const stats = [...new Set(instances.map(i => i.status).filter(Boolean))].sort((a, b) => rank(a) - rank(b));
  const key = stats.join("\x00");
  if (key === lastStatusKey) return;
  lastStatusKey = key;
  const current = sel.value;
  sel.innerHTML = '<option value="">All statuses</option>' +
    stats.map(st => `<option value="${esc(st)}">${esc(st)}</option>`).join("");
  if (stats.includes(current)) sel.value = current;
}

function filteredInstances() {
  const q = document.getElementById("filter-search").value.trim().toLowerCase();
  const cat = document.getElementById("filter-category").value;
  const status = document.getElementById("filter-status").value;
  return instances.filter(inst => {
    if (cat && (inst.category || "").trim() !== cat) return false;
    if (status && inst.status !== status) return false;
    if (!q) return true;
    return [inst.id, inst.name, inst.description, inst.category]
      .some(f => (f || "").toLowerCase().includes(q));
  });
}

// ── Pagination ────────────────────────────────────────────────────────────────

// The page size is remembered, the page number is not: a reloaded dashboard
// should start at the top, but it should keep the row count that was asked for.
const PAGE_SIZE_STORAGE_KEY = "instances-page-size";
const PAGE_SIZES = [10, 20, 50, 200];
let pageSize = 20;
let page = 1;

export function bindPagination() {
  try {
    const stored = parseInt(localStorage.getItem(PAGE_SIZE_STORAGE_KEY) || "", 10);
    if (PAGE_SIZES.includes(stored)) pageSize = stored;
  } catch {
    // Private mode or a garbled value: the default page size is a fine fallback.
  }
  const sel = document.getElementById("page-size");
  sel.value = String(pageSize);
  sel.addEventListener("change", () => {
    pageSize = parseInt(sel.value, 10) || 20;
    page = 1;
    try {
      localStorage.setItem(PAGE_SIZE_STORAGE_KEY, String(pageSize));
    } catch {
      // Remembering the size is a convenience, not a requirement.
    }
    render();
  });
  // Bound once: these controls live in the static markup, unlike the table body
  // that is rewritten on every poll.
  document.getElementById("page-prev").addEventListener("click", () => { page = Math.max(1, page - 1); render(); });
  document.getElementById("page-next").addEventListener("click", () => { page += 1; render(); });
}

// Clamps the page against the current result set, updates the bar and returns
// the slice to draw. A filter that shrinks the list under the current page
// pulls the user back to the last page that still has rows, instead of leaving
// them staring at an empty table.
function paginate(rows) {
  const bar = document.getElementById("pagination");
  const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
  page = Math.min(Math.max(page, 1), totalPages);

  // Below the smallest page size the bar can never do anything — hide it
  // rather than show three dead controls. Above it the bar stays put even on a
  // single page, so the size dropdown remains reachable.
  if (!rows.length || instances.length <= PAGE_SIZES[0]) {
    bar.classList.add("hidden");
    return rows;
  }
  bar.classList.remove("hidden");

  const from = (page - 1) * pageSize;
  const slice = rows.slice(from, from + pageSize);
  document.getElementById("pagination-info").textContent = `${from + 1}–${from + slice.length} of ${rows.length}`;
  document.getElementById("pagination-page").textContent = `Page ${page} / ${totalPages}`;
  document.getElementById("page-prev").disabled = page === 1;
  document.getElementById("page-next").disabled = page === totalPages;
  return slice;
}

function renderTable(rows) {
  const tbody = document.getElementById("instances-body");
  const pageRows = paginate(rows);

  if (!pageRows.length) {
    // `instances` is the unfiltered module-level list — if it has entries,
    // the filter (not the empty install) produced the empty view.
    const msg = instances.length ? "No instances match the current filter." : "No MCP instances yet. Upload a JSON to get started.";
    tbody.innerHTML = `<tr class="empty-row"><td colspan="9">${msg}</td></tr>`;
    return;
  }

  tbody.innerHTML = pageRows.map(inst => `
    <tr data-id="${inst.id}">
      <td class="id-cell">${esc(inst.id)}</td>
      <td>${esc(inst.name)}${inst.version ? ` <span class="version-badge">${esc(inst.version)}</span>` : ''}${bundledMark(inst)}</td>
      <td>${inst.category ? `<span class="category-badge">${esc(inst.category)}</span>` : '<span class="cell-muted">—</span>'}</td>
      <td class="status-cell">${statusBadge(inst.status, inst.error)}${healthDot(inst)}</td>
      <td class="admin-col cell-muted mem-cell">${memoryCell(inst)}</td>
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

// The health check's verdict, next to the status the process manager reports.
// The two answer different questions: "running" means the process exists, the
// dot means the MCP endpoint actually replied. No dot until the first pass has
// probed it — a colour invented before a measurement is worse than none.
function healthDot(inst) {
  const health = inst.health;
  if (!health || !health.status || health.status === "unknown") return "";
  const when = health.checked_at ? `, checked ${timeAgo(health.checked_at)}` : "";
  if (health.status === "ok") {
    const count = health.tools;
    const tools = count === null || count === undefined
      ? "" : ` with ${count} tool${count === 1 ? "" : "s"}`;
    // An instance that demands a verified user answers the check with an empty
    // catalog on purpose. Without this line "0 tools" reads as "broken".
    const note = health.note ? ` — ${health.note}` : "";
    return ` <span class="health-dot health-dot-ok" title="${esc(`MCP answered${tools}${when}${note}`)}">●</span>`;
  }
  const repeated = health.failures > 1 ? ` (${health.failures} in a row)` : "";
  const restarts = health.restarts ? ` · restarted ${health.restarts}×` : "";
  const why = health.error || "no reason given";
  return ` <span class="health-dot health-dot-bad" title="${esc(`No MCP answer${repeated}: ${why}${restarts}${when}`)}">●</span>`;
}

function timeAgo(epochSeconds) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
  if (seconds < 90) return `${seconds} s ago`;
  const minutes = Math.round(seconds / 60);
  return minutes < 90 ? `${minutes} min ago` : `${Math.round(minutes / 60)} h ago`;
}

// The instance's own process tree, measured by the system monitor. A dash
// means "not measured" — a stopped instance, or a manager without psutil — and
// never gets confused with a real zero.
function memoryCell(inst) {
  const stats = instanceStat(inst.id);
  if (!stats) return "—";
  const cpu = stats.cpu_percent === null || stats.cpu_percent === undefined
    ? "" : ` · ${Math.round(stats.cpu_percent)} % CPU`;
  const procs = stats.processes > 1 ? `${stats.processes} processes` : "1 process";
  return `<span title="${esc(procs)}${esc(cpu)}">${formatBytes(stats.rss)}</span>`;
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

