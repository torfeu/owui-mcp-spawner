import { API, apiFetch, esc, showAlert } from "./common.js";

// The whole policy, edited in memory and written in one go. Small by nature —
// a household, not a directory — and one save is easier to reason about than
// a request per checkbox: what you see is what lands in the file.
let policy = { users: {}, roles: {} };
let identities = [];
let instances = [];
let selected = null;        // {kind: "user"|"role", key: string}
const specsCache = new Map();
const expanded = new Set();  // instance ids whose tool list is unfolded

// Above this many governed instances the blocks start folded: a hundred
// headings with a status each is readable, a hundred open tool lists is not.
const FOLD_ABOVE = 8;

export function bindPermissions() {
  document.getElementById("perm-btn").addEventListener("click", openPermissions);
  document.getElementById("perm-close").addEventListener("click", closePermissions);
  document.getElementById("perm-backdrop").addEventListener("click", closePermissions);
  document.getElementById("perm-save").addEventListener("click", savePolicy);
  document.getElementById("perm-add").addEventListener("click", addUserByHand);
  document.getElementById("perm-people-filter")
    .addEventListener("input", event => applyPeopleFilter(event.target.value));
}

function closePermissions() {
  document.getElementById("perm-modal").classList.add("hidden");
}

async function openPermissions() {
  document.getElementById("perm-modal").classList.remove("hidden");
  selected = null;
  document.getElementById("perm-detail").innerHTML =
    '<p class="modal-hint">Pick someone on the left.</p>';
  try {
    // ?include=specs carries every function list in this one response. The
    // alternative is one request per instance per selected person, which is
    // what this dialog used to do — fine for five instances, not for a hundred.
    const [policyData, roster, instanceList] = await Promise.all([
      apiFetch("/api/policy"),
      apiFetch("/api/identities"),
      apiFetch(`${API}?include=specs`),
    ]);
    policy = policyData.policy && Object.keys(policyData.policy).length
      ? policyData.policy : { users: {}, roles: {} };
    policy.users = policy.users || {};
    policy.roles = policy.roles || {};
    identities = roster.identities || [];
    // Only instances that actually enforce anything are worth offering — a
    // checkbox that governs nothing is a promise the framework does not keep.
    instances = (instanceList || []).filter(i => i.identity_mode === "required");
    specsCache.clear();
    expanded.clear();
    for (const inst of instances) {
      if (Array.isArray(inst.specs)) specsCache.set(inst.id, inst.specs.map(s => s.name));
      if (instances.length <= FOLD_ABOVE) expanded.add(inst.id);
    }
    document.getElementById("perm-people-filter").value = "";
    applyPeopleFilter("");

    document.getElementById("perm-match-email").checked = !!policy.match_email;
    document.getElementById("perm-match-name").checked = !!policy.match_name;
    renderStatus(roster, policyData, instanceList || []);
    renderPeople();
  } catch (err) {
    showAlert("error", "Could not load permissions: " + err.message);
  }
}

function renderStatus(roster, policyData, allInstances) {
  const status = document.getElementById("perm-status");
  const governed = instances.length;
  if (policyData.error) {
    status.textContent = "Policy file unreadable — everything is denied";
    status.className = "settings-status settings-status-warn";
  } else if (!roster.identity_configured) {
    status.textContent = "No identity configured — rules cannot apply yet";
    status.className = "settings-status settings-status-warn";
  } else if (!governed) {
    status.textContent = `No instance is on "required" — nothing is governed yet`;
    status.className = "settings-status settings-status-warn";
  } else {
    status.textContent = `${governed} of ${allInstances.length} instances governed`;
    status.className = "settings-status settings-status-ok";
  }
}

