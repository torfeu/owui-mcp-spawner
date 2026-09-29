import { API, apiFetch, esc, showAlert, state } from "./common.js";
import { loadInstances, updateFromExample } from "./instances.js";

// The tools in examples/, one card each. Installing goes through the same
// pipeline as an upload; updating an installed copy uses the dashboard's own
// update-from-example path, question included — nothing here is a second way.

export function bindStore() {
  document.getElementById("store-btn").addEventListener("click", openStore);
  document.getElementById("store-close").addEventListener("click", closeStore);
  document.getElementById("store-backdrop").addEventListener("click", closeStore);
  document.getElementById("store-list").addEventListener("click", onAction);
}

function closeStore() {
  document.getElementById("store-modal").classList.add("hidden");
}

async function openStore() {
  document.getElementById("store-modal").classList.remove("hidden");
  await renderStore();
}

async function renderStore() {
  const list = document.getElementById("store-list");
  list.innerHTML = '<div class="settings-status">Loading…</div>';
  let entries;
  try {
    entries = await apiFetch("/api/store");
  } catch (e) {
    list.innerHTML = `<div class="settings-status settings-status-warn">${esc(e.message)}</div>`;
    return;
  }
  if (!entries.length) {
    list.innerHTML = '<div class="settings-status">Nothing ships in examples/ on this server.</div>';
    return;
  }
  list.innerHTML = entries.map(card).join("");
}

function card(entry) {
  const canWrite = state.editMode !== "readonly";
  let action;
  if (!entry.installed) {
    action = canWrite
      ? `<button class="btn btn-primary btn-sm" data-install="${esc(entry.id)}">Install</button>`
      : "";
  } else if (entry.update_available) {
    action = canWrite
      ? `<button class="btn btn-warning btn-sm" data-update="${esc(entry.id)}"
           data-from="${esc(entry.installed_version)}" data-to="${esc(entry.version)}">Update to ${esc(entry.version)}</button>`
      : `<span class="store-state">update ${esc(entry.version)} available</span>`;
  } else {
    action = `<span class="store-state">installed${entry.installed_version ? " · " + esc(entry.installed_version) : ""}</span>`;
  }
  // Installed already — a further copy under an id of its own. One bridge per
  // external MCP server is the reason this exists.
  if (entry.installed && canWrite) {
    action += ` <button class="btn btn-ghost btn-sm" data-copy="${esc(entry.id)}">Add another</button>`;
  }
  return `
    <div class="store-card">
      <div class="store-head">
        <span class="store-name">${esc(entry.name)}</span>
        ${entry.version ? `<span class="version-badge">${esc(entry.version)}</span>` : ""}
        ${entry.category ? `<span class="category-badge">${esc(entry.category)}</span>` : ""}
        <span class="store-action">${action}</span>
      </div>
      <div class="store-id">${esc(entry.id)}</div>
      ${entry.description ? `<div class="store-desc">${esc(entry.description)}</div>` : ""}
    </div>`;
}

/** A free id for the next copy: foo_2, foo_3, … — only a suggestion, the
 * server refuses a taken one anyway. Asked of the API, not read off the
 * table: a filter or the pagination hides rows, and a hidden id is taken too. */
function suggestId(base, rows) {
  const taken = new Set(rows.map(r => r.id));
  for (let n = 2; ; n++) {
    if (!taken.has(`${base}_${n}`)) return `${base}_${n}`;
  }
}

/** The instances that came from this shipped tool, as far as the ids tell:
 * the tool's own id and the copies named <id>_2, <id>_3, … the store suggests.
 * A copy renamed to something else is not recognised — the question below is
 * a reminder, not an inventory. */
function existingCopies(base, rows) {
  const copy = new RegExp(`^${base.replace(/[-]/g, "\\-")}_\\d+$`);
  return rows.filter(r => r.id === base || copy.test(r.id)).map(r => r.id);
}

async function onAction(event) {
  const install = event.target.closest("[data-install]");
  const copy = event.target.closest("[data-copy]");
  const update = event.target.closest("[data-update]");
  if (install || copy) {
    const button = install || copy;
    const shipped = install ? install.dataset.install : copy.dataset.copy;
    const body = {};
    if (copy) {
      // Asked first, before the id: a second instance of the same tool is a
      // deliberate choice, and "Add another" sits right next to the status of
      // the one that already exists.
      const rows = await apiFetch(API).catch(() => []);
      const existing = existingCopies(shipped, rows);
      const list = existing.length ? existing.join(", ") : shipped;
      if (!confirm(
        `${shipped} is already installed (${list}).\n\n` +
        `Really install another instance of it?`
      )) return;
      const id = prompt(
        `ID for the new instance of ${shipped} — letters, digits, _ and - only.\n` +
        `Give it a note afterwards (Edit → Note) so the copies can be told apart.`,
        suggestId(shipped, rows));
      if (id === null) return;
      if (!/^[A-Za-z0-9_-]+$/.test(id.trim())) {
        showAlert("error", `"${id}" is not a usable ID — letters, digits, _ and - only`);
        return;
      }
      body.id = id.trim();
    }
    button.disabled = true;
    button.textContent = "Installing…";
    try {
      const res = await apiFetch(`/api/store/${encodeURIComponent(shipped)}/install`, {
        method: "POST", body: JSON.stringify(body),
      });
      showAlert("success", `Installed: ${res.id} on port ${res.port} — start it from the dashboard.`);
      (res.warnings || []).forEach(w => showAlert("info", w));
      loadInstances();
    } catch (e) {
      showAlert("error", `Install failed: ${e.message}`);
    }
    await renderStore();
  } else if (update) {
    const { update: id, from, to } = update.dataset;
    await updateFromExample(id, { version: from, bundled_update: to });
    await renderStore();
  }
}
