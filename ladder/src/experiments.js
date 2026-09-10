// Experiments queued here and played somewhere else.
//
// The Worker still never runs a game. It holds the request, hands it to whichever runner asks
// for work, and keeps the positions and results that runner reports back. Everything a browser
// reads comes out of D1; everything in D1 was put there by a machine that actually played.

const RUN_ID = /^[0-9a-z-]{6,64}$/;
const GAME_ID = /^[a-z0-9_-]{1,32}$/;
const MAX_FRAMES = 400;
const RUNNER_FRESH_MS = 90_000;

const now = () => Date.now();
const parse = (value, fallback) => {
  try {
    return value ? JSON.parse(value) : fallback;
  } catch {
    return fallback;
  }
};

function runId() {
  const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
  return `${stamp}-${crypto.randomUUID().slice(0, 6)}`;
}

function presentRun(row, games) {
  const manifest = parse(row.manifest, null);
  const listed = games.map((game) => ({
    id: game.id,
    white: game.white,
    black: game.black,
    opponent: game.opponent,
    opening: parse(game.opening, {}),
    status: game.status,
    result: game.result,
    termination: game.termination,
    plies: game.plies,
  }));
  return {
    ...(manifest || {}),
    id: row.id,
    label: row.label,
    owner: row.owner,
    status: row.status,
    runner: row.runner,
    error: row.error,
    created_at: row.created_at / 1000,
    request: parse(row.request, {}),
    games: listed.length ? listed : (manifest?.games ?? []),
  };
}

// ── what the site reads ───────────────────────────────────────────────────────────────────

export async function listRuns(env) {
  const { results } = await env.DB.prepare(
    `SELECT r.id, r.label, r.owner, r.status, r.runner, r.created_at,
            COUNT(g.id) AS games,
            SUM(CASE WHEN g.status = 'completed' THEN 1 ELSE 0 END) AS completed
       FROM runs r LEFT JOIN games g ON g.run_id = r.id
      GROUP BY r.id ORDER BY r.created_at DESC LIMIT 50`,
  ).all();
  return results.map((row) => ({
    id: row.id,
    label: row.label,
    owner: row.owner,
    status: row.status,
    runner: row.runner,
    created_at: row.created_at / 1000,
    games: row.games ?? 0,
    completed: row.completed ?? 0,
  }));
}

export async function readRun(env, id) {
  const row = await env.DB.prepare("SELECT * FROM runs WHERE id = ?").bind(id).first();
  if (!row) return null;
  const { results } = await env.DB.prepare(
    "SELECT * FROM games WHERE run_id = ? ORDER BY ordinal",
  ).bind(id).all();
  return presentRun(row, results);
}

export async function readGame(env, id, gameId) {
  const row = await env.DB.prepare(
    "SELECT * FROM games WHERE run_id = ? AND id = ?",
  ).bind(id, gameId).first();
  if (!row) return null;
  const { results } = await env.DB.prepare(
    "SELECT frame FROM frames WHERE run_id = ? AND game_id = ? ORDER BY ply",
  ).bind(id, gameId).all();
  return {
    ...parse(row.detail, {}),
    id: row.id,
    white: row.white,
    black: row.black,
    opponent: row.opponent,
    opening: parse(row.opening, {}),
    status: row.status,
    result: row.result,
    termination: row.termination,
    plies: row.plies,
    pgn: row.pgn,
    frames: results.map((entry) => parse(entry.frame, {})),
  };
}

export async function listRunners(env) {
  const { results } = await env.DB.prepare(
    "SELECT * FROM runners WHERE seen_at > ? ORDER BY seen_at DESC",
  ).bind(now() - RUNNER_FRESH_MS).all();
  return results.map((row) => ({
    id: row.id,
    owner: row.owner,
    machine: row.machine,
    seen_at: row.seen_at / 1000,
    run_id: row.run_id,
    catalog: parse(row.catalog, null),
  }));
}

// ── what the site writes ──────────────────────────────────────────────────────────────────

