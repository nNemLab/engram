from tests.rag import fresh_conn


def test_record_cited_increments_and_emits_event(tmp_path):
    from engram.rag.usage import record_cited
    conn = fresh_conn(tmp_path)
    record_cited(conn, ["h1", "h2"], query="flashinfer")
    record_cited(conn, ["h1"], query="oom")
    rows = {r["content_hash"]: r["use_count"]
            for r in conn.execute("SELECT content_hash, use_count FROM content_usage")}
    assert rows == {"h1": 2, "h2": 1}
    events = conn.execute("SELECT type FROM events WHERE type='cited'").fetchall()
    assert len(events) == 2


def test_record_cited_is_idempotent_per_turn(tmp_path):
    from engram.rag.usage import record_cited
    conn = fresh_conn(tmp_path)
    record_cited(conn, ["h1"], query="x", turn_id="t1")
    record_cited(conn, ["h1"], query="x", turn_id="t1")   # same turn -> no double count
    n = conn.execute("SELECT use_count FROM content_usage WHERE content_hash='h1'").fetchone()
    assert n["use_count"] == 1


def test_rebuild_usage_from_log(tmp_path):
    from engram.rag.usage import rebuild_usage, record_cited
    conn = fresh_conn(tmp_path)
    record_cited(conn, ["h1", "h2"], query="a")
    record_cited(conn, ["h1"], query="b")
    conn.execute("DELETE FROM content_usage")          # simulate cache loss
    rebuild_usage(conn)
    rows = {r["content_hash"]: r["use_count"]
            for r in conn.execute("SELECT content_hash, use_count FROM content_usage")}
    assert rows == {"h1": 2, "h2": 1}


def test_usage_factor_grows_with_count(tmp_path):
    from engram.rag.usage import record_cited, usage_factor
    conn = fresh_conn(tmp_path)
    base = usage_factor(conn, "h1", weight=0.5)
    record_cited(conn, ["h1"], query="x")
    record_cited(conn, ["h1"], query="y")
    assert usage_factor(conn, "h1", weight=0.5) > base == 1.0


def test_record_cited_returns_fresh_count(tmp_path):
    from engram.rag.usage import record_cited
    conn = fresh_conn(tmp_path)
    assert record_cited(conn, ["h1", "h2"], query="x", turn_id="t1") == 2
    assert record_cited(conn, ["h1", "h3"], query="x", turn_id="t1") == 1  # h1 deduped
