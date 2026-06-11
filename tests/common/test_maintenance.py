"""Tests for engram.maintenance — snapshot / verify / restore of the event log.

SAFETY: every test operates on temp DBs built in tmp_path. Nothing here touches
the real ~/.engram/db.sqlite. The maintenance module is path-explicit by design.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

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