function renderPeople() {
  const list = document.getElementById("perm-list");
  const known = new Map(identities.map(row => [row.sub, row]));
  for (const sub of Object.keys(policy.users)) {
    if (!known.has(sub)) known.set(sub, { sub, name: "", email: "", never_seen: true });
  }
  list.innerHTML = [...known.values()].map(row => {
    const label = row.name || row.email || row.sub;
    const active = selected?.kind === "user" && selected.key === row.sub ? " perm-active" : "";
    const marks = [];
    if (policy.users[row.sub]) marks.push('<span class="perm-dot" title="has rules">●</span>');
    if (row.agent) marks.push('<span class="perm-agent" title="agent identity — a token, not a person">⚙</span>');
    // "Never called" reads as a typo for a person, but it is the normal state
    // of an agent whose token was issued a minute ago — and giving it its
    // rules now is exactly the point.
    if (row.never_seen && !row.agent) {
      marks.push('<span class="perm-warn" title="never called — check for a typo">?</span>');
    }
    // A user id is an unreadable UUID and gets cut; an agent id was typed by
    // hand and is the thing you recognise it by, so it stays whole.
    const idText = row.sub.length <= 20 ? esc(row.sub) : `${esc(row.sub.slice(0, 8))}…`;
    return `<li class="perm-item${active}" data-kind="user" data-key="${esc(row.sub)}">
      <span class="perm-name">${esc(label)}</span>${marks.join("")}
      <span class="perm-sub">${idText}</span></li>`;
  }).join("") || '<li class="modal-hint">Nobody has called yet.</li>';

  const roles = new Set([...Object.keys(policy.roles), ...identities.map(r => r.role).filter(Boolean)]);
  document.getElementById("perm-roles").innerHTML = [...roles].map(role => {
    const active = selected?.kind === "role" && selected.key === role ? " perm-active" : "";
    const dot = policy.roles[role] ? '<span class="perm-dot" title="has rules">●</span>' : "";
    return `<li class="perm-item${active}" data-kind="role" data-key="${esc(role)}">
      <span class="perm-name">${esc(role)}</span>${dot}</li>`;
  }).join("") || '<li class="modal-hint">No roles seen.</li>';

  // Both lists were just rebuilt, so the filter has to be laid over them again.
  applyPeopleFilter(document.getElementById("perm-people-filter").value);

  for (const item of document.querySelectorAll(".perm-item")) {
    item.addEventListener("click", () => {
      selected = { kind: item.dataset.kind, key: item.dataset.key };
      renderPeople();
      renderDetail();
    });
  }
}

function entryFor(create = false) {
  if (!selected) return null;
  const bucket = selected.kind === "user" ? policy.users : policy.roles;
  if (!bucket[selected.key] && create) bucket[selected.key] = {};
  return bucket[selected.key] || null;
}

async function specsFor(instanceId) {
  if (!specsCache.has(instanceId)) {
    try {
      const data = await apiFetch(`${API}/${instanceId}/specs`);
      specsCache.set(instanceId, (data.specs || []).map(s => s.name));
    } catch {
      specsCache.set(instanceId, []);
    }
  }
  return specsCache.get(instanceId);
}

