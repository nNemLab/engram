import json
import sqlite3

import pytest
import sqlite_vec
from watchdog.events import FileDeletedEvent, FileMovedEvent

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


def test_startup_reconcile_skips_empty_note_and_keeps_processing(conn, tmp_path):
    vault = tmp_path / "vault"
    (vault / "030-research").mkdir(parents=True)

    (vault / "030-research/empty.md").write_text("", encoding="utf-8")
    (vault / "030-research/valid.md").write_text("valid body", encoding="utf-8")

    watcher._reconcile_startup(conn, vault, ignore=[])

    assert conn.execute(
        "SELECT 1 FROM content WHERE hash = ?",
        (content_hash("valid body"),),
    ).fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM content WHERE hash = ?",
        (content_hash(""),),
    ).fetchone() is None


def test_startup_reconcile_isolates_per_file_change_errors(conn, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "030-research").mkdir(parents=True)
    (vault / "030-research/bad.md").write_text("bad", encoding="utf-8")
    (vault / "030-research/good.md").write_text("good", encoding="utf-8")

    orig = watcher._on_change

    def _flaky(c, rel, abs_path):
        if rel.endswith("bad.md"):
            raise RuntimeError("boom")
        return orig(c, rel, abs_path)

    monkeypatch.setattr(watcher, "_on_change", _flaky)

    # bad.md failure is contained; good.md still processed.
    watcher._reconcile_startup(conn, vault, ignore=[])

    assert conn.execute(
        "SELECT 1 FROM content WHERE hash = ?",
        (content_hash("good"),),
    ).fetchone() is not None


def test_on_move_over_existing_tracked_path_replaces_dest(conn):
    src = "030-research/src.md"
    dest = "030-research/dest.md"
    src_hash = _seed_projected_row(conn, rel=src, body="src body", source_url="https://x/src")
    dest_hash = _seed_projected_row(conn, rel=dest, body="dest body", source_url="https://x/dest")

    watcher._on_move(conn, src, dest)

    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE vault_path = ?", (src,)
    ).fetchone() is None
    moved = conn.execute(
        "SELECT content_hash FROM vault_state WHERE vault_path = ?", (dest,)
    ).fetchone()
    assert moved["content_hash"] == src_hash

    src_row = conn.execute("SELECT vault_path FROM content WHERE hash = ?", (src_hash,)).fetchone()
    assert src_row["vault_path"] == dest

    old_dest_row = conn.execute("SELECT vault_path FROM content WHERE hash = ?", (dest_hash,)).fetchone()
    assert old_dest_row["vault_path"] is None


class _FakeDebouncer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def trigger(self, key: str, rel: str, abs_path: str) -> None:
        self.calls.append((key, rel, abs_path))


def test_handler_on_moved_both_in_vault_repoints(conn, tmp_path):
    vault = tmp_path / "vault"
    (vault / "030-research").mkdir(parents=True)
    src_rel = "030-research/a.md"
    dest_rel = "030-research/b.md"
    src_hash = _seed_projected_row(conn, rel=src_rel, body="a body", source_url="https://x/a")

    handler = watcher._Handler(vault, _FakeDebouncer(), [], conn)
    handler.on_moved(FileMovedEvent(str(vault / src_rel), str(vault / dest_rel)))

    moved = conn.execute(
        "SELECT content_hash FROM vault_state WHERE vault_path = ?", (dest_rel,)
    ).fetchone()
    assert moved is not None
    assert moved["content_hash"] == src_hash


def test_handler_on_moved_src_only_deletes(conn, tmp_path):
    vault = tmp_path / "vault"
    (vault / "030-research").mkdir(parents=True)
    src_rel = "030-research/outgoing.md"
    _seed_projected_row(conn, rel=src_rel, body="body", source_url="https://x/out")

    handler = watcher._Handler(vault, _FakeDebouncer(), [], conn)
    handler.on_moved(FileMovedEvent(str(vault / src_rel), str(tmp_path / "outside.md")))

    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE vault_path = ?", (src_rel,)
    ).fetchone() is None


def test_handler_on_moved_dest_only_triggers_ingest(conn, tmp_path):
    vault = tmp_path / "vault"
    (vault / "030-research").mkdir(parents=True)
    dest_rel = "030-research/incoming.md"
    dest_abs = vault / dest_rel
    dest_abs.write_text("incoming", encoding="utf-8")

    debouncer = _FakeDebouncer()
    handler = watcher._Handler(vault, debouncer, [], conn)
    handler.on_moved(FileMovedEvent(str(tmp_path / "outside.md"), str(dest_abs)))

    assert debouncer.calls == [(dest_rel, dest_rel, str(dest_abs))]


def test_handler_on_deleted_calls_delete(conn, tmp_path):
    vault = tmp_path / "vault"
    (vault / "030-research").mkdir(parents=True)
    rel = "030-research/deleted-by-handler.md"
    _seed_projected_row(conn, rel=rel, body="body", source_url="https://x/handler-del")

    handler = watcher._Handler(vault, _FakeDebouncer(), [], conn)
    handler.on_deleted(FileDeletedEvent(str(vault / rel)))

    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE vault_path = ?", (rel,)
    ).fetchone() is None
