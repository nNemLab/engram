-- Engram event log schema
-- Append-only log is canonical; vault is a materialized view.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- =============================================================
-- events: append-only log of everything that happened
-- =============================================================
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    type            TEXT    NOT NULL,
    payload         TEXT    NOT NULL,            -- JSON
    actor           TEXT,                        -- 'agent' | 'human' | 'reactor' | 'cron' | 'system'
    correlation_id  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_type_ts      ON events(type, ts);
CREATE INDEX IF NOT EXISTS idx_events_correlation  ON events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_events_ts           ON events(ts);

-- Known event types (documented, not enforced):
--   ingested(hash, source, title)
--   merged(hash_kept, hash_tombstoned, reason)
--   contradicted(hash_a, hash_b, detected_by)
--   retrieved(hash, query)
--   stale_marked(hash, score)
--   refresh_requested(hash, source_url)
--   goal_set(goal_id, text)
--   goal_resolved(goal_id)
--   vault_edit(path, hash, diff)
--   playbook_run(run_id, playbook, params, summary_hash)
--   system(component, message, level)

-- =============================================================
-- content: deduplicated content store, addressed by SHA-256
-- =============================================================
CREATE TABLE IF NOT EXISTS content (
    hash             TEXT    PRIMARY KEY,        -- SHA-256 of normalized body
    body             TEXT    NOT NULL,
    title            TEXT,
    source_url       TEXT,
    source_tier      TEXT,                       -- 'peer-reviewed'|'vendor-doc'|'blog'|'forum'|'agent-derived'|'manual'
    fetched_at       TEXT,
    confidence       REAL    NOT NULL DEFAULT 0.5,
    staleness_score  REAL    NOT NULL DEFAULT 0.0,
    ttl_days         INTEGER,
    vault_path       TEXT,                       -- path relative to vault root, NULL = not projected
    kind             TEXT    NOT NULL DEFAULT 'kb', -- 'kb'|'episode'|'entity'|'research'|'playbook-summary'
    tombstoned       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_content_vault      ON content(vault_path);
CREATE INDEX IF NOT EXISTS idx_content_kind       ON content(kind) WHERE tombstoned = 0;
CREATE INDEX IF NOT EXISTS idx_content_staleness  ON content(staleness_score) WHERE tombstoned = 0;
CREATE INDEX IF NOT EXISTS idx_content_fetched    ON content(fetched_at);

-- =============================================================
-- content_fts: BM25 full-text search over content
-- =============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    hash UNINDEXED,
    title,
    body,
    tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS content_ai AFTER INSERT ON content BEGIN
    INSERT INTO content_fts(rowid, hash, title, body)
    VALUES (new.rowid, new.hash, COALESCE(new.title, ''), new.body);
END;

CREATE TRIGGER IF NOT EXISTS content_ad AFTER DELETE ON content BEGIN
    DELETE FROM content_fts WHERE rowid = old.rowid;
END;

CREATE TRIGGER IF NOT EXISTS content_au AFTER UPDATE ON content BEGIN
    DELETE FROM content_fts WHERE rowid = old.rowid;
    INSERT INTO content_fts(rowid, hash, title, body)
    VALUES (new.rowid, new.hash, COALESCE(new.title, ''), new.body);
END;

-- =============================================================
-- embeddings: vector store (sqlite-vec virtual table)
-- Created at runtime after the sqlite-vec extension is loaded.
-- Schema: vec0(content_hash TEXT PRIMARY KEY, embedding FLOAT[384])
-- See src/engram/common/db.py: ensure_vec_table()
-- =============================================================

-- =============================================================
-- entities: semantic memory — people, tools, concepts
-- =============================================================
CREATE TABLE IF NOT EXISTS entities (
    id           TEXT    PRIMARY KEY,            -- slug
    name         TEXT    NOT NULL,
    kind         TEXT,                           -- 'person'|'tool'|'concept'|'org'|...
    aliases      TEXT,                           -- JSON array
    description  TEXT,
    vault_path   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id     TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    PRIMARY KEY (entity_id, content_hash),
    FOREIGN KEY (entity_id)    REFERENCES entities(id)    ON DELETE CASCADE,
    FOREIGN KEY (content_hash) REFERENCES content(hash)   ON DELETE CASCADE
);

-- =============================================================
-- goals: active investigations driving agentic behavior
-- =============================================================
CREATE TABLE IF NOT EXISTS goals (
    id          TEXT    PRIMARY KEY,
    text        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'active',  -- 'active'|'paused'|'resolved'
    priority    INTEGER NOT NULL DEFAULT 0,
    metadata    TEXT,                                -- JSON
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);

-- =============================================================
-- contradictions: surfaces conflicting content for human resolution
-- =============================================================
CREATE TABLE IF NOT EXISTS contradictions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    hash_a           TEXT    NOT NULL,
    hash_b           TEXT    NOT NULL,
    detected_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    detected_by      TEXT,                          -- 'agent'|'reactor'|'human'
    resolved         INTEGER NOT NULL DEFAULT 0,
    resolution       TEXT,                          -- 'kept_a'|'kept_b'|'merged'|'both_valid'
    resolution_note  TEXT,
    FOREIGN KEY (hash_a) REFERENCES content(hash) ON DELETE CASCADE,
    FOREIGN KEY (hash_b) REFERENCES content(hash) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_contradictions_unresolved ON contradictions(resolved) WHERE resolved = 0;

-- =============================================================
-- retrieval_log: lightweight access counter, drives demand-based refresh
-- =============================================================
CREATE TABLE IF NOT EXISTS retrieval_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash  TEXT    NOT NULL,
    query         TEXT,
    ts            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (content_hash) REFERENCES content(hash) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_retrieval_hash_ts ON retrieval_log(content_hash, ts);

-- =============================================================
-- vault_state: tracks last-rendered version of each vault file
-- Used by the watcher to detect manual edits (diff against this).
-- =============================================================
CREATE TABLE IF NOT EXISTS vault_state (
    vault_path     TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,
    rendered_body  TEXT NOT NULL,            -- exact bytes the projector wrote
    rendered_at    TEXT NOT NULL,
    FOREIGN KEY (content_hash) REFERENCES content(hash) ON DELETE CASCADE
);

-- =============================================================
-- schema_version: simple migration tracker
-- =============================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
