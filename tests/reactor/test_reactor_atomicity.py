"""Unit C: reactor handler atomicity (#152) and cursor-vs-effects atomicity (#153).

* #152 -- each handler's multi-statement side-effects run in ONE transaction, so a
  failure partway rolls the whole handler back (no embedded row without its
  merged event, no tombstone with the embedding still present, etc.).
* #153 -- each event's handler side-effects AND its cursor advance commit in ONE
  transaction, so a crash between a processed event's effects and the cursor write
  leaves the cursor at the last fully-processed event -- the event is NOT replayed
  and its non-idempotent events (merged / stale_marked / refresh_requested) are
  NOT re-emitted on the next loop.

These use an autocommit (isolation_level=None) connection to match the production
reactor connection (common.db._connect), so the handlers' / loop's transactions
always own a real BEGIN/COMMIT and atomicity is deterministic.
"""
import sqlite3
import struct
from types import SimpleNamespace

import pytest
import sqlite_vec

from engram import log as event_log
from engram.common.db import init_schema
from engram.reactor import handlers as H
from engram.reactor import reactor as rmod

DIM = 4


def _conn(tmp_path):
    # isolation_level=None mirrors the production reactor connection.
    c = sqlite3.connect(tmp_path / "t.sqlite", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    init_schema(c, embed_dim=DIM)
    return c


def _vec(*xs):
    return struct.pack(f"{len(xs)}f", *xs)


def _cfg():
    return SimpleNamespace(
        rag=SimpleNamespace(chunk_size_tokens=512, chunk_overlap_tokens=64,
                            near_dup_threshold=0.92),
        reactor=SimpleNamespace(retrieval_staleness_threshold=0.5),
    )


def _add(conn, h, body, *, tombstoned=0, is_current=1):
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, fetched_at, "
        "confidence, kind, tombstoned, is_current) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (h, h, body, None, "manual", "2026-06-10T00:00:00Z", 0.8, "kb", tombstoned, is_current),
    )


class _StopTick(Exception):
    """Sentinel raised from a patched time.sleep to end the loop after N ticks."""


def _stopper(max_ticks):
    count = [0]

    def _stop(_):
        count[0] += 1
        if count[0] >= max_ticks:
            raise _StopTick

    return _stop


# --- #152: handler side-effects are atomic -----------------------------------


def _patch_embed(monkeypatch, vec):
    monkeypatch.setattr(H, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(H.embedder, "embed_one", lambda text: vec)
    monkeypatch.setattr(H.chunker, "chunk_markdown", lambda *a, **k: ["chunk"])
    monkeypatch.setattr(H.chunker, "embed_prefix", lambda body, n: body)


def test_on_ingested_merge_rolls_back_atomically_on_failure(tmp_path, monkeypatch):
    """A failure at the merged-event append (the last step of the near-dup merge)
    rolls back the ENTIRE handler: the content is not left tombstoned, no merged
    event is recorded, and the embed write is undone too -- no partial state."""
    conn = _conn(tmp_path)
    _add(conn, "A", "the quick brown fox")
    conn.execute("INSERT INTO embeddings (content_hash, embedding) VALUES (?, ?)",
                 ("A", _vec(1.0, 0.0, 0.0, 0.0)))
    _add(conn, "B", "a wholly different sentence")  # embeds identically to A -> near-dup
    _patch_embed(monkeypatch, _vec(1.0, 0.0, 0.0, 0.0))

    # Blow up at the `merged` event append, AFTER the tombstone + embedding delete.
    def _boom(c, etype, *a, **k):
        raise RuntimeError("event append exploded")

    monkeypatch.setattr(H.event_log, "append", _boom)

    evt = SimpleNamespace(type="ingested", payload={"hash": "B"}, id=1)
    with pytest.raises(RuntimeError, match="exploded"):
        H.on_ingested(conn, evt)

    # The whole handler rolled back: B is NOT tombstoned and no merged event exists.
    assert conn.execute("SELECT tombstoned FROM content WHERE hash='B'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE type='merged'").fetchone()[0] == 0


def test_on_retrieved_rolls_back_atomically_on_failure(tmp_path, monkeypatch):
    """A failure mid-handler in on_retrieved rolls back the staleness bump AND any
    emitted stale_marked/refresh_requested -- never a bumped score with no event."""
    conn = _conn(tmp_path)
    # A stale, sourced row: on_retrieved bumps staleness and emits BOTH stale_marked
    # and refresh_requested. We fail at the SECOND append (refresh_requested).
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, confidence, "
        "kind, tombstoned, fetched_at, ttl_days) "
        "VALUES ('s', 't', 'b', 'https://x/p', 'manual', 0.8, 'kb', 0, '2000-01-01T00:00:00Z', 1)"
    )
    monkeypatch.setattr(H, "load_config", lambda *a, **k: _cfg())

    calls = {"n": 0}
    real_append = H.event_log.append

    def _boom_second(c, etype, *a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:  # let stale_marked through, blow up on refresh_requested
            raise RuntimeError("second append exploded")
        return real_append(c, etype, *a, **k)

    monkeypatch.setattr(H.event_log, "append", _boom_second)

    evt = SimpleNamespace(type="retrieved", payload={"hashes": ["s"], "query": "q", "count": 1}, id=1)
    with pytest.raises(RuntimeError, match="second append exploded"):
        H.on_retrieved(conn, evt)

    # Rolled back wholesale: staleness_score unchanged, and NEITHER event persisted
    # (not even the first stale_marked that had already been appended in-txn).
    assert conn.execute("SELECT staleness_score FROM content WHERE hash='s'").fetchone()[0] == 0.0
    assert conn.execute("SELECT count(*) FROM events WHERE type='stale_marked'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE type='refresh_requested'").fetchone()[0] == 0


# --- #153: handler effects + cursor advance are one transaction ---------------


def test_crash_before_cursor_write_does_not_reemit(tmp_path, monkeypatch):
    """A crash between a processed event's handler effects and the cursor write
    must NOT replay/re-emit the event. With per-event atomicity, the failed
    attempt's stale_marked rolls back together with the un-written cursor, so the
    next loop processes the event exactly once -> exactly ONE stale_marked."""
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, confidence, "
        "kind, tombstoned, fetched_at, ttl_days) "
        "VALUES ('s', 't', 'b', NULL, 'manual', 0.8, 'kb', 0, '2000-01-01T00:00:00Z', 1)"
    )
    evt_id = event_log.append(conn, "retrieved", {"hashes": ["s"], "query": "q", "count": 1})

    monkeypatch.setattr(H, "load_config", lambda *a, **k: _cfg())

    # The cursor write fails on the FIRST attempt (crash after the handler effects,
    # before the cursor persists), then succeeds.
    real_write = rmod._write_cursor
    wc = {"n": 0}

    def _flaky_write(c, last):
        wc["n"] += 1
        if wc["n"] == 1:
            raise sqlite3.OperationalError("cursor write crashed")
        real_write(c, last)

    monkeypatch.setattr(rmod, "_write_cursor", _flaky_write)
    monkeypatch.setattr(rmod.time, "sleep", _stopper(2))  # tick1 crashes, tick2 succeeds
    with pytest.raises(_StopTick):
        rmod._run_loop(conn)

    # Exactly ONE stale_marked despite the tick-1 crash: the first attempt's
    # emission rolled back atomically with the un-written cursor; only tick 2
    # committed. (Without per-event atomicity this would be TWO.)
    assert conn.execute(
        "SELECT count(*) FROM events WHERE type='stale_marked'"
    ).fetchone()[0] == 1
    # The cursor advanced past the event once tick 2 committed.
    assert conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name='reactor'"
    ).fetchone()["last_event_id"] == evt_id


