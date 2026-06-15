"""#115: retry budget + dead-letter for deterministically-failing handlers.

#111 (issue #85) made the reactor stop advancing its cursor when a handler
raised, so transient failures retry instead of silently dropping content. The
trade-off was head-of-line blocking: a *parseable* event whose handler fails
DETERMINISTICALLY (a code bug, or a permanently-bad-but-parseable payload) would
block every later event indefinitely.

The fix adds a bounded retry budget: a handler failure is retried once per poll
cycle up to ``MAX_HANDLER_ATTEMPTS``; after that the event is moved to the
``dead_letter`` table and the cursor advances past it, so the stream can never be
stalled forever -- while transient failures still retry (preserving #111's
no-silent-drop guarantee).

This is distinct from the poison path (#84/#101), which dead-letters
*unparseable* payloads; that class is covered by test_reactor_poison.py.
"""
import sqlite3
import struct
from types import SimpleNamespace

import pytest
import sqlite_vec

from engram.common.db import init_schema

DIM = 4


def _conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.sqlite")
    c.row_factory = sqlite3.Row
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)

    init_schema(c, embed_dim=DIM)
    return c


def _seed_content(conn, h, body):
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, kind, tombstoned) "
        "VALUES (?, ?, ?, 'https://x/p', 'vendor-doc', 0.7, 'kb', 0)",
        (h, body, body),
    )


class _StopTick(Exception):
    """Sentinel raised from a patched time.sleep to end run() after N ticks."""


def _cfg():
    return SimpleNamespace(
        rag=SimpleNamespace(chunk_size_tokens=512, chunk_overlap_tokens=64,
                            near_dup_threshold=0.92),
        reactor=SimpleNamespace(retrieval_staleness_threshold=0.5),
    )


def _vec(*vals):
    """sqlite-vec compatible vector encoding."""
    return struct.pack(f"{len(vals)}f", *vals)


def _patch_handlers(monkeypatch):
    """Give the handler stubs a no-op vector so embedder/chunker don't crash."""
    from engram.reactor import handlers as H

    monkeypatch.setattr(H, "load_config", lambda: _cfg())
    monkeypatch.setattr(H.embedder, "embed_one", lambda text: _vec(1.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(H.chunker, "chunk_markdown", lambda *a, **k: ["chunk"])
    monkeypatch.setattr(H.chunker, "embed_prefix", lambda body, n: body)


def _stopper(max_ticks):
    """A time.sleep replacement that ends run() after `max_ticks` completed ticks."""
    count = [0]

    def _stop(_):
        count[0] += 1
        if count[0] >= max_ticks:
            raise _StopTick

    return _stop


# ---------------------------------------------------------------------------
# (a) A transiently-failing event is retried and eventually succeeds: the cursor
#     advances and its content is NOT dropped (no dead-letter).
# ---------------------------------------------------------------------------

def test_transient_failure_retries_then_succeeds(tmp_path, monkeypatch):
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import handlers as H
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    h = content_hash("body transient")
    _seed_content(conn, h, "body transient")

    evt_id = event_log.append(conn, "ingested", {"hash": h})
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)
    monkeypatch.setattr(rmod, "MAX_HANDLER_ATTEMPTS", 3)
    _patch_handlers(monkeypatch)

    attempts = {"n": 0}

    def _transient(conn, evt):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("embedder temporarily unavailable")
        H.on_ingested(conn, evt)

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _transient)
    monkeypatch.setattr(rmod.time, "sleep", _stopper(2))  # tick1: fail, tick2: succeed
    with pytest.raises(_StopTick):
        rmod.run()

    # Retried once, then succeeded.
    assert attempts["n"] == 2

    # Cursor advanced past the event (content committed, not dropped).
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == evt_id

    # The content really was embedded (no silent drop).
    assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 1

    # No dead-letter, and the retry counter was cleared on success.
    assert conn.execute("SELECT count(*) FROM dead_letter").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM reactor_attempts").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# (b) A deterministically-failing event is retried up to the threshold, then
#     dead-lettered; the cursor advances PAST it and later events are processed
#     (no permanent head-of-line block).
# ---------------------------------------------------------------------------

