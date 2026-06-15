"""Unit D (#152): kb.* multi-statement writes are atomic.

`kb.tombstone` (per hash: UPDATE content tombstoned + DELETE embedding + merged
event) and `kb.flag_contradiction` (INSERT contradiction + contradicted event)
each wrap their writes in `common.db.transaction()`, so an injected failure
mid-sequence rolls the whole thing back -- never content tombstoned with the
embedding gone but no event, never a contradiction row with no event.

Uses an autocommit (isolation_level=None) connection to match the production MCP
server connection, so `transaction()` owns a real BEGIN/COMMIT/ROLLBACK.
"""
import sqlite3
import struct

import pytest
import sqlite_vec

import engram.mcp_server.tools.kb as kbmod
from engram.common.db import init_schema
from engram.mcp_server.tools.kb import register


def _conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.sqlite", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    c.execute("PRAGMA foreign_keys = ON")
    init_schema(c, embed_dim=4)
    return c


def _boom(*a, **k):
    raise RuntimeError("event append exploded")


def test_tombstone_rolls_back_on_event_failure(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    h = "h0"
    conn.execute(
        "INSERT INTO content (hash, body, title, tombstoned, kind) VALUES (?, 'b', 't', 0, 'kb')",
        (h,),
    )
    conn.execute(
        "INSERT INTO embeddings (content_hash, embedding) VALUES (?, ?)",
        (h, struct.pack("4f", 1.0, 0.0, 0.0, 0.0)),
    )
    tombstone = register(conn)["kb.tombstone"]["handler"]

    monkeypatch.setattr(kbmod.event_log, "append", _boom)
    with pytest.raises(RuntimeError, match="exploded"):
        tombstone({"hash": h})

    # Rolled back wholesale: content NOT tombstoned, embedding intact, no event.
    assert conn.execute("SELECT tombstoned FROM content WHERE hash=?", (h,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM embeddings WHERE content_hash=?", (h,)
    ).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM events WHERE type='merged'").fetchone()[0] == 0


def test_flag_contradiction_rolls_back_on_event_failure(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO content (hash, body, kind, tombstoned) VALUES ('a', 'x', 'kb', 0)")
    conn.execute("INSERT INTO content (hash, body, kind, tombstoned) VALUES ('b', 'y', 'kb', 0)")
    flag = register(conn)["kb.flag_contradiction"]["handler"]

    monkeypatch.setattr(kbmod.event_log, "append", _boom)
    with pytest.raises(RuntimeError, match="exploded"):
        flag({"hash_a": "a", "hash_b": "b"})

    # Rolled back: no contradiction row and no event.
    assert conn.execute("SELECT count(*) FROM contradictions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM events WHERE type='contradicted'").fetchone()[0] == 0
