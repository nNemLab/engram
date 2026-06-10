"""#37: protect human-edited sourced rows from silent supersede clobber."""
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
    # Patch where gate() looks load_config up (engram.dedup.load_config).
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    yield c


def test_protected_column_exists_and_defaults_zero(conn):
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(content)")}
    assert "protected" in cols
    assert cols["protected"]["dflt_value"] in ("0", 0)
    # schema_version advanced to 4
    v = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert v >= 4


def _seed_sourced_row(conn, *, hash_, body, source_url, vault_path):
    """Insert a current sourced content row + its vault_state projection."""
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, kind, revision, is_current) "
        "VALUES (?, ?, 'T', ?, 'vendor-doc', 0.7, 'research', 1, 1)",
        (hash_, body, source_url),
    )
    conn.execute(
        "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
        (vault_path, hash_, body),
    )
    conn.commit()


def test_watcher_human_edit_sets_protected(conn, tmp_path, monkeypatch):
    from engram.dedup import content_hash
    from engram.watcher import watcher

    # Vault file on disk + matching content/vault_state row.
    vault = tmp_path / "vault"
    (vault / "050-kb").mkdir(parents=True)
    rel = "050-kb/page.md"
    (vault / rel).write_text("original sourced body")
    h = content_hash("original sourced body")
    _seed_sourced_row(conn, hash_=h, body="original sourced body",
                      source_url="https://x/p", vault_path=rel)

    # Human edits the file, then the watcher observes the change.
    (vault / rel).write_text("HUMAN edited body")
    watcher._on_change(conn, rel, str(vault / rel))

    row = conn.execute("SELECT protected, body FROM content WHERE hash = ?", (h,)).fetchone()
    assert row["protected"] == 1
    assert row["body"] == "HUMAN edited body"
    # vault_edit event still recorded.
    n = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'vault_edit'").fetchone()[0]
    assert n == 1
