"""Tests for engram.maintenance — snapshot / verify / restore of the event log.

SAFETY: every test operates on temp DBs built in tmp_path. Nothing here touches
the real ~/.engram/db.sqlite. The maintenance module is path-explicit by design.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

import pytest
import sqlite_vec

from engram import log as event_log
from engram import maintenance
from engram.dedup import content_hash

REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "schema"


def _open(path: Path) -> sqlite3.Connection:
    """Open a DB by path with sqlite-vec loaded (mirrors db._connect)."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _apply_schema(conn: sqlite3.Connection, embed_dim: int = 4) -> None:
    for fn in (
        "001_initial.sql",
        "002_sources_and_revisions.sql",
        "003_grounding.sql",
        "004_protected.sql",
        "005_event_hash_chain.sql",
    ):
        conn.executescript((SCHEMA / fn).read_text())
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0("
        f"content_hash TEXT PRIMARY KEY, embedding FLOAT[{embed_dim}])"
    )


def _insert_content(conn: sqlite3.Connection, body: str, *, tombstoned: int = 0) -> str:
    h = content_hash(body)
    conn.execute(
        "INSERT INTO content (hash, body, title, tombstoned) VALUES (?, ?, ?, ?)",
        (h, body, "t", tombstoned),
    )
    return h


def _make_db(path: Path, *, with_embedding: bool = True) -> dict:
    """Build a clean, internally-consistent DB. Returns some inserted hashes."""
    conn = _open(path)
    _apply_schema(conn)
    h1 = _insert_content(conn, "First body about CUDA kernels.")
    h2 = _insert_content(conn, "Second body about SQLite WAL mode.")
    h3 = _insert_content(conn, "Tombstoned merged body.", tombstoned=1)
    # An event or two.
    conn.execute(
        "INSERT INTO events (type, payload, actor) VALUES ('ingested', '{}', 'agent')"
    )
    conn.execute(
        "INSERT INTO events (type, payload, actor) VALUES ('ingested', '{}', 'agent')"
    )
    if with_embedding:
        emb = sqlite_vec.serialize_float32([0.1, 0.2, 0.3, 0.4])
        conn.execute(
            "INSERT INTO embeddings (content_hash, embedding) VALUES (?, ?)", (h1, emb)
        )
    conn.commit()
    conn.close()
    return {"h1": h1, "h2": h2, "h3": h3}


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
def test_snapshot_round_trip(tmp_path):
    src = tmp_path / "db.sqlite"
    hashes = _make_db(src)
    out = tmp_path / "snap.sqlite"

    result = maintenance.snapshot(src, out)

    assert result["path"] == out
    assert result["size_bytes"] > 0
    assert out.exists()
    assert out.stat().st_size == result["size_bytes"]
    assert result["event_count"] == 2
    assert result["content_count"] == 3  # includes tombstoned

    # Reopen the snapshot and confirm the rows survived intact.
    conn = _open(out)
    n_content = conn.execute("SELECT COUNT(*) FROM content").fetchone()[0]
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_emb = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    body = conn.execute(
        "SELECT body FROM content WHERE hash = ?", (hashes["h1"],)
    ).fetchone()[0]
    conn.close()
    assert n_content == 3
    assert n_events == 2
    assert n_emb == 1
    assert body == "First body about CUDA kernels."


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def test_verify_passes_on_clean_db(tmp_path):
    src = tmp_path / "db.sqlite"
    _make_db(src)

    result = maintenance.verify(src)

    assert result["ok"] is True, result
    assert result["hash_mismatches"] == []
    assert result["content_checked"] == 3
    assert all(c["ok"] for c in result["checks"]), result["checks"]
    # Sanity: the named checks are all present.
    names = {c["name"] for c in result["checks"]}
    assert {"daemon_cursors", "embeddings_ref", "superseded_by_ref", "integrity_check"} <= names


