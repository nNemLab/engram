"""Vault watcher: detect manual edits in Obsidian → emit vault_edit events.

Strategy:
  - Watch the vault tree with watchdog (cross-platform inotify).
  - On a debounced modify, look up vault_state.rendered_body for the path.
  - If on-disk body differs, emit vault_edit(path, hash, diff) and update the
    content row's body + the vault_state's rendered_body so subsequent renders
    don't clobber the human's edit.
  - This makes Obsidian edits authoritative for the affected entries.
"""
from __future__ import annotations

import fnmatch
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .. import log as event_log
from ..common.config import load_config
from ..common.db import get_connection
from .differ import unified_diff

logger = logging.getLogger("engram.watcher")


class _Debouncer:
    def __init__(self, delay_ms: int, fn) -> None:
        self.delay = delay_ms / 1000.0
        self.fn = fn
        self.timers: dict[str, threading.Timer] = {}
        self.lock = threading.Lock()

    def trigger(self, key: str, *args) -> None:
        with self.lock:
            old = self.timers.pop(key, None)
            if old:
                old.cancel()
            t = threading.Timer(self.delay, self.fn, args=args)
            t.daemon = True
            self.timers[key] = t
            t.start()


def _ignored(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


class _Handler(FileSystemEventHandler):
    def __init__(self, vault: Path, debouncer: _Debouncer, ignore: list[str]) -> None:
        self.vault = vault
        self.debouncer = debouncer
        self.ignore = ignore

    def _maybe(self, path: str) -> None:
        p = Path(path)
        if not p.is_file() or p.suffix != ".md":
            return
        try:
            rel = str(p.relative_to(self.vault))
        except ValueError:
            return
        if _ignored(rel, self.ignore):
            return
        self.debouncer.trigger(rel, rel, str(p))

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory:
            self._maybe(event.src_path)

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._maybe(event.src_path)


def _on_change(conn: sqlite3.Connection, rel: str, abs_path: str) -> None:
    p = Path(abs_path)
    if not p.exists():
        return
    new_body = p.read_text()
    row = conn.execute(
        "SELECT content_hash, rendered_body FROM vault_state WHERE vault_path = ?",
        (rel,),
    ).fetchone()
    if not row:
        # New file dropped into vault by hand. Treat as an inbox ingest.
        from .. import dedup
        result = dedup.gate(
            conn, body=new_body, title=p.stem, source_tier="manual",
            confidence=0.7, kind="kb", actor="human",
        )
        logger.info("inbox ingest from %s -> %s (%s)", rel, result.outcome, result.hash)
        return
    if new_body == row["rendered_body"]:
        return  # no real change (touch / Obsidian metadata write)
    diff = unified_diff(row["rendered_body"], new_body, rel)
    h = row["content_hash"]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE content SET body = ?, updated_at = ? WHERE hash = ?", (new_body, now, h))
    conn.execute(
        "UPDATE vault_state SET rendered_body = ?, rendered_at = ? WHERE vault_path = ?",
        (new_body, now, rel),
    )
    event_log.append(
        conn, "vault_edit",
        {"path": rel, "hash": h, "diff": diff[:8000]},
        actor="human",
    )
    logger.info("vault_edit recorded: %s (hash=%s)", rel, h)


def run() -> None:
    cfg = load_config()
    vault = cfg.paths.vault
    vault.mkdir(parents=True, exist_ok=True)
    conn = get_connection()

    debouncer = _Debouncer(cfg.watcher.debounce_ms, lambda rel, abs_p: _on_change(conn, rel, abs_p))
    handler = _Handler(vault, debouncer, cfg.watcher.ignore)
    observer = Observer()
    observer.schedule(handler, str(vault), recursive=True)
    observer.start()
    logger.info("watcher started; vault=%s", vault)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
