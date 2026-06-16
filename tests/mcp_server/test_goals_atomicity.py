"""Unit D (#152): goals.* writes are atomic.

`goals.set` (upsert goals + goal_set event) and `goals.resolve` (UPDATE goals +
goal_resolved event) each wrap their writes in `common.db.transaction()`, so an
injected failure rolls the whole sequence back -- never a persisted goal/status
change with no event (or vice versa).

Lives under tests/mcp_server (a CI-gated suite) since these are mcp_server tools.
"""
import sqlite3
from pathlib import Path

import pytest

import engram.mcp_server.tools.goals as gmod
from engram.mcp_server.tools.goals import register

REPO = Path(__file__).resolve().parents[2]


def _conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.sqlite", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript((REPO / "schema" / "001_initial.sql").read_text())
    return c


def _boom(*a, **k):
    raise RuntimeError("event append exploded")


def test_goals_set_rolls_back_on_event_failure(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    set_ = register(conn)["goals.set"]["handler"]

    monkeypatch.setattr(gmod.event_log, "append", _boom)
    with pytest.raises(RuntimeError, match="exploded"):
        set_({"id": "g1", "text": "Investigate X"})

    # The upsert rolled back: no goal row, no goal_set event.
    assert conn.execute("SELECT count(*) FROM goals").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE type='goal_set'").fetchone()[0] == 0


def test_goals_resolve_rolls_back_on_event_failure(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    tools = register(conn)
    tools["goals.set"]["handler"]({"id": "g1", "text": "Investigate X"})  # active goal

    monkeypatch.setattr(gmod.event_log, "append", _boom)
    with pytest.raises(RuntimeError, match="exploded"):
        tools["goals.resolve"]["handler"]({"id": "g1"})

    # The status UPDATE rolled back: goal still active, no goal_resolved event.
    assert conn.execute("SELECT status FROM goals WHERE id='g1'").fetchone()["status"] == "active"
    assert conn.execute(
        "SELECT count(*) FROM events WHERE type='goal_resolved'"
    ).fetchone()[0] == 0
