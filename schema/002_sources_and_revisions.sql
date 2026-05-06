-- 002: sources registry + content revision chain.
--
-- NOT idempotent. SQLite has no `ADD COLUMN IF NOT EXISTS`; re-running on a
-- DB already at version 2 will error on the ALTER statements. Migrations
-- are gated by `schema_version`; apply once.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    adapter         TEXT NOT NULL,
    url             TEXT NOT NULL,
    config          TEXT NOT NULL DEFAULT '{}',
    schedule        TEXT NOT NULL,
    source_tier     TEXT NOT NULL DEFAULT 'vendor-doc',
    paused          INTEGER NOT NULL DEFAULT 0,
    next_poll_at    TEXT,
    last_polled_at  TEXT,
    last_success_at TEXT,
    cursor          TEXT,
    error_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_sources_due
    ON sources(next_poll_at) WHERE paused = 0;

ALTER TABLE content ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE content ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1;
ALTER TABLE content ADD COLUMN superseded_by TEXT REFERENCES content(hash);
ALTER TABLE content ADD COLUMN source_id TEXT REFERENCES sources(id);

CREATE INDEX IF NOT EXISTS idx_content_url_current
    ON content(source_url, is_current);
CREATE INDEX IF NOT EXISTS idx_content_source
    ON content(source_id, is_current);

INSERT OR IGNORE INTO schema_version (version) VALUES (2);
