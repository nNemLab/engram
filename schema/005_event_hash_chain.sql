-- 005: tamper-evident hash chain over the event log (#45).
--
-- Each new event carries event_hash = SHA-256 over its canonical fields plus the
-- prev_hash (the event_hash of the chain head at insert time), forming a chain so
-- any retroactive edit of an event row is detectable by maintenance.verify.
--
-- Chain genesis is the migration boundary: rows that predate this migration keep
-- event_hash = NULL and are NOT chained. verify walks only rows with a non-null
-- event_hash, so pre-chain rows never false-positive. The first event appended
-- after this migration has prev_hash = '' (empty, the genesis marker).
--
-- NOT idempotent (SQLite has no ADD COLUMN IF NOT EXISTS); gated by
-- schema_version like 002/003/004. Apply once.
PRAGMA foreign_keys = ON;

ALTER TABLE events ADD COLUMN prev_hash  TEXT;
ALTER TABLE events ADD COLUMN event_hash TEXT;

INSERT OR IGNORE INTO schema_version (version) VALUES (5);