def test_verify_detects_hash_mismatch(tmp_path):
    src = tmp_path / "db.sqlite"
    hashes = _make_db(src)
    # Corrupt one body WITHOUT updating its stored hash.
    conn = _open(src)
    conn.execute(
        "UPDATE content SET body = ? WHERE hash = ?",
        ("tampered text", hashes["h2"]),
    )
    conn.commit()
    conn.close()

    result = maintenance.verify(src)

    assert result["ok"] is False
    assert hashes["h2"] in result["hash_mismatches"]
    assert len(result["hash_mismatches"]) == 1


def test_verify_detects_dangling_embedding(tmp_path):
    src = tmp_path / "db.sqlite"
    _make_db(src)
    conn = _open(src)
    emb = sqlite_vec.serialize_float32([0.5, 0.5, 0.5, 0.5])
    conn.execute(
        "INSERT INTO embeddings (content_hash, embedding) VALUES (?, ?)",
        ("deadbeef" * 8, emb),  # references a hash that does not exist
    )
    conn.commit()
    conn.close()

    result = maintenance.verify(src)

    assert result["ok"] is False
    emb_check = next(c for c in result["checks"] if c["name"] == "embeddings_ref")
    assert emb_check["ok"] is False


def test_verify_detects_dangling_superseded_by(tmp_path):
    src = tmp_path / "db.sqlite"
    hashes = _make_db(src)
    conn = _open(src)
    # Simulate latent corruption: FK enforcement off (as if the bad ref slipped
    # in with foreign_keys=OFF, or via page corruption) so verify must catch it.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "UPDATE content SET superseded_by = ? WHERE hash = ?",
        ("nonexistent" * 4, hashes["h1"]),
    )
    conn.commit()
    conn.close()

    result = maintenance.verify(src)

    assert result["ok"] is False
    sb_check = next(c for c in result["checks"] if c["name"] == "superseded_by_ref")
    assert sb_check["ok"] is False


def test_verify_skips_absent_daemon_cursors(tmp_path):
    # _make_db never creates daemon_cursors (daemons make it lazily at runtime).
    src = tmp_path / "db.sqlite"
    _make_db(src)
    result = maintenance.verify(src)
    cur_check = next(c for c in result["checks"] if c["name"] == "daemon_cursors")
    assert cur_check["ok"] is True
    assert "absent" in cur_check["detail"].lower() or "skipped" in cur_check["detail"].lower()


def test_verify_detects_cursor_past_max_event(tmp_path):
    src = tmp_path / "db.sqlite"
    _make_db(src)
    conn = _open(src)
    conn.execute(
        "CREATE TABLE daemon_cursors (name TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO daemon_cursors (name, last_event_id) VALUES ('projector', 999)"
    )  # MAX(events.id) is 2
    conn.commit()
    conn.close()

    result = maintenance.verify(src)

    assert result["ok"] is False
    cur_check = next(c for c in result["checks"] if c["name"] == "daemon_cursors")
    assert cur_check["ok"] is False


# --------------------------------------------------------------------------- #
# event hash chain (#45)
# --------------------------------------------------------------------------- #
def test_verify_passes_on_intact_chain(tmp_path):
    src = tmp_path / "db.sqlite"
    conn = _open(src)
    _apply_schema(conn)
    for i in range(5):
        event_log.append(conn, "system", {"i": i}, actor="agent")
    conn.commit()
    conn.close()

    result = maintenance.verify(src)

    assert result["ok"] is True, result
    chain = next(c for c in result["checks"] if c["name"] == "event_chain")
    assert chain["ok"] is True
    assert result["chain_checked"] == 5


def test_verify_detects_tampered_event_body(tmp_path):
    src = tmp_path / "db.sqlite"
    conn = _open(src)
    _apply_schema(conn)
    for i in range(5):
        event_log.append(conn, "system", {"i": i}, actor="agent")
    # Retroactively edit a chained event's payload, leaving its event_hash stale.
    conn.execute("UPDATE events SET payload = '{\"i\":999}' WHERE id = 3")
    conn.commit()
    conn.close()

    result = maintenance.verify(src)

    assert result["ok"] is False
    chain = next(c for c in result["checks"] if c["name"] == "event_chain")
    assert chain["ok"] is False


