"use strict";
// The hosted arena. Everything shown here came out of D1, and everything in D1 was put there by
// a machine that actually played the game. This page never computes a position; it renders what
// a runner reported, which is why it can be a static asset with no engine anywhere near it.

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const FILES = "abcdefgh";
const PIECES = { p: "pawn", n: "knight", b: "bishop", r: "rook", q: "queen", k: "king" };
const START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

let capability = null;      // what a live runner says it can play
let runs = [];
let current = null;         // the run being watched
let gameId = "";
let detail = null;
let flipped = false;
let following = true;
let timer = null;

// Behind Access the browser is already identified. On a deployment that is not, the catalogue
// page keeps a ladder token in this browser and every request carries it, same as /index.html.
const token = () => localStorage.getItem("ladder-token") || "";

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (token()) headers.set("Authorization", `Bearer ${token()}`);
  const response = await fetch(path, { ...options, headers });
  const body = await response.json().catch(() => ({ error: `Request failed (${response.status})` }));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
const post = (path, payload) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload ?? {}),
});

// ── board ─────────────────────────────────────────────────────────────────────────────────

function piecesOf(fen) {
  const pieces = {};
  fen.split(" ")[0].split("/").forEach((row, rank) => {
    let file = 0;
    for (const symbol of row) {
      if (/\d/.test(symbol)) file += Number(symbol);
      else { pieces[FILES[file] + (8 - rank)] = symbol; file++; }
    }
  });
  return pieces;
}
function drawBoard(fen, uci) {
  const pieces = piecesOf(fen), last = [uci?.slice(0, 2), uci?.slice(2, 4)];
  let html = "";
  for (let row = 0; row < 8; row++) for (let col = 0; col < 8; col++) {
    const file = flipped ? 7 - col : col, rank = flipped ? row + 1 : 8 - row;
    const square = FILES[file] + rank, piece = pieces[square];
    const alt = piece
      ? `${piece === piece.toUpperCase() ? "White" : "Black"} ${PIECES[piece.toLowerCase()]} on ${square}` : "";
    // Case-insensitive filesystems cannot hold both P.svg and p.svg, so the colour is explicit.
    const art = piece ? `${piece === piece.toUpperCase() ? "w" : "b"}-${piece.toLowerCase()}` : "";
    html += `<div class="square ${(file + rank) % 2 === 1 ? "dark" : ""} ${last.includes(square) ? "last" : ""}">`
      + (piece ? `<img src="/pieces/${art}.svg" alt="${alt}" draggable="false">` : "") + "</div>";
  }
  $("board").innerHTML = html;
}
const clock = (ms) => {
  if (ms == null) return "—";
  const seconds = Math.max(0, ms / 1000);
  return `${Math.floor(seconds / 60)}:${Math.floor(seconds % 60).toString().padStart(2, "0")}`;
};

// ── the form ──────────────────────────────────────────────────────────────────────────────

const engineName = (id) => capability?.engines.find((e) => e.id === id)?.name || id;

function renderForm() {
  const engines = (capability?.engines || []).filter((engine) => engine.available);
  $("capacity").textContent = capability
    ? `${engines.length} engines · ${capability.openings.length} positions`
    : "no runner online";
  $("candidate").innerHTML = engines
    .map((engine) => `<option value="${esc(engine.id)}">${esc(engine.name)}</option>`).join("");
  $("opponents").innerHTML = engines
    .map((engine) => `<label class="opponent"><input type="checkbox" value="${esc(engine.id)}"> <span>${esc(engine.name)}<small>${esc(engine.family)}</small></span></label>`)
    .join("");
  updateQueueNote();
}
const chosen = () => [...document.querySelectorAll("#opponents input:checked")]
  .map((box) => box.value).filter((id) => id !== $("candidate").value);

