"""Vault projector: tail event log → write markdown into Obsidian vault.

Runs as a daemon. Writes vault_state on every render so the watcher can detect
manual edits as diffs against the last-rendered version.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from .. import log as event_log
from ..common.config import load_config
from ..common.db import get_connection
from .renderers import RENDERERS

logger = logging.getLogger("engram.projector")

CURSOR_KEY = "projector"


def _atomic_write(path: Path, body: str) -> None:
    """Write `body` to `path` atomically via a temp file + `os.replace`.

    The rename is atomic on POSIX, so a watcher in another process observing the
    vault tree never sees a partially-written file — it sees either the old bytes
    or the complete new bytes, never a torn intermediate (#96).
    """
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(body)
    os.replace(tmp, path)


def _read_cursor(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daemon_cursors (name TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT last_event_id FROM daemon_cursors WHERE name = ?", (CURSOR_KEY,)).fetchone()
    return int(row["last_event_id"]) if row else 0


def _write_cursor(conn: sqlite3.Connection, last_id: int) -> None:
    conn.execute(
        "INSERT INTO daemon_cursors (name, last_event_id) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET last_event_id = excluded.last_event_id",
        (CURSOR_KEY, last_id),
    )


def _project_one(conn: sqlite3.Connection, vault: Path, content_hash: str, kind_dirs: dict[str, str]) -> None:
    row = conn.execute(
        "SELECT * FROM content WHERE hash = ? AND tombstoned = 0",
        (content_hash,),
    ).fetchone()
    if not row:
        return
    renderer = RENDERERS.get(row["kind"], RENDERERS["kb"])
    kind_dir = kind_dirs.get(row["kind"], kind_dirs.get("kb", "050-kb"))
    rel_path, body = renderer(row, kind_dir)
    abs_path = vault / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Commit the rendered_body update BEFORE writing the vault file (#96). The
    # watcher runs in a SEPARATE process and fires on the file change; its
    # feedback-loop guard compares the on-disk body against
    # vault_state.rendered_body. If the file landed first, a watcher read in the
    # window before this row was durable would see a STALE rendered_body,
    # mismatch, and fabricate a spurious actor="human" vault_edit. Making the row
    # committed/visible cross-process first closes that window; the in-process
    # RLock (#83/#112) cannot serialize across processes.
    conn.execute(
        "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(vault_path) DO UPDATE SET content_hash=excluded.content_hash, "
        "rendered_body=excluded.rendered_body, rendered_at=excluded.rendered_at",
        (rel_path, content_hash, body, now),
    )
    conn.execute("UPDATE content SET vault_path = ? WHERE hash = ?", (rel_path, content_hash))
    conn.commit()
    _atomic_write(abs_path, body)


def _handle_event(conn: sqlite3.Connection, vault: Path, evt: event_log.Event,
                  kind_dirs: dict[str, str]) -> None:
    if evt.type == "ingested":
        _project_one(conn, vault, evt.payload["hash"], kind_dirs)
    elif evt.type == "merged":
        # Tombstone vault file for the merged-away hash; ensure the kept hash is rendered.
        tombstoned = evt.payload.get("hash_tombstoned")
        kept = evt.payload.get("hash_kept")
        if tombstoned:
            row = conn.execute(
                "SELECT vault_path FROM vault_state WHERE content_hash = ?", (tombstoned,)
            ).fetchone()
            if row and row["vault_path"]:
                p = vault / row["vault_path"]
                if p.exists():
                    p.unlink()
                conn.execute("DELETE FROM vault_state WHERE content_hash = ?", (tombstoned,))
        if kept:
            _project_one(conn, vault, kept, kind_dirs)
    elif evt.type == "superseded":
        hash_old = evt.payload.get("hash_old")
        hash_new = evt.payload.get("hash_new")
        old_state = conn.execute(
            "SELECT vault_path, rendered_body FROM vault_state WHERE content_hash = ?",
            (hash_old,),
        ).fetchone()
        if not (old_state and old_state["vault_path"]):
            # No prior vault file for hash_old (no vault_state) — project the
            # now-current new row fresh so it still lands in the vault.
            if hash_new:
                _project_one(conn, vault, hash_new, kind_dirs)
            return
        if old_state and old_state["vault_path"]:
            old_path = old_state["vault_path"]
            new_row = conn.execute(
                "SELECT * FROM content WHERE hash = ? AND tombstoned = 0",
                (hash_new,),
            ).fetchone()
            if new_row:
                renderer = RENDERERS.get(new_row["kind"], RENDERERS["kb"])
                kind_dir = kind_dirs.get(new_row["kind"], kind_dirs.get("kb", "050-kb"))
                _, body = renderer(new_row, kind_dir)
                abs_path = vault / old_path
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                # Same cross-process ordering as _project_one (#96): repoint and
                # commit vault_state to the NEW revision's body before the file
                # write, so the watcher never reads the OLD rendered_body against
                # the NEW on-disk bytes and misclassifies it as a human edit.
                conn.execute("DELETE FROM vault_state WHERE content_hash = ?", (hash_old,))
                conn.execute(
                    "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(vault_path) DO UPDATE SET content_hash=excluded.content_hash, "
                    "rendered_body=excluded.rendered_body, rendered_at=excluded.rendered_at",
                    (old_path, hash_new, body, now),
                )
                conn.execute("UPDATE content SET vault_path = ? WHERE hash = ?",
                             (old_path, hash_new))
                conn.commit()
                _atomic_write(abs_path, body)


def run() -> None:
    # Own the long-lived connection's lifecycle: close it on any loop exit
    # (KeyboardInterrupt/SIGINT or a fatal error) so the daemon doesn't leak the
    # connection + WAL sidecars on shutdown (#92). common/db.get_connection
    # documents that the caller must close.
    conn = get_connection()
    try:
        _run_loop(conn)
    finally:
        conn.close()


def _run_loop(conn: sqlite3.Connection) -> None:
    cfg = load_config()
    vault = cfg.paths.vault
    vault.mkdir(parents=True, exist_ok=True)

    cursor = _read_cursor(conn)
    logger.info("projector starting; vault=%s cursor=%d", vault, cursor)

    poll = cfg.projector.poll_interval
    while True:
        try:
            last_seen = cursor
            for evt in event_log.since(conn, cursor, types=["ingested", "merged", "superseded"],
                                       yield_poison=True):
                if evt.poison:
                    # Dead-letter an un-parseable payload and advance past it so
                    # one corrupt row can't freeze the loop and drop every later
                    # event (#84).
                    logger.warning(
                        "projector skipping poison event id=%d (unparseable payload)",
                        evt.id,
                    )
                    last_seen = evt.id
                    continue
                _handle_event(conn, vault, evt, cfg.projector.kind_dirs)
                last_seen = evt.id
            if last_seen != cursor:
                _write_cursor(conn, last_seen)
                cursor = last_seen
        except Exception:
            logger.exception("projector tick failed")
        time.sleep(poll)