export async function queueRun(request, env, who) {
  const body = await request.json();
  const label = String(body.label ?? "").trim().slice(0, 100) || "Untitled experiment";
  const batch = body.request;
  if (!batch || typeof batch !== "object" || Array.isArray(batch)) {
    return { error: "A run needs a batch request object", status: 400 };
  }
  if (!Array.isArray(batch.opponents) || !batch.opponents.length) {
    return { error: "Choose at least one opponent", status: 400 };
  }
  const id = runId();
  await env.DB.prepare(
    "INSERT INTO runs (id, label, owner, status, request, created_at) VALUES (?, ?, ?, 'queued', ?, ?)",
  ).bind(id, label, who.email, JSON.stringify(batch), now()).run();
  return { run: { id, label, owner: who.email, status: "queued" } };
}

export async function stopRun(env, id) {
  const row = await env.DB.prepare("SELECT status FROM runs WHERE id = ?").bind(id).first();
  if (!row) return { error: "No such run", status: 404 };
  // A queued run can be dropped outright; a claimed one is asked to stop after its game.
  const next = row.status === "queued" ? "stopped" : "stopping";
  await env.DB.prepare("UPDATE runs SET status = ? WHERE id = ?").bind(next, id).run();
  return { run: { id, status: next } };
}

// ── what a runner writes ──────────────────────────────────────────────────────────────────

export async function claimJob(request, env, who) {
  const body = await request.json().catch(() => ({}));
  const runner = String(body.runner ?? "").slice(0, 64) || "runner";
  const machine = String(body.machine ?? "").slice(0, 120);
  // The claim doubles as a heartbeat, and carries what this machine can play so the site
  // can only offer opponents and positions that some runner actually has.
  const catalog = body.catalog ? JSON.stringify(body.catalog).slice(0, 200_000) : null;
  await env.DB.prepare(
    `INSERT INTO runners (id, owner, machine, seen_at, run_id, catalog)
     VALUES (?, ?, ?, ?, NULL, ?)
     ON CONFLICT(id) DO UPDATE SET owner = excluded.owner, machine = excluded.machine,
                                   seen_at = excluded.seen_at,
                                   catalog = COALESCE(excluded.catalog, runners.catalog)`,
  ).bind(runner, who.email, machine, now(), catalog).run();
  const row = await env.DB.prepare(
    "SELECT id, request FROM runs WHERE status = 'queued' ORDER BY created_at LIMIT 1",
  ).first();
  if (!row) return { job: null };
  // Claiming is conditional, so two runners asking at once cannot both take the same run.
  const taken = await env.DB.prepare(
    "UPDATE runs SET status = 'running', runner = ?, claimed_at = ? WHERE id = ? AND status = 'queued'",
  ).bind(runner, now(), row.id).run();
  if (!taken.meta.changes) return { job: null };
  await env.DB.prepare("UPDATE runners SET run_id = ? WHERE id = ?").bind(row.id, runner).run();
  return { job: { run_id: row.id, request: parse(row.request, {}) } };
}

// A runner in the middle of a batch is not talking to /api/jobs/claim, so without this it
// vanishes from the site for the length of the experiment and the form empties out with it.
// Deliberately separate from putRunStatus: a heartbeat must never be able to overwrite a stop
// the site has just asked for.
export async function beat(request, env) {
  const body = await request.json().catch(() => ({}));
  const runner = String(body.runner ?? "").slice(0, 64);
  if (!runner) return { error: "A heartbeat has to say which runner it is", status: 400 };
  await env.DB.prepare("UPDATE runners SET seen_at = ? WHERE id = ?").bind(now(), runner).run();
  return { ok: true };
}

