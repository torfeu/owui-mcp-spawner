export const API = "/api/instances";

export const state = {
  pollTimer: null,
  editMode: "full",
  guestMode: false,
  authEnabled: false,
};

export function applyEditMode() {
  const hideUpload = state.editMode === "readonly" || state.guestMode;
  const hideNewTool = state.editMode !== "full" || state.guestMode;
  document.getElementById("upload-btn").classList.toggle("hidden", hideUpload);
  document.getElementById("editor-btn").classList.toggle("hidden", hideNewTool);
  document.getElementById("settings-btn").classList.toggle("hidden", state.guestMode);
  document.getElementById("stats-btn").classList.toggle("hidden", state.guestMode);
  document.getElementById("perm-btn").classList.toggle("hidden", state.guestMode);
  document.getElementById("login-btn").classList.toggle("hidden", !state.guestMode);
  document.getElementById("logout-btn").classList.toggle("hidden", !(state.authEnabled && !state.guestMode));
  document.body.classList.toggle("guest", state.guestMode);
}

export function getToken() { return sessionStorage.getItem("mcp_token") || ""; }
export function setToken(token) { sessionStorage.setItem("mcp_token", token); }
export function clearToken() { sessionStorage.removeItem("mcp_token"); }

export function authHeaders() {
  const token = getToken();
  return token ? { "Authorization": `Bearer ${token}` } : {};
}

export function showLoginModal() {
  document.getElementById("login-modal").classList.remove("hidden");
  document.getElementById("login-password").focus();
}

export function hideLoginModal() {
  document.getElementById("login-modal").classList.add("hidden");
  document.getElementById("login-password").value = "";
  document.getElementById("login-error").classList.add("hidden");
}

// HTTPException details are not always strings: 422s carry {message, errors:[…]}
// and FastAPI validation errors are arrays of {msg, …}. Flatten them into a
// readable message instead of the "[object Object]" that String() would give.
export function formatDetail(detail, fallback = "Request failed") {
  if (detail == null || detail === "") return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map(d => formatDetail(d, fallback)).join("; ");
  }
  if (typeof detail === "object") {
    const parts = [];
    if (detail.message) parts.push(String(detail.message));
    if (detail.msg) parts.push(String(detail.msg));
    if (Array.isArray(detail.errors)) parts.push(...detail.errors.map(String));
    if (parts.length) return parts.join(" — ");
  }
  try { return JSON.stringify(detail); } catch { return fallback; }
}

// Shared fetch core for every API call: adds the auth header, centralizes the
// 401 → login-modal handling and turns error bodies into readable messages.
// Returns the raw Response so callers can pick json()/text()/blob(); it does
// NOT set a Content-Type (FormData uploads must let the browser set it).
export async function apiFetchRaw(url, opts = {}) {
  // Merge headers explicitly — a plain {...opts} spread after `headers` would
  // let a caller-supplied opts.headers replace the whole merged object
  // (silently dropping Authorization).
  const { headers: extraHeaders, ...rest } = opts;
  const response = await fetch(url, {
    ...rest,
    headers: { ...authHeaders(), ...extraHeaders },
  });
  if (response.status === 401) {
    clearToken();
    showLoginModal();
    throw new Error("Session expired — please log in again");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const error = new Error(formatDetail(body.detail, response.statusText));
    // Keep the structured payload inspectable: the editor renders 422 details
    // ({message, errors:[…]}) in its results panel instead of a plain alert.
    error.detail = body.detail;
    throw error;
  }
  return response;
}

export async function apiFetch(url, opts = {}) {
  const { headers: extraHeaders, ...rest } = opts;
  const response = await apiFetchRaw(url, {
    ...rest,
    headers: { "Content-Type": "application/json", ...extraHeaders },
  });
  return response.json().catch(() => ({}));
}

// One blob-download routine for every "Export" button.
export async function downloadBlob(url, filename, opts = {}) {
  const response = await apiFetchRaw(url, opts);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(objectUrl);
}

// Shared by the storage settings and the system monitor. TB is in the list
// because a disk tile is the one place a four-digit GB figure shows up.
export function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${value < 10 && unit ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

export async function fetchVenvs() {
  try {
    return await apiFetch("/api/venvs");
  } catch {
    return [{ name: "default", is_default: true, instances: 0, exists: true }];
  }
}

// Sentinel for "let me type a name". Deliberately contains a character the
// server's venv-name rule rejects, so it can never collide with a real venv.
export const NEW_VENV = "+new";

export function fillVenvSelect(select, venvs, selected, { allowNew = false } = {}) {
  select.innerHTML = venvs.map(venv =>
    `<option value="${esc(venv.name)}"${venv.name === selected ? " selected" : ""}>${esc(venv.name)}${venv.is_default ? " (default)" : ""}</option>`
  ).join("");
  if (selected && !venvs.some(venv => venv.name === selected)) {
    const option = document.createElement("option");
    option.value = selected;
    option.textContent = `${selected} (current)`;
    option.selected = true;
    select.appendChild(option);
  }
  if (allowNew) select.appendChild(new Option("+ New venv…", NEW_VENV));
}

/** Wire a venv select to the text field that appears when "+ New venv…" is picked.
 *
 * Called on every dialog open (the select is repopulated then), so the change
 * listener is attached exactly once per element — re-adding it each open would
 * pile up a listener per visit for the session.
 */
export function bindNewVenvField(selectId, inputId) {
  const select = document.getElementById(selectId);
  const input = document.getElementById(inputId);
  const sync = () => {
    const creating = select.value === NEW_VENV;
    input.classList.toggle("hidden", !creating);
    if (creating) input.focus(); else input.value = "";
  };
  if (!select.dataset.newVenvBound) {
    select.dataset.newVenvBound = "1";
    select.addEventListener("change", sync);
  }
  sync();
}

/**
 * The venv the dialog is asking for, or null when the typed name is unusable.
 * The server validates the name again and creates the venv on demand — this
 * only spares the user a round trip for the obvious mistakes.
 */
export function venvChoice(selectId, inputId) {
  const select = document.getElementById(selectId);
  if (select.value !== NEW_VENV) return select.value;
  const name = document.getElementById(inputId).value.trim();
  if (!name) {
    showAlert("error", "Enter a name for the new venv");
    return null;
  }
  if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
    showAlert("error", `Invalid venv name '${name}': letters, digits, _ and - only`);
    return null;
  }
  return name;
}

/** Put *text* on the clipboard. Returns false when even the fallback failed.
 *
 * `navigator.clipboard` exists only in a secure context, and this manager is
 * normally reached over plain HTTP on a LAN address — measured on the live
 * installation: `http://192.168.1.100:7860` reports `isSecureContext: false`
 * and `navigator.clipboard` as `undefined`. So the modern API is tried where it
 * exists and the legacy path is used where it does not, rather than telling
 * somebody to select a URL that is only visible while the mouse hovers it.
 *
 * The legacy path needs a real, focusable element: `display: none` or
 * `visibility: hidden` leave the selection empty and make the copy a silent
 * no-op. It also needs the user gesture that is still in flight, which is why
 * nothing is awaited before it on the path that will use it.
 */
export async function copyText(text) {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Refused by a permissions policy or a sandbox — the fallback may still
      // work, so fall through instead of giving up here.
    }
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    area.setSelectionRange(0, text.length);   // iOS ignores select() alone
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

export function showAlert(type, message) {
  const area = document.getElementById("alert-area");
  const element = document.createElement("div");
  element.className = `alert alert-${type}`;
  element.textContent = message;
  area.prepend(element);
  setTimeout(() => element.remove(), 5000);
}

export function esc(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
