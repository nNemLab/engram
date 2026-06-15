"""sources.* MCP tools: assert that each mutator emits an audit event."""
import json
import sqlite3
from pathlib import Path

import pytest

REPO_PATH = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO_PATH / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "db.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    _apply(c)
    yield c


def test_add_emits_source_added(conn):
    from engram.mcp_server.tools.sources import register

    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "docker-docs",
        "name": "Docker Docs",
        "adapter": "sitemap",
        "url": "https://docs.docker.com/sitemap.xml",
    })

    rows = conn.execute(
        "SELECT id, type, payload FROM events WHERE type = ?", ("source_added",)
    ).fetchall()
    assert len(rows) == 1
    event = rows[0]
    assert json.loads(event["payload"])["source_id"] == "docker-docs"


def test_remove_emits_source_removed(conn):
    from engram.mcp_server.tools.sources import register

    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x",
        "name": "x",
        "adapter": "sitemap",
        "url": "u",
    })

    tools["sources.remove"]["handler"]({"id": "x"})

    rows = conn.execute(
        "SELECT id, type, payload FROM events WHERE type = ?", ("source_removed",)
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["source_id"] == "x"


def test_fetch_now_emits_source_fetch_requested(conn):
    from engram.mcp_server.tools.sources import register

    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x",
        "name": "x",
        "adapter": "sitemap",
        "url": "u",
    })

    tools["sources.fetch_now"]["handler"]({"id": "x"})

    rows = conn.execute(
        "SELECT id, type, payload FROM events WHERE type = ?",
        ("source_fetch_requested",),
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["source_id"] == "x"


def test_set_emits_source_updated(conn):
    from engram.mcp_server.tools.sources import register

    tools = register(conn)
    tools["sources.add"]["handler"]({
        "id": "x",
        "name": "x",
        "adapter": "sitemap",
        "url": "u",
    })

    tools["sources.set"]["handler"]({"id": "x", "paused": True, "schedule": "1d"})

    rows = conn.execute(
        "SELECT id, type, payload FROM events WHERE type = ?", ("source_updated",)
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["source_id"] == "x"
    assert "paused" in payload["updated_fields"]


def test_set_on_missing_id_emits_no_event(conn):
    """Regression guard for #166 cross-review: sources.set on a non-existent
    id must not emit a source_updated audit event — the event should only fire
    when the row actually exists.
    """
    from engram.mcp_server.tools.sources import register

    tools = register(conn)

    out = tools["sources.set"]["handler"]({"id": "ghost", "paused": True})
    assert out["error"] == "not found"
    assert out["id"] == "ghost"

    rows = conn.execute(
        "SELECT id FROM events WHERE type = ?", ("source_updated",)
    ).fetchall()
    assert len(rows) == 0
