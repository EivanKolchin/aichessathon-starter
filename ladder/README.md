# Chess Ladder — shared agent catalogue

A small Cloudflare Worker that holds everybody's agent builds in one place. You drop a zip (or
a folder) on the site, it is validated, and it appears for everyone signed in. Each person then
pulls the catalogue onto their own machine, where the agents register as ordinary Chess Lab
opponents and play through the unchanged event referee.

**The Worker never runs an agent.** It stores, validates and lists them. Games stay on your
machine, on the harness that already exists.

```
browser ──drop zip──▶ Worker ──▶ R2 (builds) + D1 (catalogue)
                        │
   python -m chesslab ladder pull ──▶ .chesslab/uploads/<id>/ ──▶ Chess Lab registry
```

## Running experiments from the site

The Worker still never plays a game. **Arena** queues an experiment; a runner on somebody's
machine claims it, plays it through the unchanged referee, and posts the positions back as they
are produced. The browser only ever talks to the Worker.

```
browser ──queue──▶ Worker ──job──▶ chesslab runner ──plays locally──▶ frames, results ──▶ Worker ──▶ browser
```

Start a runner on the machine that should do the work:

```bash
python -m chesslab runner --url https://your-worker.example.com
```

It dials out only: no inbound port, no certificate on localhost, no CORS. It claims one job at a
time, pulls any build it does not already have, and reports positions, results and the manifest.
The claim doubles as a heartbeat and carries the engines and positions that machine can play, so
the site only ever offers opponents a runner actually has. With no runner online, queued
experiments simply wait.

Two things this design does not give you, and should not be read as giving you:

- **Games run on whoever started them.** A run measures that machine. The manifest already
  records the platform, the core count and how many games were in flight; the runner adds its
  own name, and the site shows it on every run. A table across two machines is indicative, not
  authoritative.
- **Uploaded agents run as ordinary processes on that machine.** Sandboxing them
  (`docker --network none --read-only --cpus 1 --memory 2g`) is the right answer and is not done
  here yet.

## One-time setup

Everything below runs from this `ladder/` directory. Nothing is created until you run it.

```bash
npm install
npx wrangler login
```

Create the two stores. The first command prints a `database_id`; paste it into
`wrangler.toml` over `PASTE_DATABASE_ID_FROM_WRANGLER_D1_CREATE`.

```bash
npx wrangler d1 create chess-ladder
npx wrangler r2 bucket create chess-ladder-agents
```

Create the tables, set the shared token, and deploy. `schema.sql` holds the catalogue and the
experiment tables; it is safe to re-run, but it only creates - if you change a column later you
have to `ALTER TABLE` yourself:

```bash
npx wrangler d1 execute chess-ladder --remote --file=./schema.sql
npx wrangler secret put LADDER_TOKEN
npx wrangler deploy
```

Before the real deploy, `npx wrangler deploy --dry-run` bundles the Worker and prints the
bindings it resolved without touching your account. `env.DB`, `env.BUCKET` and `env.ASSETS`
all have to appear in that table; the Worker serves every page through `env.ASSETS`, so a
missing assets binding is a blank site rather than a build error.

`wrangler secret put` prompts for a value. Generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"` and give the same string to
everyone who uploads. D1 and R2 both have free tiers that comfortably cover two people.

## Locking it down

As deployed above, the token is the only thing protecting the site, and the URL is public.
That is fine for an afternoon; it is not where you want to leave it.

1. Put the Worker on a custom domain (`routes` in `wrangler.toml`), then set
   `workers_dev = false` so the public `*.workers.dev` address stops answering.
2. In the Cloudflare dashboard, Zero Trust → Access → Applications, add a self-hosted
   application for that hostname with an email allowlist policy for you and your friend.
3. Set `ACCESS_ENABLED = "true"` and `ALLOWED_EMAILS = "you@example.com,friend@example.com"`
   in `wrangler.toml`, then `npx wrangler deploy`.

**Do not set `ACCESS_ENABLED = "true"` before step 1 is done.** That flag tells the Worker to
believe the `Cf-Access-Authenticated-User-Email` header. Access sets that header and strips any
incoming copy — but only on requests that actually pass through Access. If the `workers.dev`
route is still reachable, anyone can send that header themselves and be whoever they like.
While the flag is `"false"` the header is ignored entirely and the bearer token is the only
way in, which is why that is the default.

For the `chesslab ladder` client, keep using the bearer token; a command-line tool cannot do
the interactive Access login. Cloudflare Access service tokens are the tidier long-term
answer if you outgrow the shared secret.

## Uploading

Open the site. Drop `submission.zip`, the folder your `agent.py` lives in, or the loose files
themselves — a folder is zipped in the browser, in the same shape `make zip` produces, with
fixed timestamps so an unchanged agent re-zips to an identical archive.

From the command line instead:

```bash
python -m chesslab ladder push submission.zip --name "A0 checkpoint 4" --family "Original alpha-beta"
```

## Playing what other people uploaded

```bash
python -m chesslab ladder pull --url https://your-ladder.example.com
python -m chesslab serve
```

`pull` downloads any build it does not already have, verifies it against the hash the
catalogue recorded, unpacks it under `.chesslab/uploads/<id>/`, and writes a registry entry.
Uploads then show up as opponents in the lab exactly like the built-in baselines. The URL and
token are remembered in `.chesslab/ladder.json`, so later runs are just
`python -m chesslab ladder pull`. Withdraw an agent on the site and the next pull deletes the
local copy and its registration.

Hand-registered engines in `.chesslab/engines.json` are left alone; `pull` only rewrites the
entries it owns.

## What gets rejected

Checked in the Worker on upload, and again locally before anything is unpacked — the second
pass assumes the catalogue could serve something the first pass missed.

- No `agent.py` at the root of the zip (the platform does `import agent`)
- Paths that escape the agent directory, absolute paths, drive letters, symlinks
- Native binaries by extension (`.so`, `.pyd`, `.dll`, `.dylib`, `.exe`, …) **and** by magic
  number, so renaming `engine.so` to `weights.onnx` does not get past it
- `.pyc` files — the event wants source a judge can read
- Anything over 50 MB unzipped, matching `MAX_UNZIPPED_BYTES` in `harness/rules.py`
- Zip64 archives, and more than 4000 entries

This mirrors the event's own rules, so a zip the ladder refuses would probably have cost you
an upload slot on the real platform.

## Limits worth knowing

- **Compressed uploads are capped at 30 MB** (`MAX_ZIP_BYTES`). The Worker buffers the whole
  zip in memory to hash and inspect it, and Workers have a 128 MB ceiling. If you ship a net
  that pushes past this, move to a presigned direct-to-R2 upload and inspect it afterwards.
- **This slice does not run games.** Everyone plays locally, which means results from your
  machine and your friend's are not directly comparable at a wall-clock time control. Record
  which machine produced a result before you read anything into a leaderboard.
- **An uploaded agent is code that runs on your machine when you play it.** Validation stops
  native binaries and path escapes; it does not stop `agent.py` from doing whatever Python can
  do. Running the lab inside a container (`--network none`, read-only, `--cpus 1`,
  `--memory 2g`) both fixes that and reproduces the platform's real constraints.

## Local development

```bash
npx wrangler dev
```

Uses local D1 and R2 emulation. Apply the schema to the local database first with
`npx wrangler d1 execute chess-ladder --local --file=./schema.sql`.