async function renderDetail() {
  const detail = document.getElementById("perm-detail");
  if (!selected) return;
  const entry = entryFor() || {};
  const person = identities.find(row => row.sub === selected.key);
  const isUser = selected.kind === "user";

  const head = isUser
    ? `<h3>${esc(person?.name || selected.key)}</h3>
       <div class="perm-meta">${esc(person?.email || "")} ${person?.role ? `· role ${esc(person.role)}` : ""}
       ${person?.source ? `· ${esc(person.source)}` : ""}</div>
       <div class="perm-meta"><code>${esc(selected.key)}</code></div>`
    : `<h3>Role: ${esc(selected.key)}</h3>
       <div class="perm-meta">Base equipment for everyone with this role. A personal entry adds to it.</div>`;

  const rows = await Promise.all(instances.map(async inst => {
    const current = (entry.instances || {})[inst.id];
    const mode = current === "*" ? "all" : Array.isArray(current) ? "some" : "none";
    const tools = await specsFor(inst.id);
    const chosen = new Set(Array.isArray(current) ? current : []);
    const open = expanded.has(inst.id);
    const toolBoxes = tools.map(tool => `
      <label class="perm-tool"><input type="checkbox" data-tool="${esc(inst.id)}|${esc(tool)}"
        ${chosen.has(tool) ? "checked" : ""} ${mode === "some" ? "" : "disabled"}/> ${esc(tool)}</label>`).join("");
    return `<div class="perm-instance" data-instance="${esc(inst.id)}">
      <div class="perm-instance-head">
        <button type="button" class="perm-fold" data-fold="${esc(inst.id)}"
                aria-expanded="${open}" title="Show or hide the tools">${open ? "▾" : "▸"}</button>
        <strong>${esc(inst.name || inst.id)}</strong>
        <span class="perm-count" data-count="${esc(inst.id)}">${statusText(mode, chosen.size, tools.length)}</span>
        <select data-mode="${esc(inst.id)}">
          <option value="none"${mode === "none" ? " selected" : ""}>no access</option>
          <option value="all"${mode === "all" ? " selected" : ""}>all tools</option>
          <option value="some"${mode === "some" ? " selected" : ""}>selected tools</option>
        </select>
      </div>
      <div class="perm-tools${mode === "some" ? "" : " perm-dim"}${open ? "" : " hidden"}">${toolBoxes || '<span class="modal-hint">no tools</span>'}</div>
    </div>`;
  }));

  detail.innerHTML = `${head}
    <div class="form-grid perm-account">
      <label title="Opaque to the spawner: it reads the file and hands the value to the tool.">Account</label>
      <input type="text" id="perm-account" value="${esc(entry.account || "")}" placeholder="none" />
      <label>Credentials file</label>
      <input type="text" id="perm-credentials" value="${esc(entry.credentials_file || "")}"
             placeholder="secrets/accounts/&lt;name&gt;" />
    </div>
    <p class="modal-hint">The file holds the secret — one per account, <code>chmod 600</code>. Its last non-empty line is used, so an old value may stay above the new one.</p>
    ${rows.length > 1 ? `<div class="perm-filter">
      <input type="search" id="perm-tool-filter" class="perm-search" placeholder="Filter tools across all instances…" />
      <span class="perm-filter-count" id="perm-tool-count"></span>
    </div>` : ""}
    ${rows.join("") || '<p class="modal-hint">No instance is set to <code>required</code>, so there is nothing to grant yet.</p>'}
    ${isUser ? `<div class="modal-actions"><button class="btn btn-danger" id="perm-remove">Remove all rules</button></div>` : ""}`;

  detail.querySelectorAll("[data-mode]").forEach(select => {
    select.addEventListener("change", () => {
      const target = entryFor(true);
      target.instances = target.instances || {};
      const id = select.dataset.mode;
      if (select.value === "none") delete target.instances[id];
      else if (select.value === "all") target.instances[id] = "*";
      else target.instances[id] = Array.isArray(target.instances[id]) ? target.instances[id] : [];
      renderDetail();
      renderPeople();
    });
  });
  detail.querySelectorAll("[data-tool]").forEach(box => {
    box.addEventListener("change", () => {
      const [id, tool] = box.dataset.tool.split("|");
      const target = entryFor(true);
      target.instances = target.instances || {};
      const list = new Set(Array.isArray(target.instances[id]) ? target.instances[id] : []);
      box.checked ? list.add(tool) : list.delete(tool);
      target.instances[id] = [...list];
      // The head keeps the score, so a folded block still says what it grants.
      const counter = detail.querySelector(`[data-count="${CSS.escape(id)}"]`);
      if (counter) counter.textContent = statusText("some", list.size, specsCache.get(id)?.length ?? 0);
    });
  });
  detail.querySelectorAll("[data-fold]").forEach(button => {
    button.addEventListener("click", () => {
      const id = button.dataset.fold;
      expanded.has(id) ? expanded.delete(id) : expanded.add(id);
      applyToolFilter(document.getElementById("perm-tool-filter")?.value || "");
    });
  });
  document.getElementById("perm-tool-filter")
    ?.addEventListener("input", event => applyToolFilter(event.target.value));
  for (const [id, field] of [["perm-account", "account"], ["perm-credentials", "credentials_file"]]) {
    detail.querySelector(`#${id}`)?.addEventListener("input", event => {
      const target = entryFor(true);
      if (event.target.value.trim()) target[field] = event.target.value.trim();
      else delete target[field];
    });
  }
  detail.querySelector("#perm-remove")?.addEventListener("click", () => {
    if (!confirm("Remove all rules for this user?\n\nThey fall back to whatever "
        + "their role and the default grant — which may be more than nothing. To "
        + "stop somebody, use Remove under Settings \u2192 Identity, which writes a "
        + "block instead.")) return;
    delete policy.users[selected.key];
    selected = null;
    document.getElementById("perm-detail").innerHTML =
      '<p class="modal-hint">Pick someone on the left.</p>';
    renderPeople();
  });
}