def test_deterministic_failure_dead_lettered_and_stream_advances(tmp_path, monkeypatch):
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)

    ok1 = event_log.append(conn, "ingested", {"hash": content_hash("body ok1")})
    fail_id = event_log.append(conn, "ingested", {"hash": content_hash("body fail")})
    ok2 = event_log.append(conn, "ingested", {"hash": content_hash("body ok2")})
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)
    monkeypatch.setattr(rmod, "MAX_HANDLER_ATTEMPTS", 3)

    calls = []

    def _deterministic(conn, evt):
        calls.append(evt.id)
        if evt.id == fail_id:
            raise RuntimeError("permanently bad payload")

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _deterministic)
    # 3 ticks: the failing event is attempted once per tick until the budget is
    # spent on tick 3, where it is dead-lettered and ok2 is processed.
    monkeypatch.setattr(rmod.time, "sleep", _stopper(3))
    with pytest.raises(_StopTick):
        rmod.run()

    # The failing event was retried exactly MAX_HANDLER_ATTEMPTS times.
    assert calls.count(fail_id) == 3

    # The event AFTER the failing one was eventually processed (no permanent block).
    assert ok2 in calls

    # Cursor advanced past the dead-lettered event all the way to the last event.
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == ok2

    # The good event before the failure was processed too.
    assert ok1 in calls


# ---------------------------------------------------------------------------
# (c) The dead-letter record is created with the expected fields.
# ---------------------------------------------------------------------------

def test_dead_letter_record_created(tmp_path, monkeypatch):
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    fail_id = event_log.append(conn, "ingested", {"hash": content_hash("body fail")})
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)
    monkeypatch.setattr(rmod, "MAX_HANDLER_ATTEMPTS", 3)

    def _always_fail(conn, evt):
        raise RuntimeError("boom: deterministic handler bug")

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _always_fail)
    monkeypatch.setattr(rmod.time, "sleep", _stopper(3))
    with pytest.raises(_StopTick):
        rmod.run()

    row = conn.execute(
        "SELECT event_id, event_type, attempts, error, dead_lettered_ts "
        "FROM dead_letter WHERE event_id = ?",
        (fail_id,),
    ).fetchone()
    assert row is not None
    assert row["event_id"] == fail_id
    assert row["event_type"] == "ingested"
    assert row["attempts"] == 3
    assert "boom: deterministic handler bug" in row["error"]
    assert row["dead_lettered_ts"]  # non-empty timestamp

    # Once dead-lettered, the transient attempt counter is cleared.
    assert conn.execute(
        "SELECT count(*) FROM reactor_attempts WHERE event_id = ?", (fail_id,)
    ).fetchone()[0] == 0

    # And the cursor advanced past it (stream no longer blocked).
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == fail_id


# ---------------------------------------------------------------------------
# (d) Even if the dead-letter WRITE itself raises, the reactor must still advance
#     past the budget-exhausted event and process later events -- a DLQ write
#     failure must not permanently re-block the stream. (Fails on pre-fix code,
#     where the exception escaped to the tick-level handler and wedged the loop.)
# ---------------------------------------------------------------------------

def test_dead_letter_write_failure_still_advances(tmp_path, monkeypatch):
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    fail_id = event_log.append(conn, "ingested", {"hash": content_hash("body fail")})
    ok_id = event_log.append(conn, "ingested", {"hash": content_hash("body ok")})
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)
    monkeypatch.setattr(rmod, "MAX_HANDLER_ATTEMPTS", 3)

    calls = []

    def _deterministic(conn, evt):
        calls.append(evt.id)
        if evt.id == fail_id:
            raise RuntimeError("permanently bad payload")

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _deterministic)

    # The dead-letter write itself blows up (DB error / lock / schema drift).
    def _boom_dead_letter(*a, **k):
        raise sqlite3.OperationalError("dead_letter write exploded")

    monkeypatch.setattr(rmod, "_dead_letter", _boom_dead_letter)

    # 3 ticks: budget is spent on tick 3, where the dead-letter write fails.
    monkeypatch.setattr(rmod.time, "sleep", _stopper(3))
    with pytest.raises(_StopTick):
        rmod.run()

    # Despite the failed DLQ write, the cursor advanced PAST the exhausted event
    # and the later event was processed -- the stream is NOT permanently blocked.
    assert ok_id in calls
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == ok_id
