"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const initialFen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const initialParams = new URLSearchParams(window.location.search);
let catalog, token, run, game, runId = initialParams.get("run") || "", gameId = "", flipped = false, following = true;
let frameIndex = 0, timer = null, polling = false, historyKey = "", gamesKey = "", moveKey = "";
let selectionRevision = 0, lastRender = null;
let play = null, playTimer = null, playRevision = 0, selected = "", targets = new Map();
let clockTimer = null, clockAnchor = null, resultSeen = false;
const files = "abcdefgh";
const HUMAN = "you";
const engine = (id) => id === HUMAN ? {name:"You", family:"Human player"}
  : run?.engines?.[id] || catalog?.engines.find((e) => e.id === id) || {name:id, family:""};
// Scrollbars are hidden, so a pane fades at its edge only while something is still below.
const fadeWhenScrollable = (element) =>
  element.classList.toggle("more-below", element.scrollHeight - element.scrollTop - element.clientHeight > 4);
function trackScrollable(element) {
  const update = () => fadeWhenScrollable(element);
  element.addEventListener("scroll", update, {passive:true});
  if (window.ResizeObserver) new ResizeObserver(update).observe(element);
  update();
}
const pct = (n) => n == null ? "—" : `${(n * 100).toFixed(1)}%`;
const failureNames = new Set(["crash", "illegal", "flag", "init", "both_failed"]);

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json", "X-CSRF-Token":token}, body:JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
function showError(error) {
  const message = error.message || String(error);
  $("error").textContent = message; $("error").hidden = false;
  for (const name of ["setup", "play"]) {
    if (!$(`${name}-dialog`).open) continue;
    $(`${name}-error`).textContent = message; $(`${name}-error`).hidden = false;
  }
}
function clearError() { for (const id of ["error", "setup-error", "play-error"]) $(id).hidden = true; }
function selectedOpponents() { return [...document.querySelectorAll("#opponents input:checked")].map((e) => e.value); }
function chosenOpenings() {
  const groups = new Map();
  for (const opening of catalog.openings.filter((o) => o.split === $("split").value)) {
    if (!groups.has(opening.family)) groups.set(opening.family, []);
    groups.get(opening.family).push(opening);
  }
  // Round-robin over families before taking a second position from any family.
  const spread = [];
  for (let i = 0; i < 4; i++) for (const values of groups.values()) if (values[i]) spread.push(values[i]);
  return spread.slice(0, $("opening-count").value === "all" ? spread.length : Number($("opening-count").value));
}
function updateSetup() {
  const ids = selectedOpponents();
  const families = new Set(ids.map((id) => catalog.engines.find((e) => e.id === id).family));
  const openings = chosenOpenings();
  $("pool-count").textContent = ids.length;
  $("diversity").textContent = `${ids.length} opponents · ${families.size} engine families`;
  $("opening-preview").textContent = [...new Set(openings.map((o) => o.family))].join(" · ");
  const total = ids.length * openings.length * 2, atOnce = Number($("parallel").value);
  $("game-count").textContent = `${total} games`;
  $("launch-note").textContent = atOnce > 1
    ? `${atOnce} at once · clocks not comparable with the event` : "One at a time";
  updateCompleteTest();
}
function renderOpponents() {
  const previous = catalog ? selectedOpponents() : [];
  $("opponents").innerHTML = catalog.engines.filter((e) => e.id !== $("candidate").value).map((e) => {
    const checked = previous.length ? previous.includes(e.id) : ["greedy", "minimax", "positional", "rollout"].includes(e.id);
    return `<label class="opponent-option ${e.available ? "" : "unavailable"}" title="${esc(e.available ? e.description : e.reason)}"><input type="checkbox" value="${esc(e.id)}" ${e.available ? (checked ? "checked" : "") : "disabled"}><span><strong>${esc(e.name)}</strong><small>${esc(e.available ? e.family : e.reason)}</small></span><span class="engine-kind">${e.kind === "uci" ? "UCI" : "PY"}</span></label>`;
  }).join("");
  fadeWhenScrollable($("opponents"));
  updateSetup();
}
function clock(value) {
  if (value == null) return "—";
  const seconds = Math.max(0, value / 1000);
  return `${Math.floor(seconds / 60)}:${Math.floor(seconds % 60).toString().padStart(2,"0")}${seconds < 10 ? "." + Math.floor((seconds % 1) * 10) : ""}`;
}
function currentFrame() { return game?.frames?.[frameIndex] || {fen:initialFen}; }
function piecesOf(fen) {
  const pieces = {};
  fen.split(" ")[0].split("/").forEach((row, r) => {
    let file = 0;
    for (const symbol of row) {
      if (/\d/.test(symbol)) file += Number(symbol);
      else { pieces[files[file] + (8-r)] = symbol; file++; }
    }
  });
  return pieces;
}
const PIECE_NAMES = {p:"pawn", n:"knight", b:"bishop", r:"rook", q:"queen", k:"king"};
const columnOf = (square, flip) => flip ? 7 - files.indexOf(square[0]) : files.indexOf(square[0]);
const rowOf = (square, flip) => flip ? Number(square[1]) - 1 : 8 - Number(square[1]);
// The arena board and the game window draw from here, so there is one board renderer.
function boardHtml(fen, uci, flip) {
  const pieces = piecesOf(fen), last = [uci?.slice(0,2), uci?.slice(2,4)];
  let html = "";
  for (let row = 0; row < 8; row++) for (let col = 0; col < 8; col++) {
    const file = flip ? 7-col : col, rank = flip ? row+1 : 8-row;
    const square = files[file] + rank, piece = pieces[square];
    const alt = piece
      ? `${piece === piece.toUpperCase() ? "White" : "Black"} ${PIECE_NAMES[piece.toLowerCase()]} on ${square}`
      : "";
    html += `<div class="square ${(file + rank) % 2 === 1 ? "dark" : ""} ${last.includes(square) ? "last" : ""}" data-square="${square}">${col === 0 ? `<span class="coordinate rank">${rank}</span>` : ""}${row === 7 ? `<span class="coordinate file">${files[file]}</span>` : ""}${piece ? `<img src="/pieces/${piece}.svg" alt="${alt}" draggable="false">` : ""}</div>`;
  }
  return html;
}
// One ply, same game, same orientation: slide the piece that moved instead of cutting across.
function movePlan(frames, from, to, beforeFen, flip) {
  if (!frames || from === null || from === to) return null;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return null;
  const forward = to > from;
  const uci = forward ? frames[to]?.uci : from - to === 1 ? frames[from]?.uci : null;
  if (!uci || uci.length < 4) return null;
  const start = uci.slice(0,2), end = uci.slice(2,4);
  const before = piecesOf(beforeFen), origin = forward ? start : end, mover = before[origin];
  if (!mover) return null;
  const slides = [[origin, forward ? end : start]], ghosts = [];
  if (mover.toLowerCase() === "k" && Math.abs(files.indexOf(start[0]) - files.indexOf(end[0])) === 2) {
    const rank = start[1], corner = (end[0] === "g" ? "h" : "a") + rank;
    const landing = (end[0] === "g" ? "f" : "d") + rank;
    slides.push(forward ? [corner, landing] : [landing, corner]);
  }
  if (forward && before[end]) ghosts.push([end, before[end]]);
  else if (forward && mover.toLowerCase() === "p" && start[0] !== end[0] && before[end[0] + start[1]]) {
    ghosts.push([end[0] + start[1], before[end[0] + start[1]]]);   // the pawn taken en passant
  }
  return {slides, ghosts, flip};
}
// Web Animations rather than a CSS transition: nothing is left stranded if a frame never lands.
function animateMove(board, plan) {
  const size = board.getBoundingClientRect().width / 8;
  if (!plan || !size || !board.animate) return;
  for (const [square, symbol] of plan.ghosts) {
    const ghost = document.createElement("img");
    ghost.className = "capture-ghost"; ghost.src = `/pieces/${symbol}.svg`; ghost.alt = "";
    board.querySelector(`[data-square="${square}"]`)?.append(ghost);
    const fading = ghost.animate([{opacity:1}, {opacity:0, transform:"scale(.7)"}],
      {duration:180, easing:"ease-in", fill:"forwards"});
    fading.finished.then(() => ghost.remove(), () => ghost.remove());
  }
  for (const [start, end] of plan.slides) {
    const square = board.querySelector(`[data-square="${end}"]`), piece = square?.querySelector("img");
    if (!piece) continue;
    const dx = (columnOf(start, plan.flip) - columnOf(end, plan.flip)) * size;
    const dy = (rowOf(start, plan.flip) - rowOf(end, plan.flip)) * size;
    square.classList.add("moving");
    const sliding = piece.animate([{transform:`translate(${dx}px, ${dy}px)`}, {transform:"none"}],
      {duration:190, easing:"cubic-bezier(.22, .61, .36, 1)"});
    const settle = () => square.classList.remove("moving");
    sliding.finished.then(settle, settle);
  }
}
function movesHtml(frames) {
  const rows = new Map();
  frames.slice(1).forEach((frame, index) => {
    const before = frames[index].fen.split(" "), number = Number(before[5]);
    if (!rows.has(number)) rows.set(number, {number});
    rows.get(number)[before[1]] = {san:frame.san, index:index+1, ms:Math.round(frame.elapsed_ms)};
  });
  if (!rows.size) return '<p class="empty-moves">Waiting for the first move.</p>';
  return [...rows.values()].map((row) => `<div class="move-row"><span class="move-number">${row.number}.</span>${["w","b"].map((side) => row[side]
    ? `<button data-frame="${row[side].index}" title="Go to ${esc(row[side].san)} · ${row[side].ms} ms">${esc(row[side].san)}</button>`
    : "<span></span>").join("")}</div>`).join("");
}
function renderBoard() {
  const frame = currentFrame(), fen = frame.fen;
  const boardKey = `${fen}:${frame.uci}:${flipped}`;
  if ($("board").dataset.key !== boardKey) {
    // With no game there is nothing to animate between, and two null renders in a row
    // used to compare undefined against undefined and then reach for its frames.
    const sameGame = game && lastRender && lastRender.gameId === game.id
      && lastRender.flipped === flipped;
    const plan = sameGame
      ? movePlan(game.frames, lastRender.index, frameIndex, lastRender.fen, flipped) : null;
    $("board").innerHTML = boardHtml(fen, frame.uci, flipped);
    $("board").dataset.key = boardKey;
    animateMove($("board"), plan);
  }
  lastRender = {gameId:game?.id, index:frameIndex, fen, flipped};
  renderSelection();
  $("board").setAttribute("aria-label", `Chessboard, ${fen.split(" ")[1] === "w" ? "White" : "Black"} to move, ${frame.san || "starting position"}`);
  for (const [location, side] of [["top",flipped ? "white" : "black"],["bottom",flipped ? "black" : "white"]]) {
    const spec = game ? engine(game[side]) : {name:side === "white" ? "White" : "Black",family:"Waiting for a game"};
    $(`${location}-name`).textContent = spec.name;
    $(`${location}-family`).textContent = spec.family;
    $(`${location}-clock`).textContent = clock(frame[`${side}_ms`]);
    $(`${location}-clock`).classList.toggle("turn", Boolean(game) && fen.split(" ")[1] === side[0]);
    $(`${location}-clock`).classList.remove("low");
    $(`${location}-clock`).title = "";
    $(`${location}-dot`).className = `piece-dot ${side}`;
  }
  // frameIndex outlives the game it was an index into: selecting a run that has no games yet
  // leaves it pointing at the last one. Without a game there is no ply to name.
  $("position-number").textContent = game && frameIndex
    ? `Ply ${frameIndex} / ${game.frames.length-1} · ${frame.san}` : "Starting position";
  $("move-detail").textContent = frameIndex ? `Last move: ${Math.round(frame.elapsed_ms)} ms · recorded clocks` : "Clocks recorded after each move";
  $("follow").classList.toggle("active", following);
  $("follow").setAttribute("aria-pressed", String(following));
  for (const id of ["first","previous"]) $(id).disabled = !game || frameIndex === 0;
  for (const id of ["next","last"]) $(id).disabled = !game || frameIndex >= game.frames.length-1;
  $("play").disabled = !game || game.frames.length <= 1;
  document.querySelectorAll("#moves button").forEach((button) => button.classList.toggle("selected", Number(button.dataset.frame) === frameIndex));
}
function follow(value) { following = value; if (value && game) frameIndex = game.frames.length-1; renderBoard(); }
function selectFrame(index) { following = false; frameIndex = Math.max(0,Math.min(index, (game?.frames.length || 1)-1)); renderBoard(); }
function pauseReplay() { clearInterval(timer); timer = null; $("play").textContent = "▶"; $("play").setAttribute("aria-label","Play replay"); }
function outcomeText(g) {
  if (g.status === "running") return "Playing · " + (g.frames?.at(-1)?.fen.split(" ")[1] === "w" ? "White" : "Black") + " to move";
  if (g.status !== "completed") return g.status === "queued" ? "Queued for play" : g.status;
  const result = g.result === "draw" ? "½–½" : g.result === "white" ? "1–0" : g.result === "black" ? "0–1" : "Void";
  return `${result} · ${g.termination.replaceAll("_"," ")}`;
}
function renderGame() {
  if (!game) { renderBoard(); return; }
  $("opening-name").textContent = game.opening.name;
  $("game-meta").textContent = `${game.opening.family} · ${game.opening.split} / ${game.id}`;
  $("outcome").textContent = outcomeText(game);
  $("download-pgn").disabled = !game.pgn;
  const key = `${game.id}:${game.frames.length}`;
  if (moveKey !== key) {
    $("moves").innerHTML = movesHtml(game.frames);
    moveKey = key;
    if (following) $("moves").scrollTop = $("moves").scrollHeight;
    fadeWhenScrollable($("moves"));
  }
  renderBoard();
  $("panel-eyebrow").textContent = play ? "Your game" : "Game viewer";
  document.querySelector(".provenance summary").textContent =
    play ? "Engine output" : "Build provenance & game logs";
  if (run) $("provenance").textContent = JSON.stringify({run_id:run.id, environment:run.environment,
    limits:run.limits, engines:run.engines, game_engine_info:game.engine_info, game_logs:game.logs}, null, 2);
}
function mapClass(g) {
  if (g.status === "running") return "running";
  if (g.status !== "completed" || g.result === "void") return "pending";
  if (g.result === "draw") return "draw";
  return g[g.result] === run.candidate ? "win" : "loss";
}
function renderRun() {
  if (!run) return;
  $("run-title").textContent = run.label;
  $("arena-eyebrow").textContent = "Research arena";
  $("arena-subtitle").textContent = "A closer look at how your engines play.";
  $("run-status").textContent = run.status;
  $("run-status").className = `status ${run.status}`;
  const completed = run.games.filter((g) => g.status === "completed").length;
  const atOnce = run.environment?.parallel_games || 1;
  $("run-progress").textContent = `${completed} / ${run.games.length} games · ${run.limits.base_ms/1000}s + ${run.limits.increment_ms/1000}s`
    + (atOnce > 1 ? ` · ${atOnce} at once` : "");
  $("run-eta").textContent = ["running","stopping"].includes(run.status) ? progressNote(run) : "";
  const active = ["running","preparing","stopping"].includes(run.status);
  // Anything left unplayed can be picked up again, whether it was stopped or cut off.
  const left = run.games.filter((g) => g.status !== "completed").length;
  $("resume").hidden = active || !left;
  $("resume").disabled = Boolean(play);
  $("resume-count").textContent = left ? ` ${left} game${left === 1 ? "" : "s"}` : "";
  $("stop").hidden = !active;
  $("stop").disabled = run.status === "stopping";
  $("stop").textContent = run.status === "stopping" ? "Finishing current game…" : "Stop after this game";
  $("export").hidden = active;
  $("export").href = `/api/export?run=${encodeURIComponent(run.id)}`;
  if (run.error) showError(new Error(run.error));
  const key = run.id + ":" + run.games.map((g) => g.status + g.result).join(",");
  if (gamesKey !== key) {
    $("game-select").innerHTML = run.games.map((g,i) => `<option value="${g.id}">${i+1}. ${esc(g.opening.name)} · ${esc(engine(g.opponent).name)} · ${g.white === run.candidate ? "W" : "B"}${g.status === "completed" ? " · " + outcomeText(g) : " · " + g.status}</option>`).join("");
    gamesKey = key;
  }
  $("game-select").value = gameId;
  const summaries = run.summary || [];
  $("results-body").innerHTML = summaries.map((row) => `<tr><td><strong>${esc(engine(row.opponent).name)}</strong><small>${esc(engine(row.opponent).family)}</small></td><td>${row.wins} / ${row.draws} / ${row.losses}</td><td>${row.pairs}</td><td class="score-value">${pct(row.score)}</td><td title="${esc(row.interval_method)}">${row.interval ? `${Math.round(row.interval[0]*100)}–${Math.round(row.interval[1]*100)}%` : "—"}</td><td class="${row.candidate_failures ? "failure-count" : ""}" title="Candidate clock, crash, illegal move or init failures, including draws and void games">${row.candidate_failures}${row.void ? ` (+${row.void} void)` : ""}</td></tr>`).join("");
  const families = new Set(summaries.map((r) => engine(r.opponent).family));
  $("coverage-count").textContent = `${summaries.length} OPPONENTS / ${families.size} FAMILIES`;
  $("contention").hidden = atOnce < 2;
  $("stats-note").textContent = "Pair score uses complete colour pairs; W/D/L includes unpaired games. The conservative 95% bound assumes independent opening pairs. Public development positions can be correlated. Treat these as exploratory results." + (run.limits.ply_cap < 600 ? " Shortened games: use full games to assess strength." : "");
  $("game-map").innerHTML = run.games.map((g,i) => `<button class="${mapClass(g)} ${g.id === gameId ? "selected" : ""}" data-game="${g.id}" aria-label="Game ${i+1}: ${esc(engine(g.opponent).name)}, ${esc(g.opening.name)}, ${esc(outcomeText(g))}" title="${i+1}. ${esc(engine(g.opponent).name)} / ${esc(g.opening.name)} / ${esc(outcomeText(g))}">${g.status === "completed" && failureNames.has(g.termination) ? "!" : ""}</button>`).join("");
}
async function poll() {
  if (polling) return;
  polling = true;
  const revision = selectionRevision;
  try {
    const state = await api(`/api/state${runId ? "?run=" + encodeURIComponent(runId) : ""}`);
    if (revision !== selectionRevision) return;
    const conn = $("connection");
    if (conn) {
      conn.classList.remove("offline");
      conn.innerHTML = "<i></i> Local workspace";
    }
    $("start").disabled = state.busy || playing();
    $("nav-status").textContent = state.busy ? "Live" : "Idle";
    const history = JSON.stringify(state.runs);
    if (historyKey !== history) {
      $("history").innerHTML = state.runs.length ? state.runs.map((r) => `<option value="${r.id}">${esc(r.label)} · ${r.status}</option>`).join("") : '<option value="">No experiments yet</option>';
      $("experiment-list").innerHTML = state.runs.length ? state.runs.map((r) => `<button class="experiment-item" data-run="${r.id}"><span><strong>${esc(r.label)}</strong><small>${new Date(r.created_at*1000).toLocaleDateString(undefined,{month:"short",day:"numeric"})} · ${esc(r.status)}</small></span></button>`).join("") : '<p class="sidebar-empty">Start an experiment to build your research history.</p>';
      historyKey = history;
      fadeWhenScrollable($("experiment-list"));
    }
    run = state.run;
    if (state.playing && !playing()) {
      const session = (await api("/api/play")).play;
      if (session) { enterPlay(session); return; }
    }
    if (playing()) return;                 // while a game is on, the arena belongs to it
    if (run) {
      runId = run.id; $("history").value = runId;
      document.querySelectorAll(".experiment-item").forEach((item) => { item.classList.toggle("selected",item.dataset.run === runId); item.setAttribute("aria-current",String(item.dataset.run === runId)); });
      const live = run.games.filter((g) => g.status === "running");
      const chosen = live.find((g) => g.id === preferredLive) || live[0];
      if (following && chosen) gameId = chosen.id;
      renderLiveMatches(run);
      if (!gameId || !run.games.some((g) => g.id === gameId)) gameId = run.games[0].id;
      const detail = await api(`/api/game?run=${encodeURIComponent(runId)}&game=${encodeURIComponent(gameId)}`);
      if (revision !== selectionRevision) return;
      game = detail;
      if (following) frameIndex = game.frames.length-1;
      else frameIndex = Math.min(frameIndex, game.frames.length-1);
      renderRun(); renderGame();
    }
  } catch (error) {
    const conn = $("connection");
    if (conn) {
      conn.classList.add("offline");
      conn.textContent = "Connection lost";
    }
    showError(error);
  } finally { polling = false; }
}
async function chooseGame(id) { selectionRevision++; pauseReplay(); following = false; gameId = id; frameIndex = 0; moveKey = ""; await poll(); }