def test_double_write_failure_does_not_advance_or_lose_event(tmp_path, monkeypatch):
    """BLOCKING regression: when a budget-exhausted event's dead-letter+cursor
    transaction fails AND the fallback cursor-only write ALSO fails, the cursor
    must NOT advance past the event. The event stays pending and is retried from
    the persisted cursor on the next pass -- it is never silently skipped/lost.

    Distinguishing: with the old unconditional `return True`, the in-memory cursor
    advanced past the failing event despite nothing being persisted, so the loop
    moved on to the LATER event (skipping the failed one). With the fix, the loop
    breaks and re-reads from the persisted cursor, so the later event is never
    reached while the first one is unresolved.
    """
    conn = _conn(tmp_path)
    fail_id = event_log.append(conn, "ingested", {"hash": "x"})
    later_id = event_log.append(conn, "ingested", {"hash": "y"})

    monkeypatch.setattr(rmod, "MAX_HANDLER_ATTEMPTS", 1)  # exhaust the budget at once

    seen = []

    def _always_fail(conn, evt):
        seen.append(evt.id)
        raise RuntimeError("permanent handler failure")

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _always_fail)

    # BOTH the dead-letter write transaction and the fallback cursor write fail.
    def _boom_dead_letter(*a, **k):
        raise sqlite3.OperationalError("dead_letter write exploded")

    def _boom_cursor(*a, **k):
        raise sqlite3.OperationalError("cursor write exploded")

    monkeypatch.setattr(rmod, "_dead_letter", _boom_dead_letter)
    monkeypatch.setattr(rmod, "_write_cursor", _boom_cursor)

    monkeypatch.setattr(rmod.time, "sleep", _stopper(2))  # two scheduled passes
    with pytest.raises(_StopTick):
        rmod._run_loop(conn)

    # The cursor was never durably advanced past the failing event (no row, or
    # still behind it) -- the event is not lost.
    row = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name='reactor'"
    ).fetchone()
    assert row is None or row["last_event_id"] < fail_id

    # The loop never skipped the failing event to reach the later one (which would
    # be the symptom of the in-memory cursor advancing without durable persistence).
    assert later_id not in seen
    assert fail_id in seen


def test_handler_effects_and_cursor_commit_together(tmp_path, monkeypatch):
    """Happy path: a processed event's effects and its cursor advance both land."""
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, confidence, "
        "kind, tombstoned, fetched_at, ttl_days) "
        "VALUES ('s', 't', 'b', NULL, 'manual', 0.8, 'kb', 0, '2000-01-01T00:00:00Z', 1)"
    )
    evt_id = event_log.append(conn, "retrieved", {"hashes": ["s"], "query": "q", "count": 1})
    monkeypatch.setattr(H, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(rmod.time, "sleep", _stopper(1))
    with pytest.raises(_StopTick):
        rmod._run_loop(conn)

    assert conn.execute(
        "SELECT count(*) FROM events WHERE type='stale_marked'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name='reactor'"
    ).fetchone()["last_event_id"] == evt_id