def test_verify_detects_deleted_event_breaks_chain(tmp_path):
    src = tmp_path / "db.sqlite"
    conn = _open(src)
    _apply_schema(conn)
    for i in range(5):
        event_log.append(conn, "system", {"i": i}, actor="agent")
    # Excising a middle event breaks the prev_hash linkage of its successor.
    conn.execute("DELETE FROM events WHERE id = 3")
    conn.commit()
    conn.close()

    result = maintenance.verify(src)

    assert result["ok"] is False
    chain = next(c for c in result["checks"] if c["name"] == "event_chain")
    assert chain["ok"] is False


def test_verify_detects_reordered_events(tmp_path):
    # Reordering two chained events is detectable: each row's event_hash is
    # bound to its own id/payload, so swapping two rows' bodies leaves their
    # stored event_hash stale and the recompute in verify mismatches.
    src = tmp_path / "db.sqlite"
    conn = _open(src)
    _apply_schema(conn)
    for i in range(3):
        event_log.append(conn, "system", {"i": i}, actor="agent")
    # Swap the payloads of two events without touching their event_hash.
    p2 = conn.execute("SELECT payload FROM events WHERE id = 2").fetchone()["payload"]
    p3 = conn.execute("SELECT payload FROM events WHERE id = 3").fetchone()["payload"]
    conn.execute("UPDATE events SET payload = ? WHERE id = 2", (p3,))
    conn.execute("UPDATE events SET payload = ? WHERE id = 3", (p2,))
    conn.commit()
    conn.close()

    result = maintenance.verify(src)

    assert result["ok"] is False
    chain = next(c for c in result["checks"] if c["name"] == "event_chain")
    assert chain["ok"] is False


def test_verify_skips_pre_chain_rows(tmp_path):
    # Rows that predate the migration carry NULL event_hash and must NOT
    # false-positive: the chain starts at the migration boundary.
    src = tmp_path / "db.sqlite"
    conn = _open(src)
    _apply_schema(conn)
    # Simulate legacy rows: inserted as if before 005, no hash columns set.
    conn.execute("INSERT INTO events (type, payload, actor) VALUES ('system', '{}', 'agent')")
    conn.execute("INSERT INTO events (type, payload, actor) VALUES ('system', '{}', 'agent')")
    # Then chained rows arrive via the normal append path.
    event_log.append(conn, "system", {"i": 0}, actor="agent")
    event_log.append(conn, "system", {"i": 1}, actor="agent")
    conn.commit()
    conn.close()

    result = maintenance.verify(src)

    assert result["ok"] is True, result
    chain = next(c for c in result["checks"] if c["name"] == "event_chain")
    assert chain["ok"] is True
    assert result["chain_checked"] == 2  # only the two chained rows


def test_verify_chain_skipped_when_column_absent(tmp_path):
    # A pre-005 DB has no event_hash column; the chain check is skipped cleanly.
    src = tmp_path / "db.sqlite"
    conn = _open(src)
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql", "003_grounding.sql"):
        conn.executescript((SCHEMA / fn).read_text())
    conn.execute("INSERT INTO events (type, payload, actor) VALUES ('system', '{}', 'agent')")
    conn.commit()
    conn.close()

    result = maintenance.verify(src)

    chain = next(c for c in result["checks"] if c["name"] == "event_chain")
    assert chain["ok"] is True
    assert "absent" in chain["detail"].lower() or "skipped" in chain["detail"].lower()


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #
def test_restore_round_trip(tmp_path):
    live = tmp_path / "db.sqlite"
    hashes = _make_db(live)

    # Take a good snapshot first.
    snap = tmp_path / "good-snap.sqlite"
    maintenance.snapshot(live, snap)

    # Now corrupt the live DB (hash mismatch).
    conn = _open(live)
    conn.execute(
        "UPDATE content SET body = 'tampered' WHERE hash = ?", (hashes["h1"],)
    )
    conn.commit()
    conn.close()
    assert maintenance.verify(live)["ok"] is False

    result = maintenance.restore(snap, live)

    assert result["restored_from"] == snap
    assert result["db_path"] == live
    assert result["previous_backup"] is not None
    assert Path(result["previous_backup"]).exists()
    # The live DB verifies clean again.
    assert maintenance.verify(live)["ok"] is True