// ── Adding an agent from the browser ──────────────────────────────────────────────────────
const engineKind = () => document.querySelector("input[name=engine-kind]:checked").value;
const listField = (value) => value.split(",").map((item) => item.trim()).filter(Boolean);
const slug = (value) => value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
function renderRegistry() {
  $("registry-count").textContent = catalog.engines.length;
  const ready = catalog.engines.filter((e) => e.available).length;
  $("registry-summary").textContent = `${ready} of ${catalog.engines.length} ready to play`;
  $("registry-engines").innerHTML = catalog.engines.map((e) => `<div class="registry-engine" title="${esc(e.available ? e.description : e.reason)}"><span class="engine-avatar" aria-hidden="true">${e.kind === "uci" ? "♜" : "♘"}</span><div><strong>${esc(e.name)}</strong><small>${esc(e.family)} · ${e.kind === "uci" ? "UCI engine" : "Python agent"}${e.custom ? " · added here" : ""}</small></div><span class="availability ${e.available ? "" : "missing"}">${e.available ? "Ready" : "Unavailable"}</span>${e.custom ? `<button type="button" class="remove-engine" data-engine="${esc(e.id)}" aria-label="Remove ${esc(e.name)}" title="Remove ${esc(e.name)}">Remove</button>` : ""}</div>`).join("");
}
function applyCatalog(next) {
  catalog = {...catalog, ...next};
  renderRegistry();
  renderOpponents();
  const candidate = $("candidate").value;
  $("candidate").innerHTML = catalog.engines.filter((e) => e.available)
    .map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join("");
  if (catalog.engines.some((e) => e.id === candidate && e.available)) $("candidate").value = candidate;
  renderPlayOptions();
}

