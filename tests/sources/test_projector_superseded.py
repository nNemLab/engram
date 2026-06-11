"""Projector handles `superseded` events: re-renders the new hash to the same
canonical path the old hash occupied, updates vault_state."""
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql",
               "003_grounding.sql", "004_protected.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c)
    # Patch where gate() looks the symbol up (engram.dedup.load_config), not
    # where it's defined — robust even if dedup is imported before this runs.
    from types import SimpleNamespace

    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
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


def test_handle_superseded_projects_fresh_when_no_old_vault_state(conn, tmp_path):
    """#54 fix: if hash_old has no vault_state (was never projected), the
    superseded handler must still project hash_new fresh — vault file + vault_state."""
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.projector.projector import _handle_event

    vault = tmp_path / "vault"
    vault.mkdir()
    kind_dirs = {"research": "030-research"}

    h_old = content_hash("old body")
    h_new = content_hash("new current body")
    # Both rows exist in content; the OLD one has NO vault_state.
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current) "
        "VALUES (?, 'old body', 'T', 'https://x/p', 'vendor-doc', 0.7, 'research', 1, 0)",
        (h_old,),
    )
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current) "
        "VALUES (?, 'new current body', 'T', 'https://x/p', 'vendor-doc', 0.7, 'research', 2, 1)",
        (h_new,),
    )
    event_log.append(
        conn, "superseded",
        {"hash_old": h_old, "hash_new": h_new, "source_url": "https://x/p"},
        actor="human",
    )
    conn.commit()

    sup_evt = list(event_log.since(conn, 0, types=["superseded"]))[-1]
    _handle_event(conn, vault, sup_evt, kind_dirs)

    # New row got a vault file and a vault_state row.
    state = conn.execute(
        "SELECT vault_path FROM vault_state WHERE content_hash=?", (h_new,)
    ).fetchone()
    assert state is not None
    rel = state["vault_path"]
    assert "new current body" in (vault / rel).read_text()
    # content.vault_path is no longer NULL for the new row.
    assert conn.execute(
        "SELECT vault_path FROM content WHERE hash=?", (h_new,)
    ).fetchone()["vault_path"] == rel
