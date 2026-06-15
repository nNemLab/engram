"""#101: a single un-parseable event payload must not freeze the reactor.

A poison row (corrupt JSON payload) sitting between two good events must be
dead-lettered and skipped: the cursor advances past it and later good events
are still processed, instead of the loop restarting from the same poison row
forever and silently dropping everything after it.
"""
import sqlite3
import struct
from types import SimpleNamespace

import pytest
import sqlite_vec

from engram.common.db import init_schema

DIM = 4


def _vec(*vals):
    """sqlite-vec compatible vector encoding."""
    return struct.pack(f"{len(vals)}f", *vals)


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


def test_run_skips_poison_event_between_good_events(tmp_path, monkeypatch):
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import handlers as H
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    h_a = content_hash("body A")
    h_b = content_hash("body B")
    _seed_content(conn, h_a, "body A")
    _seed_content(conn, h_b, "body B")

    # event order: good (A) -> poison -> good (B)
    event_log.append(conn, "ingested", {"hash": h_a})
    poison_id = event_log.append(conn, "ingested", {"hash": "unused"})
    conn.execute("UPDATE events SET payload = ? WHERE id = ?", ("{not valid json", poison_id))
    last_id = event_log.append(conn, "ingested", {"hash": h_b})
    conn.commit()

    fake_cfg = SimpleNamespace(
        rag=SimpleNamespace(chunk_size_tokens=512, chunk_overlap_tokens=64,
                            near_dup_threshold=0.92),
        reactor=SimpleNamespace(retrieval_staleness_threshold=0.5),
    )
    # Patch load_config where the handlers import it.
    monkeypatch.setattr(H, "load_config", lambda: fake_cfg)
    # Mock embedder/chunker so on_ingested doesn't crash on good events.
    # Give distinct (orthogonal) vectors to A and B so the near-dup tombstone
    # path is NOT exercised — keeps this test focused on poison-skip /
    # cursor-advance.  Vectors must be sqlite-vec compatible (struct-packed).
    monkeypatch.setattr(
        H.embedder, "embed_one",
        lambda text: _vec(1.0, 0.0, 0.0, 0.0) if "A" in text else _vec(0.0, 1.0, 0.0, 0.0),
    )
    monkeypatch.setattr(H.chunker, "chunk_markdown", lambda *a, **k: ["chunk"])
    monkeypatch.setattr(H.chunker, "embed_prefix", lambda body, n: body)

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)

    def _stop(_):
        raise _StopTick

    monkeypatch.setattr(rmod.time, "sleep", _stop)
    with pytest.raises(_StopTick):
        rmod.run()

    # Both good events were embedded despite the poison row in between.
    assert conn.execute(
        "SELECT count(*) FROM embeddings",
    ).fetchone()[0] == 2

    # The cursor advanced PAST the poison row (loop is not stuck).
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == last_id


def test_poison_event_does_not_trigger_handlers(tmp_path, monkeypatch):
    """A poison row should skip handler dispatch; good events must still dispatch."""
    from engram import log as event_log
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    _seed_content(conn, "hash_good", "body good")

    # Poison event only — no good events at all.
    poison_id = event_log.append(conn, "ingested", {"hash": "unused"})
    conn.execute("UPDATE events SET payload = ? WHERE id = ?", ("{broken", poison_id))
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)

    # Patch the ACTUAL dispatch target: reactor.HANDLERS (bound at import time),
    # NOT H.on_ingested.  Patching the dict guarantees the call-site uses our stub.
    ingest_calls = []

    def _count_calls(conn, evt):
        ingest_calls.append(evt.id)

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _count_calls)

    def _stop(_):
        raise _StopTick

    monkeypatch.setattr(rmod.time, "sleep", _stop)
    with pytest.raises(_StopTick):
        rmod.run()

    # No handler was dispatched for the poison event.
    assert ingest_calls == []

    # Cursor still advanced past the poison row.
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == poison_id


def test_good_ingested_dispatches_handler_poison_does_not(tmp_path, monkeypatch):
    """Positive control: one good event increments counter; poison does not."""
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.reactor import reactor as rmod

    conn = _conn(tmp_path)
    h_good = content_hash("body good")
    _seed_content(conn, h_good, "body good")

    # event order: good (A) -> poison
    good_id = event_log.append(conn, "ingested", {"hash": h_good})
    poison_id = event_log.append(conn, "ingested", {"hash": "unused"})
    conn.execute("UPDATE events SET payload = ? WHERE id = ?", ("{broken", poison_id))
    conn.commit()

    monkeypatch.setattr(rmod, "get_connection", lambda: conn)

    ingest_calls = []

    def _count_calls(conn, evt):
        ingest_calls.append(evt.id)

    monkeypatch.setitem(rmod.HANDLERS, "ingested", _count_calls)

    def _stop(_):
        raise _StopTick

    monkeypatch.setattr(rmod.time, "sleep", _stop)
    with pytest.raises(_StopTick):
        rmod.run()

    # Only the good event triggered the handler; poison was skipped.
    assert ingest_calls == [good_id]

    # Cursor advanced past both events.
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'reactor'"
    ).fetchone()["last_event_id"]
    assert cursor == poison_id