function syncEngineKind() {
  const uci = engineKind() === "uci";
  $("engine-path-field").hidden = uci;
  $("engine-command-field").hidden = !uci;
  $("engine-requires-field").hidden = uci;
  $("engine-path").required = !uci;
  $("engine-command").required = uci;
  $("engine-hint").innerHTML = uci
    ? "The executable has to be on PATH or given as a full path. External engines are for research only."
    : "Paths are relative to the repository root. The folder needs an <code>agent.py</code> exposing <code>get_move</code>.";
}
function openRegistry(paths) {
  clearError();
  if (!$("registry-dialog").open) $("registry-dialog").showModal();
  if (paths) $("registry-dialog").querySelector(".path-form").open = true;
}
function bindRegistry() {
  bindDrop();
  $("open-registry").addEventListener("click", () => openRegistry(false));
  $("add-from-setup").addEventListener("click", () => { $("setup-dialog").close(); openRegistry(false); });
  for (const input of document.querySelectorAll("input[name=engine-kind]")) {
    input.addEventListener("change", syncEngineKind);
  }
  // The id is derived from the name until someone types their own.
  $("engine-name").addEventListener("input", () => {
    if (!$("engine-id").dataset.edited) $("engine-id").value = slug($("engine-name").value);
  });
  $("engine-id").addEventListener("input", () => { $("engine-id").dataset.edited = "yes"; });
  $("registry-engines").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-engine]");
    if (!button) return;
    button.disabled = true;
    try {
      applyCatalog((await api("/api/engines/remove", {id: button.dataset.engine})).catalog);
    } catch (error) { showError(error); button.disabled = false; }
  });
  $("engine-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("engine-error").hidden = true;
    $("engine-save").disabled = true;
    const uci = engineKind() === "uci";
    const request = {
      id: slug($("engine-id").value),
      name: $("engine-name").value.trim(),
      family: $("engine-family").value.trim(),
      kind: uci ? "uci" : "python",
      description: $("engine-description").value.trim(),
    };
    if (uci) request.command = [$("engine-command").value.trim()];
    else {
      request.path = $("engine-path").value.trim();
      request.requires = listField($("engine-requires").value);
    }
    try {
      const result = await api("/api/engines", request);
      applyCatalog(result.catalog);
      $("engine-form").reset();
      delete $("engine-id").dataset.edited;
      syncEngineKind();
      $("registry-dialog").querySelector(".path-form").open = false;
    } catch (error) {
      $("engine-error").textContent = error.message || String(error);
      $("engine-error").hidden = false;
    } finally { $("engine-save").disabled = false; }
  });
  syncEngineKind();
}

