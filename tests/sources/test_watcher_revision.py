"""#55: a human vault edit must become a first-class new revision, not an
in-place body mutation that breaks content-addressing.

Before the fix, `_on_change` did `UPDATE content SET body=? WHERE hash=?`,
leaving `content_hash(body) != hash` for every human-edited row (verify's
hash_integrity check then flagged it as corruption).
"""
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

from engram.common.db import init_schema

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def conn(tmp_path, monkeypatch):
    # Apply the FULL schema via the app's init_schema path -- crucially this runs
    # every migration, including 007's UNIQUE partial index
    # `content(source_url) WHERE is_current=1`. Earlier this test applied only
    # 001-004, so the index was absent and the sourced-edit regression it now
    # guards against was invisible. Mirror the production connection
    # (isolation_level=None autocommit + sqlite-vec) so the watcher's
    # `transaction()` and the migration runner behave exactly as in the daemon.
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    init_schema(c, embed_dim=4)
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    yield c
    c.close()


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


def test_sourced_edit_with_unique_index_keeps_one_current(conn, tmp_path):
    """The previously-broken production path: with the one-current-per-source_url
    index present (schema 007), a human edit of a SOURCED vault file creates the
    new revision and leaves EXACTLY one is_current=1 row for that source_url.

    The old insert(is_current=1)->promote->demote ordering raised IntegrityError
    here (INSERT OR IGNORE silently dropped the new revision, then the
    superseded_by FK failed); the demote-before-promote reorder fixes it.
    """
    # Guard: the fixture really did apply the index (so this test can't silently
    # revert to a pre-007 schema and re-mask the regression).
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_content_one_current_per_url'"
    ).fetchone() is not None, "schema 007 unique index missing -- test would not exercise the bug"

    old_hash, new_hash, _ = _do_edit(conn, tmp_path)

    current = conn.execute(
        "SELECT hash FROM content WHERE source_url = ? AND is_current = 1",
        ("https://x/p",),
    ).fetchall()
    assert len(current) == 1, "exactly one current row must remain for the source_url"
    assert current[0]["hash"] == new_hash


def test_cross_source_identical_body_edit_does_not_zero_out_source(conn, tmp_path):
    """BLOCKING (cross-source hash reuse): editing sourced file A to bytes that
    already exist as a content row under a DIFFERENT source_url B must NOT demote
    A's current row and promote/mutate B's row. A must keep exactly one current
    row and B must be left completely untouched.

    Without the cross-source guard + exactly-one-current assertion, the swap
    demotes A's old row and promotes B's row by hash, leaving A with ZERO current
    rows and repointing B's row at A's vault file.
    """
    from engram.dedup import content_hash
    from engram.watcher import watcher

    vault = tmp_path / "vault"
    (vault / "030-research").mkdir(parents=True)

    # B: a sourced content row with a distinctive body, projected to its own file.
    b_rel = "030-research/B.md"
    b_body = "content that already lives under source B"
    b_hash = content_hash(b_body)
    _seed_projected_row(conn, hash_=b_hash, body=b_body,
                        source_url="https://x/B", vault_path=b_rel)
    (vault / b_rel).write_text(b_body)

    # A: a different sourced row, projected to A's own file.
    a_rel = "030-research/A.md"
    a_body = "original body for source A"
    a_hash = content_hash(a_body)
    _seed_projected_row(conn, hash_=a_hash, body=a_body,
                        source_url="https://x/A", vault_path=a_rel)
    (vault / a_rel).write_text(a_body)

    # The human edits A's file to be byte-identical to B's existing content.
    (vault / a_rel).write_text(b_body)
    watcher._on_change(conn, a_rel, str(vault / a_rel))

    # A still has EXACTLY ONE current row, and it is still A's original revision.
    current_a = conn.execute(
        "SELECT hash FROM content WHERE source_url = ? AND is_current = 1 AND tombstoned = 0",
        ("https://x/A",),
    ).fetchall()
    assert len(current_a) == 1, "source A must never end with zero/two current rows"
    assert current_a[0]["hash"] == a_hash

    # B's row is completely unaffected: still current, still owned by B, still its
    # own file, body unchanged.
    b_row = conn.execute(
        "SELECT source_url, is_current, vault_path, body FROM content WHERE hash = ?",
        (b_hash,),
    ).fetchone()
    assert b_row["source_url"] == "https://x/B"
    assert b_row["is_current"] == 1
    assert b_row["vault_path"] == b_rel
    assert b_row["body"] == b_body
    # No row was promoted/repointed under the wrong source_url (B never claims A's file).
    assert conn.execute(
        "SELECT COUNT(*) FROM content WHERE source_url = ? AND vault_path = ?",
        ("https://x/B", a_rel),
    ).fetchone()[0] == 0
    # B's vault_state still maps B's file to B's hash.
    assert conn.execute(
        "SELECT content_hash FROM vault_state WHERE vault_path = ?", (b_rel,)
    ).fetchone()["content_hash"] == b_hash


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
