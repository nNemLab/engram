"""Vault watcher: detect manual edits in Obsidian → emit vault_edit events.

Strategy:
  - Watch the vault tree with watchdog (cross-platform inotify).
  - On a debounced modify, look up vault_state.rendered_body for the path.
  - If on-disk body differs, record the edit as a first-class new content
    revision: insert a new current+protected row addressed by
    content_hash(new_body), supersede the prior revision, repoint vault_state,
    and emit vault_edit(path, hash_old, hash_new, diff). Rehashing keeps the
    content store content-addressed (#55) instead of mutating body in place.
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
from ..common.db import db_lock, get_connection, transaction
from .differ import unified_diff

logger = logging.getLogger("engram.watcher")


class _Debouncer:
    def __init__(self, delay_ms: int, fn) -> None:
        self.delay = delay_ms / 1000.0
        self.fn = fn
        self.timers: dict[str, threading.Timer] = {}
        self._gen: dict[str, int] = {}
        self.lock = threading.Lock()

    def trigger(self, key: str, *args) -> None:
        with self.lock:
            old = self.timers.pop(key, None)
            if old:
                old.cancel()
            gen = self._gen.get(key, 0) + 1
            self._gen[key] = gen
            t = threading.Timer(self.delay, self._fire, args=(key, gen, args))
            t.daemon = True
            self.timers[key] = t
            t.start()

    def _fire(self, key: str, gen: int, args: tuple) -> None:
        try:
            self.fn(*args)
        finally:
            # Drop the fired timer so the map can't grow one dead Timer per
            # edited path (#92). Skip if a newer trigger() superseded this
            # generation while the callback was running.
            with self.lock:
                if self._gen.get(key) == gen:
                    self.timers.pop(key, None)
                    self._gen.pop(key, None)

    def cancel_all(self) -> None:
        """Cancel outstanding timers (shutdown path, #92)."""
        with self.lock:
            for t in self.timers.values():
                t.cancel()
            self.timers.clear()
            self._gen.clear()


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
    # Serialize the whole change-handling op (#83): the watcher fires this from
    # `threading.Timer` daemon threads (one per debounced path), all driving the
    # single shared connection. The lock guarantees the read-then-write revision
    # sequence below never interleaves with another thread's use of the
    # connection; the inner `transaction` calls re-enter the same RLock.
    with db_lock():
        p = Path(abs_path)
        if not p.exists():
            return
        new_body = p.read_text(encoding="utf-8")
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
        from ..dedup import content_hash

        diff = unified_diff(row["rendered_body"], new_body, rel)
        old_hash = row["content_hash"]
        new_hash = content_hash(new_body)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        if new_hash == old_hash:
            # Edit normalizes to the same content (whitespace/case only). The row
            # stays content-addressed; just refresh the stored bytes and protect it.
            with transaction(conn):
                conn.execute(
                    "UPDATE content SET body = ?, updated_at = ?, protected = 1 WHERE hash = ?",
                    (new_body, now, old_hash),
                )
                conn.execute(
                    "UPDATE vault_state SET rendered_body = ?, rendered_at = ? WHERE vault_path = ?",
                    (new_body, now, rel),
                )
                event_log.append(
                    conn, "vault_edit",
                    {"path": rel, "hash": new_hash, "hash_old": old_hash,
                     "hash_new": new_hash, "diff": diff[:8000]},
                    actor="human",
                )
            logger.info("vault_edit recorded (normalized no-op): %s (hash=%s)", rel, new_hash)
            return

        # A human edit is a first-class new revision, not an in-place body mutation
        # (#55 — mutating body while keeping the hash breaks content-addressing).
        # Insert a new current+protected revision addressed by content_hash(new_body),
        # carry the source metadata forward, supersede the old revision, and repoint
        # the vault_state projection. protected=1 keeps the poller from superseding
        # the human's edit (#37).
        old = conn.execute("SELECT * FROM content WHERE hash = ?", (old_hash,)).fetchone()
        if old is None:
            logger.warning("vault_state %s references missing content %s; skipping", rel, old_hash)
            return
        # Atomic (#83): the insert + repoint + supersede + projection refresh is a
        # 4-statement revision swap. A failure partway through would otherwise
        # leave two current rows (or a vault_state pointing at a missing row);
        # ROLLBACK leaves the prior revision intact.
        with transaction(conn):
            conn.execute(
                """INSERT OR IGNORE INTO content
                   (hash, body, title, source_url, source_tier, fetched_at, confidence,
                    ttl_days, kind, revision, is_current, protected, vault_path, source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)""",
                (new_hash, new_body, old["title"], old["source_url"], old["source_tier"],
                 old["fetched_at"], old["confidence"], old["ttl_days"], old["kind"],
                 old["revision"] + 1, rel, old["source_id"]),
            )
            # Idempotent regardless of whether the row was freshly inserted or already
            # existed (human edited a file to match other existing content).
            conn.execute(
                "UPDATE content SET is_current = 1, protected = 1, vault_path = ?, updated_at = ? "
                "WHERE hash = ?",
                (rel, now, new_hash),
            )
            conn.execute(
                "UPDATE content SET is_current = 0, superseded_by = ?, vault_path = NULL, updated_at = ? "
                "WHERE hash = ?",
                (new_hash, now, old_hash),
            )
            conn.execute(
                "UPDATE vault_state SET content_hash = ?, rendered_body = ?, rendered_at = ? "
                "WHERE vault_path = ?",
                (new_hash, new_body, now, rel),
            )
            event_log.append(
                conn, "vault_edit",
                {"path": rel, "hash": new_hash, "hash_old": old_hash,
                 "hash_new": new_hash, "diff": diff[:8000]},
                actor="human",
            )
        logger.info("vault_edit recorded: %s (rev %d->%d, hash %s->%s)",
                    rel, old["revision"], old["revision"] + 1, old_hash, new_hash)


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
        pass
    finally:
        # Graceful shutdown (#92): cancel any pending debounced timers, stop the
        # observer, and close the long-lived DB connection so the daemon doesn't
        # leak threads / file descriptors / WAL sidecars.
        debouncer.cancel_all()
        observer.stop()
        observer.join()
        conn.close()
