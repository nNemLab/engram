"""#55: a human vault edit must become a first-class new revision, not an
in-place body mutation that breaks content-addressing.

Before the fix, `_on_change` did `UPDATE content SET body=? WHERE hash=?`,
leaving `content_hash(body) != hash` for every human-edited row (verify's
hash_integrity check then flagged it as corruption).
"""
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
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    yield c


def _seed_projected_row(conn, *, hash_, body, source_url, vault_path):
    """A current sourced content row + its vault_state projection (as the
    projector would have left it)."""
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, ttl_days, kind, revision, is_current, vault_path) "
        "VALUES (?, ?, 'T', ?, 'vendor-doc', 0.7, 180, 'research', 1, 1, ?)",
        (hash_, body, source_url, vault_path),
    )
    conn.execute(
        "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
        (vault_path, hash_, body),
    )
    conn.commit()


def _do_edit(conn, tmp_path):
    """Seed a projected row, apply a human edit on disk, run the watcher.
    Returns (old_hash, new_hash, rel)."""
    from engram.dedup import content_hash
    from engram.watcher import watcher

    vault = tmp_path / "vault"
    (vault / "030-research").mkdir(parents=True)
    rel = "030-research/page.md"
    (vault / rel).write_text("original sourced body")
    old_hash = content_hash("original sourced body")
    _seed_projected_row(conn, hash_=old_hash, body="original sourced body",
                        source_url="https://x/p", vault_path=rel)

    (vault / rel).write_text("HUMAN edited body")
    watcher._on_change(conn, rel, str(vault / rel))
    return old_hash, content_hash("HUMAN edited body"), rel


def test_human_edit_creates_new_revision_row(conn, tmp_path):
    old_hash, new_hash, _ = _do_edit(conn, tmp_path)
    assert old_hash != new_hash

    new = conn.execute("SELECT * FROM content WHERE hash = ?", (new_hash,)).fetchone()
    assert new is not None, "a new content row addressed by the edited body must exist"
    assert new["body"] == "HUMAN edited body"
    assert new["is_current"] == 1
    assert new["protected"] == 1
    assert new["revision"] == 2
    # Source metadata carried forward from the superseded revision.
    assert new["source_url"] == "https://x/p"
    assert new["kind"] == "research"
    assert new["vault_path"] == "030-research/page.md"


def test_old_revision_is_superseded_and_unmutated(conn, tmp_path):
    old_hash, new_hash, _ = _do_edit(conn, tmp_path)

    old = conn.execute("SELECT * FROM content WHERE hash = ?", (old_hash,)).fetchone()
    assert old is not None, "the original revision must be retained, not overwritten"
    assert old["body"] == "original sourced body", "old row body must NOT be mutated"
    assert old["is_current"] == 0
    assert old["superseded_by"] == new_hash


def test_content_addressing_holds_after_edit(conn, tmp_path):
    """The core invariant #55 is about: every row's body hashes to its hash."""
    from engram.dedup import content_hash
    _do_edit(conn, tmp_path)
    for row in conn.execute("SELECT hash, body FROM content"):
        assert content_hash(row["body"]) == row["hash"]


def test_verify_hash_integrity_passes_after_edit(conn, tmp_path):
    from engram import maintenance
    _do_edit(conn, tmp_path)
    result = maintenance.verify(conn)
    assert result["hash_mismatches"] == []
    integrity = next(c for c in result["checks"] if c["name"] == "hash_integrity")
    assert integrity["ok"] is True


def test_vault_state_repointed_to_new_hash(conn, tmp_path):
    old_hash, new_hash, rel = _do_edit(conn, tmp_path)
    vs = conn.execute(
        "SELECT content_hash, rendered_body FROM vault_state WHERE vault_path = ?",
        (rel,),
    ).fetchone()
    assert vs["content_hash"] == new_hash
    assert vs["rendered_body"] == "HUMAN edited body"


def test_vault_edit_event_carries_old_and_new_hashes(conn, tmp_path):
    import json
    old_hash, new_hash, rel = _do_edit(conn, tmp_path)
    rows = conn.execute(
        "SELECT payload FROM events WHERE type = 'vault_edit'"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["hash_old"] == old_hash
    assert payload["hash_new"] == new_hash
    assert payload["path"] == rel


def test_second_edit_extends_the_revision_chain(conn, tmp_path):
    from engram.dedup import content_hash
    from engram.watcher import watcher

    old_hash, h2, rel = _do_edit(conn, tmp_path)  # revision 1 -> 2

    # A second human edit: revision 2 -> 3.
    vault = tmp_path / "vault"
    (vault / rel).write_text("HUMAN edited AGAIN")
    watcher._on_change(conn, rel, str(vault / rel))
    h3 = content_hash("HUMAN edited AGAIN")

    cur = conn.execute(
        "SELECT hash, revision, is_current, protected FROM content WHERE is_current = 1"
    ).fetchall()
    assert len(cur) == 1
    assert cur[0]["hash"] == h3
    assert cur[0]["revision"] == 3
    assert cur[0]["protected"] == 1
    # h2 is now superseded by h3; chain old_hash -> h2 -> h3 intact.
    assert conn.execute("SELECT superseded_by FROM content WHERE hash = ?",
                        (h2,)).fetchone()["superseded_by"] == h3
    assert conn.execute("SELECT superseded_by FROM content WHERE hash = ?",
                        (old_hash,)).fetchone()["superseded_by"] == h2
    # Content-addressing still holds across the whole chain.
    for row in conn.execute("SELECT hash, body FROM content"):
        assert content_hash(row["body"]) == row["hash"]