function chosenOpenings() {
  const split = $("split").value;
  const inSplit = (capability?.openings || []).filter((opening) => opening.split === split);
  const groups = new Map();
  for (const opening of inSplit) {
    if (!groups.has(opening.family)) groups.set(opening.family, []);
    groups.get(opening.family).push(opening);
  }
  const spread = [];
  for (let index = 0; index < 4; index++) {
    for (const family of groups.values()) if (family[index]) spread.push(family[index]);
  }
  const wanted = $("positions").value;
  return wanted === "all" ? spread : spread.slice(0, Number(wanted));
}
function updateQueueNote() {
  const opponents = chosen(), openings = chosenOpenings();
  const games = opponents.length * openings.length * 2;
  const atOnce = Number($("parallel").value);
  $("pool").textContent = `${opponents.length} selected`;
  $("queue-note").textContent = games
    ? `${games} games, ${atOnce} at once, on whichever runner takes the job`
      + (atOnce > 1 ? " · clocks will not be comparable with the event" : "")
    : "Choose a candidate and at least one opponent";
  $("queue").disabled = !games || !capability;
}
async function queueRun() {
  const [base, increment] = $("clock").value.split(",").map(Number);
  $("queue").disabled = true;
  try {
    const result = await post("/api/runs", {
      label: $("label").value.trim() || "Untitled experiment",
      request: {
        candidate: $("candidate").value,
        opponents: chosen(),
        openings: chosenOpenings().map((opening) => opening.id),
        base_ms: base,
        increment_ms: increment,
        parallel: Number($("parallel").value),
      },
    });
    say(`Queued ${result.run.id}. It starts when a runner picks it up.`, "good");
    await refresh();
    watch(result.run.id);
  } catch (error) {
    say(error.message, "bad");
  } finally {
    updateQueueNote();
  }
}
function say(message, tone) {
  $("queue-status").textContent = message;
  $("queue-status").className = `status ${tone}`;
  $("queue-status").hidden = false;
}

// ── runs ──────────────────────────────────────────────────────────────────────────────────

function renderRuns() {
  $("runs").innerHTML = runs.length
    ? runs.map((run) => `<button type="button" class="run ${run.id === current?.id ? "selected" : ""}" data-run="${esc(run.id)}"><span class="dot ${esc(run.status)}"></span><span><strong>${esc(run.label)}</strong><small>${esc(run.status)} · ${run.completed}/${run.games} games${run.runner ? ` · ${esc(run.runner)}` : ""}</small></span></button>`).join("")
    : '<p class="empty">No experiments yet. Queue one above.</p>';
}
function renderRunners(list) {
  const fresh = list.filter((runner) => Date.now() / 1000 - runner.seen_at < 90);
  capability = fresh.map((runner) => runner.catalog).find(Boolean) || capability;
  $("runners").textContent = fresh.length
    ? `${fresh.length} runner${fresh.length === 1 ? "" : "s"} online · ${esc(fresh[0].machine)}`
    : "No runner online — queued experiments will wait";
}
function watch(id) {
  gameId = "";
  detail = null;
  current = runs.find((run) => run.id === id) || { id };
  renderRuns();
  refreshRun();
}

let formSignature = "";

async function refresh() {
  const state = await api("/api/runs");
  runs = state.runs;
  renderRunners(state.runners || []);
  // The form is rebuilt when a runner brings a different set of engines, not on every poll.
  const signature = JSON.stringify((capability?.engines || []).map((engine) => engine.id));
  if (signature !== formSignature) {
    formSignature = signature;
    renderForm();
  }
  renderRuns();
}

async function refreshRun() {
  if (!current) return;
  const { run } = await api(`/api/runs/${encodeURIComponent(current.id)}`);
  current = run;
  $("viewer").hidden = false;
  $("run-title").textContent = run.label;
  const atOnce = run.environment?.parallel_games || 1;
  $("run-meta").textContent = [
    run.status,
    run.runner ? `on ${run.runner}` : "waiting for a runner",
    atOnce > 1 ? `${atOnce} at once` : "",
  ].filter(Boolean).join(" · ");
  $("stop-run").hidden = !["queued", "running"].includes(run.status);
  $("contended").hidden = atOnce < 2;
  $("contended").textContent = "Games in this run shared a machine, so every engine measured "
    + "contended time. Read it as coverage, not as a clock-accurate strength claim.";

  const live = run.games.filter((game) => game.status === "running");
  $("live-strip").hidden = live.length < 2;
  $("live-strip").innerHTML = live.map((game) => `<button type="button" class="live ${game.id === gameId ? "selected" : ""}" data-live="${esc(game.id)}">${esc(engineName(game.opponent))}<small>${esc(game.opening.name || "")}</small></button>`).join("");

  if (following && live.length) gameId = live.some((g) => g.id === gameId) ? gameId : live[0].id;
  if (!gameId && run.games.length) gameId = run.games[0].id;
  $("game-select").innerHTML = run.games.map((game, index) =>
    `<option value="${esc(game.id)}">${index + 1}. ${esc(game.opening.name || game.id)} · ${esc(engineName(game.opponent))}${game.status === "completed" ? ` · ${esc(game.termination || "")}` : ` · ${esc(game.status)}`}</option>`).join("");
  $("game-select").value = gameId;

  $("results").innerHTML = (run.summary || []).map((row) =>
    `<tr><td>${esc(engineName(row.opponent))}</td><td>${row.wins} / ${row.draws} / ${row.losses}</td><td>${row.pairs}</td><td>${row.score == null ? "—" : `${(row.score * 100).toFixed(1)}%`}</td><td>${row.candidate_failures}</td></tr>`).join("")
    || '<tr><td colspan="5" class="empty">Results appear as games finish.</td></tr>';

  if (gameId) await refreshGame();
}

