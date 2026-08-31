import { API, apiFetch, esc, state } from "./common.js";
import { mountTestCall } from "./testcall.js";
// instances.js imports nothing but common.js, so this is not a cycle.
import { updateFromExample } from "./instances.js";

export function bindInfo() {
  document.getElementById("info-backdrop").addEventListener("click", closeInfo);
  document.getElementById("info-close").addEventListener("click", closeInfo);
}

export async function openInfo(id, inst = null) {
  document.getElementById("info-title").textContent = id;
  document.getElementById("info-meta").innerHTML = metaGrid(id, inst, "");
  document.getElementById("info-fn-count").textContent = "";
  document.getElementById("info-functions").innerHTML = '<p class="modal-hint">Loading…</p>';
  document.getElementById("info-test-bar").classList.add("hidden");
  document.getElementById("info-modal").classList.remove("hidden");

  // Fetched on open, never in the poll cycle — the specs of a single instance
  // are far heavier than a whole list row.
  try {
    const data = await apiFetch(`${API}/${id}/specs`);
    document.getElementById("info-meta").innerHTML =
      metaGrid(id, inst, data.description, data.usage, data.bundled);
    document.getElementById("info-fn-count").textContent = data.specs.length ? `(${data.specs.length})` : "";
    document.getElementById("info-functions").innerHTML = renderFunctions(data.specs);
    // After the list is in the DOM: the test panel hangs itself off the items.
    await mountTestCall(id, data.specs, data.test_call);

    // Close first: the update restarts the instance and reloads the list, so a
    // dialog left open would show the state it had before the click.
    document.getElementById("info-update-btn")?.addEventListener("click", () => {
      closeInfo();
      updateFromExample(id, { ...(inst || {}), bundled_update: data.bundled.version });
    });
  } catch (e) {
    document.getElementById("info-functions").innerHTML =
      `<p class="modal-hint">Could not load functions: ${esc(e.message)}</p>`;
  }
}

function metaGrid(id, inst, description, usage = null, bundled = null) {
  const rows = [
    ["ID", `<code>${esc(id)}</code>`],
    ["Name", inst?.name ? esc(inst.name) : muted()],
    ["Description", description ? esc(description) : muted()],
    ["Category", inst?.category ? `<span class="category-badge">${esc(inst.category)}</span>` : muted()],
    ["Version", versionCell(inst?.version, bundled, !!inst?.locked)],
    ["Venv", `<span class="venv-badge">${esc(inst?.venv || "default")}</span>`],
    ["Status", inst?.status ? `<span class="badge badge-${esc(inst.status)}">${esc(inst.status)}</span>` : muted()],
  ];
  if (usage) rows.push(["Usage", usageText(usage)]);
  return rows.map(([label, value]) =>
    `<div class="info-row"><span class="info-label">${label}</span><span class="info-value">${value}</span></div>`
  ).join("");
}

function muted() { return '<span class="cell-muted">—</span>'; }

// A tool installed from examples/ ages silently. The badge is deliberately the
// same one the header uses for a spawner update, so "outdated" looks identical
// everywhere in the dashboard. It only ever reports: updating means opening the
// file and pasting the code, because a button here would overwrite an instance
// someone may have adapted.
function versionCell(installed, bundled, locked = false) {
  const badge = installed ? `<span class="version-badge">${esc(installed)}</span>` : muted();
  if (!bundled?.update_available) return badge;
  const title = `Version ${esc(bundled.version)} ships with this spawner — you have ${esc(installed || "an unknown version")} installed`;
  const canUpdate = state.editMode !== "readonly" && !locked && !state.guestMode;
  const control = canUpdate
    ? `<button class="update-badge" id="info-update-btn" title="${title}">Update ${esc(bundled.version)}</button>`
    : `<span class="update-badge" title="${title}">Update ${esc(bundled.version)}</span>`;
  const hint = canUpdate
    ? `Shipped in <code>${esc(bundled.path)}</code>. Updating replaces the tool's code — Valve values are kept, the previous version goes to <code>runtime/history</code>.`
    : `Shipped in <code>${esc(bundled.path)}</code>. ${locked ? "Unlock the instance" : "Leave read-only mode"} to apply it here.`;
  return `${badge} ${control}<div class="info-hint">${hint}</div>`;
}

