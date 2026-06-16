import json
import sqlite3

import pytest
import sqlite_vec

from engram.common.db import init_schema
from engram.dedup import content_hash
from engram.watcher import watcher


@pytest.fixture
def conn(tmp_path, monkeypatch):
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


def _seed_projected_row(conn, *, rel: str, body: str, source_url: str):
    h = content_hash(body)
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, ttl_days, kind, revision, is_current, vault_path) "
        "VALUES (?, ?, 'T', ?, 'vendor-doc', 0.7, 180, 'research', 1, 1, ?)",
        (h, body, source_url, rel),
    )
    conn.execute(
        "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
        (rel, h, body),
    )
    conn.commit()
    return h


def test_on_delete_removes_vault_state_and_content_path(conn):
    rel = "030-research/deleted.md"
    _seed_projected_row(conn, rel=rel, body="to be deleted", source_url="https://x/delete")

    watcher._on_delete(conn, rel)

    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE vault_path = ?", (rel,)
    ).fetchone() is None
    row = conn.execute("SELECT vault_path FROM content WHERE source_url = ?", ("https://x/delete",)).fetchone()
    assert row["vault_path"] is None


def test_on_move_repoints_existing_vault_state_row(conn):
    src = "030-research/old-name.md"
    dest = "030-research/new-name.md"
    h = _seed_projected_row(conn, rel=src, body="same bytes", source_url="https://x/move")

    watcher._on_move(conn, src, dest)

    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE vault_path = ?", (src,)
    ).fetchone() is None
    moved = conn.execute(
        "SELECT vault_path, content_hash FROM vault_state WHERE vault_path = ?", (dest,)
    ).fetchone()
    assert moved["content_hash"] == h
    assert moved["vault_path"] == dest
    content_row = conn.execute("SELECT vault_path FROM content WHERE hash = ?", (h,)).fetchone()
    assert content_row["vault_path"] == dest


def test_startup_reconcile_applies_create_modify_delete(conn, tmp_path):
    vault = tmp_path / "vault"
    (vault / "030-research").mkdir(parents=True)

    # modified while daemon was down
    modified_rel = "030-research/modified.md"
    _seed_projected_row(
        conn,
        rel=modified_rel,
        body="old body",
        source_url="https://x/modified",
    )
    (vault / modified_rel).write_text("new body", encoding="utf-8")

    # created while daemon was down (not in vault_state)
    created_rel = "030-research/created.md"
    (vault / created_rel).write_text("created body", encoding="utf-8")

    # deleted while daemon was down (still in vault_state)
    deleted_rel = "030-research/deleted.md"
    _seed_projected_row(
        conn,
        rel=deleted_rel,
        body="deleted body",
        source_url="https://x/deleted",
    )

    watcher._reconcile_startup(conn, vault, ignore=[])

    # modify applied: existing row repointed to new hash/body
    modified = conn.execute(
        "SELECT content_hash, rendered_body FROM vault_state WHERE vault_path = ?",
        (modified_rel,),
    ).fetchone()
    assert modified["rendered_body"] == "new body"
    assert modified["content_hash"] == content_hash("new body")

    # create applied: startup reconcile uses the same unknown-path handling as
    # live events (manual ingest), which records content + an ingested event.
    created_hash = content_hash("created body")
    created_row = conn.execute(
        "SELECT hash, source_tier, kind FROM content WHERE hash = ?",
        (created_hash,),
    ).fetchone()
    assert created_row is not None
    assert created_row["source_tier"] == "manual"
    assert created_row["kind"] == "kb"

    ingested = [
        json.loads(r["payload"])
        for r in conn.execute("SELECT payload FROM events WHERE type = 'ingested'").fetchall()
    ]
    assert any(evt.get("hash") == created_hash for evt in ingested)

    # delete applied: stale row removed
    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE vault_path = ?", (deleted_rel,)
    ).fetchone() is None
