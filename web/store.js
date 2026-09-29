import { apiFetch, esc, showAlert, state } from "./common.js";
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

async function onAction(event) {
  const install = event.target.closest("[data-install]");
  const update = event.target.closest("[data-update]");
  if (install) {
    const id = install.dataset.install;
    install.disabled = true;
    install.textContent = "Installing…";
    try {
      const res = await apiFetch(`/api/store/${encodeURIComponent(id)}/install`, {
        method: "POST", body: JSON.stringify({}),
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
