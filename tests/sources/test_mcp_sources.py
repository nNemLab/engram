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


def test_remove_deletes(conn):
    from engram.mcp_server.tools.sources import register
    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x", "name": "x", "adapter": "sitemap", "url": "u"})
    tools["sources.remove"]["handler"]({"id": "x"})
    assert conn.execute("SELECT 1 FROM sources WHERE id='x'").fetchone() is None


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
