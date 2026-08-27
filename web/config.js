import { API, apiFetch, esc, fetchVenvs, fillVenvSelect, showAlert } from "./common.js";
import { loadInstances } from "./instances.js";

let currentEditId = null;

export function bindEdit() {
  document.getElementById("edit-backdrop").addEventListener("click", closeEdit);
  document.getElementById("edit-cancel").addEventListener("click", closeEdit);
  document.getElementById("edit-save").addEventListener("click", () => saveEdit(false));
  document.getElementById("edit-save-restart").addEventListener("click", () => saveEdit(true));
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
  fillVenvSelect(document.getElementById("edit-venv"), await fetchVenvs(), cfg.venv || "default");

  // Choosing a mode with no way to establish an identity is the one
  // combination that fails silently later — required refuses every call,
  // optional never sees a user. Say it here, where the choice is made.
  const settings = await apiFetch("/api/settings").catch(() => null);
  const canIdentify = settings === null
    || settings.user_jwt_secret_set || settings.user_trust_headers;
  document.getElementById("edit-identity-hint").classList.toggle("hidden", !!canIdentify);

  // Which fields are credentials is the server's call (it does the masking) —
  // it ships the classification in the payload instead of us re-guessing here.
  const secretFields = new Set(cfg.secret_fields || []);

  const container = document.getElementById("edit-values-container");
  if (cfg.values && Object.keys(cfg.values).length > 0) {
    container.innerHTML = '<div class="values-grid">' +
      Object.entries(cfg.values).map(([k, v]) => {
        const isSecret = secretFields.has(k);
        const type = typeof v === "boolean" ? "checkbox"
                   : typeof v === "number" ? "number" : "text";
        if (type === "checkbox") {
          return `<label>${esc(k)}</label><input type="checkbox" data-val="${esc(k)}" ${v ? "checked" : ""} />`;
        }
        const inputVal = isSecret ? "" : esc(String(v));
        const placeholder = isSecret ? "●●●●●●●●" : "";
        return `<label>${esc(k)}</label><input type="${type}" data-val="${esc(k)}"${isSecret ? ' data-secret="1"' : ""} value="${inputVal}" placeholder="${placeholder}" />`;
      }).join("") + "</div>";
  } else {
    container.innerHTML = "<p style='color:var(--text-muted);font-size:13px'>No configurable values.</p>";
  }

  document.getElementById("edit-modal").classList.remove("hidden");
}

async function saveEdit(restart) {
  const id = currentEditId;
  const values = {};

  document.querySelectorAll("[data-val]").forEach(input => {
    const key = input.dataset.val;
    if (input.type === "checkbox") values[key] = input.checked;
    else if (input.type === "number") { if (input.value !== "") values[key] = Number(input.value); }
    // Secrets: an empty field means "unchanged" (the real value is never shown).
    // Non-secret fields are sent as-is, so a value can be cleared to "".
    else if (input.dataset.secret) { if (input.value !== "") values[key] = input.value; }
    else values[key] = input.value;
  });

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

  const body = {
    name: document.getElementById("edit-name").value,
    category: document.getElementById("edit-category").value.trim(),
    server,
    lifecycle: { auto_start: document.getElementById("edit-autostart").checked },
    values,
    install: { dependencies: deps },
    venv: document.getElementById("edit-venv").value || "default",
    identity_mode: document.getElementById("edit-identity-mode").value,
  };

  try {
    await apiFetch(`${API}/${id}`, { method: "PUT", body: JSON.stringify(body) });
    closeEdit();
    showAlert("success", "Config saved.");
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

