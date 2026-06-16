-- 008: projector retry budget + dead-letter for deterministically-failing handlers (#158).
--
-- Mirrors 006_reactor_dead_letter.sql for the projector loop: parseable events
-- whose projection path raises are retried with a bounded budget, then moved to
-- a projector-specific dead-letter table so the cursor can advance past a
-- permanently-failing event instead of head-of-line-blocking all later events.
--
-- Distinct from poison payload handling (#84), which covers unparseable JSON
-- event rows yielded as Event.poison.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projector_attempts (
    event_id        INTEGER PRIMARY KEY,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_attempt_ts TEXT
);

CREATE TABLE IF NOT EXISTS projector_dead_letter (
    event_id         INTEGER PRIMARY KEY,
    event_type       TEXT NOT NULL,
    attempts         INTEGER NOT NULL,
    error            TEXT,
    dead_lettered_ts TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version (version) VALUES (8);