// Counted per tool call in the instance itself, so it also works without the
// shared port. "Never used" is the interesting case: it says the instance can
// be stopped.
function usageText(usage) {
  if (!usage.calls) return '<span class="cell-muted">never used</span>';
  const when = new Date(usage.last_call * 1000);
  const parts = [`${usage.calls} call${usage.calls === 1 ? "" : "s"}`];
  parts.push(`last ${ago(usage.last_call)}`);
  const detail = [when.toLocaleString(), usage.last_tool].filter(Boolean).join(" · ");
  return `${esc(parts.join(", "))} <span class="cell-muted">(${esc(detail)})</span>`;
}

function ago(seconds) {
  const diff = Math.max(0, Date.now() / 1000 - seconds);
  const steps = [[60, "s"], [3600, "min"], [86400, "h"], [Infinity, "d"]];
  const divisors = [1, 60, 3600, 86400];
  for (let i = 0; i < steps.length; i++) {
    if (diff < steps[i][0]) return `${Math.floor(diff / divisors[i])}${steps[i][1]} ago`;
  }
  return "just now";
}

function renderFunctions(specs) {
  if (!specs.length) {
    return '<p class="modal-hint">No function metadata found in the tool file.</p>';
  }
  return `<div class="fn-list">${specs.map(fn => `
    <div class="fn-item" data-fn="${esc(fn.name)}">
      <div class="fn-head">
        <div class="fn-name">${esc(fn.name)}</div>
        <button class="btn btn-secondary btn-sm fn-test-btn" data-test-toggle>Test</button>
      </div>
      ${fn.description ? `<div class="fn-desc">${esc(unwrap(fn.description))}</div>` : ""}
      ${renderParams(fn.parameters)}
      <div class="fn-test hidden" data-test-panel></div>
    </div>
  `).join("")}</div>`;
}

// Descriptions come from Python docstrings, hard-wrapped at ~72 characters.
// Joining those breaks lets the text reflow to the dialog width; blank lines,
// bullets and indented lines are left alone so structure survives.
function unwrap(text) {
  return String(text).replace(/([^\n])\n(?![\n\s*•\-\d])/g, "$1 ");
}

function renderParams(parameters) {
  const props = parameters?.properties;
  if (!props || typeof props !== "object" || !Object.keys(props).length) {
    return '<div class="fn-params fn-params-empty">no parameters</div>';
  }
  const required = Array.isArray(parameters.required) ? parameters.required : [];
  const rows = Object.entries(props).map(([name, schema]) => {
    const s = schema && typeof schema === "object" ? schema : {};
    return `<div class="fn-param">
      <code class="fn-param-name">${esc(name)}</code>
      <span class="fn-param-type">${esc(typeLabel(s))}</span>
      ${required.includes(name) ? '<span class="fn-param-req">required</span>' : ""}
      ${s.description ? `<span class="fn-param-desc">${esc(s.description)}</span>` : ""}
    </div>`;
  });
  return `<div class="fn-params">${rows.join("")}</div>`;
}

// JSON schema is not always a plain {"type": "string"}: enums, anyOf unions and
// array item types all show up in the specs, and "undefined" would be a useless
// label for the reader.
function typeLabel(schema) {
  if (Array.isArray(schema.enum)) return schema.enum.map(String).join(" | ");
  if (Array.isArray(schema.anyOf)) return schema.anyOf.map(typeLabel).join(" | ");
  const type = Array.isArray(schema.type) ? schema.type.join(" | ") : schema.type;
  if (type === "array" && schema.items) return `${type}<${typeLabel(schema.items)}>`;
  return type || "any";
}

function closeInfo() {
  document.getElementById("info-modal").classList.add("hidden");
}
