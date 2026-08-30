import { API, apiFetch, apiFetchRaw, bindNewVenvField, downloadBlob, esc, fetchVenvs, fillVenvSelect, showAlert, venvChoice } from "./common.js";
import { loadInstances } from "./instances.js";

let editorCM = null;
let editorEditId = null;   // null = new tool, string = editing existing instance

document.getElementById("editor-btn").addEventListener("click", () => openEditor());
document.getElementById("editor-cancel").addEventListener("click", closeEditor);
document.getElementById("editor-close").addEventListener("click", closeEditor);
document.getElementById("editor-backdrop").addEventListener("click", closeEditor);
document.getElementById("editor-validate").addEventListener("click", runValidate);
document.getElementById("editor-export").addEventListener("click", runExport);
document.getElementById("editor-install").addEventListener("click", runInstall);

export async function openEditorForInstance(id) {
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
    const resp = await apiFetchRaw("/api/tools/template");
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
  if (opts.category !== undefined) document.getElementById("editor-category").value = opts.category;

  // Lock ID field when editing (can't rename an existing instance)
  document.getElementById("editor-id").disabled = !!editorEditId;

  // Venv + port only apply when creating a new instance
  document.getElementById("editor-venv-field").classList.toggle("hidden", !!editorEditId);
  document.getElementById("editor-port-field").classList.toggle("hidden", !!editorEditId);
  if (!editorEditId) {
    document.getElementById("editor-port").value = "";
    fillVenvSelect(document.getElementById("editor-venv"), await fetchVenvs(), opts.venv || "default", { allowNew: true });
    bindNewVenvField("editor-venv", "editor-venv-new");
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
    category: document.getElementById("editor-category").value.trim(),
  };
}

async function runValidate() {
  const btn = document.getElementById("editor-validate");
  btn.disabled = true;
  btn.textContent = "Validating…";
  try {
    const data = await apiFetch("/api/tools/validate", {
      method: "POST",
      // Pass the instance so the server validates in its venv (installed deps resolve)
      body: JSON.stringify({ code: getEditorCode(), ...(editorEditId ? { instance_id: editorEditId } : {}) }),
    });
    showValidationResults({
      valid: data.valid ?? false,
      errors: data.errors ?? ["Unknown error"],
      warnings: data.warnings ?? [],
      tools: data.tools ?? [],
      valves: data.valves ?? {},
    });
  } catch (e) {
    showAlert("error", "Validation request failed: " + e.message);
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

  try {
    await downloadBlob("/api/tools/export", `${meta.id}.json`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: getEditorCode(), ...meta }),
    });
  } catch (e) {
    showAlert("error", "Export failed: " + e.message);
  }
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
      const data = await apiFetch(`${API}/${editorEditId}/tool-code`, {
        method: "PUT",
        body: JSON.stringify({ code: getEditorCode() }),
      });
      if (data.warnings?.length) showAlert("warning", "Warnings: " + data.warnings.join("; "));
      showAlert("success", data.restarted
        ? `Tool '${meta.name}' saved and restarted.`
        : `Tool '${meta.name}' saved. Restart to apply changes.`);
    } else {
      // New tool: create_tool installs deps, validates in the venv and saves — one step.
      // A venv named here that does not exist yet is created by the server.
      const newVenv = venvChoice("editor-venv", "editor-venv-new");
      if (newVenv === null) return;   // unusable name — venvChoice said so
      const body = {
        code: getEditorCode(),
        id: meta.id,
        name: meta.name,
        description: meta.description,
        category: meta.category,
        venv: newVenv || "default",
      };
      const portVal = document.getElementById("editor-port").value;
      if (portVal) body.port = Number(portVal);
      const data = await apiFetch("/api/tools/create", { method: "POST", body: JSON.stringify(body) });
      if (data.warnings?.length) showAlert("warning", "Warnings: " + data.warnings.join("; "));
      showAlert("success", `Tool '${meta.name}' installed on port ${data.port}!`);
    }
    closeEditor();
    loadInstances();
  } catch (e) {
    // Structured 422s (validation / dependency failures) go to the results panel
    if (e.detail !== undefined) showEditorErrors({ detail: e.detail });
    else showAlert("error", "Request failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

