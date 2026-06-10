import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _engram_config(tmp_path, monkeypatch):
    """Point ENGRAM_CONFIG at a throwaway config so handlers that call
    load_config() (rag.query computes the verdict + ranks via it) work on a clean
    machine / CI runner with no ~/.engram/config.yml. Only `paths:` is needed —
    rag/grounding/confidence fall back to their dataclass defaults."""
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


def test_rag_cite_records_usage():
    from engram.mcp_server.tools.rag import register
    conn = _conn()
    handler = register(conn)["rag.cite"]["handler"]
    out = handler({"hashes": ["h1", "h2"], "query": "flashinfer", "turn_id": "t1"})
    assert out.get("cited") == 2 or out.get("ok")
    rows = {r["content_hash"]: r["use_count"]
            for r in conn.execute("SELECT content_hash, use_count FROM content_usage")}
    assert rows == {"h1": 1, "h2": 1}


def test_session_prime_returns_block():
    from engram.mcp_server.tools.session import register
    conn = _conn()
    conn.execute("INSERT INTO goals (id,text,status,priority,metadata,created_at,updated_at) "
                 "VALUES ('g1','ship docker','active',5,'{}','2026-06-09T00:00:00Z','2026-06-09T00:00:00Z')")
    out = register(conn)["session.prime"]["handler"]({"cwd": "/x"})
    assert "ship docker" in out["block"]


def test_rag_query_returns_verdict(monkeypatch):
    from engram.mcp_server.tools import rag as ragtool
    conn = _conn()
    conn.execute("INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
                 "confidence,kind,tombstoned) VALUES "
                 "('h1','T','flashinfer oom guardrails',NULL,'manual','2026-06-10T00:00:00Z',0.8,'kb',0)")
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [("h1", 0.91)])
    out = ragtool.register(conn)["rag.query"]["handler"]({"query": "flashinfer", "token_budget": 500})
    assert out["verdict"] in ("STRONG", "WEAK", "NONE")
    assert "results" in out


def test_rag_query_token_budget_trims_results(monkeypatch):
    from engram.mcp_server.tools import rag as ragtool
    conn = _conn()
    for i in range(5):
        conn.execute("INSERT INTO content (hash,title,body,source_url,source_tier,fetched_at,"
                     "confidence,kind,tombstoned) VALUES (?,?,?,?,?,?,?,?,0)",
                     (f"h{i}", f"Title{i}", "alpha " * 200, None, "manual", "2026-06-10T00:00:00Z", 0.8, "kb"))
    import engram.rag.query as q
    monkeypatch.setattr(q, "embed_one", lambda s: b"x")
    monkeypatch.setattr(q, "_vector_hits", lambda conn, emb, k: [(f"h{i}", 0.8) for i in range(5)])
    full = ragtool.register(conn)["rag.query"]["handler"]({"query": "alpha"})
    tight = ragtool.register(conn)["rag.query"]["handler"]({"query": "alpha", "token_budget": 30})
    assert len(tight["results"]) < len(full["results"])
    assert len(tight["results"]) >= 1