// ── Running many games at once ────────────────────────────────────────────────────────────
// Above one game at a time the machine is shared, so nothing here pretends the clocks still
// compare with the event; the run records what it actually did and the results say so.
let preferredLive = "", dataView = false;

const availableOpponents = () =>
  (catalog?.engines || []).filter((e) => e.available && e.id !== $("candidate").value);
const splitOpenings = () =>
  (catalog?.openings || []).filter((o) => o.split === $("split").value);

function updateCompleteTest() {
  const games = availableOpponents().length * splitOpenings().length * 2;
  $("complete-count").textContent = games ? ` · ${games} games` : "";
  $("complete-test").disabled = !games;
  $("complete-test").title = games
    ? `Every ready opponent against every ${$("split").value} position, both colours`
    : "No opponents are ready";
}
function applyCompleteTest() {
  document.querySelectorAll("#opponents input:not(:disabled)").forEach((box) => box.checked = true);
  $("opening-count").value = "all";
  $("clock-preset").value = "smoke";
  $("clock-preset").dispatchEvent(new Event("change"));
  $("ply-cap").value = "600";                 // full games; the clock is what makes it quick
  $("parallel").value = "4";
  $("label").value = `Complete test · ${$("split").value}`;
  updateSetup();
}
function setDataView(on) {
  dataView = on;
  document.body.classList.toggle("data-mode", on);
  $("data-view").textContent = on ? "Board view" : "Data view";
  $("data-view").setAttribute("aria-pressed", String(on));
}
// A rate from the games that have finished beats a guess made before the batch started.
function progressNote(runNow) {
  const done = runNow.games.filter((g) => g.status === "completed");
  const left = runNow.games.length - done.length;
  if (!left) return "";
  const stamps = done.map((g) => g.finished_at).filter(Boolean);
  if (stamps.length < 2) return "";
  const first = runNow.games.map((g) => g.started_at).filter(Boolean).sort()[0];
  const elapsed = Math.max(...stamps) - (first ?? runNow.created_at);
  if (!(elapsed > 0)) return "";
  const remaining = (elapsed / done.length) * left;
  return `about ${remaining < 90 ? `${Math.ceil(remaining)}s` : `${Math.ceil(remaining / 60)} min`} left`;
}
function renderLiveMatches(runNow) {
  const live = runNow.games.filter((game) => game.status === "running");
  $("live-matches").hidden = live.length < 2 || dataView;
  if (live.length < 2) return;
  $("live-matches").innerHTML = live.map((game) => {
    const watching = game.id === gameId;
    return `<button type="button" class="live-chip ${watching ? "selected" : ""}" data-live="${esc(game.id)}" aria-pressed="${watching}"><strong>${esc(engine(game.opponent).name)}</strong><small>${esc(game.opening.name)} · ${game.white === runNow.candidate ? "candidate white" : "candidate black"}</small></button>`;
  }).join("");
}
function bindSweep() {
  $("complete-test").addEventListener("click", applyCompleteTest);
  $("data-view").addEventListener("click", () => setDataView(!dataView));
  for (const id of ["candidate", "split"]) $(id).addEventListener("change", updateCompleteTest);
  $("live-matches").addEventListener("click", (event) => {
    const chip = event.target.closest("button[data-live]");
    if (!chip) return;
    preferredLive = chip.dataset.live;
    gameId = preferredLive;
    following = true;
    moveKey = "";
    poll();
  });
}

// ── One game, opened on its own ───────────────────────────────────────────────────────────
// The game map opens a game here rather than taking over the arena, so a live batch keeps
// running and keeps showing the game it is playing while you read an older one.
let detail = null, detailIndex = 0, detailFlip = false, detailTimer = null, detailShown = null;

