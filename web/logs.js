import { API, apiFetchRaw } from "./common.js";

let currentLogsId = null;
let currentLogsTab = "install";

export function bindLogs() {
  document.getElementById("logs-backdrop").addEventListener("click", closeLogs);
  document.getElementById("logs-close").addEventListener("click", closeLogs);
  document.getElementById("logs-refresh").addEventListener("click", () => loadLog(currentLogsId, currentLogsTab));

  document.getElementById("tab-install").addEventListener("click", () => switchTab("install"));
  document.getElementById("tab-runtime").addEventListener("click", () => switchTab("runtime"));
}

export async function openLogs(id) {
  currentLogsId = id;
  currentLogsTab = "install";
  document.getElementById("logs-title").textContent = id;
  switchTab("install");
  document.getElementById("logs-modal").classList.remove("hidden");
}

async function switchTab(tab) {
  currentLogsTab = tab;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  await loadLog(currentLogsId, tab);
}

async function loadLog(id, tab) {
  const pre = document.getElementById("log-content");
  pre.textContent = "Loading…";
  try {
    const res = await apiFetchRaw(`${API}/${id}/logs/${tab}`);
    const text = await res.text();
    pre.textContent = text || "(empty)";
    pre.scrollTop = pre.scrollHeight;
  } catch (e) {
    pre.textContent = `(failed to load: ${e.message})`;
  }
}

function closeLogs() {
  document.getElementById("logs-modal").classList.add("hidden");
  currentLogsId = null;
}

