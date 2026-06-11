"""rag.* temporal surface (#40): the `until` bound on rag.query and the
rag.timeline tool, exercised through the MCP tool handlers."""
import sqlite3
from pathlib import Path

import pytest

from engram import log as event_log

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _engram_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "paths:\n"
        f"  root: {tmp_path}\n  vault: {tmp_path}/vault\n"
        f"  playbooks_scratch: {tmp_path}/ps\n  playbooks_curated: {tmp_path}/pc\n"
        f"  playbooks_runs: {tmp_path}/pr\n  db: {tmp_path}/db.sqlite\n"
    )
    monkeypatch.setenv("ENGRAM_CONFIG", str(cfg))
    from engram.common.config import load_config
    load_config.cache_clear()
    yield
    load_config.cache_clear()


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql", "003_grounding.sql"):
        c.executescript((REPO / "schema" / fn).read_text())
    return c


def test_rag_query_until_passthrough(monkeypatch):
    from engram.mcp_server.tools import rag as ragtool
    conn = _conn()
    for h, ts in (("old", "2026-01-01T00:00:00Z"), ("new", "2026-09-01T00:00:00Z")):
        conn.execute("INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
                     "confidence,kind,tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
                     (h, h, "alpha term", None, "manual", ts, 0.8, "kb"))
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("old", 0.8), ("new", 0.8)])
    out = ragtool.register(conn)["rag.query"]["handler"](
        {"query": "alpha", "until": "2026-06-01T00:00:00Z"})
    assert [r["hash"] for r in out["results"]] == ["old"]


def test_rag_timeline_tool_returns_ordered_events():
    from engram.mcp_server.tools import rag as ragtool
    conn = _conn()
    event_log.append(conn, "ingested", {"hash": "a", "title": "A"})
    event_log.append(conn, "superseded", {"hash_old": "a", "hash_new": "b", "source_url": "u"})
    out = ragtool.register(conn)["rag.timeline"]["handler"]({})
    assert "timeline" in out
    assert [e["event"] for e in out["timeline"]] == ["ingested", "superseded"]
    assert out["count"] == 2
    # Each entry carries id, ts, event, and payload.
    first = out["timeline"][0]
    assert {"id", "ts", "event", "payload"} <= set(first)
