from datetime import UTC, datetime, timedelta

from tests.rag import fresh_conn


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _add_content(conn, h, title, *, staleness, conf, tomb=0):
    conn.execute(
        "INSERT INTO content (hash, title, body, source_tier, fetched_at, confidence, "
        "staleness_score, kind, tombstoned) VALUES (?,?,?,?,?,?,?,?,?)",
        (h, title, "body", "manual", "2026-06-10T00:00:00Z", conf, staleness, "kb", tomb),
    )


def _add_goal(conn, gid, text, *, status, updated_at):
    conn.execute(
        "INSERT INTO goals (id,text,status,priority,metadata,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (gid, text, status, 0, "{}", "2026-01-01T00:00:00Z", updated_at),
    )


def _add_contra(conn, ha, hb, *, resolved, detected_by="agent", detected_at="2026-06-01T00:00:00Z"):
    conn.execute(
        "INSERT INTO contradictions (hash_a, hash_b, detected_at, detected_by, resolved) "
        "VALUES (?,?,?,?,?)",
        (ha, hb, detected_at, detected_by, resolved),
    )


def test_unresolved_contradictions_counted_and_sampled(tmp_path):
    conn = fresh_conn(tmp_path)
    _add_content(conn, "h1", "A", staleness=0.0, conf=0.5)
    _add_content(conn, "h2", "B", staleness=0.0, conf=0.5)
    _add_contra(conn, "h1", "h2", resolved=0)
    _add_contra(conn, "h1", "h2", resolved=0)
    _add_contra(conn, "h1", "h2", resolved=1)  # excluded
    from engram.rag.reflect import reflect
    out = reflect(conn)
    uc = out["unresolved_contradictions"]
    assert uc["count"] == 2
    assert len(uc["sample"]) == 2
    assert {"id", "hash_a", "hash_b", "detected_at", "detected_by"} <= set(uc["sample"][0])


def test_stale_high_value_thresholds_order_cap(tmp_path):
    conn = fresh_conn(tmp_path)
    # Above both thresholds
    _add_content(conn, "s1", "Stale1", staleness=0.9, conf=0.8)
    _add_content(conn, "s2", "Stale2", staleness=0.6, conf=0.7)
    # Below staleness
    _add_content(conn, "x1", "LowStale", staleness=0.3, conf=0.9)
    # Below confidence
    _add_content(conn, "x2", "LowConf", staleness=0.9, conf=0.4)
    # Tombstoned (excluded even though qualifying)
    _add_content(conn, "t1", "Tomb", staleness=0.9, conf=0.9, tomb=1)
    from engram.rag.reflect import reflect
    out = reflect(conn)
    shv = out["stale_high_value"]
    assert shv["count"] == 2
    hashes = [r["hash"] for r in shv["sample"]]
    assert hashes == ["s1", "s2"]  # ordered by staleness desc
    assert {"hash", "title", "staleness_score", "confidence"} <= set(shv["sample"][0])


def test_stale_high_value_capped_at_sample(tmp_path):
    conn = fresh_conn(tmp_path)
    for i in range(8):
        _add_content(conn, f"c{i}", f"T{i}", staleness=0.5 + i * 0.01, conf=0.7)
    from engram.rag.reflect import reflect
    out = reflect(conn, sample=3)
    assert out["stale_high_value"]["count"] == 8
    assert len(out["stale_high_value"]["sample"]) == 3


def test_idle_goals_flags_old_active_only(tmp_path):
    conn = fresh_conn(tmp_path)
    now = datetime.now(UTC)
    _add_goal(conn, "old", "old active goal", status="active", updated_at=_iso(now - timedelta(days=14)))
    _add_goal(conn, "fresh", "fresh goal", status="active", updated_at=_iso(now - timedelta(days=2)))
    _add_goal(conn, "paused", "paused old", status="paused", updated_at=_iso(now - timedelta(days=30)))
    from engram.rag.reflect import reflect
    out = reflect(conn, idle_days=10)
    ig = out["idle_goals"]
    assert ig["count"] == 1
    s = ig["sample"][0]
    assert s["id"] == "old"
    assert s["days_idle"] >= 13
    assert {"id", "text", "updated_at", "days_idle"} <= set(s)


def test_brief_renders_deterministically(tmp_path):
    conn = fresh_conn(tmp_path)
    now = datetime.now(UTC)
    _add_content(conn, "h1", "A", staleness=0.0, conf=0.5)
    _add_content(conn, "h2", "B", staleness=0.0, conf=0.5)
    _add_contra(conn, "h1", "h2", resolved=0)
    _add_contra(conn, "h1", "h2", resolved=0)
    _add_contra(conn, "h1", "h2", resolved=0)
    _add_content(conn, "s1", "Stale1", staleness=0.9, conf=0.8)
    _add_goal(conn, "g1", "ship docker", status="active", updated_at=_iso(now - timedelta(days=14)))
    from engram.rag.reflect import reflect
    out = reflect(conn, idle_days=10)
    brief = out["brief"]
    assert "3 contradictions unresolved" in brief
    assert "1 stale high-value" in brief
    assert "ship docker" in brief


def test_reflect_skips_goals_with_invalid_updated_at(tmp_path):
    conn = fresh_conn(tmp_path)
    now = datetime.now(UTC)
    _add_goal(conn, "good", "good goal", status="active", updated_at=_iso(now - timedelta(days=14)))
    _add_goal(conn, "bad", "bad goal", status="active", updated_at="not-a-timestamp")

    from engram.rag.reflect import reflect

    out = reflect(conn, idle_days=10)
    ids = [g["id"] for g in out["idle_goals"]["sample"]]
    assert "good" in ids
    assert "bad" not in ids


def test_brief_quiet_when_nothing(tmp_path):
    conn = fresh_conn(tmp_path)
    from engram.rag.reflect import reflect
    out = reflect(conn)
    assert out["unresolved_contradictions"]["count"] == 0
    assert out["stale_high_value"]["count"] == 0
    assert out["idle_goals"]["count"] == 0
    assert isinstance(out["brief"], str)


def test_session_reflect_tool_returns_structure():
    import sqlite3
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql", "003_grounding.sql"):
        conn.executescript((repo / "schema" / fn).read_text())
    conn.execute("INSERT INTO content (hash,title,body,source_tier,fetched_at,confidence,"
                 "staleness_score,kind,tombstoned) VALUES "
                 "('s1','Stale','b','manual','2026-06-10T00:00:00Z',0.8,0.9,'kb',0)")
    from engram.mcp_server.tools.session import register
    out = register(conn)["session.reflect"]["handler"]({})
    assert "brief" in out
    assert out["stale_high_value"]["count"] == 1
    assert "unresolved_contradictions" in out
    assert "idle_goals" in out