async function refreshGame() {
  const { game } = await api(
    `/api/runs/${encodeURIComponent(current.id)}/games/${encodeURIComponent(gameId)}`,
  ).catch(() => ({ game: null }));
  if (!game) return;
  detail = game;
  const frames = game.frames.length ? game.frames : [{ fen: START, san: "", uci: "" }];
  const frame = frames[frames.length - 1];
  drawBoard(frame.fen, frame.uci);
  for (const [where, side] of [["top", flipped ? "white" : "black"], ["bottom", flipped ? "black" : "white"]]) {
    $(`${where}-name`).textContent = engineName(game[side]);
    $(`${where}-clock`).textContent = clock(frame[`${side}_ms`]);
    $(`${where}-clock`).classList.toggle("turn", frame.fen.split(" ")[1] === side[0]);
  }
  $("position").textContent = frames.length > 1
    ? `Ply ${frames.length - 1} · ${frame.san}` : "Starting position";
  $("outcome").textContent = game.status === "completed"
    ? `${game.result === "draw" ? "½–½" : game.result === "white" ? "1–0" : "0–1"} · ${String(game.termination).replaceAll("_", " ")}`
    : game.status === "running" ? "Being played now" : game.status;

  const rows = new Map();
  frames.slice(1).forEach((entry, index) => {
    const before = frames[index].fen.split(" "), number = Number(before[5]);
    if (!rows.has(number)) rows.set(number, { number });
    rows.get(number)[before[1]] = entry.san;
  });
  $("moves").innerHTML = [...rows.values()].map((row) =>
    `<div class="move"><span>${row.number}.</span><span>${esc(row.w || "")}</span><span>${esc(row.b || "")}</span></div>`).join("");
  $("moves").scrollTop = $("moves").scrollHeight;
}

// ── wiring ────────────────────────────────────────────────────────────────────────────────

async function tick() {
  try {
    await refresh();
    if (current) await refreshRun();
  } catch (error) {
    $("runners").textContent = error.message;
  }
}

function start() {
  $("queue").addEventListener("click", queueRun);
  $("opponents").addEventListener("change", updateQueueNote);
  for (const id of ["candidate", "split", "positions", "parallel"]) {
    $(id).addEventListener("change", updateQueueNote);
  }
  $("runs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-run]");
    if (button) watch(button.dataset.run);
  });
  $("live-strip").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-live]");
    if (button) { gameId = button.dataset.live; following = true; refreshGame(); }
  });
  $("game-select").addEventListener("change", () => {
    gameId = $("game-select").value;
    following = false;
    $("follow").checked = false;
    refreshGame();
  });
  $("follow").addEventListener("change", () => { following = $("follow").checked; });
  $("flip").addEventListener("click", () => { flipped = !flipped; refreshGame(); });
  $("stop-run").addEventListener("click", async () => {
    try { await post(`/api/runs/${encodeURIComponent(current.id)}/stop`); await refreshRun(); }
    catch (error) { say(error.message, "bad"); }
  });
  tick();
  timer = setInterval(tick, 1500);
  window.addEventListener("beforeunload", () => clearInterval(timer));
}

start();
