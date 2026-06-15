"""#119: projector's superseded handler honours content.protected — skips vault file
write when the new content is protected, still updates DB state."""
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
    from types import SimpleNamespace

    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    yield c


def test_supersede_protected_skips_vault_file_write(conn, tmp_path):
    """Protected content: DB state is updated but the vault file is NOT
    overwritten — it retains whatever the human wrote."""
    from engram import log as event_log
    from engram.projector.projector import _handle_event

    vault = tmp_path / "vault"
    vault.mkdir()
    kind_dirs = {"research": "030-research"}

    # Seed: hash_old with vault projection, hash_new (protected) in content.
    h_old = "hash_old_119"
    h_new = "hash_new_119_protected"
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, kind, revision, is_current, protected) "
        "VALUES (?, 'old body', 'T', 'https://x/p', 'vendor-doc', 0.7, "
        "'research', 1, 0, 0)",
        (h_old,),
    )
    # vault_state points to h_old at path "030-research/page.md"
    rel_path = "030-research/page.md"
    conn.execute(
        "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
        (rel_path, h_old, "old body",),
    )
    # The new content (to supersede in) is PROTECTED.
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, kind, revision, is_current, protected) "
        "VALUES (?, 'protected new body', 'T', 'https://x/p', 'vendor-doc', 0.7, "
        "'research', 2, 1, 1)",
        (h_new,),
    )
    conn.commit()

    # Write the original vault file so we can verify it stays untouched.
    vault_file = vault / rel_path
    vault_file.parent.mkdir(parents=True, exist_ok=True)
    vault_file.write_text("HUMAN-edited content (should survive)")

    # Emit and process the superseded event.
    event_log.append(
        conn, "superseded",
        {"hash_old": h_old, "hash_new": h_new, "source_url": "https://x/p"},
        actor="human",
    )
    conn.commit()

    sup_evt = list(event_log.since(conn, 0, types=["superseded"]))[-1]
    _handle_event(conn, vault, sup_evt, kind_dirs)

    # vault_state was repointed: h_old deleted, h_new at same path.
    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE content_hash=?", (h_old,)
    ).fetchone() is None
    new_state = conn.execute(
        "SELECT vault_path, content_hash FROM vault_state WHERE content_hash=?",
        (h_new,),
    ).fetchone()
    assert new_state["vault_path"] == rel_path
    assert new_state["content_hash"] == h_new

    # content.vault_path updated for hash_new.
    cvp = conn.execute(
        "SELECT vault_path FROM content WHERE hash=?", (h_new,)
    ).fetchone()
    assert cvp["vault_path"] == rel_path

    # CRITICAL: the vault file was NOT overwritten — human content preserved.
    assert vault_file.read_text() == "HUMAN-edited content (should survive)"


def test_supersede_unprotected_overwrites_vault_file(conn, tmp_path):
    """Non-protected content: the vault file IS projected/overwritten as
    before — existing behaviour preserved."""
    from engram import log as event_log
    from engram.projector.projector import _handle_event

    vault = tmp_path / "vault"
    vault.mkdir()
    kind_dirs = {"research": "030-research"}

    h_old = "hash_old_119u"
    h_new = "hash_new_119_unprotected"
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, kind, revision, is_current, protected) "
        "VALUES (?, 'old body', 'T', 'https://x/q', 'vendor-doc', 0.7, "
        "'research', 1, 0, 0)",
        (h_old,),
    )
    rel_path = "030-research/page2.md"
    conn.execute(
        "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
        (rel_path, h_old, "old body",),
    )
    # The new content is NOT protected.
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, kind, revision, is_current, protected) "
        "VALUES (?, 'new body', 'T', 'https://x/q', 'vendor-doc', 0.7, "
        "'research', 2, 1, 0)",
        (h_new,),
    )
    conn.commit()

    vault_file = vault / rel_path
    vault_file.parent.mkdir(parents=True, exist_ok=True)
    vault_file.write_text("something else")

    event_log.append(
        conn, "superseded",
        {"hash_old": h_old, "hash_new": h_new, "source_url": "https://x/q"},
        actor="poller",
    )
    conn.commit()

    sup_evt = list(event_log.since(conn, 0, types=["superseded"]))[-1]
    _handle_event(conn, vault, sup_evt, kind_dirs)

    # DB state updated.
    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE content_hash=?", (h_old,)
    ).fetchone() is None
    new_state = conn.execute(
        "SELECT vault_path, content_hash FROM vault_state WHERE content_hash=?",
        (h_new,),
    ).fetchone()
    assert new_state["vault_path"] == rel_path

    # CRITICAL: the vault file WAS overwritten with the rendered body
    # (renderer adds YAML frontmatter wrapping the original body).
    text = vault_file.read_text()
    assert "new body" in text
