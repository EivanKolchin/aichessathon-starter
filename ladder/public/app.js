"use strict";
import { buildZip } from "./zipper.js";

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Directories that are never part of an agent; dropping a whole checkout should not
// upload a virtualenv or a build cache.
const SKIP = new Set([
  "__pycache__", ".git", ".venv", ".cache", ".chesslab", ".tools",
  ".mypy_cache", ".ruff_cache", "node_modules", ".DS_Store", ".idea", ".vscode",
]);

let staged = null;
let agents = [];

const bytes = (value) => {
  if (value < 1000) return `${value} B`;
  if (value < 1000000) return `${(value / 1000).toFixed(1)} kB`;
  return `${(value / 1000000).toFixed(1)} MB`;
};

function token() {
  try {
    return localStorage.getItem("ladder-token") || "";
  } catch {
    return "";
  }
}

// The name this browser goes by when the ladder is open and asks for no sign-in. Shared with
// the arena, so an upload from here and a run from there belong to the same person.
function viewer() {
  try {
    let name = localStorage.getItem("ladder-viewer") || "";
    if (!name) {
      name = `viewer-${crypto.randomUUID().slice(0, 8)}`;
      localStorage.setItem("ladder-viewer", name);
    }
    return name;
  } catch {
    return "anyone";
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Ladder-Owner", viewer());
  if (token()) headers.set("Authorization", `Bearer ${token()}`);
  const response = await fetch(path, { ...options, headers });
  const result = await response.json().catch(() => ({ error: `Request failed (${response.status})` }));
  if (response.status === 401) $("token-row").hidden = false;
  if (!response.ok) {
    const error = new Error(result.error || `Request failed (${response.status})`);
    error.problems = result.problems || [];
    throw error;
  }
  return result;
}

function status(message, kind = "") {
  $("status").textContent = message;
  $("status").className = `status ${kind}`;
  $("status").hidden = !message;
}

function problems(list) {
  $("problems").innerHTML = list.length
    ? `<h3>This zip would not pass validation</h3><ul>${list.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`
    : "";
  $("problems").hidden = !list.length;
}

// --- collecting files -------------------------------------------------------

function readEntries(reader) {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function walk(entry, prefix, out) {
  if (SKIP.has(entry.name)) return;
  const path = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    out.push({ name: path, blob: file });
    return;
  }
  const reader = entry.createReader();
  for (;;) {
    const batch = await readEntries(reader);
    if (!batch.length) break;
    for (const child of batch) await walk(child, path, out);
  }
}

async function fromDrop(transfer) {
  const roots = [...transfer.items].map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  if (!roots.length) return [...transfer.files].map((file) => ({ name: file.name, blob: file }));
  const out = [];
  for (const root of roots) await walk(root, "", out);
  return out;
}

function fromInput(list) {
  return [...list]
    .map((file) => ({ name: file.webkitRelativePath || file.name, blob: file }))
    .filter((file) => !file.name.split("/").some((part) => SKIP.has(part)));
}

// A dropped folder arrives as "my-agent/agent.py"; the platform imports `agent` from the
// zip root, so a single wrapping directory is stripped.
function normalise(files) {
  if (files.some((file) => file.name === "agent.py")) return files;
  const tops = new Set(files.map((file) => file.name.split("/")[0]));
  if (tops.size !== 1) return files;
  const prefix = `${[...tops][0]}/`;
  const stripped = files.map((file) => ({ ...file, name: file.name.slice(prefix.length) }));
  return stripped.some((file) => file.name === "agent.py") ? stripped : files;
}

async function stage(files) {
  problems([]);
  if (!files.length) return status("Nothing to upload", "bad");

  if (files.length === 1 && /\.zip$/i.test(files[0].name)) {
    staged = { blob: files[0].blob, count: null, label: files[0].name };
    $("name").value ||= files[0].name.replace(/\.zip$/i, "");
    status(`Ready: ${files[0].name}, ${bytes(files[0].blob.size)}`, "good");
  } else {
    const chosen = normalise(files);
    if (!chosen.some((file) => file.name === "agent.py")) {
      problems(["No agent.py at the top level of what you dropped; the platform does `import agent`"]);
      staged = null;
      return status("Not uploadable yet", "bad");
    }
    status(`Zipping ${chosen.length} files...`);
    const blob = await buildZip(chosen);
    staged = { blob, count: chosen.length, label: `${chosen.length} files` };
    status(`Ready: ${chosen.length} files, ${bytes(blob.size)} zipped`, "good");
  }
  $("upload").disabled = false;
}

// --- uploading --------------------------------------------------------------

function send(form) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/upload");
    request.setRequestHeader("X-Ladder-Owner", viewer());
    if (token()) request.setRequestHeader("Authorization", `Bearer ${token()}`);
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        $("bar").hidden = false;
        $("bar").value = (event.loaded / event.total) * 100;
      }
    });
    request.addEventListener("load", () => {
      $("bar").hidden = true;
      let body = {};
      try {
        body = JSON.parse(request.responseText);
      } catch {
        body = { error: `Request failed (${request.status})` };
      }
      if (request.status === 401) $("token-row").hidden = false;
      if (request.status >= 200 && request.status < 300) resolve(body);
      else {
        const error = new Error(body.error || `Request failed (${request.status})`);
        error.problems = body.problems || [];
        reject(error);
      }
    });
    request.addEventListener("error", () => {
      $("bar").hidden = true;
      reject(new Error("Network error"));
    });
    request.send(form);
  });
}

