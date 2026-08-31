/**
 * Call a tool from the dialog and look at what comes back.
 *
 * The question this answers is the one a chat window cannot: was it the tool
 * that failed, or the small local model that called a working tool wrongly?
 * Filling the parameters in by hand settles it — and shows the length of the
 * answer, which is the recurring culprit with that model.
 *
 * The form is generated from the tool's own schema rather than being a JSON
 * text box, because typing JSON by hand is how the wrong call gets made in the
 * first place. Scalars — string, number, boolean, enum — get a real field.
 * Anything nested gets a JSON field *in the same form*, so no tool schema can
 * ever leave the panel blocked, however exotic it is.
 */
import { apiFetch, esc, state } from "./common.js";

// Long enough for a tool that generates a document or asks a remote API. The
// server's own budget is the same number; the counter here exists so a slow
// call looks like a slow call rather than a hung dialog.
const CALL_TIMEOUT_S = 60;

let identities = [];
let current = { instance: "", meta: null };

/** Populate the panel for one instance. Call after the function list is in the DOM. */
export async function mountTestCall(instanceId, specs, meta) {
  const bar = document.getElementById("info-test-bar");
  current = { instance: instanceId, meta: meta || {} };
  // Guests never see it: the route behind it is password-only, so a button
  // that can only ever answer 401 is worse than no button.
  if (state.guestMode || !specs.length) {
    bar.classList.add("hidden");
    dropButtons();
    return;
  }

  bar.classList.remove("hidden");
  bar.innerHTML = renderBar(current.meta);
  // A locked instance keeps the explanation but loses the buttons: a call that
  // can only ever come back 403 should not be on offer.
  if (current.meta.locked) {
    dropButtons();
    return;
  }
  document.querySelectorAll("#info-functions .fn-item[data-fn]").forEach(item => {
    const name = item.dataset.fn;
    const spec = specs.find(fn => fn.name === name);
    if (!spec) return;
    item.querySelector("[data-test-toggle]")
      ?.addEventListener("click", () => toggleForm(item, spec));
  });

  if (current.meta.can_identify) await loadIdentities();
}

/** Take the Test buttons away where a call cannot be made. */
function dropButtons() {
  document.querySelectorAll("#info-functions [data-test-toggle]")
    .forEach(button => button.remove());
}

function renderBar(meta) {
  if (meta.locked) {
    return `<div class="test-note">This instance is locked — unlock it in the dashboard to make test calls.
      A call runs its real code with its real credentials, which is what the lock is there to prevent.</div>`;
  }
  const mode = meta.identity_mode || "off";
  const select = `<label class="test-as">Call as
      <select id="test-as" disabled><option value="">nobody (like the health check)</option></select>
    </label>`;
  let note = "";
  if (!meta.can_identify) {
    // Says it once, here, instead of once per failed call: without a secret
    // and without trusted headers there is no way to be anybody.
    note = `<div class="test-note">No user-JWT secret and no trusted user headers on this server — a test call can only be made as nobody.</div>`;
  } else if (mode === "required") {
    note = `<div class="test-note">This instance requires an identified user. As nobody it answers with an empty catalog — pick someone to see what they actually get.</div>`;
  }
  return `${meta.can_identify ? select : ""}${note}`;
}

async function loadIdentities() {
  const select = document.getElementById("test-as");
  if (!select) return;
  try {
    if (!identities.length) identities = (await apiFetch("/api/identities")).identities || [];
  } catch {
    return;  // The dropdown stays at "nobody"; the call itself still works.
  }
  select.innerHTML = '<option value="">nobody (like the health check)</option>'
    + identities.map(who => {
      const label = [who.name || who.email || who.sub, who.role ? `(${who.role})` : "",
                     who.agent ? "· agent" : ""].filter(Boolean).join(" ");
      return `<option value="${esc(who.sub)}">${esc(label)}</option>`;
    }).join("");
  select.disabled = false;
}

function toggleForm(item, spec) {
  const panel = item.querySelector("[data-test-panel]");
  if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  if (panel.dataset.built) return;
  panel.dataset.built = "1";
  panel.innerHTML = renderForm(spec);
  panel.querySelector("[data-test-run]").addEventListener("click", () => run(panel, spec));
}

