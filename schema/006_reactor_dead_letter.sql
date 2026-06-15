-- 006: reactor retry budget + dead-letter for deterministically-failing handlers (#115).
--
-- #111 (issue #85) made the reactor stop advancing its cursor when a handler
-- raised, so transient failures retry instead of silently dropping content. The
-- trade-off was head-of-line blocking: a *parseable* event whose handler fails
-- DETERMINISTICALLY (a code bug, or a permanently-bad-but-parseable payload) would
-- block every later event indefinitely, with no retry cap and no escape hatch.
--
-- This migration adds the persistence for a bounded retry budget:
--   * reactor_attempts - per-event handler-failure counter. Incremented once per
--                        poll cycle the handler raises on that event; cleared when
--                        the event finally succeeds (transient recovery).
--   * dead_letter      - terminal record for events that exhausted the budget. The
--                        reactor writes the row, drops the attempts counter, and
--                        advances its cursor past the event, so one permanently-
--                        failing event can no longer stall the whole stream.
--
-- This is DISTINCT from the poison path (#84/#101), which dead-letters
-- UNPARSEABLE payloads -- a different failure class that is unchanged here.
--
-- Idempotent (CREATE TABLE IF NOT EXISTS) but still gated by schema_version like
-- the prior migrations. Apply once.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS reactor_attempts (
    event_id        INTEGER PRIMARY KEY,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_ts TEXT
);

CREATE TABLE IF NOT EXISTS dead_letter (
    event_id         INTEGER PRIMARY KEY,
    event_type       TEXT NOT NULL,
    attempts         INTEGER NOT NULL,
    error            TEXT,
    dead_lettered_ts TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version (version) VALUES (6);