const seconds = (ms) => ms >= 10000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
function sideTiming(frames) {
  const sides = {w:[], b:[]};
  frames.slice(1).forEach((frame, index) => sides[frames[index].fen.split(" ")[1]]?.push(frame));
  const stat = (list) => list.length
    ? {
        moves: list.length,
        total: list.reduce((sum, frame) => sum + frame.elapsed_ms, 0),
        slowest: list.reduce((worst, frame) => frame.elapsed_ms > worst.elapsed_ms ? frame : worst),
      }
    : {moves:0, total:0, slowest:null};
  return {white: stat(sides.w), black: stat(sides.b)};
}
function factRow(term, value) {
  return `<div><dt>${esc(term)}</dt><dd>${value}</dd></div>`;
}
function renderFacts() {
  const timing = sideTiming(detail.frames), limits = run?.limits;
  const rows = [
    factRow("Opening", `${esc(detail.opening.name)}<small>${esc(detail.opening.family)} · ${esc(detail.opening.split)} split</small>`),
    factRow("Moves", `${detail.frames.length - 1} plies<small>${detail.status === "completed" ? esc(String(detail.termination).replaceAll("_", " ")) : esc(detail.status)}</small>`),
  ];
  for (const side of ["white", "black"]) {
    const spec = engine(detail[side]), spent = timing[side];
    const badge = detail[side] === run?.candidate ? " · candidate" : "";
    const slowest = spent.slowest
      ? `<small>slowest ${esc(spent.slowest.san)} at ${seconds(spent.slowest.elapsed_ms)}</small>` : "";
    rows.push(factRow(side === "white" ? "White" : "Black",
      `${esc(spec.name)}${badge}<small>${seconds(spent.total)} over ${spent.moves} moves</small>${slowest}`));
  }
  if (limits) {
    rows.push(factRow("Clock", `${limits.base_ms / 1000}s + ${limits.increment_ms / 1000}s<small>ply cap ${limits.ply_cap}</small>`));
  }
  rows.push(factRow("Recorded as", `${esc(detail.id)}<small>pair ${esc(detail.pair_id || "—")} · seed ${esc(String(detail.seed ?? "—"))}</small>`));
  $("g-facts").innerHTML = rows.join("");
}
function renderDetail() {
  if (!detail) return;
  const frame = detail.frames[detailIndex] || {fen:initialFen};
  const key = `${detail.id}:${frame.fen}:${frame.uci}:${detailFlip}`;
  if ($("g-board").dataset.key !== key) {
    const same = detailShown && detailShown.id === detail.id && detailShown.flip === detailFlip;
    const plan = same
      ? movePlan(detail.frames, detailShown.index, detailIndex, detailShown.fen, detailFlip) : null;
    $("g-board").innerHTML = boardHtml(frame.fen, frame.uci, detailFlip);
    $("g-board").dataset.key = key;
    animateMove($("g-board"), plan);
  }
  detailShown = {id:detail.id, index:detailIndex, fen:frame.fen, flip:detailFlip};
  $("g-board").setAttribute("aria-label",
    `Chessboard, ${frame.fen.split(" ")[1] === "w" ? "White" : "Black"} to move, ${frame.san || "starting position"}`);
  for (const [where, side] of [["top", detailFlip ? "white" : "black"], ["bottom", detailFlip ? "black" : "white"]]) {
    const spec = engine(detail[side]);
    $(`g-${where}-name`).textContent = spec.name;
    $(`g-${where}-family`).textContent = spec.family;
    $(`g-${where}-clock`).textContent = clock(frame[`${side}_ms`]);
    $(`g-${where}-clock`).classList.toggle("turn", frame.fen.split(" ")[1] === side[0]);
    $(`g-${where}-dot`).className = `piece-dot ${side}`;
  }
  $("g-position").textContent = detailIndex
    ? `Ply ${detailIndex} / ${detail.frames.length - 1} · ${detail.frames[detailIndex].san}`
    : "Starting position";
  $("g-timing").textContent = detailIndex ? `took ${seconds(detail.frames[detailIndex].elapsed_ms)}` : "";
  for (const id of ["g-first", "g-previous"]) $(id).disabled = detailIndex === 0;
  for (const id of ["g-next", "g-last"]) $(id).disabled = detailIndex >= detail.frames.length - 1;
  $("g-play").disabled = detail.frames.length <= 1;
  document.querySelectorAll("#g-moves button").forEach((button) =>
    button.classList.toggle("selected", Number(button.dataset.frame) === detailIndex));
  const selected = $("g-moves").querySelector("button.selected");
  if (selected) selected.scrollIntoView({block:"nearest"});
}
function detailTone() {
  if (detail.status !== "completed" || detail.result === "void") return "";
  if (detail.result === "draw") return "drawn";
  return detail[detail.result] === run?.candidate ? "won" : "lost";
}
function showGame(loaded, index) {
  detail = loaded;
  detailShown = null;
  detailIndex = Math.max(0, Math.min(index ?? loaded.frames.length - 1, loaded.frames.length - 1));
  detailFlip = run ? loaded.black === run.candidate : false;
  const position = run ? run.games.findIndex((game) => game.id === loaded.id) + 1 : 0;
  $("game-eyebrow").textContent = position
    ? `Game ${position} of ${run.games.length}` : "Game";
  $("game-dialog-title").textContent = loaded.opening.name;
  $("game-dialog-meta").textContent =
    `${engine(loaded.white).name} versus ${engine(loaded.black).name}`;
  $("g-outcome").textContent = outcomeText(loaded);
  $("g-outcome").className = `outcome ${detailTone()}`;
  $("g-moves").innerHTML = movesHtml(loaded.frames);
  renderFacts();
  $("g-note").textContent = loaded.pgn ? "Clocks are the ones the referee recorded." : "Still being played.";
  $("g-pgn").disabled = !loaded.pgn;
  $("g-logs").textContent = JSON.stringify(
    {engines: loaded.engine_info, logs: loaded.logs, seed: loaded.seed}, null, 2);
  renderDetail();
  if (!$("game-dialog").open) $("game-dialog").showModal();
}
async function openGame(gameId, index) {
  pauseDetail();
  try {
    showGame(await api(`/api/game?run=${encodeURIComponent(runId)}&game=${encodeURIComponent(gameId)}`), index);
  } catch (error) { showError(error); }
}
function pauseDetail() {
  clearInterval(detailTimer);
  detailTimer = null;
  $("g-play").textContent = "▶";
  $("g-play").setAttribute("aria-label", "Play replay");
}
function stepDetail(index) {
  if (!detail) return;
  detailIndex = Math.max(0, Math.min(index, detail.frames.length - 1));
  renderDetail();
}
function bindGameWindow() {
  $("g-flip").addEventListener("click", () => { detailFlip = !detailFlip; detailShown = null; renderDetail(); });
  $("g-first").addEventListener("click", () => { pauseDetail(); stepDetail(0); });
  $("g-previous").addEventListener("click", () => { pauseDetail(); stepDetail(detailIndex - 1); });
  $("g-next").addEventListener("click", () => { pauseDetail(); stepDetail(detailIndex + 1); });
  $("g-last").addEventListener("click", () => { pauseDetail(); stepDetail(detail.frames.length - 1); });
  $("g-play").addEventListener("click", () => {
    if (detailTimer) { pauseDetail(); return; }
    if (detailIndex >= detail.frames.length - 1) stepDetail(0);
    $("g-play").textContent = "Ⅱ";
    $("g-play").setAttribute("aria-label", "Pause replay");
    detailTimer = setInterval(() => {
      if (detailIndex >= detail.frames.length - 1) pauseDetail();
      else stepDetail(detailIndex + 1);
    }, 650);
  });
  $("g-moves").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-frame]");
    if (target) { pauseDetail(); stepDetail(Number(target.dataset.frame)); }
  });
  $("g-copy-fen").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(detail.frames[detailIndex].fen);
      $("g-copy-fen").textContent = "Copied";
      setTimeout(() => $("g-copy-fen").textContent = "Copy FEN", 1500);
    } catch { showError(new Error("Clipboard unavailable. The FEN is in the exported game JSON.")); }
  });
  $("g-pgn").addEventListener("click", () => savePgn(detail));
  $("g-open").addEventListener("click", () => {
    const id = detail.id;
    $("game-dialog").close();
    chooseGame(id);
  });
  $("game-dialog").addEventListener("close", pauseDetail);
  $("game-dialog").addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    if (["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
    event.preventDefault();
    pauseDetail();
    stepDetail(detailIndex + (event.key === "ArrowLeft" ? -1 : 1));
  });
}
function savePgn(source) {
  if (!source?.pgn) return;
  const url = URL.createObjectURL(new Blob([source.pgn], {type:"application/x-chess-pgn"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${runId || "game"}-${source.id}.pgn`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ── Dropping an agent in ──────────────────────────────────────────────────────────────────
// Whatever is dropped becomes the zip `make zip` would have produced, and the server checks it
// the way the platform does. A dropped file has no path the browser will share, so the bytes
// travel rather than a reference to them.
const SKIP = new Set([
  "__pycache__", ".git", ".venv", ".cache", ".chesslab", ".tools",
  ".mypy_cache", ".ruff_cache", "node_modules", ".DS_Store", ".idea", ".vscode",
]);
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index++) {
    let value = index;
    for (let bit = 0; bit < 8; bit++) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    table[index] = value;
  }
  return table;
})();
const crc32 = (bytes) => {
  let crc = 0xffffffff;
  for (let index = 0; index < bytes.length; index++) crc = CRC_TABLE[(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
};
const size = (value) => value < 1000 ? `${value} B`
  : value < 1000000 ? `${(value / 1000).toFixed(1)} kB` : `${(value / 1000000).toFixed(1)} MB`;
let staged = null;
let dragDepth = 0;

async function deflateRaw(bytes) {
  if (typeof CompressionStream === "undefined") return bytes;
  const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}
// Same shape as ladder/public/zipper.js: fixed timestamps, so re-zipping is a pure function.
async function buildZip(files) {
  const encoder = new TextEncoder(), parts = [], directory = [];
  let offset = 0;
  for (const file of files) {
    const name = encoder.encode(file.name);
    const raw = new Uint8Array(await file.blob.arrayBuffer());
    const crc = crc32(raw);
    const packed = raw.length ? await deflateRaw(raw) : new Uint8Array(0);
    const deflated = packed.length < raw.length;
    const body = deflated ? packed : raw, method = deflated ? 8 : 0;
    const header = new DataView(new ArrayBuffer(30));
    header.setUint32(0, 0x04034b50, true); header.setUint16(4, 20, true);
    header.setUint16(6, 0x0800, true); header.setUint16(8, method, true);
    header.setUint16(10, 0, true); header.setUint16(12, 33, true);
    header.setUint32(14, crc, true); header.setUint32(18, body.length, true);
    header.setUint32(22, raw.length, true); header.setUint16(26, name.length, true);
    parts.push(new Uint8Array(header.buffer), name, body);
    const entry = new DataView(new ArrayBuffer(46));
    entry.setUint32(0, 0x02014b50, true); entry.setUint16(4, 20, true); entry.setUint16(6, 20, true);
    entry.setUint16(8, 0x0800, true); entry.setUint16(10, method, true);
    entry.setUint16(12, 0, true); entry.setUint16(14, 33, true);
    entry.setUint32(16, crc, true); entry.setUint32(20, body.length, true);
    entry.setUint32(24, raw.length, true); entry.setUint16(28, name.length, true);
    entry.setUint32(38, 0o100644 << 16, true); entry.setUint32(42, offset, true);
    directory.push(new Uint8Array(entry.buffer), name);
    offset += 30 + name.length + body.length;
  }
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, directory.reduce((total, chunk) => total + chunk.length, 0), true);
  end.setUint32(16, offset, true);
  return new Blob([...parts, ...directory, new Uint8Array(end.buffer)], {type:"application/zip"});
}
function readEntries(reader) {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}
async function walkEntry(entry, prefix, out) {
  if (SKIP.has(entry.name)) return;
  const path = prefix ? `${prefix}/${entry.name}` : entry.name;
  if (entry.isFile) {
    out.push({name:path, blob: await new Promise((resolve, reject) => entry.file(resolve, reject))});
    return;
  }
  const reader = entry.createReader();
  for (;;) {
    const batch = await readEntries(reader);
    if (!batch.length) break;
    for (const child of batch) await walkEntry(child, path, out);
  }
}
async function filesFromDrop(transfer) {
  const roots = [...transfer.items].map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  if (!roots.length) return [...transfer.files].map((file) => ({name:file.name, blob:file}));
  const out = [];
  for (const root of roots) await walkEntry(root, "", out);
  return out;
}
function filesFromInput(list) {
  return [...list].map((file) => ({name:file.webkitRelativePath || file.name, blob:file}))
    .filter((file) => !file.name.split("/").some((part) => SKIP.has(part)));
}
// A dropped folder arrives as "my-agent/agent.py"; the platform imports `agent` from the root.
function stripWrapper(files) {
  if (files.some((file) => file.name === "agent.py")) return {files, folder:""};
  const tops = new Set(files.map((file) => file.name.split("/")[0]));
  if (tops.size !== 1) return {files, folder:""};
  const folder = [...tops][0], prefix = `${folder}/`;
  const stripped = files.map((file) => ({...file, name:file.name.slice(prefix.length)}));
  return stripped.some((file) => file.name === "agent.py") ? {files:stripped, folder} : {files, folder};
}
function titleFrom(text) {
  const words = text.replace(/\.(zip|py)$/i, "").replace(/[-_]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "";
}
function stageDrop(collected) {
  $("drop-error").hidden = true;
  $("drop-report").hidden = true;
  if (!collected.length) throw new Error("There were no files in that drop");
  if (collected.length === 1 && /\.zip$/i.test(collected[0].name)) {
    staged = {blob:collected[0].blob, label:collected[0].name, count:null,
      bytes:collected[0].blob.size, suggestion:titleFrom(collected[0].name)};
  } else {
    const {files, folder} = stripWrapper(collected);
    const bytes = files.reduce((total, file) => total + file.blob.size, 0);
    if (!files.some((file) => file.name === "agent.py")) {
      throw new Error("No agent.py in there. Drop the folder that holds it, or the file itself.");
    }
    staged = {files, label: folder || files.map((file) => file.name).join(", "),
      count:files.length, bytes, suggestion:titleFrom(folder || "New agent")};
  }
  $("drop-summary").textContent = staged.count === null
    ? `${staged.label} · ${size(staged.bytes)}`
    : `${staged.label} · ${staged.count} file${staged.count === 1 ? "" : "s"} · ${size(staged.bytes)}`;
  $("drop-name").value = staged.suggestion;
  $("drop-family").value = "";
  $("drop-form").hidden = false;
  $("drop-name").focus();
  $("drop-name").select();
}
function veil(show) {
  $("drop-veil").hidden = !show;
  if (!show) dragDepth = 0;
}
async function receive(collected) {
  openRegistry(false);
  try {
    stageDrop(collected);
  } catch (error) {
    staged = null;
    $("drop-form").hidden = true;
    $("drop-error").textContent = error.message;
    $("drop-error").hidden = false;
    $("drop-error").scrollIntoView({block:"nearest"});
  }
}
function bindDrop() {
  const carriesFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  document.addEventListener("dragenter", (event) => {
    if (!carriesFiles(event)) return;
    dragDepth++;
    veil(true);
  });
  document.addEventListener("dragover", (event) => { if (carriesFiles(event)) event.preventDefault(); });
  document.addEventListener("dragleave", () => { if (--dragDepth <= 0) veil(false); });
  document.addEventListener("drop", async (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    veil(false);
    await receive(await filesFromDrop(event.dataTransfer));
  });
  $("drop-zone").addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    $("folder-input").click();
  });
  $("pick-folder").addEventListener("click", () => $("folder-input").click());
  $("pick-files").addEventListener("click", () => $("files-input").click());
  for (const id of ["folder-input", "files-input"]) {
    $(id).addEventListener("change", async (event) => {
      await receive(filesFromInput(event.target.files));
      event.target.value = "";
    });
  }
  $("drop-cancel").addEventListener("click", () => { staged = null; $("drop-form").hidden = true; });
  $("drop-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!staged) return;
    $("drop-error").hidden = true;
    $("drop-save").disabled = true;
    $("drop-save").textContent = "Checking the agent…";
    try {
      const blob = staged.blob || await buildZip(staged.files);
      const query = new URLSearchParams({name:$("drop-name").value.trim(),
        family:$("drop-family").value.trim(), notes:$("drop-notes").value.trim()});
      const response = await fetch(`/api/agents?${query}`, {method:"POST", body:blob,
        headers:{"Content-Type":"application/zip", "X-CSRF-Token":token}});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Upload failed (${response.status})`);
      applyCatalog(result.catalog);
      staged = null;
      $("drop-form").hidden = true;
      $("drop-form").reset();
      $("drop-report").textContent = `${result.engine.name} is in the registry. ${result.report}`;
      $("drop-report").hidden = false;
    } catch (error) {
      $("drop-error").textContent = error.message || String(error);
      $("drop-error").hidden = false;
    } finally {
      $("drop-save").disabled = false;
      $("drop-save").textContent = "Add it";
    }
  });
}