def test_restore_refuses_corrupt_snapshot(tmp_path):
    live = tmp_path / "db.sqlite"
    _make_db(live)

    # Build a snapshot that fails verify (hash mismatch baked in).
    bad_snap = tmp_path / "bad-snap.sqlite"
    conn = _open(bad_snap)
    _apply_schema(conn)
    h = content_hash("real body")
    conn.execute(
        "INSERT INTO content (hash, body) VALUES (?, ?)", (h, "DIFFERENT body")
    )
    conn.commit()
    conn.close()
    assert maintenance.verify(bad_snap)["ok"] is False

    # Capture live bytes; restore must NOT touch them.
    before = live.read_bytes()
    with pytest.raises(Exception):
        maintenance.restore(bad_snap, live)
    assert live.read_bytes() == before


def test_restore_uses_backup_dir(tmp_path):
    live = tmp_path / "db.sqlite"
    _make_db(live)
    snap = tmp_path / "snap.sqlite"
    maintenance.snapshot(live, snap)

    backup_dir = tmp_path / "backups"
    result = maintenance.restore(snap, live, backup_dir=backup_dir)

    assert result["previous_backup"] is not None
    assert Path(result["previous_backup"]).parent == backup_dir
    assert Path(result["previous_backup"]).exists()


def test_restore_pre_restore_backup_names_are_collision_proof(tmp_path, monkeypatch):
    live = tmp_path / "db.sqlite"
    _make_db(live)
    snap = tmp_path / "snap.sqlite"
    maintenance.snapshot(live, snap)

    # Force both restores to share the exact same timestamp component.
    monkeypatch.setattr(
        maintenance,
        "utcnow_iso",
        lambda precision="ms": "2026-06-14T12:34:56.789Z",
    )

    first = maintenance.restore(snap, live)
    second = maintenance.restore(snap, live)

    first_backup = Path(first["previous_backup"])
    second_backup = Path(second["previous_backup"])
    assert first_backup.exists()
    assert second_backup.exists()
    assert first_backup != second_backup


def test_restore_refuses_when_live_db_is_in_use(tmp_path):
    live = tmp_path / "db.sqlite"
    _make_db(live)
    snap = tmp_path / "snap.sqlite"
    maintenance.snapshot(live, snap)

    blocker = _open(live)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(maintenance.RestoreError, match="appears in use"):
            maintenance.restore(snap, live)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_restore_proceeds_when_live_db_not_in_use(tmp_path):
    live = tmp_path / "db.sqlite"
    _make_db(live)
    snap = tmp_path / "snap.sqlite"
    maintenance.snapshot(live, snap)

    result = maintenance.restore(snap, live)

    assert result["restored_from"] == snap
    assert result["previous_backup"] is not None
    assert Path(result["previous_backup"]).exists()
    assert maintenance.verify(live)["ok"] is True


