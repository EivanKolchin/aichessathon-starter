// Sitting down against a build, through the ladder.
//
// The Worker cannot play chess and does not try to. A browser asks for a game, the request
// waits here, and the next runner to look picks it up and plays it on its own machine with the
// same code that plays experiments. Every move the person makes is one instruction parked here
// until that runner applies it; every position on the page is one the runner sent back.
//
// One live session per person, and a runner attends to a session or an experiment, never both:
// a game played beside a batch would be taking time out of the batch's measurements.

const START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const COLOURS = new Set(["white", "black", "random"]);
const PLAY_ID = /^[0-9a-z-]{6,64}$/;

const now = () => Date.now();
const parse = (value, fallback) => {
  try {
    return value ? JSON.parse(value) : fallback;
  } catch {
    return fallback;
  }
};
const clamp = (value, fallback, low, high) => {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(high, Math.max(low, Math.round(number)));
};

function playId() {
  const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
  return `${stamp}-${crypto.randomUUID().slice(0, 6)}`;
}

// Before a runner has said anything there is still a game to draw: the engine that was asked
// for, the clock that was asked for, and an empty board. `waiting` is what says it is not real
// yet, so the page can offer to cancel instead of pretending a move is possible.
function waitingState(row, request) {
  const human = request.colour === "black" ? "black" : "white";
  const engineColour = human === "white" ? "black" : "white";
  const fen = request.fen || START;
  const base = request.base_ms;
  return {
    id: row.id,
    engine: request.engine,
    engine_name: request.engine,
    engine_colour: engineColour,
    your_colour: human,
    white: human === "white" ? "you" : request.engine,
    black: human === "black" ? "you" : request.engine,
    status: "waiting",
    thinking: false,
    your_turn: false,
    clocks: {white: base, black: base},
    running: null,
    result: null,
    termination: null,
    opening: {name: "Waiting for a runner", family: "", split: "", fen},
    limits: {base_ms: base, increment_ms: request.increment_ms, ply_cap: request.ply_cap},
    legal: [],
    frames: [{fen, san: "", uci: "", white_ms: base, black_ms: base, elapsed_ms: 0}],
    pgn: "",
    engine_info: {},
    log: "",
    created_at: row.created_at / 1000,
    can_undo: false,
    waiting: true,
  };
}

function present(row) {
  const request = parse(row.request, {});
  const state = parse(row.state, null);
  const known = state || waitingState(row, request);
  return {
    ...known,
    session: row.id,
    session_status: row.status,
    runner: row.runner,
    pending: Boolean(row.command),
  };
}

async function openSession(env, owner) {
  return env.DB.prepare(
    "SELECT * FROM plays WHERE owner = ? AND status != 'ended' ORDER BY created_at DESC LIMIT 1",
  ).bind(owner).first();
}

// ── what the browser reads ────────────────────────────────────────────────────────────────

export async function readPlay(env, who) {
  const row = await openSession(env, who.email);
  return row ? present(row) : null;
}

// ── what the browser writes ───────────────────────────────────────────────────────────────

export async function startPlay(request, env, who) {
  const body = await request.json().catch(() => ({}));
  const engine = String(body.engine ?? "").slice(0, 64);
  if (!engine) return {error: "Choose an engine to play", status: 400};
  const colour = COLOURS.has(body.colour) ? body.colour : "white";
  const wanted = {
    engine,
    colour: colour === "random" ? (Math.random() < 0.5 ? "white" : "black") : colour,
    base_ms: clamp(body.base_ms, 60_000, 100, 3_600_000),
    increment_ms: clamp(body.increment_ms, 500, 0, 60_000),
    ply_cap: clamp(body.ply_cap, 600, 20, 600),
  };
  if (typeof body.opening === "string" && body.opening) wanted.opening = body.opening.slice(0, 64);
  if (typeof body.fen === "string" && body.fen.trim()) wanted.fen = body.fen.trim().slice(0, 120);
  // Leaving the old game behind is what starting a new one means; a runner attending the old
  // session sees it disappear on its next look and tears the board down.
  await env.DB.prepare("UPDATE plays SET status = 'ended', updated_at = ? WHERE owner = ? AND status != 'ended'")
    .bind(now(), who.email).run();
  const id = playId();
  const stamp = now();
  await env.DB.prepare(
    "INSERT INTO plays (id, owner, request, status, created_at, updated_at) VALUES (?, ?, ?, 'requested', ?, ?)",
  ).bind(id, who.email, JSON.stringify(wanted), stamp, stamp).run();
  const row = {id, owner: who.email, runner: null, request: JSON.stringify(wanted),
    command: null, state: null, status: "requested", created_at: stamp, updated_at: stamp};
  return {play: present(row)};
}

