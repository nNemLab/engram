-- 004: protect human-edited rows from silent supersede clobber (#37).
-- A human edit (watcher vault_edit) sets content.protected = 1. The dedup gate
-- refuses to supersede a protected row, raising a contradiction instead.
--
-- NOT idempotent (SQLite has no ADD COLUMN IF NOT EXISTS); gated by
-- schema_version like 002/003. Apply once.
PRAGMA foreign_keys = ON;

ALTER TABLE content ADD COLUMN protected INTEGER NOT NULL DEFAULT 0;

INSERT OR IGNORE INTO schema_version (version) VALUES (4);