def test_restore_atomically_swaps_in_snapshot(tmp_path, monkeypatch):
    # A successful restore swaps the snapshot in via an atomic os.replace rename,
    # NOT an in-place overwrite. We prove the rename path is actually taken (spy
    # os.replace) and that the live file's inode changes (an in-place copy2 would
    # keep the same inode), in addition to bytes matching and no sidecars left.
    live = tmp_path / "db.sqlite"
    hashes = _make_db(live)
    snap = tmp_path / "snap.sqlite"
    maintenance.snapshot(live, snap)

    # Diverge the live DB so it is byte-different from the snapshot.
    conn = _open(live)
    conn.execute("UPDATE content SET body = 'tampered' WHERE hash = ?", (hashes["h1"],))
    conn.commit()
    conn.close()
    assert live.read_bytes() != snap.read_bytes()

    inode_before = live.stat().st_ino

    replace_calls: list[tuple[Path, Path]] = []
    real_replace = maintenance.os.replace

    def spy_replace(src, dst):
        replace_calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(maintenance.os, "replace", spy_replace)

    result = maintenance.restore(snap, live)

    assert result["restored_from"] == snap
    # The swap went through os.replace exactly once, renaming a temp staging file
    # (beside the live DB) onto db_path — not overwriting db_path in place.
    assert len(replace_calls) == 1
    staged_src, replace_dst = replace_calls[0]
    assert replace_dst == live
    assert staged_src.parent == live.parent
    assert staged_src.name.startswith("db.sqlite.restore-")
    # Atomic rename => the live path now points at a NEW inode.
    assert live.stat().st_ino != inode_before
    # Atomic swap: live is now byte-for-byte the snapshot.
    assert live.read_bytes() == snap.read_bytes()
    assert not live.with_name(live.name + "-wal").exists()
    assert not live.with_name(live.name + "-shm").exists()
    assert maintenance.verify(live)["ok"] is True


@pytest.mark.parametrize(
    "fault",
    ["copy_to_temp", "os_replace", "lock_acquire"],
    ids=["copy-to-temp-fails", "os-replace-fails", "lock-acquire-fails"],
)
def test_restore_failure_leaves_live_db_intact(tmp_path, monkeypatch, fault):
    # Whatever stage of the restore blows up — staging the snapshot copy, the
    # atomic rename, or acquiring the exclusive lock / WAL checkpoint — the live
    # DB must remain the ORIGINAL intact database (never truncated/partial) and
    # stay verify()-clean, with no temp staging file left behind.
    live = tmp_path / "db.sqlite"
    _make_db(live)
    snap = tmp_path / "snap.sqlite"
    maintenance.snapshot(live, snap)

    before = live.read_bytes()

    if fault == "os_replace":

        def _boom_replace(src, dst):
            raise OSError("simulated disk-full during rename")

        monkeypatch.setattr(maintenance.os, "replace", _boom_replace)
        expected = (OSError, "simulated disk-full")
    elif fault == "copy_to_temp":
        real_copy2 = maintenance.shutil.copy2

        def _boom_copy2(src, dst, *args, **kwargs):
            # Fail only the snapshot->temp staging copy; let the pre-restore
            # backup copy succeed so we exercise the staging failure path.
            if Path(src) == snap:
                raise OSError("simulated disk-full during temp staging")
            return real_copy2(src, dst, *args, **kwargs)

        monkeypatch.setattr(maintenance.shutil, "copy2", _boom_copy2)
        expected = (OSError, "simulated disk-full")
    else:  # lock_acquire

        def _boom_lock(db_path):
            raise maintenance.RestoreError("simulated lock/checkpoint failure")

        monkeypatch.setattr(maintenance, "_acquire_exclusive_lock", _boom_lock)
        expected = (maintenance.RestoreError, "simulated lock/checkpoint failure")

    with pytest.raises(expected[0], match=expected[1]):
        maintenance.restore(snap, live)

    # Original DB is byte-for-byte intact (not truncated/empty) and still valid.
    assert live.read_bytes() == before
    assert maintenance.verify(live)["ok"] is True
    # No leftover staging temp file in the DB directory.
    leftovers = list(tmp_path.glob("db.sqlite.restore-*"))
    assert leftovers == []


