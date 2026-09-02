import { apiFetch, copyText, downloadBlob, esc, showAlert, state } from "./common.js";

// Two views of the same thing.
//
// On the dashboard, a bar under the filter row showing the endpoint of the
// category that is currently filtered for — contextual, and only then, because
// that is the moment the question "how do I register all of these at once?"
// comes up.
//
// In the settings, under the switch that turns the feature on, the full list:
// every category with its tool count and its own Export. That belongs next to
// the switch rather than on the dashboard — it is set-up, not daily work.
//
// The row itself is the copy button: clicking a category name puts its URL on
// the clipboard, and hovering the row shows the URL in place of the counts, so
// the address is checkable without taking up a column of its own.

let categories = [];
let enabled = false;
let lastKey = "";

function selected() {
  return document.getElementById("filter-category").value;
}

export async function loadCategories() {
  // Guests never see it: the URL is a registration address and the export next
  // to it carries the Bearer token.
  if (state.guestMode) { categories = []; enabled = false; renderBar(); return; }
  try {
    const data = await apiFetch("/api/categories");
    categories = data.categories || [];
    enabled = data.enabled === true;
  } catch {
    // Silently skip, like the instance poll — a failed refresh must not throw
    // an alert onto the screen every four seconds.
  }
  renderBar();
}

function counts(entry) {
  // "not measured" and "none" must not look the same: the tool count comes
  // from the health check, which has not necessarily run yet.
  const tools = entry.tools === null || entry.tools === undefined
    ? "tool count not measured yet"
    : `${entry.tools} tool${entry.tools === 1 ? "" : "s"}`;
  return `${entry.running} of ${entry.total} instance${entry.total === 1 ? "" : "s"} running · ${tools}`;
}

function button(action, name, label, title, extra = "") {
  return `<button class="btn btn-ghost btn-sm" data-cat-action="${action}"
    data-cat="${esc(name)}" title="${esc(title)}" ${extra}>${label}</button>`;
}

function bind(root) {
  root.querySelectorAll("[data-cat-action]").forEach(btn => {
    btn.addEventListener("click", () => act(btn.dataset.catAction, btn.dataset.cat));
  });
}

// ── the dashboard bar ────────────────────────────────────────────────────────

function renderBar() {
  const bar = document.getElementById("category-endpoint");
  const name = selected();
  const entry = enabled && name ? categories.find(c => c.name === name) : null;
  if (!entry) {
    bar.classList.add("hidden");
    bar.innerHTML = "";
    lastKey = "";
    return;
  }

  // Rebuilt only when something actually changed. The poll comes through here
  // every four seconds, and an innerHTML rewrite under a finger that is
  // pressing Export loses the click — the same reason the category dropdown
  // guards its own rebuild.
  const key = JSON.stringify([entry.name, entry.url, entry.running, entry.total, entry.tools]);
  if (key === lastKey) return;
  lastKey = key;

  bar.innerHTML = `
    <div class="cat-head">
      <span class="cat-label">Category endpoint</span>
      <code class="cat-url">${esc(entry.url)}</code>
      ${button("copy", entry.name, "Copy", "Copy the URL")}
      ${button("export", entry.name, "Export", "Download it as an OpenWebUI MCP server entry")}
    </div>
    <div class="cat-note">${esc(counts(entry))} · tools are named
      <code>&lt;instance&gt;.&lt;tool&gt;</code>. A stopped instance is simply absent.</div>`;
  bar.classList.remove("hidden");
  bind(bar);
}

// ── the list in the settings, under the switch ───────────────────────────────

function settingsSwitch() {
  return document.getElementById("settings-category-endpoints");
}

/** Show or hide the list to match the checkbox — no fetch, no save, no wait. */
export function toggleCategorySettingsList() {
  const box = document.getElementById("settings-category-list");
  const box_switch = settingsSwitch();
  if (!box || !box_switch) return;
  box.classList.toggle("hidden", !(box_switch.checked && categories.length));
}

export async function renderCategorySettingsList() {
  const box = document.getElementById("settings-category-list");
  if (!box) return;
  try {
    // Answers with the categories either way and says whether the endpoints
    // are on, so the list can be filled before the switch is saved.
    const data = await apiFetch("/api/categories");
    categories = data.categories || [];
    enabled = data.enabled === true;
  } catch {
    categories = [];
  }
  if (!categories.length) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.innerHTML = categories.map(entry => `
    <div class="cat-row">
      <button class="cat-row-name" data-cat-action="copy" data-cat="${esc(entry.name)}"
        title="Click to copy ${esc(entry.url)}">${esc(entry.name)}</button>
      <span class="cat-row-meta">${esc(counts(entry))}</span>
      <code class="cat-row-url">${esc(entry.url)}</code>
      ${button("export", entry.name, "Export",
               enabled ? `Download the '${entry.name}' category as an OpenWebUI MCP server entry`
                       : "Save the switch above first — this URL does not answer yet",
               enabled ? "" : "disabled")}
    </div>`).join("") + `
    <div class="cat-note cat-unsaved${enabled ? " hidden" : ""}">These endpoints answer
      once the switch above is saved.</div>`;
  bind(box);
  toggleCategorySettingsList();
}

// ── what the buttons do ──────────────────────────────────────────────────────

async function act(action, name) {
  const entry = categories.find(c => c.name === name);
  if (action === "copy") {
    if (!entry) return;
    if (await copyText(entry.url)) {
      showAlert("info", `Copied: ${entry.url}`);
    } else {
      // Both paths refused. Say what would fix it rather than "could not copy":
      // over plain HTTP this is the browser's rule, not a bug in the page.
      showAlert("error", `Could not copy — this page is not on HTTPS or localhost. The URL is ${entry.url}`);
    }
    return;
  }
  try {
    await downloadBlob(`/api/categories/${encodeURIComponent(name)}/export`,
                       `${name}-category-mcp.json`);
  } catch (e) {
    showAlert("error", "Export failed: " + e.message);
  }
}

export function bindCategories() {
  document.getElementById("filter-category").addEventListener("change", renderBar);
  // The list follows the checkbox immediately, not the save: ticking it is the
  // moment somebody wants to see what they are about to switch on.
  const box_switch = settingsSwitch();
  if (box_switch) box_switch.addEventListener("change", toggleCategorySettingsList);
}
