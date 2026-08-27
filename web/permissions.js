import { API, apiFetch, esc, showAlert } from "./common.js";

// The whole policy, edited in memory and written in one go. Small by nature —
// a household, not a directory — and one save is easier to reason about than
// a request per checkbox: what you see is what lands in the file.
let policy = { users: {}, roles: {} };
let identities = [];
let instances = [];
let selected = null;        // {kind: "user"|"role", key: string}
const specsCache = new Map();

export function bindPermissions() {
  document.getElementById("perm-btn").addEventListener("click", openPermissions);
  document.getElementById("perm-close").addEventListener("click", closePermissions);
  document.getElementById("perm-backdrop").addEventListener("click", closePermissions);
  document.getElementById("perm-save").addEventListener("click", savePolicy);
  document.getElementById("perm-add").addEventListener("click", addUserByHand);
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
    const [policyData, roster, instanceList] = await Promise.all([
      apiFetch("/api/policy"),
      apiFetch("/api/identities"),
      apiFetch(API),
    ]);
    policy = policyData.policy && Object.keys(policyData.policy).length
      ? policyData.policy : { users: {}, roles: {} };
    policy.users = policy.users || {};
    policy.roles = policy.roles || {};
    identities = roster.identities || [];
    // Only instances that actually enforce anything are worth offering — a
    // checkbox that governs nothing is a promise the framework does not keep.
    instances = (instanceList || []).filter(i => i.identity_mode === "required");

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
    if (row.never_seen) marks.push('<span class="perm-warn" title="never called — check for a typo">?</span>');
    return `<li class="perm-item${active}" data-kind="user" data-key="${esc(row.sub)}">
      <span class="perm-name">${esc(label)}</span>${marks.join("")}
      <span class="perm-sub">${esc(row.sub.slice(0, 8))}…</span></li>`;
  }).join("") || '<li class="modal-hint">Nobody has called yet.</li>';

  const roles = new Set([...Object.keys(policy.roles), ...identities.map(r => r.role).filter(Boolean)]);
  document.getElementById("perm-roles").innerHTML = [...roles].map(role => {
    const active = selected?.kind === "role" && selected.key === role ? " perm-active" : "";
    const dot = policy.roles[role] ? '<span class="perm-dot" title="has rules">●</span>' : "";
    return `<li class="perm-item${active}" data-kind="role" data-key="${esc(role)}">
      <span class="perm-name">${esc(role)}</span>${dot}</li>`;
  }).join("") || '<li class="modal-hint">No roles seen.</li>';

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
    const toolBoxes = tools.map(tool => `
      <label class="perm-tool"><input type="checkbox" data-tool="${esc(inst.id)}|${esc(tool)}"
        ${chosen.has(tool) ? "checked" : ""} ${mode === "some" ? "" : "disabled"}/> ${esc(tool)}</label>`).join("");
    return `<div class="perm-instance">
      <div class="perm-instance-head">
        <strong>${esc(inst.name || inst.id)}</strong>
        <select data-mode="${esc(inst.id)}">
          <option value="none"${mode === "none" ? " selected" : ""}>no access</option>
          <option value="all"${mode === "all" ? " selected" : ""}>all tools</option>
          <option value="some"${mode === "some" ? " selected" : ""}>selected tools</option>
        </select>
      </div>
      <div class="perm-tools${mode === "some" ? "" : " perm-dim"}">${toolBoxes || '<span class="modal-hint">no tools</span>'}</div>
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
    });
  });
  for (const [id, field] of [["perm-account", "account"], ["perm-credentials", "credentials_file"]]) {
    detail.querySelector(`#${id}`)?.addEventListener("input", event => {
      const target = entryFor(true);
      if (event.target.value.trim()) target[field] = event.target.value.trim();
      else delete target[field];
    });
  }
  detail.querySelector("#perm-remove")?.addEventListener("click", () => {
    if (!confirm(`Remove all rules for this user?\nThey will then reach nothing.`)) return;
    delete policy.users[selected.key];
    selected = null;
    document.getElementById("perm-detail").innerHTML =
      '<p class="modal-hint">Pick someone on the left.</p>';
    renderPeople();
  });
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