def test_restore_handles_stale_wal_sidecars(tmp_path):
    # Simulate a crash that left a stale -wal/-shm pair next to the main file:
    # data committed only into the WAL (a "marker") was never checkpointed. The
    # restored snapshot must be authoritative — the stale WAL must NOT leak its
    # marker into the result, and the sidecars must be gone afterwards.
    snap_source = tmp_path / "source.sqlite"
    _make_db(snap_source)
    snap = tmp_path / "snap.sqlite"
    maintenance.snapshot(snap_source, snap)

    # Build a crash state: a WAL-mode DB whose latest write lives only in -wal.
    crash = tmp_path / "db.sqlite"
    crash_wal = crash.with_name(crash.name + "-wal")
    crash_shm = crash.with_name(crash.name + "-shm")
    src_conn = _open(snap_source)
    src_conn.execute("PRAGMA journal_mode=WAL")
    src_conn.execute("PRAGMA wal_autocheckpoint=0")
    marker = content_hash("stale-wal-only marker body")
    src_conn.execute(
        "INSERT INTO content (hash, body, title, tombstoned) VALUES (?, ?, ?, 0)",
        (marker, "stale-wal-only marker body", "t"),
    )
    src_conn.commit()  # committed into the WAL, NOT checkpointed into the main file
    # Copy main + live sidecars out while still open => a realistic crash image.
    shutil.copy2(snap_source, crash)
    shutil.copy2(snap_source.with_name(snap_source.name + "-wal"), crash_wal)
    shutil.copy2(snap_source.with_name(snap_source.name + "-shm"), crash_shm)
    src_conn.close()

    assert crash_wal.exists() and crash_shm.exists()

    result = maintenance.restore(snap, crash)

    assert result["restored_from"] == snap
    # Restored content is authoritative: byte-identical to the snapshot, with the
    # stale sidecars removed and the WAL-only marker absent.
    assert crash.read_bytes() == snap.read_bytes()
    assert not crash_wal.exists()
    assert not crash_shm.exists()
    conn = _open(crash)
    row = conn.execute(
        "SELECT COUNT(*) FROM content WHERE hash = ?", (marker,)
    ).fetchone()
    conn.close()
    assert row[0] == 0
    assert maintenance.verify(crash)["ok"] is True


def test_restore_lock_close_does_not_recreate_sidecars(tmp_path):
    # restore holds an exclusive lock connection across the swap and unlinks the
    # -wal/-shm sidecars BEFORE closing that connection (the unlink is inside the
    # held lock; the close happens in the finally afterward). The lock connection
    # opened the live DB in WAL mode, so a naive close could re-spawn -wal/-shm by
    # name. This proves it does not: after a successful restore the sidecars are
    # absent and STAY absent once the lock connection is closed.
    live = tmp_path / "db.sqlite"
    _make_db(live)
    snap = tmp_path / "snap.sqlite"
    maintenance.snapshot(live, snap)

    wal = live.with_name(live.name + "-wal")
    shm = live.with_name(live.name + "-shm")

    # Force the live DB into WAL mode with sidecars present on disk but NO active
    # connection (so restore's not-in-use check passes): write in WAL mode, copy
    # the live sidecars out while open, close (which removes them), then restore
    # the sidecar files. The restore's lock connection then opens a genuine
    # WAL-mode database.
    c = _open(live)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("INSERT INTO events (type, payload, actor) VALUES ('x', '{}', 'a')")
    c.commit()
    shutil.copy2(wal, tmp_path / "wal.bak")
    shutil.copy2(shm, tmp_path / "shm.bak")
    c.close()
    shutil.copy2(tmp_path / "wal.bak", wal)
    shutil.copy2(tmp_path / "shm.bak", shm)
    assert wal.exists() and shm.exists()

    result = maintenance.restore(snap, live)

    assert result["restored_from"] == snap
    # Closing the (now-replaced) WAL-mode lock connection did not recreate them.
    assert not wal.exists()
    assert not shm.exists()
    assert maintenance.verify(live)["ok"] is True


# --------------------------------------------------------------------------- #
# reembed (#43)
# --------------------------------------------------------------------------- #
def _fake_embedder(dim: int):
    """Deterministic, torch-free embedder: maps each body to a `dim`-wide vector.

    Returns sqlite-vec float32 bytes so widths match the real embed pipeline.
    """
    def embed_many(texts):
        out = []
        for t in texts:
            seed = float(len(t) % 7) + 0.1
            out.append(sqlite_vec.serialize_float32([seed] * dim))
        return out

    return embed_many


