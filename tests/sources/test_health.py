"""source_health(): deterministic read-only observability over sources + content.

Pure SQL/Python — no network, no LLM. Covers the derived status/overdue/dup_ratio
fields and the sources.health MCP tool.
"""
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "db.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    _apply(c)
    yield c


def _add_source(conn, sid, **over):
    cols = {
        "id": sid,
        "name": sid,
        "adapter": "sitemap",
        "url": "https://example.com/sitemap.xml",
        "config": "{}",
        "schedule": "7d",
        "source_tier": "vendor-doc",
        "paused": 0,
        "next_poll_at": None,
        "last_polled_at": None,
        "last_success_at": None,
        "error_count": 0,
        "last_error": None,
    }
    cols.update(over)
    keys = ", ".join(cols)
    qs = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO sources ({keys}) VALUES ({qs})", tuple(cols.values()))
    conn.commit()


def _add_content(conn, source_id, hash_, *, is_current=1, fetched_at=None):
    conn.execute(
        "INSERT INTO content (hash, body, source_id, is_current, fetched_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (hash_, "body for " + hash_, source_id, is_current, fetched_at),
    )
    conn.commit()


def _by_id(records):
    return {r["id"]: r for r in records}


def test_healthy_source_with_current_content(conn):
    from engram.sources.health import source_health
    _add_source(
        conn, "ok-src",
        next_poll_at="2099-01-01T00:00:00Z",
        last_polled_at="2026-06-01T00:00:00Z",
        last_success_at="2026-06-01T00:00:00Z",
    )
    _add_content(conn, "ok-src", "h1", is_current=1, fetched_at="2026-06-01T00:00:00Z")

    rec = _by_id(source_health(conn))["ok-src"]
    assert rec["status"] == "ok"
    assert rec["overdue"] is False
    assert rec["content_total"] == 1
    assert rec["content_current"] == 1
    assert rec["dup_ratio"] == 0.0
    assert rec["last_new_content_at"] == "2026-06-01T00:00:00Z"


def test_superseded_revisions_compute_dup_ratio(conn):
    from engram.sources.health import source_health
    _add_source(
        conn, "churny",
        next_poll_at="2099-01-01T00:00:00Z",
        last_polled_at="2026-06-01T00:00:00Z",
        last_success_at="2026-06-01T00:00:00Z",
    )
    # 4 rows, 1 current -> dup_ratio = 1 - 1/4 = 0.75
    _add_content(conn, "churny", "c1", is_current=0, fetched_at="2026-05-01T00:00:00Z")
    _add_content(conn, "churny", "c2", is_current=0, fetched_at="2026-05-10T00:00:00Z")
    _add_content(conn, "churny", "c3", is_current=0, fetched_at="2026-05-20T00:00:00Z")
    _add_content(conn, "churny", "c4", is_current=1, fetched_at="2026-06-01T00:00:00Z")

    rec = _by_id(source_health(conn))["churny"]
    assert rec["content_total"] == 4
    assert rec["content_current"] == 1
    assert rec["dup_ratio"] == 0.75
    assert rec["last_new_content_at"] == "2026-06-01T00:00:00Z"
    assert rec["status"] == "ok"


def test_paused_source(conn):
    from engram.sources.health import source_health
    _add_source(
        conn, "paused-src",
        paused=1,
        # even with an overdue next_poll_at, paused wins over overdue (overdue must be False)
        next_poll_at="2000-01-01T00:00:00Z",
        last_polled_at="2026-06-01T00:00:00Z",
        last_success_at="2026-06-01T00:00:00Z",
    )
    rec = _by_id(source_health(conn))["paused-src"]
    assert rec["status"] == "paused"
    assert rec["overdue"] is False


def test_erroring_source(conn):
    from engram.sources.health import source_health
    _add_source(
        conn, "err-src",
        error_count=3,
        last_error="HTTP 503",
        last_polled_at="2026-06-05T00:00:00Z",
        last_success_at="2026-06-01T00:00:00Z",  # older than last_polled_at
        next_poll_at="2099-01-01T00:00:00Z",
    )
    rec = _by_id(source_health(conn))["err-src"]
    assert rec["status"] == "erroring"
    assert rec["error_count"] == 3


def test_erroring_source_with_null_last_success(conn):
    from engram.sources.health import source_health
    _add_source(
        conn, "never-ok",
        error_count=1,
        last_polled_at="2026-06-05T00:00:00Z",
        last_success_at=None,
        next_poll_at="2099-01-01T00:00:00Z",
    )
    rec = _by_id(source_health(conn))["never-ok"]
    assert rec["status"] == "erroring"


def test_overdue_source_with_non_z_timestamp_still_detected(conn):
    from engram.sources.health import source_health
    _add_source(
        conn, "late-mixed",
        next_poll_at="2000-01-01T00:00:00+00:00",
        last_polled_at="1999-12-01T00:00:00Z",
        last_success_at="1999-12-01T00:00:00Z",
    )
    rec = _by_id(source_health(conn))["late-mixed"]
    assert rec["overdue"] is True
    assert rec["status"] == "overdue"


def test_overdue_source(conn):
    from engram.sources.health import source_health
    _add_source(
        conn, "late-src",
        next_poll_at="2000-01-01T00:00:00Z",  # in the past
        last_polled_at="1999-12-01T00:00:00Z",
        last_success_at="1999-12-01T00:00:00Z",
    )
    rec = _by_id(source_health(conn))["late-src"]
    assert rec["overdue"] is True
    assert rec["status"] == "overdue"


def test_no_content_dup_ratio_is_zero(conn):
    from engram.sources.health import source_health
    _add_source(conn, "empty", next_poll_at="2099-01-01T00:00:00Z")
    rec = _by_id(source_health(conn))["empty"]
    assert rec["content_total"] == 0
    assert rec["content_current"] == 0
    assert rec["dup_ratio"] == 0.0
    assert rec["last_new_content_at"] is None


def test_erroring_with_mixed_precision_ordering(conn):
    from engram.sources.health import source_health
    _add_source(
        conn, "mixed-prec",
        error_count=1,
        last_polled_at="2026-06-05T00:00:00.100Z",
        last_success_at="2026-06-05T00:00:00Z",
        next_poll_at="2099-01-01T00:00:00Z",
    )
    rec = _by_id(source_health(conn))["mixed-prec"]
    assert rec["status"] == "erroring"


def test_mcp_tool_returns_records(conn):
    from engram.mcp_server.tools.sources import register
    _add_source(
        conn, "m1",
        next_poll_at="2099-01-01T00:00:00Z",
        last_polled_at="2026-06-01T00:00:00Z",
        last_success_at="2026-06-01T00:00:00Z",
    )
    _add_content(conn, "m1", "mh1", is_current=1, fetched_at="2026-06-01T00:00:00Z")

    tools = register(conn)
    out = tools["sources.health"]["handler"]({})
    assert "sources" in out
    recs = _by_id(out["sources"])
    assert recs["m1"]["status"] == "ok"


def test_mcp_tool_id_filter(conn):
    from engram.mcp_server.tools.sources import register
    _add_source(conn, "a", next_poll_at="2099-01-01T00:00:00Z")
    _add_source(conn, "b", next_poll_at="2099-01-01T00:00:00Z")
    tools = register(conn)
    out = tools["sources.health"]["handler"]({"id": "a"})
    ids = [r["id"] for r in out["sources"]]
    assert ids == ["a"]
