-- 007: enforce at most one current revision per source_url (#153).
--
-- The dedup gate keeps exactly one is_current=1 row per source_url, but nothing
-- enforced it at the storage layer, so two connections racing a supersede could
-- both insert is_current=1 at the same revision and leave TWO current rows for a
-- single source_url -- a silent data-integrity break. This adds the hard
-- backstop: a UNIQUE partial index over source_url restricted to is_current=1
-- rows. In normal use the gate inserts a revision non-current and only then
-- promotes it (demote-before-promote), so it never trips the index; the index
-- only fires on a true cross-connection race, which the gate catches and retries.
--
-- NULL source_url is intentionally exempt: SQLite treats NULLs as DISTINCT in a
-- UNIQUE index, so agent-derived rows (no source_url) stay unconstrained.
--
-- Step 1 first REPAIRS any pre-existing duplicate-current rows -- otherwise
-- CREATE UNIQUE INDEX would fail on them. For each source_url with more than one
-- is_current=1 row, the canonical live row (highest revision, then highest
-- rowid) stays current and the rest are demoted to is_current=0. The predicate
-- demotes a current row only when a strictly-more-canonical current sibling
-- exists, so the surviving row is never demoted and the result is order- and
-- mutation-independent. Step 2 then creates the index.
--
-- NOT idempotent past the schema_version gate; apply once. Plain DDL/DML (no
-- compound statements), applied atomically by the migration runner.
PRAGMA foreign_keys = ON;

UPDATE content
SET is_current = 0
WHERE is_current = 1
  AND source_url IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM content AS other
      WHERE other.source_url = content.source_url
        AND other.is_current = 1
        AND (
            other.revision > content.revision
            OR (other.revision = content.revision AND other.rowid > content.rowid)
        )
  );

CREATE UNIQUE INDEX IF NOT EXISTS idx_content_one_current_per_url
    ON content(source_url) WHERE is_current = 1;

INSERT OR IGNORE INTO schema_version (version) VALUES (7);
