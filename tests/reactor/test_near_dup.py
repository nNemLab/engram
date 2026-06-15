"""Reactor on_ingested near-dup post-check (Bug A regression).

The post-embed near-dup query ran `WHERE embedding MATCH ? AND content_hash != ?
ORDER BY distance LIMIT 1`. sqlite-vec rejects a KNN MATCH carrying an extra
non-MATCH predicate without an explicit `k = ?` constraint, raising
`OperationalError: A LIMIT or 'k = ?' constraint is required`. That killed the
handler on every ingest once any embedding existed, so near-dup tombstoning
never fired.
"""
import sqlite3
import struct
from types import SimpleNamespace

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


def _vec(xs):
    return struct.pack(f"{len(xs)}f", *xs)


def _add(conn, h, body, tombstoned=0, is_current=1):
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, fetched_at, "
        "confidence, kind, tombstoned, is_current) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (h, h, body, None, "manual", "2026-06-10T00:00:00Z", 0.8, "kb", tombstoned, is_current),
    )


def _cfg():
    return SimpleNamespace(
        rag=SimpleNamespace(chunk_size_tokens=512, chunk_overlap_tokens=64,
                            near_dup_threshold=0.92),
    )


def test_on_ingested_tombstones_near_dup_without_crashing(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    # Existing content A with a stored embedding.
    _add(conn, "A", "the quick brown fox")
    conn.execute("INSERT INTO embeddings (content_hash, embedding) VALUES (?, ?)",
                 ("A", _vec([1.0, 0.0, 0.0, 0.0])))
    # New content B, body distinct text but embeds identically to A (a near-dup).
    _add(conn, "B", "a wholly different sentence")

    import engram.reactor.handlers as H
    monkeypatch.setattr(H, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(H.embedder, "embed_one", lambda text: _vec([1.0, 0.0, 0.0, 0.0]))
    monkeypatch.setattr(H.chunker, "chunk_markdown", lambda *a, **k: ["chunk"])
    monkeypatch.setattr(H.chunker, "embed_prefix", lambda body, n: body)

    evt = SimpleNamespace(type="ingested", payload={"hash": "B"}, id=1)
    H.on_ingested(conn, evt)  # must not raise

    # B embedded, then tombstoned as a near-dup of A (cosine 1.0 >= 0.92),
    # and its embedding is removed as part of tombstoning.
    assert conn.execute("SELECT count(*) FROM embeddings WHERE content_hash='B'").fetchone()[0] == 0
    assert conn.execute("SELECT tombstoned FROM content WHERE hash='B'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM events WHERE type='merged'").fetchone()[0] == 1


def test_on_ingested_ignores_tombstoned_nearest_neighbor(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    # Existing tombstoned content A with a stored embedding that is identical to B.
    _add(conn, "A", "dead near-dup", tombstoned=1)
    conn.execute("INSERT INTO embeddings (content_hash, embedding) VALUES (?, ?)",
                 ("A", _vec([1.0, 0.0, 0.0, 0.0])))
    # New content B should NOT be tombstoned against dead A.
    _add(conn, "B", "fresh content")

    import engram.reactor.handlers as H
    monkeypatch.setattr(H, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(H.embedder, "embed_one", lambda text: _vec([1.0, 0.0, 0.0, 0.0]))
    monkeypatch.setattr(H.chunker, "chunk_markdown", lambda *a, **k: ["chunk"])
    monkeypatch.setattr(H.chunker, "embed_prefix", lambda body, n: body)

    evt = SimpleNamespace(type="ingested", payload={"hash": "B"}, id=1)
    H.on_ingested(conn, evt)

    assert conn.execute("SELECT tombstoned FROM content WHERE hash='B'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE type='merged'").fetchone()[0] == 0


def test_on_ingested_ignores_non_current_nearest_neighbor(tmp_path, monkeypatch):
    """A superseded (is_current=0, not tombstoned) row with its embedding still
    present must never be the near-dup target for newly ingested content (#139)."""
    conn = _conn(tmp_path)
    # Existing superseded content A (is_current=0) with an embedding identical to B.
    _add(conn, "A", "superseded near-dup", is_current=0)
    conn.execute("INSERT INTO embeddings (content_hash, embedding) VALUES (?, ?)",
                 ("A", _vec([1.0, 0.0, 0.0, 0.0])))
    # New content B should NOT be tombstoned against superseded A.
    _add(conn, "B", "fresh content")

    import engram.reactor.handlers as H
    monkeypatch.setattr(H, "load_config", lambda *a, **k: _cfg())
    monkeypatch.setattr(H.embedder, "embed_one", lambda text: _vec([1.0, 0.0, 0.0, 0.0]))
    monkeypatch.setattr(H.chunker, "chunk_markdown", lambda *a, **k: ["chunk"])
    monkeypatch.setattr(H.chunker, "embed_prefix", lambda body, n: body)

    evt = SimpleNamespace(type="ingested", payload={"hash": "B"}, id=1)
    H.on_ingested(conn, evt)

    assert conn.execute("SELECT tombstoned FROM content WHERE hash='B'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE type='merged'").fetchone()[0] == 0


def test_on_ingested_keeps_distinct_content(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _add(conn, "A", "the quick brown fox")
    conn.execute("INSERT INTO embeddings (content_hash, embedding) VALUES (?, ?)",
                 ("A", _vec([1.0, 0.0, 0.0, 0.0])))
    _add(conn, "B", "a wholly different sentence")

    import engram.reactor.handlers as H
    monkeypatch.setattr(H, "load_config", lambda *a, **k: _cfg())
    # B embeds orthogonally to A -> cosine 0 -> not a near-dup.
    monkeypatch.setattr(H.embedder, "embed_one", lambda text: _vec([0.0, 1.0, 0.0, 0.0]))
    monkeypatch.setattr(H.chunker, "chunk_markdown", lambda *a, **k: ["chunk"])
    monkeypatch.setattr(H.chunker, "embed_prefix", lambda body, n: body)

    evt = SimpleNamespace(type="ingested", payload={"hash": "B"}, id=1)
    H.on_ingested(conn, evt)

    assert conn.execute("SELECT tombstoned FROM content WHERE hash='B'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE type='merged'").fetchone()[0] == 0
