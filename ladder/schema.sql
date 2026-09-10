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