// ── Playing the engine yourself ───────────────────────────────────────────────────────────
// The session the server hands back is shaped like a game, so the board, the move list, the
// clocks and the piece animation all render it without knowing a person is on one side.
const PROMOTIONS = "qrbn";
const playing = () => Boolean(play);
const playOrigins = () => new Set((play?.legal || []).map((uci) => uci.slice(0,2)));
function yourMove() {
  return Boolean(play?.your_turn) && frameIndex === (game?.frames.length || 1) - 1;
}
function closePromotion() { $("board").querySelector(".promotion")?.remove(); }
function selectSquare(square) {
  selected = square;
  targets = new Map();
  for (const uci of square ? play.legal : []) {
    if (uci.slice(0,2) !== square) continue;
    const to = uci.slice(2,4);
    targets.set(to, [...(targets.get(to) || []), uci]);
  }
  renderSelection();
}
function renderSelection() {
  const board = $("board"), origins = yourMove() ? playOrigins() : new Set();
  board.classList.toggle("playable", yourMove());
  for (const square of board.querySelectorAll(".square")) {
    const name = square.dataset.square, target = targets.has(name);
    square.classList.toggle("chosen", name === selected);
    square.classList.toggle("target", target);
    square.classList.toggle("occupied-target", target && Boolean(square.querySelector("img")));
    if (target || origins.has(name)) { square.tabIndex = 0; square.setAttribute("role", "button"); }
    else { square.removeAttribute("tabindex"); square.removeAttribute("role"); }
  }
}
function promptPromotion(square, options) {
  closePromotion();
  const cell = $("board").querySelector(`[data-square="${square}"]`);
  if (!cell) return;
  const names = {q:"queen", r:"rook", b:"bishop", n:"knight"};
  const picker = document.createElement("div");
  picker.className = rowOf(square, flipped) === 0 ? "promotion" : "promotion upward";
  picker.innerHTML = [...options].sort((a,b) => PROMOTIONS.indexOf(a[4]) - PROMOTIONS.indexOf(b[4]))
    .map((uci) => {
      const piece = play.your_colour === "white" ? uci[4].toUpperCase() : uci[4];
      return `<button type="button" data-uci="${esc(uci)}" title="Promote to ${names[uci[4]]}"><img src="/pieces/${piece}.svg" alt="${names[uci[4]]}"></button>`;
    }).join("");
  cell.append(picker);
  picker.querySelector("button")?.focus();
}
// Both clocks run. The server sends what is left; this paints the running one down between polls.
const clockSides = () => [["top", flipped ? "white" : "black"], ["bottom", flipped ? "black" : "white"]];
function paintClocks() {
  if (!play || !clockAnchor) return;
  const elapsed = performance.now() - clockAnchor.at;
  for (const [location, colour] of clockSides()) {
    const running = clockAnchor.running === colour;
    const left = Math.max(0, (clockAnchor.clocks[colour] ?? 0) - (running ? elapsed : 0));
    const cell = $(`${location}-clock`);
    cell.textContent = clock(left);
    cell.classList.toggle("low", running && left <= 10000);
  }
}
function startClocks() {
  clearInterval(clockTimer);
  clockTimer = setInterval(() => { if (play && following) paintClocks(); }, 100);
}
const TERMINATIONS = {
  checkmate: "by checkmate",
  stalemate: "by stalemate",
  insufficient_material: "neither side can force mate",
  threefold_repetition: "by repetition",
  fifty_moves: "by the fifty-move rule",
  ply_cap: "the game reached the ply cap",
  flag: "on time",
  illegal: "the engine played an illegal move",
  crash: "the engine crashed",
  init: "the engine never started",
  both_failed: "neither side could play",
};
function resultReason() {
  const name = engine(play.engine).name;
  if (play.termination === "resignation") {
    return play.result === play.your_colour ? `${name} resigned` : "you resigned";
  }
  return TERMINATIONS[play.termination] || String(play.termination).replaceAll("_", " ");
}
const undoable = () => play.can_undo && !failureNames.has(play.termination);
function resultTone() {
  if (play.result === "draw") return "drawn";
  return play.result === play.your_colour ? "won" : "lost";
}
const resultHeadline = () => ({won:"You won", lost:"You lost", drawn:"Draw"})[resultTone()];
function renderResult() {
  const over = play.status === "finished";
  if (!over) resultSeen = false;          // a take back re-arms the card for the next ending
  $("play-result").hidden = !over || resultSeen;
  if (!over) return;
  const tone = resultTone();
  $("play-result").className = `play-result ${tone}`;
  $("result-headline").textContent = resultHeadline();
  $("result-detail").textContent = resultReason();
  $("result-undo").disabled = !undoable();
}
function bindResult() {
  $("close-result").addEventListener("click", () => { resultSeen = true; renderResult(); });
  $("result-rematch").addEventListener("click", () => { resultSeen = true; renderResult(); openPlayDialog(); });
  $("result-undo").addEventListener("click", () => { resultSeen = true; playAction("/api/play/undo"); });
}
function playStatus() {
  const name = engine(play.engine).name;
  if (play.status === "starting") return `Starting ${name}…`;
  if (play.status === "finished") return `${resultHeadline()} · ${resultReason()}`;
  return play.thinking ? `${name} is thinking…` : "Your move";
}
function renderPlay() {
  document.body.classList.toggle("play-mode", playing());
  $("play-badge").hidden = !playing();
  $("play-controls").hidden = !playing();
  if (!play) return;
  game = {...play, status: play.status === "finished" ? "completed" : "running"};
  frameIndex = following ? game.frames.length-1 : Math.min(frameIndex, game.frames.length-1);
  renderGame();
  const name = engine(play.engine).name;
  $("arena-eyebrow").textContent = "Sparring";
  $("run-title").textContent = `You versus ${name}`;
  $("arena-subtitle").textContent = play.status === "finished"
    ? "The game is over. Take a move back, or start another." : "Choose a piece, then its square.";
  const over = play.status === "finished";
  $("run-status").textContent = over ? {won:"you won", lost:"you lost", drawn:"draw"}[resultTone()] : "playing";
  $("run-status").className = `status ${over ? resultTone() : "running"}`;
  $("outcome").className = `outcome ${over ? resultTone() : ""}`;
  $("run-progress").textContent = `${name} on ${play.limits.base_ms/1000}s + ${play.limits.increment_ms/1000}s · ${play.opening.name}`;
  $("game-meta").textContent = `${play.opening.family} · you play ${play.your_colour}`;
  $("outcome").textContent = playStatus();
  // Both clocks read as time remaining; the ticker keeps the running one moving between polls.
  clockAnchor = {clocks: play.clocks, running: play.running, at: performance.now()};
  if (following) paintClocks();
  renderResult();
  // Whatever the engine printed, so a build that dies mid-game explains itself here.
  $("provenance").textContent = play.log || "The engine has printed nothing to stderr.";
  $("play-undo").disabled = !undoable() || play.status === "starting";
  $("play-resign").disabled = play.status !== "playing";
  renderSelection();
}
function schedulePlayPoll(delay) {
  clearTimeout(playTimer);
  playTimer = setTimeout(pollPlay, delay);
}
async function pollPlay() {
  if (!play) return;
  const revision = playRevision;
  try {
    const result = await api("/api/play");
    // A move, a take back or a resignation landed while this was in flight; that state is newer.
    if (!play || revision !== playRevision) return;
    if (!result.play) { await leavePlay(); return; }
    play = result.play;
    renderPlay();
  } catch (error) { showError(error); }
  if (play) schedulePlayPoll(play.thinking || play.status === "starting" ? 220 : 700);
}
function enterPlay(session) {
  selectionRevision++;
  playRevision++;
  resultSeen = false;
  startClocks();
  pauseReplay();
  play = session;
  flipped = play.your_colour === "black";
  following = true;
  moveKey = ""; frameIndex = 0; lastRender = null;
  selectSquare("");
  renderPlay();
  schedulePlayPoll(150);
}
async function leavePlay() {
  clearTimeout(playTimer);
  clearInterval(clockTimer);
  playRevision++;
  resultSeen = false;
  clockAnchor = null;
  const had = playing();
  play = null; game = null; gameId = ""; moveKey = ""; frameIndex = 0; following = true;
  selected = ""; targets = new Map();
  closePromotion();
  renderPlay();
  if (had) { try { await api("/api/play/end", {}); } catch (error) { showError(error); } }
  await poll();
}
async function sendMove(uci) {
  closePromotion();
  selectSquare("");
  following = true;
  try {
    play = (await api("/api/play/move", {uci})).play;
    playRevision++;
    renderPlay();
    schedulePlayPoll(120);
  } catch (error) { showError(error); await pollPlay(); }
}
async function playAction(path) {
  clearError();
  try {
    play = (await api(path, {})).play;
    playRevision++;
    following = true;
    selectSquare("");
    renderPlay();
    schedulePlayPoll(150);
  } catch (error) { showError(error); }
}
function updatePlaySummary() {
  const colour = document.querySelector("input[name=play-colour]:checked")?.value || "white";
  const name = catalog?.engines.find((e) => e.id === $("play-engine").value)?.name || "the engine";
  $("play-summary").textContent = colour === "random"
    ? `Random colour versus ${name}` : `You play ${colour} versus ${name}`;
}
function renderPlayOptions() {
  $("play-engine").innerHTML = catalog.engines.filter((e) => e.available)
    .map((e) => `<option value="${esc(e.id)}">${esc(e.name)} · ${esc(e.family)}</option>`).join("");
  const splits = new Map();
  for (const opening of catalog.openings) {
    if (!splits.has(opening.split)) splits.set(opening.split, []);
    splits.get(opening.split).push(opening);
  }
  $("play-position").innerHTML = '<option value="start">Standard start</option>'
    + [...splits].map(([split, list]) => `<optgroup label="${esc(split)} positions">${list.map((o) => `<option value="${esc(o.id)}">${esc(o.name)}</option>`).join("")}</optgroup>`).join("")
    + '<option value="custom">Custom position (FEN)</option>';
  updatePlaySummary();
}
function openPlayDialog() {
  clearError();
  updatePlaySummary();
  $("play-dialog").showModal();
}
function bindPlay() {
  for (const id of ["open-play", "open-play-top"]) $(id).addEventListener("click", openPlayDialog);
  $("play-again").addEventListener("click", openPlayDialog);
  $("play-leave").addEventListener("click", leavePlay);
  $("play-undo").addEventListener("click", () => playAction("/api/play/undo"));
  $("play-resign").addEventListener("click", () => playAction("/api/play/resign"));
  bindResult();
  $("play-engine").addEventListener("change", updatePlaySummary);
  for (const input of document.querySelectorAll("input[name=play-colour]")) {
    input.addEventListener("change", updatePlaySummary);
  }
  $("play-position").addEventListener("change", () => {
    $("play-fen-field").hidden = $("play-position").value !== "custom";
  });
  $("play-clock").addEventListener("change", () => {
    const preset = {bullet:[10,0.1], blitz:[60,0.5], event:[120,0.5], rapid:[300,3]}[$("play-clock").value];
    if (preset) { $("play-base").value = preset[0]; $("play-increment").value = preset[1]; }
  });
  for (const id of ["play-base","play-increment"]) {
    $(id).addEventListener("input", () => $("play-clock").value = "custom");
  }
  $("play-form").addEventListener("submit", async (event) => {
    event.preventDefault(); clearError(); $("play-start").disabled = true;
    try {
      const position = $("play-position").value;
      const request = {engine:$("play-engine").value,
        colour:document.querySelector("input[name=play-colour]:checked").value,
        base_ms:Math.round(Number($("play-base").value)*1000),
        increment_ms:Math.round(Number($("play-increment").value)*1000)};
      if (position === "custom") request.fen = $("play-fen").value.trim();
      else request.opening = position;
      const result = await api("/api/play", request);
      $("play-dialog").close();
      enterPlay(result.play);
    } catch (error) { showError(error); } finally { $("play-start").disabled = false; }
  });
  $("board").addEventListener("click", (event) => {
    const promotion = event.target.closest(".promotion button");
    if (promotion) { sendMove(promotion.dataset.uci); return; }
    if (!yourMove()) return;
    const square = event.target.closest(".square")?.dataset.square;
    if (!square) return;
    if (targets.has(square)) {
      const options = targets.get(square);
      if (options.length === 1) sendMove(options[0]);
      else promptPromotion(square, options);
      return;
    }
    closePromotion();
    selectSquare(square !== selected && playOrigins().has(square) ? square : "");
  });
  $("board").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target.closest?.(".promotion button, .square[role=button]");
    if (!target) return;
    event.preventDefault();
    target.click();
  });
}

