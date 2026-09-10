-- Shared agent catalogue. One row per uploaded build; content is addressed by sha256,
-- so re-uploading an identical zip is idempotent rather than a second entry.
CREATE TABLE IF NOT EXISTS agents (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  family         TEXT NOT NULL,
  owner          TEXT NOT NULL,
  notes          TEXT NOT NULL DEFAULT '',
  sha256         TEXT NOT NULL UNIQUE,
  zip_bytes      INTEGER NOT NULL,
  unzipped_bytes INTEGER NOT NULL,
  files          TEXT NOT NULL,
  r2_key         TEXT NOT NULL,
  created_at     INTEGER NOT NULL,
  withdrawn_at   INTEGER
);

CREATE INDEX IF NOT EXISTS agents_created ON agents (created_at DESC);
CREATE INDEX IF NOT EXISTS agents_owner ON agents (owner, created_at DESC);

-- Experiments queued from the site and played on somebody's machine. The Worker never plays
-- a game; it holds the request, hands it to a runner, and keeps what the runner reports.
CREATE TABLE IF NOT EXISTS runs (
  id           TEXT PRIMARY KEY,
  label        TEXT NOT NULL,
  owner        TEXT NOT NULL,
  status       TEXT NOT NULL,                 -- queued running completed stopped failed
  request      TEXT NOT NULL,                 -- the batch as asked for, verbatim
  manifest     TEXT,                          -- the run as the lab recorded it
  runner       TEXT,                          -- which runner claimed it
  error        TEXT,
  created_at   INTEGER NOT NULL,
  claimed_at   INTEGER,
  finished_at  INTEGER
);

CREATE INDEX IF NOT EXISTS runs_created ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS runs_queued ON runs (status, created_at);

-- One row per game in a run. Kept separate from the manifest so a single finished game is one
-- small write rather than a rewrite of the whole experiment.
CREATE TABLE IF NOT EXISTS games (
  run_id      TEXT NOT NULL,
  id          TEXT NOT NULL,
  ordinal     INTEGER NOT NULL,
  white       TEXT NOT NULL,
  black       TEXT NOT NULL,
  opponent    TEXT NOT NULL,
  opening     TEXT NOT NULL,
  status      TEXT NOT NULL,
  result      TEXT,
  termination TEXT,
  plies       INTEGER NOT NULL DEFAULT 0,
  pgn         TEXT,
  detail      TEXT,                           -- engine info and logs, once it is finished
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (run_id, id)
);

CREATE INDEX IF NOT EXISTS games_run ON games (run_id, ordinal);
CREATE INDEX IF NOT EXISTS games_live ON games (run_id, status);

-- Positions as they are played. Append-only and keyed by ply, so a repeated post from a
-- runner that lost its connection replaces the same row instead of doubling the game.
CREATE TABLE IF NOT EXISTS frames (
  run_id   TEXT NOT NULL,
  game_id  TEXT NOT NULL,
  ply      INTEGER NOT NULL,
  frame    TEXT NOT NULL,
  PRIMARY KEY (run_id, game_id, ply)
);

-- Runners that have said hello recently. Whoever starts a run has it played on their machine,
-- so the site has to be able to say whose.
CREATE TABLE IF NOT EXISTS runners (
  id        TEXT PRIMARY KEY,
  owner     TEXT NOT NULL,
  machine   TEXT NOT NULL,
  seen_at   INTEGER NOT NULL,
  run_id    TEXT,
  catalog   TEXT              -- engines and openings this machine can actually play
);
