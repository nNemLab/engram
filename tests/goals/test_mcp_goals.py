"""goals.* MCP tools: set/list/resolve."""
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql",):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "db.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    _apply(c)
    yield c


def test_set_creates_goal(conn):
    from engram.mcp_server.tools.goals import register
    tools = register(conn)
    out = tools["goals.set"]["handler"]({"text": "Investigate X"})
    assert "id" in out
    row = conn.execute("SELECT * FROM goals WHERE id=?", (out["id"],)).fetchone()
    assert row["text"] == "Investigate X"
    assert row["status"] == "active"


def test_list_returns_active_goals(conn):
    from engram.mcp_server.tools.goals import register
    tools = register(conn)
    tools["goals.set"]["handler"]({"text": "Goal A"})
    tools["goals.set"]["handler"]({"text": "Goal B"})
    out = tools["goals.list"]["handler"]({"status": "active"})
    assert len(out) == 2


def test_resolve_on_existing_goal_succeeds(conn):
    from engram.mcp_server.tools.goals import register
    tools = register(conn)
    gid = tools["goals.set"]["handler"]({"text": "Investigate X"})["id"]
    out = tools["goals.resolve"]["handler"]({"id": gid})
    assert out["id"] == gid
    assert out["status"] == "resolved"
    row = conn.execute("SELECT status FROM goals WHERE id=?", (gid,)).fetchone()
    assert row["status"] == "resolved"


def test_resolve_on_missing_goal_returns_not_found(conn):
    """Regression for #90: resolving a non-existent goal must return
    a structured not-found error (not just presence of the key).
    """
    from engram.mcp_server.tools.goals import register
    tools = register(conn)
    out = tools["goals.resolve"]["handler"]({"id": "ghost-goal"})
    assert out["error"] == "not found"
    assert out["id"] == "ghost-goal"