def test_reembed_changes_table_dim_and_round_trips(tmp_path):
    src = tmp_path / "db.sqlite"
    hashes = _make_db(src)  # 4-dim table, 1 embedding, 2 live + 1 tombstoned content

    conn = _open(src)
    report = maintenance.reembed(conn, _fake_embedder(8), 8)
    conn.commit()

    assert report["previous_dim"] == 4
    assert report["new_dim"] == 8
    assert report["content_total"] == 3
    assert report["embedded"] == 2  # only the 2 non-tombstoned rows
    assert report["skipped_tombstoned"] == 1

    # The vec0 table is now 8-dim and round-trips a MATCH query.
    tbl_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'embeddings'"
    ).fetchone()[0]
    assert "FLOAT[8]" in tbl_sql
    n_emb = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert n_emb == 2
    probe = sqlite_vec.serialize_float32([0.1] * 8)
    nearest = conn.execute(
        "SELECT content_hash FROM embeddings WHERE embedding MATCH ? "
        "ORDER BY distance LIMIT 1",
        (probe,),
    ).fetchone()
    assert nearest["content_hash"] in (hashes["h1"], hashes["h2"])
    conn.close()


def test_reembed_only_embeds_live_content(tmp_path):
    src = tmp_path / "db.sqlite"
    hashes = _make_db(src)
    conn = _open(src)
    maintenance.reembed(conn, _fake_embedder(8), 8)
    conn.commit()
    present = {
        r["content_hash"]
        for r in conn.execute("SELECT content_hash FROM embeddings")
    }
    conn.close()
    assert hashes["h3"] not in present  # tombstoned row never gets a vector
    assert present == {hashes["h1"], hashes["h2"]}


def test_reembed_aborts_on_wrong_width(tmp_path):
    src = tmp_path / "db.sqlite"
    _make_db(src)
    conn = _open(src)
    # Embedder produces 6-dim vectors but we ask for an 8-dim table.
    with pytest.raises(maintenance.ReembedError, match="6-dim"):
        maintenance.reembed(conn, _fake_embedder(6), 8)
    conn.close()


def test_reembed_into_empty_corpus(tmp_path):
    src = tmp_path / "db.sqlite"
    conn = _open(src)
    _apply_schema(conn, embed_dim=4)  # schema, no content rows
    conn.commit()
    report = maintenance.reembed(conn, _fake_embedder(16), 16)
    conn.commit()
    assert report["embedded"] == 0
    assert report["content_total"] == 0
    tbl_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'embeddings'"
    ).fetchone()[0]
    assert "FLOAT[16]" in tbl_sql
    conn.close()


def _capturing_embedder(dim: int):
    """A fake embedder that records every string it was asked to embed."""
    seen: list[str] = []

    def embed_many(texts):
        seen.extend(texts)
        return [sqlite_vec.serialize_float32([0.1] * dim) for _ in texts]

    return embed_many, seen


def test_reembed_truncates_body_to_match_live_ingest(tmp_path):
    """A body longer than the cap must be embedded over the SAME truncated prefix
    by both reembed and the live ingest handler — otherwise a reembed produces
    vectors that re-ingesting would never reproduce (#74 review)."""
    from engram.rag import chunk as chunker

    size_tokens = 512
    cap = chunker.embed_char_cap(size_tokens)

    src = tmp_path / "db.sqlite"
    conn = _open(src)
    _apply_schema(conn, embed_dim=4)
    long_body = "A" * (cap + 9000)  # comfortably longer than the truncation cap
    h = _insert_content(conn, long_body)
    conn.commit()

    embed_many, seen = _capturing_embedder(8)
    maintenance.reembed(conn, embed_many, 8, embed_char_cap=cap)
    conn.commit()
    conn.close()

    # reembed embedded the truncated prefix, not the full body.
    assert len(seen) == 1
    assert seen[0] == long_body[:cap]
    # And that prefix is exactly what the live ingest path embeds.
    assert seen[0] == chunker.embed_prefix(long_body, size_tokens)
    assert h  # body was actually stored