// ── the form ─────────────────────────────────────────────────────────────────

/** How a parameter is asked for. "json" is the fallback that keeps this unblockable. */
function kindOf(schema) {
  if (!schema || typeof schema !== "object") return "json";
  if (Array.isArray(schema.enum)) return "enum";
  const type = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  if (type === "boolean") return "bool";
  if (type === "integer" || type === "number") return "number";
  if (type === "string") return "string";
  return "json";
}

function typeLabel(schema) {
  if (Array.isArray(schema?.enum)) return schema.enum.map(String).join(" | ");
  if (Array.isArray(schema?.anyOf)) return schema.anyOf.map(typeLabel).join(" | ");
  const type = Array.isArray(schema?.type) ? schema.type.join(" | ") : schema?.type;
  if (type === "array" && schema.items) return `${type}<${typeLabel(schema.items)}>`;
  return type || "any";
}

function renderForm(spec) {
  const props = spec.parameters?.properties;
  const required = Array.isArray(spec.parameters?.required) ? spec.parameters.required : [];
  const entries = props && typeof props === "object" ? Object.entries(props) : [];
  const fields = entries.map(([name, raw]) => {
    const schema = raw && typeof raw === "object" ? raw : {};
    const kind = kindOf(schema);
    const id = `test-arg-${spec.name}-${name}`;
    return `<div class="test-field">
      <label for="${esc(id)}">
        <code>${esc(name)}</code>
        <span class="fn-param-type">${esc(typeLabel(schema))}</span>
        ${required.includes(name) ? '<span class="fn-param-req">required</span>' : ""}
      </label>
      ${inputFor(id, name, schema, kind, required.includes(name))}
      ${schema.description ? `<div class="test-field-desc">${esc(schema.description)}</div>` : ""}
    </div>`;
  }).join("");

  return `<div class="test-form">
    ${fields || '<div class="test-note">No parameters — the call goes out as it is.</div>'}
    <div class="test-actions">
      <button class="btn btn-primary btn-sm" data-test-run>Call</button>
      <span class="test-status" data-test-status></span>
    </div>
    <div class="test-result hidden" data-test-result></div>
  </div>`;
}

function inputFor(id, name, schema, kind, required) {
  const common = `id="${esc(id)}" data-arg="${esc(name)}" data-kind="${kind}"`;
  const preset = schema.default;
  if (kind === "enum") {
    // An empty first entry unless the value is required: "leave it out" has to
    // stay expressible, or every optional enum would silently be sent.
    const options = (required ? [] : [`<option value="">— not sent —</option>`])
      .concat(schema.enum.map(value =>
        `<option value="${esc(value)}"${value === preset ? " selected" : ""}>${esc(value)}</option>`));
    return `<select ${common}>${options.join("")}</select>`;
  }
  if (kind === "bool") {
    const pick = preset === true ? "true" : preset === false ? "false" : "";
    return `<select ${common}>
      ${required ? "" : `<option value=""${pick === "" ? " selected" : ""}>— not sent —</option>`}
      <option value="true"${pick === "true" ? " selected" : ""}>true</option>
      <option value="false"${pick === "false" ? " selected" : ""}>false</option>
    </select>`;
  }
  if (kind === "number") {
    const step = (Array.isArray(schema.type) ? schema.type[0] : schema.type) === "integer" ? "1" : "any";
    return `<input type="number" step="${step}" ${common} value="${preset === undefined ? "" : esc(preset)}" />`;
  }
  if (kind === "string") {
    return `<input type="text" ${common} value="${preset === undefined ? "" : esc(preset)}" />`;
  }
  // The fallback. Nested objects and arrays are typed as JSON rather than not
  // being offered at all — a dialog that cannot call half the tools would be
  // worse than one that asks for a little syntax.
  const value = preset === undefined ? "" : JSON.stringify(preset);
  return `<textarea rows="3" class="test-json" ${common}
    placeholder="JSON, e.g. ${esc(placeholderFor(schema))}">${esc(value)}</textarea>`;
}

