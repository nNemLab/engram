"""Episodic timeline reconstruction (#40): chronological walk over
ingested / vault_edit / superseded events, optionally scoped to a topic."""
from engram import log as event_log
from engram.rag.timeline import TimelineEntry, reconstruct_timeline
from tests.rag import fresh_conn
from tests.rag.test_query_calibrated import _add


def _ev(conn, type, payload):
    return event_log.append(conn, type, payload)


def test_timeline_orders_events_chronologically(tmp_path):
    conn = fresh_conn(tmp_path)
    # Insert out of intended chronological order to prove we sort by (ts, id).
    _ev(conn, "superseded", {"hash_old": "a", "hash_new": "b", "source_url": "u"})
    _ev(conn, "ingested", {"hash": "a", "title": "A", "source_url": "u"})
    _ev(conn, "vault_edit", {"path": "n.md", "hash": "c", "hash_old": "a", "hash_new": "c"})
    out = reconstruct_timeline(conn)
    assert [e.event for e in out] == ["superseded", "ingested", "vault_edit"]
    assert all(isinstance(e, TimelineEntry) for e in out)
    # id increases monotonically with insertion order.
    assert [e.id for e in out] == sorted(e.id for e in out)


def test_timeline_ignores_unrelated_event_types(tmp_path):
    conn = fresh_conn(tmp_path)
    _ev(conn, "ingested", {"hash": "a", "title": "A"})
    _ev(conn, "retrieved", {"query": "x", "hashes": ["a"], "count": 1})
    _ev(conn, "cited", {"hashes": ["a"]})
    out = reconstruct_timeline(conn)
    assert [e.event for e in out] == ["ingested"]


def test_timeline_window_bounds(tmp_path):
    conn = fresh_conn(tmp_path)
    conn.execute("INSERT INTO events (ts,type,payload) VALUES "
                 "('2026-01-01T00:00:00.000Z','ingested','{\"hash\":\"old\"}')")
    conn.execute("INSERT INTO events (ts,type,payload) VALUES "
                 "('2026-06-01T00:00:00.000Z','ingested','{\"hash\":\"mid\"}')")
    conn.execute("INSERT INTO events (ts,type,payload) VALUES "
                 "('2026-09-01T00:00:00.000Z','ingested','{\"hash\":\"new\"}')")
    out = reconstruct_timeline(conn, since="2026-03-01T00:00:00Z", until="2026-08-01T00:00:00Z")
    assert [e.payload["hash"] for e in out] == ["mid"]


def test_timeline_scoped_to_topic_hashes(tmp_path, monkeypatch):
    conn = fresh_conn(tmp_path)
    _add(conn, "h1", "Docker OOM", "flashinfer sm120 oom guardrails")
    _add(conn, "h2", "Unrelated", "knitting patterns for socks")
    _ev(conn, "ingested", {"hash": "h1", "title": "Docker OOM"})
    _ev(conn, "ingested", {"hash": "h2", "title": "Unrelated"})
    _ev(conn, "superseded", {"hash_old": "h1", "hash_new": "h1b", "source_url": "u"})

    import engram.rag.timeline as t
    # Topic resolution returns only h1 (and its lineage hash h1b).
    monkeypatch.setattr(t, "_topic_hashes", lambda conn, query, k: {"h1", "h1b"})
    out = reconstruct_timeline(conn, query="flashinfer", top_k=5)
    events = [(e.event, e.payload) for e in out]
    assert ("ingested", {"hash": "h1", "title": "Docker OOM"}) in events
    assert ("superseded", {"hash_old": "h1", "hash_new": "h1b", "source_url": "u"}) in events
    # The unrelated h2 ingest must not appear.
    assert all(e.payload.get("hash") != "h2" for e in out)


def test_timeline_limit_caps_results(tmp_path):
    conn = fresh_conn(tmp_path)
    for i in range(10):
        _ev(conn, "ingested", {"hash": f"h{i}"})
    out = reconstruct_timeline(conn, limit=3)
    assert len(out) == 3
    # Default order is chronological ascending: the first three ingests.
    assert [e.payload["hash"] for e in out] == ["h0", "h1", "h2"]
