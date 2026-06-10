"""Projector handles `superseded` events: re-renders the new hash to the same
canonical path the old hash occupied, updates vault_state."""
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c)
    from types import SimpleNamespace

    from engram.common import config as cfg_mod
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)
    yield c


def test_handle_superseded_overwrites_vault_path(conn, tmp_path):
    from engram import log as event_log
    from engram.dedup import gate
    from engram.projector.projector import _handle_event

    vault = tmp_path / "vault"
    vault.mkdir()
    kind_dirs = {"research": "030-research"}

    # Seed: source_id row + first revision via gate (use source_url+source_id path)
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule) "
        "VALUES ('docker-docs', 'Docker', 'sitemap', 'https://x', '7d')"
    )
    r1 = gate(conn, body="v1 body", source_url="https://docs.docker.com/engine/install/",
              source_tier="vendor-doc", kind="research")
    # Manually attach source_id so URL-derived path triggers
    conn.execute("UPDATE content SET source_id='docker-docs' WHERE hash=?", (r1.hash,))

    # Render the first revision via the ingested event
    ingested_evt = list(event_log.since(conn, 0, types=["ingested"]))[-1]
    _handle_event(conn, vault, ingested_evt, kind_dirs)

    first_path = conn.execute(
        "SELECT vault_path FROM vault_state WHERE content_hash=?", (r1.hash,)
    ).fetchone()["vault_path"]
    assert (vault / first_path).read_text().find("v1 body") > 0

    # Supersede: gate emits a superseded event
    r2 = gate(conn, body="v2 changed body", source_url="https://docs.docker.com/engine/install/",
              source_tier="vendor-doc", kind="research")
    conn.execute("UPDATE content SET source_id='docker-docs' WHERE hash=?", (r2.hash,))

    sup_evt = list(event_log.since(conn, 0, types=["superseded"]))[-1]
    _handle_event(conn, vault, sup_evt, kind_dirs)

    # Same on-disk file path, new content
    new_text = (vault / first_path).read_text()
    assert "v2 changed body" in new_text
    assert "v1 body" not in new_text

    # vault_state row for old hash gone; row for new hash points to same path
    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE content_hash=?", (r1.hash,)
    ).fetchone() is None
    new_state = conn.execute(
        "SELECT vault_path FROM vault_state WHERE content_hash=?", (r2.hash,)
    ).fetchone()
    assert new_state["vault_path"] == first_path