async function init() {
  renderBoard();
  for (const id of ["moves", "opponents", "experiment-list"]) trackScrollable($(id));
  bindPlay();
  // A batch wants the core to itself, so asking for one leaves the board rather than telling
  // you to. The form opens straight away and the game is closed behind it.
  document.querySelectorAll(".open-setup").forEach((button) => button.addEventListener("click", () => {
    clearError();
    if (playing()) leavePlay().catch(showError);
    $("setup-dialog").showModal();
  }));
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
  bindRegistry();
  $("experiment-list").addEventListener("click", (event) => { const button = event.target.closest("button[data-run]"); if (button) { $("history").value = button.dataset.run; $("history").dispatchEvent(new Event("change")); } });
  try {
    catalog = await api("/api/catalog"); token = catalog.token;
    renderRegistry();
    $("candidate").innerHTML = catalog.engines.filter((e) => e.available).map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join("");
    renderOpponents();
    renderPlayOptions();
    bindSweep();
    updateCompleteTest();
    $("candidate").addEventListener("change", renderOpponents);
    $("opponents").addEventListener("change", updateSetup);
    for (const id of ["split","opening-count","parallel"]) $(id).addEventListener("change", updateSetup);
    for (const [id,checked] of [["select-all",true],["select-none",false]]) $(id).addEventListener("click", () => { document.querySelectorAll("#opponents input:not(:disabled)").forEach((e) => e.checked = checked); updateSetup(); });
    $("clock-preset").addEventListener("change", () => {
      const presets = {smoke:[2,0.02,80],fast:[10,0.1,600],event:[120,0.5,600]};
      const preset = presets[$("clock-preset").value];
      if (preset) { $("base").value = preset[0]; $("increment").value = preset[1]; $("ply-cap").value = preset[2]; }
    });
    for (const id of ["base","increment"]) $(id).addEventListener("input", () => $("clock-preset").value = "custom");
    $("run-form").addEventListener("submit", async (event) => {
      event.preventDefault(); clearError(); $("start").disabled = true;
      try {
        const result = await api("/api/run", {label:$("label").value,candidate:$("candidate").value,
          opponents:selectedOpponents(),openings:chosenOpenings().map((o) => o.id),
          base_ms:Math.round(Number($("base").value)*1000),increment_ms:Math.round(Number($("increment").value)*1000),
          ply_cap:Number($("ply-cap").value),seed:Number($("seed").value),
          parallel:Number($("parallel").value)});
        $("setup-dialog").close(); selectionRevision++; runId = result.id; gameId = ""; following = true; moveKey = ""; pauseReplay(); await poll();
        if (window.innerWidth < 650) $("run-title").scrollIntoView({block:"start"});
      } catch (error) { showError(error); $("start").disabled = false; }
    });
    $("stop").addEventListener("click", async () => { try { await api("/api/stop",{}); await poll(); } catch (error) { showError(error); } });
    $("resume").addEventListener("click", async () => {
      clearError();
      $("resume").disabled = true;
      try { await api("/api/resume", {run:runId}); following = true; moveKey = ""; await poll(); }
      catch (error) { showError(error); $("resume").disabled = false; }
    });
    $("history").addEventListener("change", async () => { selectionRevision++; runId = $("history").value; gameId = ""; following = false; frameIndex = 0; moveKey = ""; pauseReplay(); await poll(); });
    $("game-select").addEventListener("change", () => chooseGame($("game-select").value));
    bindGameWindow();
  $("game-map").addEventListener("click", (event) => { const target = event.target.closest("button[data-game]"); if (target) openGame(target.dataset.game); });
    $("moves").addEventListener("click", (event) => { const target = event.target.closest("button[data-frame]"); if (target) { pauseReplay(); selectFrame(Number(target.dataset.frame)); } });
    $("flip").addEventListener("click", () => { flipped = !flipped; renderBoard(); });
    $("first").addEventListener("click", () => { pauseReplay(); selectFrame(0); });
    $("last").addEventListener("click", () => { pauseReplay(); selectFrame(game.frames.length-1); });
    $("previous").addEventListener("click", () => { pauseReplay(); selectFrame(frameIndex-1); });
    $("next").addEventListener("click", () => { pauseReplay(); selectFrame(frameIndex+1); });
    $("follow").addEventListener("click", () => { pauseReplay(); follow(!following); poll(); });
    $("play").addEventListener("click", () => {
      if (timer) { pauseReplay(); return; }
      if (frameIndex >= game.frames.length-1) selectFrame(0);
      following = false; $("play").textContent = "Ⅱ"; $("play").setAttribute("aria-label","Pause replay");
      timer = setInterval(() => { if (frameIndex >= game.frames.length-1) pauseReplay(); else selectFrame(frameIndex+1); }, 650);
    });
    document.addEventListener("keydown", (event) => {
      if ($("setup-dialog").open || $("registry-dialog").open || ["INPUT","SELECT","TEXTAREA","BUTTON"].includes(event.target.tagName)) return;
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); pauseReplay(); selectFrame(frameIndex+(event.key === "ArrowLeft" ? -1 : 1)); }
    });
    $("copy-fen").addEventListener("click", async () => { try { await navigator.clipboard.writeText(currentFrame().fen); $("copy-fen").textContent = "Copied"; setTimeout(() => $("copy-fen").textContent = "Copy FEN", 1500); } catch { showError(new Error("Clipboard unavailable. The FEN is included in the exported game JSON.")); } });
    $("download-pgn").addEventListener("click", () => savePgn(game));
    await poll(); setInterval(poll,1200);
  } catch (error) { showError(error); }
}
init();
