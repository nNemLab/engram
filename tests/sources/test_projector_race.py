"""#96: the projector must commit `vault_state.rendered_body` BEFORE writing the
vault file, so a watcher running in a SEPARATE process can never read a stale
rendered_body for a just-written file and fabricate a spurious actor="human"
vault_edit.

The watcher's feedback-loop guard is `new_body == vault_state.rendered_body`.
If the projector writes the file first and updates rendered_body second, a
debounced watcher read landing in that window compares the projector's fresh
bytes against the OLD rendered_body, mismatches, and records a bogus human
edit + revision. PR #112's in-process RLock does NOT serialize across the
watcher and projector processes, so the only robust fix is write/commit
ordering: the row must be durable before the bytes hit disk.

These tests simulate the cross-process watcher by opening a SECOND connection
to the same database at the exact moment the projector is about to write the
file, and asserting the committed rendered_body already equals the bytes being
written (so the watcher's guard would short-circuit, not classify a human edit).
"""
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql",
               "003_grounding.sql", "004_protected.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.sqlite"


@pytest.fixture
def conn(db_path, monkeypatch):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c)
    from types import SimpleNamespace

    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    yield c


def _watcher_reads_during_write(db_path, vault):
    """Build a spy for projector._atomic_write that, at the instant the file is
    about to be written, opens a SECOND connection (the watcher's process) and
    reads vault_state.rendered_body for the target path. Returns (spy, captured).

    captured["match"] is True iff the committed rendered_body the watcher sees
    already equals the bytes being written — i.e. no stale-body window exists.
    """
    from engram.projector import projector

    captured: dict = {}
    real = projector._atomic_write

    def spy(path, body):
        other = sqlite3.connect(db_path)
        other.row_factory = sqlite3.Row
        try:
            rel = str(Path(path).relative_to(vault))
            row = other.execute(
                "SELECT rendered_body FROM vault_state WHERE vault_path = ?",
                (rel,),
            ).fetchone()
        finally:
            other.close()
        seen = row["rendered_body"] if row else None
        captured["seen"] = seen
        captured["body"] = body
        captured["match"] = seen == body
        real(path, body)

    return spy, captured


def test_normal_render_commits_rendered_body_before_file_write(conn, db_path, tmp_path, monkeypatch):
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.projector import projector

    vault = tmp_path / "vault"
    vault.mkdir()
    kind_dirs = {"research": "030-research"}

    h = content_hash("first body")
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current) "
        "VALUES (?, 'first body', 'T', 'https://x/p', 'vendor-doc', 0.7, 'research', 1, 1)",
        (h,),
    )
    event_log.append(conn, "ingested", {"hash": h}, actor="system")
    conn.commit()

    spy, captured = _watcher_reads_during_write(db_path, vault)
    monkeypatch.setattr(projector, "_atomic_write", spy)

    evt = list(event_log.since(conn, 0, types=["ingested"]))[-1]
    projector._handle_event(conn, vault, evt, kind_dirs)

    assert captured, "the projector must go through _atomic_write"
    # A separate-process watcher reading at the write instant sees a committed
    # rendered_body equal to the bytes on disk -> guard short-circuits, no
    # spurious human vault_edit (#96).
    assert captured["match"], (
        f"rendered_body the watcher would read ({captured['seen']!r}) must already "
        f"equal the bytes being written ({captured['body']!r}) before the file exists"
    )


def test_superseded_render_commits_rendered_body_before_file_write(conn, db_path, tmp_path, monkeypatch):
    """The superseded handler overwrites an existing vault file in place; its
    rendered_body update must also be committed before the file write so the
    watcher never reads the OLD revision's body against the NEW file bytes."""
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.projector import projector

    vault = tmp_path / "vault"
    (vault / "030-research").mkdir(parents=True)
    kind_dirs = {"research": "030-research"}
    rel = "030-research/page.md"

    h_old = content_hash("old body")
    h_new = content_hash("new current body")
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current, vault_path) "
        "VALUES (?, 'old body', 'T', 'https://x/p', 'vendor-doc', 0.7, 'research', 1, 0, ?)",
        (h_old, rel),
    )
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current) "
        "VALUES (?, 'new current body', 'T', 'https://x/p', 'vendor-doc', 0.7, 'research', 2, 1)",
        (h_new,),
    )
    # Old revision already projected: vault file + vault_state hold the OLD body.
    (vault / rel).write_text("old body")
    conn.execute(
        "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
        "VALUES (?, ?, 'old body', '2026-01-01T00:00:00Z')",
        (rel, h_old),
    )
    event_log.append(
        conn, "superseded",
        {"hash_old": h_old, "hash_new": h_new, "source_url": "https://x/p"},
        actor="system",
    )
    conn.commit()

    spy, captured = _watcher_reads_during_write(db_path, vault)
    monkeypatch.setattr(projector, "_atomic_write", spy)

    evt = list(event_log.since(conn, 0, types=["superseded"]))[-1]
    projector._handle_event(conn, vault, evt, kind_dirs)

    assert captured, "the superseded handler must go through _atomic_write"
    assert captured["match"], (
        f"rendered_body the watcher would read ({captured['seen']!r}) must already "
        f"equal the new bytes being written ({captured['body']!r}) before the file is replaced"
    )


def test_watcher_does_not_fabricate_edit_for_projector_write(conn, db_path, tmp_path, monkeypatch):
    """End-to-end guard: simulate the watcher's full read-and-classify on a
    SECOND connection at the moment the projector writes, and confirm it does
    NOT record a vault_edit (the body it reads matches the committed rendered_body).
    """
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.projector import projector

    vault = tmp_path / "vault"
    vault.mkdir()
    kind_dirs = {"research": "030-research"}

    h = content_hash("watched body")
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current) "
        "VALUES (?, 'watched body', 'T', 'https://x/p', 'vendor-doc', 0.7, 'research', 1, 1)",
        (h,),
    )
    event_log.append(conn, "ingested", {"hash": h}, actor="system")
    conn.commit()

    real = projector._atomic_write
    classified: dict = {}

    def spy(path, body):
        # Write the bytes first (as the real projector does), then run the
        # watcher guard from a separate connection reading the just-written file.
        real(path, body)
        other = sqlite3.connect(db_path)
        other.row_factory = sqlite3.Row
        try:
            rel = str(Path(path).relative_to(vault))
            new_body = Path(path).read_text()
            row = other.execute(
                "SELECT rendered_body FROM vault_state WHERE vault_path = ?",
                (rel,),
            ).fetchone()
        finally:
            other.close()
        # The watcher's feedback-loop guard (watcher.py): equal bodies => no edit.
        classified["is_human_edit"] = not (row and new_body == row["rendered_body"])

    monkeypatch.setattr(projector, "_atomic_write", spy)

    evt = list(event_log.since(conn, 0, types=["ingested"]))[-1]
    projector._handle_event(conn, vault, evt, kind_dirs)

    assert classified, "the projector must go through _atomic_write"
    assert classified["is_human_edit"] is False, (
        "a watcher reading the projector's own freshly-written file must NOT "
        "classify it as a human edit (#96)"
    )
