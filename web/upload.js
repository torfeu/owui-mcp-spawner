import { API, apiFetchRaw, fetchVenvs, fillVenvSelect, showAlert } from "./common.js";
import { loadInstances } from "./instances.js";

export function bindUpload() {
  const modal    = document.getElementById("upload-modal");
  const backdrop = document.getElementById("upload-backdrop");
  const openBtn  = document.getElementById("upload-btn");
  const cancelBtn = document.getElementById("upload-cancel");
  const submitBtn = document.getElementById("upload-submit");
  const fileInput = document.getElementById("file-input");
  const fileLabel = document.getElementById("file-label");
  const fileDrop  = document.getElementById("file-drop");
  const progress  = document.getElementById("upload-progress");
  const statusTxt = document.getElementById("upload-status-text");

  let selectedFile = null;

  openBtn.addEventListener("click", async () => {
    selectedFile = null; resetUpload(); modal.classList.remove("hidden");
    const uploadVenv = document.getElementById("upload-venv");
    fillVenvSelect(uploadVenv, await fetchVenvs(), "");
    uploadVenv.prepend(new Option("from JSON / default", ""));
    uploadVenv.value = "";
  });
  cancelBtn.addEventListener("click", closeUpload);
  backdrop.addEventListener("click", closeUpload);

  fileInput.addEventListener("change", () => {
    selectedFile = fileInput.files[0];
    fileLabel.textContent = selectedFile ? selectedFile.name : "Drop file here or click to select";
    submitBtn.disabled = !selectedFile;
  });

  fileDrop.addEventListener("dragover", e => { e.preventDefault(); fileDrop.classList.add("drag-over"); });
  fileDrop.addEventListener("dragleave", () => fileDrop.classList.remove("drag-over"));
  fileDrop.addEventListener("drop", e => {
    e.preventDefault();
    fileDrop.classList.remove("drag-over");
    const f = e.dataTransfer.files[0];
    if (f) { selectedFile = f; fileLabel.textContent = f.name; submitBtn.disabled = false; }
  });

  submitBtn.addEventListener("click", async () => {
    if (!selectedFile) return;
    submitBtn.disabled = true;
    progress.classList.remove("hidden");
    statusTxt.textContent = "Uploading & installing…";

    try {
      const form = new FormData();
      form.append("file", selectedFile);
      // Only a non-empty category is an explicit override. Omitting the field
      // preserves a category already present in an uploaded MCP config.
      const category = document.getElementById("upload-category").value.trim();
      if (category) form.append("category", category);
      // Only send venv when the user picked one, so a venv set in an uploaded
      // MCP config stays the default instead of being overwritten with "default".
      const venvVal = document.getElementById("upload-venv").value.trim();
      if (venvVal) form.append("venv", venvVal);
      const portVal = document.getElementById("upload-port").value;
      if (portVal) form.append("port", portVal);
      const res = await apiFetchRaw(`${API}/upload`, { method: "POST", body: form });
      const data = await res.json();
      closeUpload();
      showAlert("success", `Installed: ${data.id} on port ${data.port}`);
      loadInstances();
    } catch (err) {
      statusTxt.textContent = "";
      progress.classList.add("hidden");
      submitBtn.disabled = false;
      showAlert("error", err.message);
    }
  });

  function closeUpload() { modal.classList.add("hidden"); }
  function resetUpload() {
    fileInput.value = "";
    fileLabel.textContent = "Drop file here or click to select";
    document.getElementById("upload-category").value = "";
    document.getElementById("upload-port").value = "";
    submitBtn.disabled = true;
    progress.classList.add("hidden");
  }
}
