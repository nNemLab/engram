"""#85: handler failure must not advance the cursor — prove back-pressure behaviour.

When a handler raises on event N, the cursor must stay at N-1 so that event N
is retried on the next poll cycle.  Events successfully processed before N must
still be committed.  An all-success tick advances the cursor to the last event.
"""
import sqlite3
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
    """Sentinel raised from a patched time.sleep to end run() after one tick."""


def _cfg():
    return SimpleNamespace(
        rag=SimpleNamespace(chunk_size_tokens=512, chunk_overlap_tokens=64,
                            near_dup_threshold=0.92),
        reactor=SimpleNamespace(retrieval_staleness_threshold=0.5),
    )


def _vec(*vals):
    """sqlite-vec compatible vector encoding."""
    import struct
    return struct.pack(f"{len(vals)}f", *vals)


def _patch_handlers(monkeypatch, dim):
    """Give the handler stubs a no-op vector so embedder/chunker don't crash."""
    from engram.reactor import handlers as H

    monkeypatch.setattr(H, "load_config", lambda: _cfg())
    monkeypatch.setattr(H.embedder, "embed_one", lambda text: _vec(1.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(H.chunker, "chunk_markdown", lambda *a, **k: ["chunk"])
    monkeypatch.setattr(H.chunker, "embed_prefix", lambda body, n: body)


# ---------------------------------------------------------------------------
# Test A: handler raises → cursor stays at previous → event retried next cycle
# ---------------------------------------------------------------------------

def test_handler_failure_stays_on_failed_event(tmp_path, monkeypatch):
    """When handler raises on event N, cursor must NOT advance past N."""
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import handlers as H
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    h_fail = content_hash("body fail")
    _seed_content(conn, h_fail, "body fail")

    # Two events: first succeeds, second raises.
    event_log.append(conn, "ingested", {"hash": content_hash("body ok")})
    fail_id = event_log.append(conn, "ingested", {"hash": h_fail})
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)
    _patch_handlers(monkeypatch, DIM)

    # Make the handler raise.
    original_ingested = H.on_ingested
    call_count = {"n": 0}

    def _fail(conn, evt):
        call_count["n"] += 1
        if evt.id == fail_id:
            raise RuntimeError("embedder temporarily unavailable")
        original_ingested(conn, evt)

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _fail)

    def _stop(_):
        raise _StopTick

    monkeypatch.setattr(rmod.time, "sleep", _stop)
    with pytest.raises(_StopTick):
        rmod.run()

    # Handler was called twice: once for the good event, once for the failing event.
    assert call_count["n"] == 2

    # Cursor stayed at 1 (after the good event), NOT at 2 (after the failing one).
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == 1  # last successful event


# ---------------------------------------------------------------------------
# Test B: events before the failing one ARE committed
# ---------------------------------------------------------------------------

def test_preceding_events_committed_on_failure(tmp_path, monkeypatch):
    """Cursor must advance through all successful events before the failing one."""
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import handlers as H
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    h_ok1 = content_hash("body ok1")
    h_ok2 = content_hash("body ok2")
    h_fail = content_hash("body fail")

    # Three events: ok, ok, fail.
    event_log.append(conn, "ingested", {"hash": h_ok1})
    event_log.append(conn, "ingested", {"hash": h_ok2})
    fail_id = event_log.append(conn, "ingested", {"hash": h_fail})
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)
    _patch_handlers(monkeypatch, DIM)

    call_order = []

    def _failing_handler(conn, evt):
        call_order.append(evt.id)
        if evt.id == fail_id:
            raise RuntimeError("transient failure")
        H.on_ingested(conn, evt)

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _failing_handler)

    def _stop(_):
        raise _StopTick

    monkeypatch.setattr(rmod.time, "sleep", _stop)
    with pytest.raises(_StopTick):
        rmod.run()

    # All three events hit the handler.
    assert call_order == [1, 2, fail_id]

    # Cursor advanced to event 2 (the last successful one), not 3 (the failing one).
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == 2


