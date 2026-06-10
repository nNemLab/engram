"""dedup.gate gains a fourth outcome `superseded` when source_url already has a
live entry at a different content hash. Old row's is_current flips to 0 and
superseded_by points to the new hash. A `superseded` event is emitted.
"""
import json
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply_schema(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql",
               "003_grounding.sql", "004_protected.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply_schema(c)
    # Stub config so dedup.gate doesn't try to load ~/.engram/config.yml.
    # Patch where gate() looks the symbol up (engram.dedup.load_config), not
    # where it's defined: gate binds load_config at dedup-import time, so a
    # patch on the config module is missed once dedup is already imported.
    from types import SimpleNamespace

    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    yield c


def test_first_ingest_with_source_url_is_new(conn):
    from engram.dedup import gate
    r = gate(conn, body="hello v1", source_url="https://example.com/page",
             kind="research", source_tier="vendor-doc")
    assert r.outcome == "new"
    row = conn.execute(
        "SELECT revision, is_current, superseded_by FROM content WHERE hash=?", (r.hash,)
    ).fetchone()
    assert row["revision"] == 1
    assert row["is_current"] == 1
    assert row["superseded_by"] is None


def test_second_ingest_same_url_different_body_supersedes(conn):
    from engram.dedup import gate
    r1 = gate(conn, body="hello v1", source_url="https://example.com/page",
              kind="research", source_tier="vendor-doc")
    r2 = gate(conn, body="hello v2 changed", source_url="https://example.com/page",
              kind="research", source_tier="vendor-doc")
    assert r2.outcome == "superseded"
    old = conn.execute(
        "SELECT revision, is_current, superseded_by FROM content WHERE hash=?", (r1.hash,)
    ).fetchone()
    new = conn.execute(
        "SELECT revision, is_current, superseded_by FROM content WHERE hash=?", (r2.hash,)
    ).fetchone()
    assert old["is_current"] == 0
    assert old["superseded_by"] == r2.hash
    assert new["revision"] == 2
    assert new["is_current"] == 1
    assert new["superseded_by"] is None


def test_supersede_emits_event(conn):
    from engram.dedup import gate
    gate(conn, body="v1", source_url="https://example.com/p", kind="research",
         source_tier="vendor-doc")
    r2 = gate(conn, body="v2", source_url="https://example.com/p", kind="research",
              source_tier="vendor-doc")
    rows = conn.execute(
        "SELECT payload FROM events WHERE type='superseded'"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["hash_new"] == r2.hash
    assert payload["source_url"] == "https://example.com/p"
    assert payload["revision"] == 2


def test_exact_dup_at_same_url_is_exact_dup_not_supersede(conn):
    from engram.dedup import gate
    r1 = gate(conn, body="same", source_url="https://example.com/p", kind="research",
              source_tier="vendor-doc")
    r2 = gate(conn, body="same", source_url="https://example.com/p", kind="research",
              source_tier="vendor-doc")
    assert r2.outcome == "exact_dup"
    assert r2.hash == r1.hash


def test_supersede_only_triggers_with_source_url(conn):
    """kb.write paths without source_url must be unaffected."""
    from engram.dedup import gate
    gate(conn, body="alpha body for note", kind="kb")
    r = gate(conn, body="alpha body for note revised", kind="kb")
    assert r.outcome == "new"  # different content, no source_url, no supersede
