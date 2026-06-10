-- Usage counters for retrieval grounding (#39). Maintained from `cited` events;
-- fully rebuildable from the event log (the log stays canonical).
--
-- Intentionally NO foreign key to content(hash): this is a derived cache, not a
-- relation. cited events are the source of truth and rebuild_usage() recomputes
-- this table from them, dropping any stale rows. Decoupling it from content's
-- lifecycle is the point — do not add a FK here.
CREATE TABLE IF NOT EXISTS content_usage (
    content_hash   TEXT PRIMARY KEY,
    use_count      INTEGER NOT NULL DEFAULT 0,
    last_cited_at  TEXT
);
INSERT OR IGNORE INTO schema_version (version) VALUES (3);