function statusText(mode, chosen, total) {
  if (mode === "all") return "all tools";
  if (mode === "some") return `${chosen} of ${total}`;
  return "no access";
}

// Filtering and folding are pure show/hide over the rendered blocks: no
// re-render, so nothing anyone has ticked can get lost on the way.
function applyToolFilter(text) {
  const needle = text.trim().toLowerCase();
  const detail = document.getElementById("perm-detail");
  let shown = 0;
  let total = 0;

  for (const block of detail.querySelectorAll(".perm-instance")) {
    let hits = 0;
    for (const label of block.querySelectorAll(".perm-tool")) {
      total += 1;
      const match = !needle || label.textContent.toLowerCase().includes(needle);
      label.classList.toggle("hidden", !match);
      if (match) hits += 1;
    }
    shown += hits;

    // While a filter is active a hit decides: a match hidden behind a fold is
    // as good as no match, and an instance without one is out of the way.
    const open = needle ? hits > 0 : expanded.has(block.dataset.instance);
    block.classList.toggle("hidden", Boolean(needle) && hits === 0);
    block.querySelector(".perm-tools")?.classList.toggle("hidden", !open);
    const fold = block.querySelector("[data-fold]");
    if (fold) {
      fold.textContent = open ? "▾" : "▸";
      fold.setAttribute("aria-expanded", String(open));
    }
  }

  const counter = document.getElementById("perm-tool-count");
  if (counter) counter.textContent = needle ? `${shown} of ${total}` : "";
}

function applyPeopleFilter(text) {
  const needle = text.trim().toLowerCase();
  for (const list of ["perm-list", "perm-roles"]) {
    for (const item of document.getElementById(list).querySelectorAll("li")) {
      item.classList.toggle("hidden", Boolean(needle) && !item.textContent.toLowerCase().includes(needle));
    }
  }
}

function addUserByHand() {
  // A rule may exist before its first call — otherwise the first visit of a
  // new colleague is always a refusal.
  const sub = prompt("OpenWebUI user id (the 'sub' from whoami):");
  if (!sub || !sub.trim()) return;
  policy.users[sub.trim()] = policy.users[sub.trim()] || {};
  selected = { kind: "user", key: sub.trim() };
  renderPeople();
  renderDetail();
}

async function savePolicy() {
  policy.match_email = document.getElementById("perm-match-email").checked;
  policy.match_name = document.getElementById("perm-match-name").checked;
  if (!policy.default) policy.default = { deny: true };
  try {
    await apiFetch("/api/policy", { method: "PUT", body: JSON.stringify({ policy }) });
    showAlert("success", "Permissions saved — they apply to the next call, no restart needed.");
  } catch (err) {
    showAlert("error", "Save failed: " + err.message);
  }
}
