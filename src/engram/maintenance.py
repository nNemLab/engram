"""Backup / verify / restore for the canonical event log.

The append-only SQLite DB at the configured `paths.db` is the only piece of
engram state that must be backed up — the vault markdown, FTS index, vector
index, and all derived caches rebuild from it. This module makes that promise
real with three deterministic, network-free, path-explicit operations:

  * snapshot  — a consistent (WAL-safe) self-contained copy via VACUUM INTO
  * verify    — corruption / invariant detection over an existing DB
  * restore   — verified, backup-first swap of a snapshot over the live DB

Every function takes explicit paths (or an open connection). Nothing here reads
config or resolves the default DB location — that is the bin/ wrappers' job —
so tests and callers can never accidentally operate on the real ~/.engram DB.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import sqlite_vec

from .common.time import utcnow_iso
from .dedup import content_hash


def _open(db_path: Path) -> sqlite3.Connection:
    """Open an arbitrary DB file with sqlite-vec loaded (mirrors common.db._connect).

    Read-shaped: no schema apply, no WAL/journal mutation — we open exactly what
    is on disk so verify/snapshot observe the file as-is.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
def snapshot(db_path: Path | str, out_path: Path | str) -> dict[str, Any]:
    """Write a consistent, self-contained copy of `db_path` to `out_path`.

    Uses `VACUUM INTO`, which checkpoints WAL state and produces a single file
    containing every table — events, content, content_fts, embeddings (vec0),
    vault_state, daemon_cursors, etc. A plain file-copy would miss un-checkpointed
    WAL pages, so we never do that.
    """
    db_path = Path(db_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    conn = _open(db_path)
    try:
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        content_count = conn.execute("SELECT COUNT(*) FROM content").fetchone()[0]
        # VACUUM INTO refuses to overwrite an existing file.
        if out_path.exists():
            out_path.unlink()
        conn.execute("VACUUM INTO ?", (str(out_path),))
    finally:
        conn.close()

    return {
        "path": out_path,
        "size_bytes": out_path.stat().st_size,
        "event_count": int(event_count),
        "content_count": int(content_count),
    }


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _check(checks: list[dict[str, Any]], name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def verify(db_path_or_conn: Path | str | sqlite3.Connection) -> dict[str, Any]:
    """Corruption / invariant detection over a DB.

    Checks performed:
      * hash integrity — for every `content` row (including tombstoned ones,
        which retain body+hash), re-derive content_hash(body) and compare to the
        stored hash; collect any mismatches.
      * daemon_cursors.last_event_id <= MAX(events.id) for every cursor
        (skipped gracefully when the table is absent — daemons create it lazily).
      * every embeddings.content_hash exists in content.hash.
      * every non-null content.superseded_by references an existing content.hash.
      * PRAGMA integrity_check returns 'ok'.

    Returns {"ok", "checks":[{name,ok,detail}], "hash_mismatches":[...],
    "content_checked":N}. `ok` is True only if all checks pass AND there are no
    hash mismatches.
    """
    owns_conn = not isinstance(db_path_or_conn, sqlite3.Connection)
    conn = _open(Path(db_path_or_conn)) if owns_conn else db_path_or_conn

    checks: list[dict[str, Any]] = []
    hash_mismatches: list[str] = []
    content_checked = 0
    try:
        # --- hash integrity ---
        for row in conn.execute("SELECT hash, body FROM content"):
            content_checked += 1
            if content_hash(row["body"]) != row["hash"]:
                hash_mismatches.append(row["hash"])
        _check(
            checks,
            "hash_integrity",
            not hash_mismatches,
            "all content bodies hash to their stored hash"
            if not hash_mismatches
            else f"{len(hash_mismatches)} body/hash mismatch(es)",
        )

        # --- daemon_cursors.last_event_id <= MAX(events.id) ---
        if _table_exists(conn, "daemon_cursors"):
            max_event = conn.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
            bad = [
                dict(r)
                for r in conn.execute(
                    "SELECT name, last_event_id FROM daemon_cursors "
                    "WHERE last_event_id > ?",
                    (max_event,),
                )
            ]
            _check(
                checks,
                "daemon_cursors",
                not bad,
                f"all cursors <= MAX(events.id)={max_event}"
                if not bad
                else f"cursor(s) ahead of MAX(events.id)={max_event}: {bad}",
            )
        else:
            _check(
                checks,
                "daemon_cursors",
                True,
                "daemon_cursors table absent (created lazily by daemons) — skipped",
            )

        # --- every embeddings.content_hash exists in content.hash ---
        if _table_exists(conn, "embeddings"):
            dangling_emb = [
                r["content_hash"]
                for r in conn.execute(
                    "SELECT e.content_hash FROM embeddings e "
                    "LEFT JOIN content c ON c.hash = e.content_hash "
                    "WHERE c.hash IS NULL"
                )
            ]
            _check(
                checks,
                "embeddings_ref",
                not dangling_emb,
                "all embeddings reference an existing content row"
                if not dangling_emb
                else f"{len(dangling_emb)} embedding(s) reference a missing content hash",
            )
        else:
            _check(checks, "embeddings_ref", True, "embeddings table absent — skipped")

        # --- every non-null content.superseded_by references content.hash ---
        dangling_sb = [
            r["hash"]
            for r in conn.execute(
                "SELECT c.hash FROM content c "
                "LEFT JOIN content p ON p.hash = c.superseded_by "
                "WHERE c.superseded_by IS NOT NULL AND p.hash IS NULL"
            )
        ]
        _check(
            checks,
            "superseded_by_ref",
            not dangling_sb,
            "all superseded_by references resolve"
            if not dangling_sb
            else f"{len(dangling_sb)} content row(s) point superseded_by at a missing hash",
        )

        # --- PRAGMA integrity_check ---
        integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
        _check(
            checks,
            "integrity_check",
            integ == "ok",
            "ok" if integ == "ok" else f"PRAGMA integrity_check: {integ}",
        )
    finally:
        if owns_conn:
            conn.close()

    ok = not hash_mismatches and all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "checks": checks,
        "hash_mismatches": hash_mismatches,
        "content_checked": content_checked,
    }


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #
class RestoreError(RuntimeError):
    """Raised when a restore cannot proceed safely (e.g. corrupt snapshot)."""


def restore(
    snapshot_path: Path | str,
    db_path: Path | str,
    *,
    backup_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Verified, backup-first swap of `snapshot_path` over `db_path`.

    1. Verify the incoming snapshot; if it fails integrity, RAISE (a corrupt
       snapshot is never swapped in) — the live DB is left untouched.
    2. Back up the *current* db_path (if present) to a timestamped sidecar
       before overwriting, into `backup_dir` if given, else alongside db_path.
    3. Copy the snapshot over db_path and remove any stale -wal/-shm sidecars so
       the restored file is authoritative.

    The vault markdown files on disk are NOT touched: the projector reconciles
    forward from `vault_state` in the restored DB. Full bare-machine vault
    regeneration is a separate concern and intentionally out of scope here.
    """
    snapshot_path = Path(snapshot_path)
    db_path = Path(db_path)

    if not snapshot_path.exists():
        raise RestoreError(f"snapshot not found: {snapshot_path}")

    report = verify(snapshot_path)
    if not report["ok"]:
        failed = [c["name"] for c in report["checks"] if not c["ok"]]
        raise RestoreError(
            f"refusing to restore: snapshot {snapshot_path} failed verification "
            f"(failed checks: {failed or 'hash_integrity'}; "
            f"{len(report['hash_mismatches'])} hash mismatch(es))"
        )

    previous_backup: Path | None = None
    if db_path.exists():
        ts = utcnow_iso(precision="s").replace(":", "")
        backup_name = f"{db_path.name}.pre-restore-{ts}"
        if backup_dir is not None:
            backup_dir = Path(backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)
            previous_backup = backup_dir / backup_name
        else:
            previous_backup = db_path.with_name(backup_name)
        shutil.copy2(db_path, previous_backup)

    # Swap: copy snapshot over db_path, then drop stale WAL/SHM sidecars.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snapshot_path, db_path)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    return {
        "restored_from": snapshot_path,
        "previous_backup": previous_backup,
        "db_path": db_path,
    }
