import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


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