async function upload() {
  if (!staged) return;
  problems([]);
  $("upload").disabled = true;
  status("Uploading...");
  const form = new FormData();
  form.append("file", staged.blob, "submission.zip");
  form.append("name", $("name").value.trim());
  form.append("family", $("family").value.trim());
  form.append("notes", $("notes").value.trim());
  try {
    const result = await send(form);
    status(
      result.unchanged
        ? `Already in the catalogue as ${result.agent.id}; identical to a previous upload`
        : `Uploaded as ${result.agent.id}`,
      "good",
    );
    staged = null;
    $("name").value = "";
    $("notes").value = "";
    await refresh();
  } catch (error) {
    status(error.message, "bad");
    problems(error.problems || []);
    $("upload").disabled = false;
  }
}

// --- catalogue --------------------------------------------------------------

function render() {
  $("count").textContent = agents.length ? `${agents.length} agent${agents.length === 1 ? "" : "s"}` : "";
  if (!agents.length) {
    $("agents").innerHTML = `<p class="empty">No agents yet. Drop one above and it appears for everybody.</p>`;
    return;
  }
  $("agents").innerHTML = agents
    .map((agent) => {
      const when = new Date(agent.created_at).toLocaleString();
      const files = agent.files.length;
      return `<article class="agent ${agent.withdrawn ? "withdrawn" : ""}">
        <div class="agent-head">
          <h3>${esc(agent.name)}</h3>
          <code>${esc(agent.id)}</code>
        </div>
        <p class="muted">${esc(agent.family)} · ${esc(agent.owner)} · ${when}</p>
        ${agent.notes ? `<p class="notes">${esc(agent.notes)}</p>` : ""}
        <p class="muted small">${files} file${files === 1 ? "" : "s"} · ${bytes(agent.zip_bytes)} zipped ·
          ${bytes(agent.unzipped_bytes)} unzipped · sha ${esc(agent.sha256.slice(0, 12))}</p>
        <div class="agent-actions">
          <a href="/api/agents/${esc(agent.id)}/zip" download>Download zip</a>
          <button data-withdraw="${esc(agent.id)}" data-restore="${agent.withdrawn ? "1" : "0"}">
            ${agent.withdrawn ? "Restore" : "Withdraw"}
          </button>
        </div>
      </article>`;
    })
    .join("");
}

async function refresh() {
  try {
    const result = await api(`/api/catalog?all=${$("show-withdrawn").checked ? "1" : "0"}`);
    agents = result.agents;
    $("viewer").textContent = result.viewer.email;
    $("token-row").hidden = true;
    render();
  } catch (error) {
    status(error.message, "bad");
  }
}

// --- wiring -----------------------------------------------------------------

function init() {
  const zone = $("drop");
  for (const name of ["dragenter", "dragover"]) {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("over");
    });
  }
  for (const name of ["dragleave", "drop"]) {
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.remove("over");
    });
  }
  zone.addEventListener("drop", async (event) => {
    try {
      await stage(await fromDrop(event.dataTransfer));
    } catch (error) {
      status(error.message, "bad");
    }
  });

  $("pick-zip").addEventListener("click", () => $("zip-input").click());
  $("pick-folder").addEventListener("click", () => $("folder-input").click());
  for (const id of ["zip-input", "folder-input"]) {
    $(id).addEventListener("change", async (event) => {
      await stage(fromInput(event.target.files));
      event.target.value = "";
    });
  }

  $("upload").addEventListener("click", upload);
  $("show-withdrawn").addEventListener("change", refresh);
  $("save-token").addEventListener("click", () => {
    localStorage.setItem("ladder-token", $("token").value.trim());
    $("token").value = "";
    refresh();
  });
  $("agents").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-withdraw]");
    if (!button) return;
    const restore = button.dataset.restore === "1" ? "?restore=1" : "";
    try {
      await api(`/api/agents/${button.dataset.withdraw}/withdraw${restore}`, { method: "POST" });
      await refresh();
    } catch (error) {
      status(error.message, "bad");
    }
  });

  $("pull-command").textContent = `python -m chesslab ladder pull --url ${location.origin}`;
  refresh();
}

init();
