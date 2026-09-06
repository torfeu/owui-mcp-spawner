import { API, apiFetch, bindNewVenvField, esc, fetchVenvs, fillVenvSelect, showAlert, venvChoice } from "./common.js";
import { loadInstances } from "./instances.js";

let currentEditId = null;
// Read once per dialog: whether a download base URL is configured decides the
// hint under the file-storage switch, and re-fetching it on every toggle would
// be a request per click.
let contentBaseUrlSet = false;

export function bindEdit() {
  document.getElementById("edit-backdrop").addEventListener("click", closeEdit);
  document.getElementById("edit-cancel").addEventListener("click", closeEdit);
  document.getElementById("edit-save").addEventListener("click", () => saveEdit(false));
  document.getElementById("edit-save-restart").addEventListener("click", () => saveEdit(true));
  document.getElementById("edit-content-enabled").addEventListener("change", updateContentHint);
}

function updateContentHint() {
  const on = document.getElementById("edit-content-enabled").checked;
  document.getElementById("edit-content-hint").classList.toggle("hidden", !on || contentBaseUrlSet);
}

export async function openEdit(id) {
  currentEditId = id;
  const cfg = await apiFetch(`${API}/${id}/config`);

  document.getElementById("edit-title").textContent = cfg.name;
  document.getElementById("edit-name").value = cfg.name;
  document.getElementById("edit-category").value = cfg.category || "";
  document.getElementById("edit-host").value = cfg.server.host;
  document.getElementById("edit-port").value = cfg.server.port;
  document.getElementById("edit-endpoint").value = cfg.server.endpoint;
  document.getElementById("edit-autostart").checked = cfg.lifecycle?.auto_start ?? false;
  document.getElementById("edit-deps").value = (cfg.install?.dependencies || []).join("\n");
  document.getElementById("edit-identity-mode").value = cfg.identity_mode || "off";
  document.getElementById("edit-forward-agent-token").checked = cfg.forward_agent_token === true;
  document.getElementById("edit-content-enabled").checked = cfg.content?.enabled ?? false;
  document.getElementById("edit-content-prefix").value = cfg.content?.url_prefix ?? "/cache/files/";
  fillVenvSelect(document.getElementById("edit-venv"), await fetchVenvs(), cfg.venv || "default", { allowNew: true });
  bindNewVenvField("edit-venv", "edit-venv-new");

  // Choosing a mode with no way to establish an identity is the one
  // combination that fails silently later — required refuses every call,
  // optional never sees a user. Say it here, where the choice is made.
  const settings = await apiFetch("/api/settings").catch(() => null);
  const canIdentify = settings === null
    || settings.user_jwt_secret_set || settings.user_trust_headers;
  document.getElementById("edit-identity-hint").classList.toggle("hidden", !!canIdentify);

  // Same kind of trap as the identity hint: storage switched on without a base
  // URL works right up to the point where somebody clicks the link in a chat.
  contentBaseUrlSet = settings === null || !!settings.content_base_url;
  updateContentHint();

  // Which fields are credentials is the server's call (it does the masking) —
  // it ships the classification in the payload instead of us re-guessing here.
  const secretFields = new Set(cfg.secret_fields || []);

  const container = document.getElementById("edit-values-container");
  if (cfg.values && Object.keys(cfg.values).length > 0) {
    container.innerHTML = '<div class="values-grid">' +
      Object.entries(cfg.values).map(([k, v]) => {
        const isSecret = secretFields.has(k);
        // A valve may hold a structure — `values` is dict[str, Any], and a
        // credential can sit inside one. String(v) turns that into the literal
        // "[object Object]", which this dialog then wrote back over the real
        // thing: opening the config and pressing save was enough to destroy it.
        const isStructure = v !== null && typeof v === "object";
        const type = typeof v === "boolean" ? "checkbox"
                   : typeof v === "number" ? "number" : "text";
        if (type === "checkbox") {
          return `<label>${esc(k)}</label><input type="checkbox" data-val="${esc(k)}" ${v ? "checked" : ""} />`;
        }
        // A structure is shown even when its name reads like a credential: the
        // server has already masked the secret leaves *inside* it, and blanking
        // the whole field would hide settings that are not secrets and leave no
        // way to edit them. Whatever comes back masked is put back on save.
        const hide = isSecret && !isStructure;
        const shown = isStructure ? JSON.stringify(v) : String(v);
        const inputVal = hide ? "" : esc(shown);
        const placeholder = hide ? "●●●●●●●●" : "";
        return `<label>${esc(k)}</label><input type="${type}" data-val="${esc(k)}"${hide ? ' data-secret="1"' : ""}${isStructure ? ' data-json="1"' : ""} value="${inputVal}" placeholder="${placeholder}" />`;
      }).join("") + "</div>";
  } else {
    container.innerHTML = "<p style='color:var(--text-muted);font-size:13px'>No configurable values.</p>";
  }

  document.getElementById("edit-modal").classList.remove("hidden");
}

