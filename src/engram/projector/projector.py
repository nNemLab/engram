"""Vault projector: tail event log → write markdown into Obsidian vault.

Runs as a daemon. Writes vault_state on every render so the watcher can detect
manual edits as diffs against the last-rendered version.
"""
from __future__ import annotations

import logging
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
    abs_path.write_text(body)

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(vault_path) DO UPDATE SET content_hash=excluded.content_hash, "
        "rendered_body=excluded.rendered_body, rendered_at=excluded.rendered_at",
        (rel_path, content_hash, body, now),
    )
    conn.execute("UPDATE content SET vault_path = ? WHERE hash = ?", (rel_path, content_hash))


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
                abs_path.write_text(body)
                now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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


def run() -> None:
    cfg = load_config()
    vault = cfg.paths.vault
    vault.mkdir(parents=True, exist_ok=True)
    conn = get_connection()

    cursor = _read_cursor(conn)
    logger.info("projector starting; vault=%s cursor=%d", vault, cursor)

    poll = cfg.projector.poll_interval
    while True:
        try:
            last_seen = cursor
            for evt in event_log.since(conn, cursor, types=["ingested", "merged", "superseded"]):
                _handle_event(conn, vault, evt, cfg.projector.kind_dirs)
                last_seen = evt.id
            if last_seen != cursor:
                _write_cursor(conn, last_seen)
                cursor = last_seen
        except Exception:
            logger.exception("projector tick failed")
        time.sleep(poll)
