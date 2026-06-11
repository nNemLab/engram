"""kb.resolve_supersede MCP tool wiring (#54)."""
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply_schema(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql",
               "003_grounding.sql", "004_protected.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def resolve(monkeypatch):
    from engram.mcp_server.tools.kb import register
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_schema(conn)
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    handler = register(conn)["kb.resolve_supersede"]["handler"]
    return conn, handler


def _blocked_state(conn, *, url="https://x/p"):
    from engram.dedup import content_hash, gate
    h_human = content_hash("human edit")
    conn.execute(
        "INSERT INTO content (hash, body, source_url, source_tier, confidence, "
        "kind, revision, is_current, protected) "
        "VALUES (?, 'human edit', ?, 'vendor-doc', 0.7, 'research', 1, 1, 1)",
        (h_human, url),
    )
    conn.commit()
    gate(conn, body="new upstream bytes", source_url=url,
         source_tier="vendor-doc", kind="research", actor="poller")
    return h_human, content_hash("new upstream bytes")


def test_tool_accept_upstream(resolve):
    conn, handler = resolve
    h_human, h_up = _blocked_state(conn)
    out = handler({"hash": h_human, "choice": "accept_upstream"})
    assert out["outcome"] == "accept_upstream"
    assert conn.execute(
        "SELECT is_current FROM content WHERE hash = ?", (h_up,)
    ).fetchone()["is_current"] == 1


def test_tool_keep_mine(resolve):
    conn, handler = resolve
    h_human, h_up = _blocked_state(conn)
    out = handler({"hash": h_human, "choice": "keep_mine"})
    assert out["outcome"] == "keep_mine"
    # Default retains the rejected upstream revision (durable re-poll path).
    assert conn.execute(
        "SELECT tombstoned FROM content WHERE hash = ?", (h_up,)
    ).fetchone()["tombstoned"] == 0


def test_tool_keep_mine_can_purge(resolve):
    conn, handler = resolve
    h_human, h_up = _blocked_state(conn)
    out = handler({"hash": h_human, "choice": "keep_mine", "tombstone_upstream": True})
    assert out["outcome"] == "keep_mine"
    assert conn.execute(
        "SELECT tombstoned FROM content WHERE hash = ?", (h_up,)
    ).fetchone()["tombstoned"] == 1


def test_tool_invalid_choice(resolve):
    conn, handler = resolve
    h_human, _ = _blocked_state(conn)
    out = handler({"hash": h_human, "choice": "nope"})
    assert "error" in out