async function saveEdit(restart) {
  const id = currentEditId;
  const values = {};
  const broken = [];

  document.querySelectorAll("[data-val]").forEach(input => {
    const key = input.dataset.val;
    if (input.type === "checkbox") values[key] = input.checked;
    else if (input.type === "number") { if (input.value !== "") values[key] = Number(input.value); }
    // Secrets: an empty field means "unchanged" (the real value is never shown).
    // Non-secret fields are sent as-is, so a value can be cleared to "".
    else if (input.dataset.secret) { if (input.value !== "") values[key] = input.value; }
    else if (input.dataset.json) {
      // Came in as a structure and has to leave as one. A field edited into
      // something that is not JSON any more stops the save: sending it as a
      // string would overwrite the structure with the typo.
      try { values[key] = JSON.parse(input.value); }
      catch { broken.push(key); }
    }
    else values[key] = input.value;
  });
  if (broken.length) {
    showAlert("error", `Not valid JSON: ${broken.join(", ")} — this valve holds a ` +
      `structure, so it has to stay one. Nothing was saved.`);
    return;
  }

  const deps = document.getElementById("edit-deps").value
    .split("\n").map(s => s.trim()).filter(Boolean);

  // An emptied port field means "keep the current port": omit the key so the
  // backend falls back to the stored value — parseInt("") is NaN, which would
  // serialize to null and fail server-side validation.
  const server = {
    host: document.getElementById("edit-host").value,
    endpoint: document.getElementById("edit-endpoint").value,
  };
  const port = parseInt(document.getElementById("edit-port").value);
  if (!Number.isNaN(port)) server.port = port;

  // Moving an instance to a venv that does not exist yet is fine — the server
  // creates it and installs the dependencies there before it saves.
  const venv = venvChoice("edit-venv", "edit-venv-new");
  if (venv === null) return;   // unusable name — venvChoice said so

  const body = {
    name: document.getElementById("edit-name").value,
    category: document.getElementById("edit-category").value.trim(),
    server,
    lifecycle: { auto_start: document.getElementById("edit-autostart").checked },
    values,
    install: { dependencies: deps },
    venv: venv || "default",
    identity_mode: document.getElementById("edit-identity-mode").value,
    forward_agent_token: document.getElementById("edit-forward-agent-token").checked,
    content: {
      enabled: document.getElementById("edit-content-enabled").checked,
      url_prefix: document.getElementById("edit-content-prefix").value.trim() || "/cache/files/",
    },
  };

  try {
    const res = await apiFetch(`${API}/${id}`, { method: "PUT", body: JSON.stringify(body) });
    closeEdit();
    // The config is on disk either way. Whether the runner took it is the
    // second half of the answer, and the half worth interrupting for — but the
    // answer stops there: a restart that failed on the *start* leaves nothing
    // running, so naming the old settings as still live would be a guess.
    if (res && res.restart_error) {
      showAlert("warning", `Config saved, but the automatic restart failed: ${res.restart_error} ` +
        `— check the instance's status, it may not be running.`);
    } else {
      showAlert("success", "Config saved.");
    }
    if (restart) {
      await apiFetch(`${API}/${id}/restart`, { method: "POST" });
      showAlert("info", "Restarting…");
    }
    loadInstances();
  } catch (err) {
    showAlert("error", err.message);
  }
}

function closeEdit() {
  document.getElementById("edit-modal").classList.add("hidden");
  currentEditId = null;
}

