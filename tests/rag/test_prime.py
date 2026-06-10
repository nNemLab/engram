from tests.rag import fresh_conn
from tests.rag.test_query_calibrated import _add, _stub_cfg


def test_prime_includes_goals_and_recent(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    conn.execute("INSERT INTO goals (id,text,status,priority,metadata,created_at,updated_at) "
                 "VALUES ('g1','ship docker',  'active',5,'{}','2026-06-09T00:00:00Z','2026-06-09T00:00:00Z')")
    _add(conn, "h1", "Recent note", "recent body", conf=0.9)
    from engram.rag.prime import prime
    out = prime(conn, cwd="/data/projects/engram")
    assert "ship docker" in out["block"]
    assert "Recent note" in out["block"]


def test_prime_empty_is_quiet(tmp_path, monkeypatch):
    _stub_cfg(monkeypatch)
    conn = fresh_conn(tmp_path)
    from engram.rag.prime import prime
    out = prime(conn, cwd="/tmp")
    assert out["block"] == "" or "no active" in out["block"].lower()