def test_reembed_wrong_width_leaves_old_index_intact(tmp_path):
    """A mid-reembed abort (wrong embedder width) must ROLLBACK, leaving the
    original embeddings table and its rows untouched (#74 review: crash-safety)."""
    src = tmp_path / "db.sqlite"
    hashes = _make_db(src)  # 4-dim table with 1 embedding for h1

    conn = _open(src)
    before_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'embeddings'"
    ).fetchone()[0]
    before_rows = {
        r["content_hash"] for r in conn.execute("SELECT content_hash FROM embeddings")
    }
    assert "FLOAT[4]" in before_sql
    assert before_rows == {hashes["h1"]}

    # Embedder produces 6-dim vectors but we ask for an 8-dim table → aborts.
    with pytest.raises(maintenance.ReembedError, match="6-dim"):
        maintenance.reembed(conn, _fake_embedder(6), 8)

    # The original 4-dim table and its row survived the rollback.
    after_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'embeddings'"
    ).fetchone()[0]
    after_rows = {
        r["content_hash"] for r in conn.execute("SELECT content_hash FROM embeddings")
    }
    conn.close()
    assert after_sql == before_sql
    assert "FLOAT[4]" in after_sql
    assert after_rows == before_rows


def test_reembed_computes_embeddings_outside_write_transaction(tmp_path):
    """The CPU-bound embed_many must run with NO write lock held (#161).

    Holding the write lock across the (possibly minutes-long) embedding compute
    would block every other writer for the whole run. We assert it directly: the
    injected embedder records, at call time, whether a write transaction is open
    and whether the old `embeddings` table is still its original 4-dim shape. The
    apply transaction (drop + recreate + insert) must not have started yet.
    """
    src = tmp_path / "db.sqlite"
    _make_db(src)  # 4-dim embeddings table
    conn = _open(src)

    observations: list[dict] = []

    def embed_many(texts):
        tbl_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'embeddings'"
        ).fetchone()[0]
        observations.append(
            {"in_transaction": conn.in_transaction, "table_sql": tbl_sql}
        )
        return [sqlite_vec.serialize_float32([0.1] * 8) for _ in texts]

    report = maintenance.reembed(conn, embed_many, 8)
    conn.commit()

    # embed_many ran, and every call happened before the apply transaction:
    # no write transaction open, old 4-dim table still in place.
    assert observations, "embed_many was never called"
    for obs in observations:
        assert obs["in_transaction"] is False
        assert "FLOAT[4]" in obs["table_sql"]

    # And the rebuild still happened: table is now 8-dim with every live row.
    after_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'embeddings'"
    ).fetchone()[0]
    assert "FLOAT[8]" in after_sql
    n_emb = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    n_live = conn.execute(
        "SELECT COUNT(*) FROM content WHERE tombstoned = 0"
    ).fetchone()[0]
    conn.close()
    assert n_emb == n_live == report["embedded"] == 2


def test_reembed_apply_lock_is_bounded_by_busy_timeout(tmp_path):
    """A contended apply must raise (bounded busy_timeout), never hang (#161).

    A second connection holds the write lock; reembed's brief apply transaction
    cannot acquire it and, with a short lock_timeout_ms, raises 'database is
    locked' quickly instead of blocking. The competing connection is released in
    finally so the connections never deadlock on each other.
    """
    src = tmp_path / "db.sqlite"
    _make_db(src)
    conn = _open(src)
    blocker = _open(src)
    lock_timeout_ms = 100
    try:
        blocker.execute("BEGIN IMMEDIATE")  # holds the write lock
        blocker.execute("INSERT INTO events (type, payload, actor) VALUES ('x', '{}', 'a')")
        start = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            maintenance.reembed(conn, _fake_embedder(8), 8, lock_timeout_ms=lock_timeout_ms)
        elapsed = time.perf_counter() - start
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        conn.close()

    # Upper bound: the contended apply must give up close to lock_timeout_ms, not
    # block on some longer default (sqlite's default busy handling can wait far
    # longer). A generous ceiling keeps this deterministic on slow CI while still
    # proving the wait is bounded by the provided lock_timeout_ms (0.1s here).
    assert elapsed < 0.8, f"contended apply waited {elapsed:.3f}s, expected ~{lock_timeout_ms}ms"
