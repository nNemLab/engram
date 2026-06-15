"""sources.* MCP tools: add/list/get/set/remove/fetch_now."""
import json
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


def test_add_creates_row(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    handler = tools["sources.add"]["handler"]
    out = handler({
        "id": "docker-docs",
        "name": "Docker Docs",
        "adapter": "sitemap",
        "url": "https://docs.docker.com/sitemap.xml",
        "config": {"include": ["*/engine/*"]},
        "schedule": "7d",
    })
    assert out["id"] == "docker-docs"
    row = conn.execute("SELECT * FROM sources WHERE id='docker-docs'").fetchone()
    assert row["adapter"] == "sitemap"
    assert json.loads(row["config"])["include"] == ["*/engine/*"]


def test_add_uses_default_schedule_per_adapter(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    h = tools["sources.add"]["handler"]
    h({"id": "a", "name": "a", "adapter": "sitemap", "url": "u"})
    h({"id": "b", "name": "b", "adapter": "github-repo", "url": "https://github.com/x/y"})
    rows = {r["id"]: r["schedule"] for r in conn.execute("SELECT id, schedule FROM sources")}
    assert rows == {"a": "7d", "b": "1d"}


def test_list_returns_all(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    add = tools["sources.add"]["handler"]
    add({"id": "x", "name": "x", "adapter": "sitemap", "url": "u"})
    add({"id": "y", "name": "y", "adapter": "sitemap", "url": "u"})
    out = tools["sources.list"]["handler"]({})
    ids = sorted(s["id"] for s in out)
    assert ids == ["x", "y"]


def test_get_returns_full_row(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x", "name": "Xx", "adapter": "sitemap", "url": "u"})
    out = tools["sources.get"]["handler"]({"id": "x"})
    assert out["name"] == "Xx"


def test_set_updates_fields(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x", "name": "x", "adapter": "sitemap", "url": "u"})
    out = tools["sources.set"]["handler"]({"id": "x", "paused": True, "schedule": "1d"})
    assert "paused" in out["updated_fields"]
    row = conn.execute("SELECT paused, schedule FROM sources WHERE id='x'").fetchone()
    assert row["paused"] == 1
    assert row["schedule"] == "1d"


def test_set_merges_config_instead_of_replacing(conn):
    """Regression for #2 (PR #20): sources.set merges the provided config into the
    existing config (shallow) rather than overwriting the whole blob — updated keys
    win, untouched keys survive, new keys are added.
    """
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x", "name": "x", "adapter": "sitemap", "url": "u",
        "config": {"include": ["*/a/*"], "depth": 2},
    })
    out = tools["sources.set"]["handler"]({"id": "x", "config": {"depth": 5, "exclude": ["*/b/*"]}})
    assert "config" in out["updated_fields"]
    row = conn.execute("SELECT config FROM sources WHERE id='x'").fetchone()
    # 'include' preserved (would be dropped by a replace), 'depth' updated, 'exclude' added
    assert json.loads(row["config"]) == {"include": ["*/a/*"], "depth": 5, "exclude": ["*/b/*"]}


def test_remove_deletes(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x", "name": "x", "adapter": "sitemap", "url": "u"})
    tools["sources.remove"]["handler"]({"id": "x"})
    assert conn.execute("SELECT 1 FROM sources WHERE id='x'").fetchone() is None


def test_remove_blocked_by_content_returns_helpful_error(conn):
    """Regression for #1 (PR #8): removing a source still referenced by content
    must surface a helpful error instead of raising sqlite3.IntegrityError, and
    must not delete the source.

    Production enables FK enforcement (engram/common/db.py: PRAGMA foreign_keys
    = ON); the shared `conn` fixture does not, so enable it here to reproduce
    the constraint that triggers the bug.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "docs", "name": "Docs", "adapter": "sitemap", "url": "u"})
    # content row referencing the source via content.source_id -> sources(id)
    conn.execute(
        "INSERT INTO content (hash, body, source_id) VALUES (?, ?, ?)",
        ("h1", "body text", "docs"),
    )
    conn.commit()

    out = tools["sources.remove"]["handler"]({"id": "docs"})

    assert "error" in out, "FK-blocked remove should return an error, not raise"
    assert out["id"] == "docs"
    assert "tombstone" in out["error"] and "source_id" in out["error"]
    # the source must survive — the delete was rejected, not silently dropped
    assert conn.execute("SELECT 1 FROM sources WHERE id='docs'").fetchone() is not None


def test_fetch_now_clears_next_poll_at(conn):
    """Triggering fetch_now sets next_poll_at to NULL so the daemon picks it up next tick."""
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x", "name": "x", "adapter": "sitemap", "url": "u",
        "schedule": "7d",
    })
    conn.execute("UPDATE sources SET next_poll_at='2099-01-01T00:00:00Z' WHERE id='x'")
    out = tools["sources.fetch_now"]["handler"]({"id": "x"})
    assert out["triggered"] is True
    after = conn.execute("SELECT next_poll_at FROM sources WHERE id='x'").fetchone()
    assert after["next_poll_at"] is None


# ── Issue #90: write tools must verify the row actually matched ──────────────

def test_set_on_missing_id_returns_not_found(conn):
    """Regression for #90: sources.set on a non-existent id must not crash
    or return fake success — it must return {"error": "not found"}.
    """
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    out = tools["sources.set"]["handler"]({"id": "ghost", "paused": True})
    assert "error" in out
    assert out["id"] == "ghost"


def test_set_config_merge_on_missing_id_returns_not_found(conn):
    """Regression for #90: sources.set with config on a non-existent id
    must check fetchone() before indexing (no TypeError), and return not_found.
    """
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    out = tools["sources.set"]["handler"]({"id": "ghost", "config": {"k": "v"}})
    assert "error" in out
    assert out["id"] == "ghost"


def test_fetch_now_on_missing_id_returns_triggered_false(conn):
    """fetch_now on an unknown id: triggered=False, no crash.  (Pre-existing
    rowcount path — no rowcount fix needed here, but a regression guard.)
    """
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    out = tools["sources.fetch_now"]["handler"]({"id": "ghost"})
    assert out["triggered"] is False
    assert out["id"] == "ghost"


def test_remove_on_missing_id(conn):
    """remove on an unknown id: removed=False, no crash, consistent with
    existing rowcount-returned boolean.
    """
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    out = tools["sources.remove"]["handler"]({"id": "ghost"})
    assert out["removed"] is False