export async function putManifest(request, env, id) {
  const body = await request.json();
  const manifest = body.manifest;
  if (!manifest || typeof manifest !== "object") {
    return { error: "A manifest is required", status: 400 };
  }
  const games = Array.isArray(manifest.games) ? manifest.games : [];
  await env.DB.prepare("UPDATE runs SET manifest = ? WHERE id = ?")
    .bind(JSON.stringify({ ...manifest, games: undefined }), id).run();
  const statements = games.map((game, index) =>
    env.DB.prepare(
      `INSERT INTO games (run_id, id, ordinal, white, black, opponent, opening, status,
                          result, termination, plies, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(run_id, id) DO UPDATE SET status = excluded.status, result = excluded.result,
         termination = excluded.termination, plies = excluded.plies, updated_at = excluded.updated_at`,
    ).bind(
      id, String(game.id), index, String(game.white), String(game.black),
      String(game.opponent), JSON.stringify(game.opening ?? {}), String(game.status ?? "queued"),
      game.result ?? null, game.termination ?? null, Number(game.plies ?? 0), now(),
    ),
  );
  if (statements.length) await env.DB.batch(statements);
  return { ok: true, games: statements.length };
}

export async function putGame(request, env, id, gameId) {
  const body = await request.json();
  const game = body.game;
  if (!game || typeof game !== "object") return { error: "A game is required", status: 400 };
  await env.DB.prepare(
    `UPDATE games SET status = ?, result = ?, termination = ?, plies = ?, pgn = ?, detail = ?,
                      updated_at = ?
      WHERE run_id = ? AND id = ?`,
  ).bind(
    String(game.status ?? "running"), game.result ?? null, game.termination ?? null,
    Number(game.plies ?? 0), game.pgn ?? null,
    game.detail ? JSON.stringify(game.detail) : null, now(), id, gameId,
  ).run();
  return { ok: true };
}

export async function putFrames(request, env, id, gameId) {
  const body = await request.json();
  const frames = Array.isArray(body.frames) ? body.frames.slice(0, MAX_FRAMES) : [];
  if (!frames.length) return { ok: true, frames: 0 };
  const statements = frames.map((entry) =>
    env.DB.prepare(
      `INSERT INTO frames (run_id, game_id, ply, frame) VALUES (?, ?, ?, ?)
       ON CONFLICT(run_id, game_id, ply) DO UPDATE SET frame = excluded.frame`,
    ).bind(id, gameId, Number(entry.ply), JSON.stringify(entry.frame ?? {})),
  );
  await env.DB.batch(statements);
  return { ok: true, frames: statements.length };
}

export async function putRunStatus(request, env, id) {
  const body = await request.json().catch(() => ({}));
  const allowed = new Set(["running", "completed", "stopped", "failed", "interrupted"]);
  const status = String(body.status ?? "");
  if (!allowed.has(status)) return { error: "Unknown run status", status: 400 };
  const finished = status === "running" ? null : now();
  await env.DB.prepare("UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?")
    .bind(status, body.error ? String(body.error).slice(0, 2000) : null, finished, id).run();
  if (body.runner) {
    await env.DB.prepare("UPDATE runners SET seen_at = ?, run_id = ? WHERE id = ?")
      .bind(now(), status === "running" ? id : null, String(body.runner)).run();
  }
  return { ok: true };
}

// ── routing ───────────────────────────────────────────────────────────────────────────────

export function match(pathname) {
  const run = pathname.match(/^\/api\/runs\/([^/]+)$/);
  const manifest = pathname.match(/^\/api\/runs\/([^/]+)\/manifest$/);
  const status = pathname.match(/^\/api\/runs\/([^/]+)\/status$/);
  const stop = pathname.match(/^\/api\/runs\/([^/]+)\/stop$/);
  const game = pathname.match(/^\/api\/runs\/([^/]+)\/games\/([^/]+)$/);
  const frames = pathname.match(/^\/api\/runs\/([^/]+)\/games\/([^/]+)\/frames$/);
  const found = run || manifest || status || stop || game || frames;
  if (!found) return null;
  if (!RUN_ID.test(found[1])) return null;
  if (found[2] && !GAME_ID.test(found[2])) return null;
  return {
    kind: frames ? "frames" : game ? "game" : stop ? "stop" : status ? "status"
      : manifest ? "manifest" : "run",
    runId: found[1],
    gameId: found[2],
  };
}
