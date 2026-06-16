-- 008: add events(type) index for reactor dedupe hot-path scans (#172).
--
-- Reactor on_retrieved now checks for prior stale_marked / refresh_requested
-- events by hash. Those lookups always filter on `type = ?`; indexing type bounds
-- the scan before payload JSON extraction.
--
-- Idempotent (CREATE INDEX IF NOT EXISTS) but still version-gated.
PRAGMA foreign_keys = ON;

CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

INSERT OR IGNORE INTO schema_version (version) VALUES (8);
