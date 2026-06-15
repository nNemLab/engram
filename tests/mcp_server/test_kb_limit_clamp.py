"""kb.list limit clamping: oversized ceilings at MAX_LIMIT, zero/negative floor to 1."""
import sqlite3
from pathlib import Path

import pytest

from engram.mcp_server.tools.kb import MAX_LIMIT, register

REPO = Path(__file__).resolve().parents[2]


def _apply_schema(conn):
    for fn in (
        "001_initial.sql",
        "002_sources_and_revisions.sql",
        "003_grounding.sql",
    ):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def kb_tools():
    from types import SimpleNamespace

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_schema(conn)
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    from engram import dedup as dedup_mod
    orig_load_config = dedup_mod.load_config
    dedup_mod.load_config = lambda: fake
    try:
        tools = register(conn)
        yield conn, tools["kb.list"]["handler"]
    finally:
        dedup_mod.load_config = orig_load_config


def _seed(conn, count: int = 10):
    """Insert rows into content so list returns predictable results."""
    for i in range(count):
        conn.execute(
            "INSERT INTO content (hash, body, tombstoned, kind, updated_at) "
            "VALUES (?, ?, 0, 'kb', '2026-01-01')",
            (f"h{i}", f"body {i}"),
        )
    conn.commit()


# --- upper bound (MAX_LIMIT) ---


def test_over_max_limit_is_clamped(kb_tools):
    conn, handler = kb_tools
    _seed(conn, 200)  # insert 200 rows
    out = handler({"limit": 500})
    assert len(out) <= MAX_LIMIT


def test_limit_at_exact_max_passes(kb_tools):
    conn, handler = kb_tools
    _seed(conn, MAX_LIMIT)  # insert exactly MAX_LIMIT rows
    out = handler({"limit": MAX_LIMIT})
    assert len(out) == MAX_LIMIT


def test_limit_exactly_one_over_max_is_clamped(kb_tools):
    conn, handler = kb_tools
    _seed(conn, MAX_LIMIT + 2)
    out = handler({"limit": MAX_LIMIT + 1})
    assert len(out) <= MAX_LIMIT


# --- floor at 1 ---


def test_limit_zero_floors_to_one(kb_tools):
    conn, handler = kb_tools
    _seed(conn, 5)
    out = handler({"limit": 0})
    assert len(out) >= 1


def test_limit_negative_floors_to_one(kb_tools):
    conn, handler = kb_tools
    _seed(conn, 5)
    out = handler({"limit": -10})
    assert len(out) >= 1


# --- normal passthrough ---


def test_normal_limit_passes_through(kb_tools):
    conn, handler = kb_tools
    _seed(conn, 10)
    out = handler({"limit": 3})
    assert len(out) == 3


def test_default_limit_is_fifty(kb_tools):
    """The tool schema default is 50 — passing no limit should return ≤ 50 rows."""
    conn, handler = kb_tools
    _seed(conn, 60)
    out = handler({})
    assert len(out) == 50


def test_no_limit_arg_respects_seed_size(kb_tools):
    """With fewer rows than default, all rows return (no artificial cap)."""
    conn, handler = kb_tools
    _seed(conn, 5)
    out = handler({})
    assert len(out) == 5