# ---------------------------------------------------------------------------
# Test C: all-success → cursor advances normally to last event
# ---------------------------------------------------------------------------

def test_all_success_advances_cursor(tmp_path, monkeypatch):
    """When all events succeed, cursor advances to the last event as before."""
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    h1 = content_hash("body A")
    h2 = content_hash("body B")
    h3 = content_hash("body C")

    event_log.append(conn, "ingested", {"hash": h1})
    event_log.append(conn, "ingested", {"hash": h2})
    event_log.append(conn, "ingested", {"hash": h3})
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)
    _patch_handlers(monkeypatch, DIM)

    def _stop(_):
        raise _StopTick

    monkeypatch.setattr(rmod.time, "sleep", _stop)
    with pytest.raises(_StopTick):
        rmod.run()

    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == 3  # all three events processed


# ---------------------------------------------------------------------------
# Test D: first event fails → cursor stays at 0 (prior cursor)
# ---------------------------------------------------------------------------

def test_first_event_failure_keeps_zero_cursor(tmp_path, monkeypatch):
    """When the very first event fails, cursor must remain at its initial value."""
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    h_fail = content_hash("body fail")
    _seed_content(conn, h_fail, "body fail")

    # Only one event, which will fail.
    event_log.append(conn, "ingested", {"hash": h_fail})
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)
    _patch_handlers(monkeypatch, DIM)

    def _fail(conn, evt):
        raise RuntimeError("embedder down")

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _fail)

    def _stop(_):
        raise _StopTick

    monkeypatch.setattr(rmod.time, "sleep", _stop)
    with pytest.raises(_StopTick):
        rmod.run()

    # Cursor stayed at 0 (the initial value before any event).
    # _write_cursor is only called when last != cursor; with the very first
    # event failing, last == cursor == 0 so no row is written.
    cursor_row = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()
    assert cursor_row is None or cursor_row["last_event_id"] == 0


# ---------------------------------------------------------------------------
# Test E: second of three fails → events 1 & 2 committed, 3 retried
# ---------------------------------------------------------------------------

def test_mixed_success_failure_then_recovery(tmp_path, monkeypatch):
    """Events before the failure are committed; after failure, the failed event
    is retried on the next tick (proven by a two-tick run)."""
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import handlers as H
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    h_ok = content_hash("body ok")
    h_fail = content_hash("body fail")
    h_ok2 = content_hash("body ok2")

    # Four events: ok, fail, ok, ok.
    event_log.append(conn, "ingested", {"hash": h_ok})
    fail_id = event_log.append(conn, "ingested", {"hash": h_fail})
    event_log.append(conn, "ingested", {"hash": h_ok2})
    event_log.append(conn, "ingested", {"hash": content_hash("body ok3")})
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)
    _patch_handlers(monkeypatch, DIM)

    call_count = {"n": 0}
    tick = {"n": 0}

    def _failing_handler(conn, evt):
        call_count["n"] += 1
        # Succeed after first failure (simulating transient recover).
        if evt.id == fail_id:
            tick["n"] += 1
            if tick["n"] == 1:
                raise RuntimeError("transient failure")
        H.on_ingested(conn, evt)

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _failing_handler)

    sleep_count = [0]
    max_sleeps = 3  # enough for tick 1 (stop after) + tick 2 (stop after)

    def _stop(_):
        sleep_count[0] += 1
        if sleep_count[0] >= max_sleeps:
            raise _StopTick

    monkeypatch.setattr(rmod.time, "sleep", _stop)
    with pytest.raises(_StopTick):
        rmod.run()

    # The failing event was attempted twice (first tick: fail, second tick: succeed).
    assert call_count["n"] == 5  # tick1: 4 events (1 fail); tick2: 3 events (fail now ok)
    # Cursor after both ticks = 4 (all events processed).
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == 4