function placeholderFor(schema) {
  const type = Array.isArray(schema?.type) ? schema.type[0] : schema?.type;
  if (type === "array") return "[1, 2, 3]";
  if (type === "object") return '{"key": "value"}';
  return '"text", 42, true, null, [] or {}';
}

/** Read the form. Returns {arguments} or {error} — never a half-filled call. */
function collect(panel, spec) {
  const required = Array.isArray(spec.parameters?.required) ? spec.parameters.required : [];
  const args = {};
  for (const field of panel.querySelectorAll("[data-arg]")) {
    const name = field.dataset.arg;
    const raw = field.value.trim();
    if (!raw) {
      if (required.includes(name)) return { error: `${name} is required` };
      continue;   // Left blank on purpose: the parameter is simply not sent.
    }
    if (field.dataset.kind === "number") {
      const value = Number(raw);
      if (!Number.isFinite(value)) return { error: `${name}: ${raw} is not a number` };
      args[name] = value;
    } else if (field.dataset.kind === "bool") {
      args[name] = raw === "true";
    } else if (field.dataset.kind === "json") {
      try {
        args[name] = JSON.parse(raw);
      } catch (e) {
        return { error: `${name}: ${e.message}` };
      }
    } else {
      args[name] = raw;
    }
  }
  return { arguments: args };
}

// ── the call ─────────────────────────────────────────────────────────────────

async function run(panel, spec) {
  const button = panel.querySelector("[data-test-run]");
  const status = panel.querySelector("[data-test-status]");
  const output = panel.querySelector("[data-test-result]");

  const collected = collect(panel, spec);
  if (collected.error) {
    status.className = "test-status test-status-error";
    status.textContent = collected.error;
    return;
  }

  const asUser = document.getElementById("test-as")?.value || "";
  button.disabled = true;
  output.classList.add("hidden");
  status.className = "test-status";
  // A tool call may legitimately take most of a minute. Without the counter a
  // slow tool and a wedged one look exactly alike from here.
  let seconds = 0;
  status.textContent = `Calling… (up to ${CALL_TIMEOUT_S} s)`;
  const timer = setInterval(() => {
    seconds += 1;
    status.textContent = `Calling… ${seconds} s (up to ${CALL_TIMEOUT_S} s)`;
  }, 1000);

  try {
    const result = await apiFetch(`/api/instances/${encodeURIComponent(current.instance)}/call`, {
      method: "POST",
      body: JSON.stringify({ tool: spec.name, arguments: collected.arguments, as_user: asUser }),
    });
    status.textContent = "";
    output.classList.remove("hidden");
    output.innerHTML = renderResult(result, asUser);
  } catch (e) {
    status.className = "test-status test-status-error";
    status.textContent = e.message;
  } finally {
    clearInterval(timer);
    button.disabled = false;
  }
}

function renderResult(result, asUser) {
  // Three outcomes, and keeping them apart is the whole point: the call never
  // arrived, the tool answered with an error, or it worked.
  const verdict = !result.ok
    ? ["error", "Call failed", result.error]
    : result.is_error
      ? ["warn", "The tool reported an error", ""]
      : ["ok", "Answered", ""];

  const facts = [`${result.duration_ms} ms`];
  if (result.ok) {
    facts.push(`${result.chars} character${result.chars === 1 ? "" : "s"}`);
    if (result.truncated) facts.push("shown shortened");
  }
  if (asUser) facts.push(`as ${asUser}`);

  const body = result.ok
    ? `<pre class="test-output">${esc(result.text || "(empty answer)")}</pre>`
    : `<pre class="test-output test-output-error">${esc(result.error)}</pre>`;
  const structured = result.structured
    ? `<div class="test-sub">Structured result</div>
       <pre class="test-output">${esc(JSON.stringify(result.structured, null, 2))}</pre>`
    : "";

  return `<div class="test-verdict test-verdict-${verdict[0]}">${esc(verdict[1])}</div>
    <div class="test-facts">${esc(facts.join(" · "))}</div>
    ${body}${structured}`;
}