// One instruction at a time. A second move sent before the runner has applied the first would
// silently replace it, so a session that is already carrying one says so instead.
export async function commandPlay(request, env, who, kind) {
  const row = await openSession(env, who.email);
  if (!row) return {error: "No game in progress", status: 404};
  if (kind === "end") {
    await env.DB.prepare("UPDATE plays SET status = 'ended', command = ?, updated_at = ? WHERE id = ?")
      .bind(JSON.stringify({kind: "end"}), now(), row.id).run();
    return {play: null};
  }
  if (row.command) return {error: "Your last move is still on its way", status: 409};
  const command = {kind};
  if (kind === "move") {
    const body = await request.json().catch(() => ({}));
    const uci = String(body.uci ?? "");
    if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(uci)) return {error: "That is not a move", status: 400};
    command.uci = uci;
  }
  const taken = await env.DB.prepare(
    "UPDATE plays SET command = ?, updated_at = ? WHERE id = ? AND command IS NULL",
  ).bind(JSON.stringify(command), now(), row.id).run();
  if (!taken.meta.changes) return {error: "Your last move is still on its way", status: 409};
  row.command = JSON.stringify(command);
  return {play: present(row)};
}

// ── what a runner reads and writes ────────────────────────────────────────────────────────

export async function claimPlay(request, env, who) {
  const body = await request.json().catch(() => ({}));
  const runner = String(body.runner ?? "").slice(0, 64) || "runner";
  // Looking for a game is also this machine saying it is still here, so a runner sitting in a
  // long game does not drop off the site between experiments.
  await env.DB.prepare("UPDATE runners SET seen_at = ? WHERE id = ?").bind(now(), runner).run();
  const row = await env.DB.prepare(
    `SELECT * FROM plays WHERE status IN ('requested', 'live', 'over') AND (runner IS NULL OR runner = ?)
      ORDER BY created_at LIMIT 1`,
  ).bind(runner).first();
  if (!row) return {play: null};
  if (row.status === "requested") {
    const taken = await env.DB.prepare(
      "UPDATE plays SET runner = ?, status = 'live', updated_at = ? WHERE id = ? AND status = 'requested'",
    ).bind(runner, now(), row.id).run();
    if (!taken.meta.changes) return {play: null};
    row.runner = runner;
  }
  // The instruction is handed over and cleared in one statement, so a runner that asks twice
  // cannot play the same move twice.
  let command = null;
  if (row.command) {
    const cleared = await env.DB.prepare(
      "UPDATE plays SET command = NULL, updated_at = ? WHERE id = ? AND command = ?",
    ).bind(now(), row.id, row.command).run();
    if (cleared.meta.changes) command = parse(row.command, null);
  }
  return {play: {id: row.id, request: parse(row.request, {}), command, fresh: !row.state}};
}

export async function putPlayState(request, env, id) {
  const body = await request.json().catch(() => ({}));
  const allowed = new Set(["live", "over", "ended"]);
  const status = allowed.has(body.status) ? body.status : "live";
  const state = body.state && typeof body.state === "object" ? JSON.stringify(body.state) : null;
  await env.DB.prepare(
    "UPDATE plays SET state = COALESCE(?, state), status = ?, updated_at = ? WHERE id = ?",
  ).bind(state, status, now(), id).run();
  return {ok: true};
}

export function matchPlay(pathname) {
  const state = pathname.match(/^\/api\/play\/([^/]+)\/state$/);
  if (!state || !PLAY_ID.test(state[1])) return null;
  return {kind: "play-state", playId: state[1]};
}
