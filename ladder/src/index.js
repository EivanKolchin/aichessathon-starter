// Upload catalogue for the private ladder. This Worker stores and lists agent builds; it
// never runs one. Games are played by whoever pulls the catalogue down to their own machine.

import {
  claimJob,
  listRunners,
  listRuns,
  match,
  putFrames,
  putGame,
  putManifest,
  putRunStatus,
  queueRun,
  readGame,
  readRun,
  stopRun,
} from "./experiments.js";
import {
  claimPlay,
  commandPlay,
  matchPlay,
  putPlayState,
  readPlay,
  startPlay,
} from "./play.js";
import { inspect } from "./unzip.js";

const ZIP_TYPES = ["application/zip", "application/x-zip-compressed", "application/octet-stream"];

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

async function digest(value) {
  const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value;
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
}

function hex(bytes) {
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function sameSecret(offered, expected) {
  if (!expected) return false;
  const [a, b] = await Promise.all([digest(offered), digest(expected)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

// Two ways in. A browser arrives with an Access identity, which is only believed once
// ACCESS_ENABLED says Access actually fronts this hostname. Everything else, including
// `python -m chesslab ladder`, presents the bearer token.
async function identify(request, env) {
  const email = request.headers.get("Cf-Access-Authenticated-User-Email");
  if (env.ACCESS_ENABLED === "true" && email) {
    const allowed = (env.ALLOWED_EMAILS || "")
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean);
    if (allowed.length && !allowed.includes(email.toLowerCase())) return null;
    return { email, via: "access" };
  }
  const header = request.headers.get("Authorization") || "";
  const offered = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (offered && (await sameSecret(offered, env.LADDER_TOKEN))) {
    return { email: request.headers.get("X-Ladder-Owner") || "token", via: "token" };
  }
  return null;
}

function slugify(value) {
  const slug = value
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/g, "");
  return slug || "agent";
}

function present(row) {
  return {
    id: row.id,
    name: row.name,
    family: row.family,
    owner: row.owner,
    notes: row.notes,
    sha256: row.sha256,
    zip_bytes: row.zip_bytes,
    unzipped_bytes: row.unzipped_bytes,
    files: JSON.parse(row.files),
    created_at: row.created_at,
    withdrawn: row.withdrawn_at !== null,
  };
}

async function catalog(env, url) {
  const all = url.searchParams.get("all") === "1";
  const query = all
    ? "SELECT * FROM agents ORDER BY created_at DESC LIMIT 500"
    : "SELECT * FROM agents WHERE withdrawn_at IS NULL ORDER BY created_at DESC LIMIT 500";
  const { results } = await env.DB.prepare(query).all();
  return results.map(present);
}

async function upload(request, env, who) {
  const maxZip = Number(env.MAX_ZIP_BYTES || 30000000);
  const maxUnzipped = Number(env.MAX_UNZIPPED_BYTES || 50000000);

  let form;
  try {
    form = await request.formData();
  } catch {
    return json({ error: "Send the zip as multipart/form-data with a file field" }, 400);
  }
  const file = form.get("file");
  if (!file || typeof file === "string" || typeof file.arrayBuffer !== "function") {
    return json({ error: "No file in the upload" }, 400);
  }
  if (file.size === 0) return json({ error: "That file is empty" }, 400);
  if (file.size > maxZip) {
    const cap = Math.round(maxZip / 1000000);
    return json({ error: `${file.size} bytes is over the ${cap} MB upload cap` }, 413);
  }
  if (file.type && !ZIP_TYPES.includes(file.type)) {
    return json({ error: `Expected a zip, got ${file.type}` }, 400);
  }

  const bytes = new Uint8Array(await file.arrayBuffer());
  const sha256 = hex(await digest(bytes));

  // Content addressing makes a repeated upload a no-op rather than a duplicate row.
  const existing = await env.DB.prepare("SELECT * FROM agents WHERE sha256 = ?").bind(sha256).first();
  if (existing) {
    if (existing.withdrawn_at !== null) {
      await env.DB.prepare("UPDATE agents SET withdrawn_at = NULL WHERE id = ?").bind(existing.id).run();
      existing.withdrawn_at = null;
    }
    return json({ agent: present(existing), unchanged: true }, 200);
  }

  const { problems, files, unzippedBytes } = await inspect(bytes, maxUnzipped);
  if (problems.length) return json({ error: "This zip would fail validation", problems }, 422);

  const fallbackName = file.name ? file.name.replace(/\.zip$/i, "") : "agent";
  const name = String(form.get("name") || "").trim().slice(0, 80) || fallbackName;
  const owned = `${who.email.split("@")[0]} builds`;
  const family = String(form.get("family") || "").trim().slice(0, 60) || owned;
  const notes = String(form.get("notes") || "").trim().slice(0, 500);
  const id = `${slugify(name)}-${sha256.slice(0, 8)}`;
  const key = `agents/${id}.zip`;

  await env.BUCKET.put(key, bytes, {
    httpMetadata: { contentType: "application/zip" },
    customMetadata: { sha256, owner: who.email },
  });

  const row = {
    id,
    name,
    family,
    owner: who.email,
    notes,
    sha256,
    zip_bytes: bytes.byteLength,
    unzipped_bytes: unzippedBytes,
    files: JSON.stringify(files),
    r2_key: key,
    created_at: Date.now(),
    withdrawn_at: null,
  };
  try {
    await env.DB.prepare(
      `INSERT INTO agents (id, name, family, owner, notes, sha256, zip_bytes, unzipped_bytes,
                           files, r2_key, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    )
      .bind(
        id,
        name,
        family,
        who.email,
        notes,
        sha256,
        row.zip_bytes,
        unzippedBytes,
        row.files,
        key,
        row.created_at,
      )
      .run();
  } catch (error) {
    await env.BUCKET.delete(key).catch(() => {});
    return json({ error: `Could not record the upload: ${error.message}` }, 500);
  }
  return json({ agent: present(row) }, 201);
}

async function download(env, id) {
  const row = await env.DB.prepare("SELECT r2_key, sha256 FROM agents WHERE id = ?").bind(id).first();
  if (!row) return json({ error: "No such agent" }, 404);
  const object = await env.BUCKET.get(row.r2_key);
  if (!object) return json({ error: "The stored build is missing" }, 410);
  return new Response(object.body, {
    headers: {
      "Content-Type": "application/zip",
      "Content-Disposition": `attachment; filename="${id}.zip"`,
      "Cache-Control": "no-store",
      ETag: `"${row.sha256}"`,
    },
  });
}

async function withdraw(request, env, id, who) {
  const row = await env.DB.prepare("SELECT * FROM agents WHERE id = ?").bind(id).first();
  if (!row) return json({ error: "No such agent" }, 404);
  const restore = new URL(request.url).searchParams.get("restore") === "1";
  const stamp = restore ? null : Date.now();
  await env.DB.prepare("UPDATE agents SET withdrawn_at = ? WHERE id = ?").bind(stamp, id).run();
  row.withdrawn_at = stamp;
  return json({ agent: present(row), by: who.email });
}

// Handlers return either a payload or {error, status}; this keeps that in one place.
function reply(result, created = 200) {
  if (result && result.error) return json({ error: result.error }, result.status || 400);
  return json(result, created);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(request);

    const who = await identify(request, env);
    if (!who) {
      const error = "Sign in through Cloudflare Access, or send the ladder token as a bearer header";
      return json({ error }, 401);
    }

    // Same-origin guard for the state-changing routes; token clients set no Origin at all.
    if (request.method === "POST") {
      const origin = request.headers.get("Origin");
      if (origin !== null && origin !== url.origin) {
        return json({ error: "Cross-origin post refused" }, 403);
      }
    }

    const zip = url.pathname.match(/^\/api\/agents\/([a-z0-9][a-z0-9_-]{0,63})\/zip$/);
    const drop = url.pathname.match(/^\/api\/agents\/([a-z0-9][a-z0-9_-]{0,63})\/withdraw$/);
    try {
      if (request.method === "GET" && url.pathname === "/api/catalog") {
        return json({ viewer: who, agents: await catalog(env, url) });
      }
      // Build storage is the one part that needs R2. Everything else - queueing experiments,
      // watching them, reading results - runs on D1 alone, so an account without R2 enabled
      // gets a clear answer here rather than a 500 from an undefined binding.
      if ((zip || url.pathname === "/api/upload") && !env.BUCKET) {
        return json({ error: "Agent builds need R2 enabled on this account" }, 503);
      }
      if (request.method === "GET" && zip) return await download(env, zip[1]);
      if (request.method === "POST" && url.pathname === "/api/upload") {
        return await upload(request, env, who);
      }
      if (request.method === "POST" && drop) return await withdraw(request, env, drop[1], who);

      // Experiments: queued here, played on somebody's machine, reported back here.
      if (request.method === "GET" && url.pathname === "/api/runs") {
        return json({
          viewer: who,
          runs: await listRuns(env),
          runners: await listRunners(env),
          play: await readPlay(env, who),
        });
      }
      if (request.method === "POST" && url.pathname === "/api/runs") {
        return reply(await queueRun(request, env, who), 201);
      }
      if (request.method === "GET" && url.pathname === "/api/runners") {
        return json({ runners: await listRunners(env) });
      }
      if (request.method === "POST" && url.pathname === "/api/jobs/claim") {
        return json(await claimJob(request, env, who));
      }

      // Sparring. The same shape of exchange as an experiment: the browser asks, a runner
      // plays, the position comes back here.
      if (request.method === "GET" && url.pathname === "/api/play") {
        return json({ play: await readPlay(env, who) });
      }
      if (request.method === "POST" && url.pathname === "/api/play") {
        return reply(await startPlay(request, env, who), 201);
      }
      if (request.method === "POST" && url.pathname === "/api/play/claim") {
        return json(await claimPlay(request, env, who));
      }
      for (const kind of ["move", "undo", "resign", "end"]) {
        if (request.method === "POST" && url.pathname === `/api/play/${kind}`) {
          return reply(await commandPlay(request, env, who, kind));
        }
      }
      const session = matchPlay(url.pathname);
      if (session && request.method === "POST") {
        return reply(await putPlayState(request, env, session.playId));
      }
      const route = match(url.pathname);
      if (route && request.method === "GET" && route.kind === "run") {
        const found = await readRun(env, route.runId);
        return found ? json({ run: found }) : json({ error: "No such run" }, 404);
      }
      if (route && request.method === "GET" && route.kind === "game") {
        const found = await readGame(env, route.runId, route.gameId);
        return found ? json({ game: found }) : json({ error: "No such game" }, 404);
      }
      if (route && request.method === "POST") {
        if (route.kind === "stop") return reply(await stopRun(env, route.runId));
        if (route.kind === "manifest") return reply(await putManifest(request, env, route.runId));
        if (route.kind === "status") return reply(await putRunStatus(request, env, route.runId));
        if (route.kind === "game") {
          return reply(await putGame(request, env, route.runId, route.gameId));
        }
        if (route.kind === "frames") {
          return reply(await putFrames(request, env, route.runId, route.gameId));
        }
      }
    } catch (error) {
      return json({ error: `${error.name}: ${error.message}` }, 500);
    }
    return json({ error: "Not found" }, 404);
  },
};
