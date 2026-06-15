"""Unit D (#152 + #96 ordering): projector DB writes are atomic AND committed
BEFORE the vault file is written.

`_project_one` wraps its two DB writes (vault_state upsert + content.vault_path)
in `common.db.transaction()`, which COMMITS before `_atomic_write` runs. Two
properties are proven:

* a DB-write failure rolls both DB writes back AND never writes the file
  (the file write is never reached);
* a file-write failure does NOT roll back the already-committed DB state -- which
  is exactly the #96 DB-before-file ordering (the row must be durable before the
  bytes hit disk so a cross-process watcher never reads a stale rendered_body).
"""
import sqlite3
from pathlib import Path

import pytest

from engram.dedup import content_hash
from engram.projector import projector

REPO = Path(__file__).resolve().parents[2]


def _conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.sqlite", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql",
               "003_grounding.sql", "004_protected.sql"):
        c.executescript((REPO / "schema" / fn).read_text())
    return c


def _seed(conn, h):
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current) "
        "VALUES (?, 'a body', 'T', 'https://x/p', 'vendor-doc', 0.7, 'research', 1, 1)",
        (h,),
    )


class _FailingConn:
    """Delegates to a real connection but raises on a statement matching a substr,
    so we can fail a specific DB write deterministically (sqlite3.Connection.execute
    can't be monkeypatched on the instance)."""

    def __init__(self, real, fail_substr):
        self._real = real
        self._fail = fail_substr

    def execute(self, sql, *a, **k):
        if self._fail in sql:
            raise sqlite3.OperationalError("db write exploded")
        return self._real.execute(sql, *a, **k)

    @property
    def in_transaction(self):
        return self._real.in_transaction

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_db_write_failure_rolls_back_and_writes_no_file(tmp_path):
    conn = _conn(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    kind_dirs = {"research": "030-research"}
    h = content_hash("a body")
    _seed(conn, h)

    # Fail the SECOND DB write (UPDATE content SET vault_path) inside the txn.
    failing = _FailingConn(conn, "UPDATE content SET vault_path")
    with pytest.raises(sqlite3.OperationalError, match="db write exploded"):
        projector._project_one(failing, vault, h, kind_dirs)

    # The transaction rolled back: no vault_state row, content.vault_path unset.
    assert conn.execute("SELECT count(*) FROM vault_state").fetchone()[0] == 0
    assert conn.execute("SELECT vault_path FROM content WHERE hash=?", (h,)).fetchone()["vault_path"] is None
    # The file write was never reached.
    assert list(vault.rglob("*.md")) == []


def test_file_write_failure_keeps_committed_db_state(tmp_path, monkeypatch):
    """The DB commit happens BEFORE the file write (#96): a file-write failure
    leaves the committed DB state in place, proving the ordering."""
    conn = _conn(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    kind_dirs = {"research": "030-research"}
    h = content_hash("a body")
    _seed(conn, h)

    def _boom_write(path, body):
        raise OSError("disk full")

    monkeypatch.setattr(projector, "_atomic_write", _boom_write)
    with pytest.raises(OSError, match="disk full"):
        projector._project_one(conn, vault, h, kind_dirs)

    # DB state was committed BEFORE the (failed) file write, so it persists:
    vs = conn.execute("SELECT content_hash FROM vault_state").fetchall()
    assert len(vs) == 1
    assert vs[0]["content_hash"] == h
    assert conn.execute(
        "SELECT vault_path FROM content WHERE hash=?", (h,)
    ).fetchone()["vault_path"] is not None
    # ...but the file itself was not written (the write raised).
    assert list(vault.rglob("*.md")) == []
