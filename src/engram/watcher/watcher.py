"""Vault watcher: detect manual edits in Obsidian → emit vault_edit events.

Strategy:
  - Watch the vault tree with watchdog (cross-platform; inotify on Linux).
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

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
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
    def __init__(
        self,
        vault: Path,
        debouncer: _Debouncer,
        ignore: list[str],
        conn: sqlite3.Connection,
    ) -> None:
        self.vault = vault
        self.debouncer = debouncer
        self.ignore = ignore
        self.conn = conn

    def _rel_for(self, path: str) -> str | None:
        p = Path(path)
        if p.suffix != ".md":
            return None
        try:
            rel = str(p.relative_to(self.vault))
        except ValueError:
            return None
        if _ignored(rel, self.ignore):
            return None
        return rel

    def _maybe(self, path: str) -> None:
        p = Path(path)
        if not p.is_file():
            return
        rel = self._rel_for(path)
        if rel is None:
            return
        self.debouncer.trigger(rel, rel, str(p))

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory:
            self._maybe(event.src_path)

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._maybe(event.src_path)

    def on_deleted(self, event: FileDeletedEvent) -> None:
        if event.is_directory:
            return
        rel = self._rel_for(event.src_path)
        if rel is None:
            return
        _on_delete(self.conn, rel)

    def on_moved(self, event: FileMovedEvent) -> None:
        if event.is_directory:
            return
        src_rel = self._rel_for(event.src_path)
        dest_rel = self._rel_for(event.dest_path)

        if src_rel and dest_rel:
            _on_move(self.conn, src_rel, dest_rel)
            return
        if src_rel and not dest_rel:
            _on_delete(self.conn, src_rel)
            return
        if dest_rel and not src_rel:
            # A move from outside the watched markdown set into it behaves as
            # a new file appearing in the vault.
            self.debouncer.trigger(dest_rel, dest_rel, event.dest_path)


def _on_delete(conn: sqlite3.Connection, rel: str) -> None:
    with db_lock():
        with transaction(conn):
            conn.execute("DELETE FROM vault_state WHERE vault_path = ?", (rel,))
            conn.execute(
                "UPDATE content SET vault_path = NULL WHERE vault_path = ?",
                (rel,),
            )


def _on_move(conn: sqlite3.Connection, src_rel: str, dest_rel: str) -> None:
    with db_lock():
        with transaction(conn):
            row = conn.execute(
                "SELECT content_hash FROM vault_state WHERE vault_path = ?",
                (src_rel,),
            ).fetchone()
            if not row:
                return

            existing_dest = conn.execute(
                "SELECT content_hash FROM vault_state WHERE vault_path = ?",
                (dest_rel,),
            ).fetchone()
            if existing_dest and existing_dest["content_hash"] != row["content_hash"]:
                conn.execute("DELETE FROM vault_state WHERE vault_path = ?", (dest_rel,))
                conn.execute(
                    "UPDATE content SET vault_path = NULL WHERE hash = ?",
                    (existing_dest["content_hash"],),
                )

            conn.execute(
                "UPDATE vault_state SET vault_path = ? WHERE vault_path = ?",
                (dest_rel, src_rel),
            )
            conn.execute(
                "UPDATE content SET vault_path = ? WHERE hash = ?",
                (dest_rel, row["content_hash"]),
            )


def _reconcile_startup(conn: sqlite3.Connection, vault: Path, ignore: list[str]) -> None:
    current_paths: set[str] = set()
    for p in vault.rglob("*.md"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(vault))
        if _ignored(rel, ignore):
            continue
        current_paths.add(rel)

    with db_lock():
        known = {
            row["vault_path"]
            for row in conn.execute("SELECT vault_path FROM vault_state").fetchall()
            if not _ignored(row["vault_path"], ignore)
        }

    for rel in sorted(current_paths):
        try:
            _on_change(conn, rel, str(vault / rel))
        except Exception:
            logger.exception("startup reconcile: failed to process %s", rel)

    for rel in sorted(known - current_paths):
        try:
            _on_delete(conn, rel)
        except Exception:
            logger.exception("startup reconcile: failed to tombstone %s", rel)


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
            if not new_body.strip():
                logger.info("skip empty manual ingest candidate: %s", rel)
                return
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
        # Cross-source hash-reuse guard (mirrors dedup #153). If the edited bytes
        # already exist as a content row under a DIFFERENT source_url (or NULL-vs-
        # sourced), that row is NOT this file's revision -- demoting the old row and
        # promoting `WHERE hash = new_hash` would mutate content under the WRONG
        # source_url and leave THIS source_url with ZERO current rows. Never operate
        # on such a hash: leave both sides intact and skip the swap. (A matching row
        # under the SAME source_url is a legitimate revert to an existing revision
        # and is handled by the swap below.)
        existing_new = conn.execute(
            "SELECT source_url FROM content WHERE hash = ?", (new_hash,)
        ).fetchone()
        if existing_new is not None and existing_new["source_url"] != old["source_url"]:
            logger.warning(
                "vault_edit: %s edited to bytes already owned by source_url %r "
                "(this file's source_url is %r); not applying as a revision to avoid "
                "cross-source corruption",
                rel, existing_new["source_url"], old["source_url"],
            )
            return
        # Atomic (#83) revision swap. Ordered demote-before-promote so the
        # one-current-per-source_url unique index (migration 007) never sees two
        # is_current=1 rows for this source_url mid-sequence, and the superseded_by
        # FK is always satisfied (the new hash exists before it is referenced):
        #   1. insert the new revision NON-current (is_current=0);
        #   2. demote the old current row and point it at the new hash;
        #   3. promote the new revision to current -> exactly one current row.
        # A failure partway through ROLLs BACK, leaving the prior revision intact.
        with transaction(conn):
            conn.execute(
                """INSERT OR IGNORE INTO content
                   (hash, body, title, source_url, source_tier, fetched_at, confidence,
                    ttl_days, kind, revision, is_current, protected, vault_path, source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)""",
                (new_hash, new_body, old["title"], old["source_url"], old["source_tier"],
                 old["fetched_at"], old["confidence"], old["ttl_days"], old["kind"],
                 old["revision"] + 1, rel, old["source_id"]),
            )
            # The new revision MUST exist before we reference it below. INSERT OR
            # IGNORE is a legitimate no-op when the human edited a file to match
            # already-existing content (the new hash is already a row), so assert the
            # row's PRESENCE rather than the insert's rowcount. Inserting it
            # NON-current is also what guarantees it isn't silently dropped: at
            # is_current=0 it can never collide with the source_url unique index --
            # the exact hazard the old is_current=1 insert hit, where INSERT OR IGNORE
            # swallowed the new revision and the swap then failed.
            if conn.execute(
                "SELECT 1 FROM content WHERE hash = ?", (new_hash,)
            ).fetchone() is None:
                raise RuntimeError(
                    f"vault_edit: new revision {new_hash} was not inserted; aborting swap"
                )
            # Demote the old current row, now that the new hash exists for the FK.
            # This briefly leaves zero current rows for the source_url -- the partial
            # index permits zero or one; only TWO would violate it.
            conn.execute(
                "UPDATE content SET is_current = 0, superseded_by = ?, vault_path = NULL, updated_at = ? "
                "WHERE hash = ?",
                (new_hash, now, old_hash),
            )
            # Promote the new revision to current+protected -> exactly one current
            # row. Clear tombstoned too, so a revert to a previously-tombstoned
            # same-source revision comes back live (mirrors dedup resurrection).
            conn.execute(
                "UPDATE content SET is_current = 1, protected = 1, tombstoned = 0, "
                "vault_path = ?, updated_at = ? WHERE hash = ?",
                (rel, now, new_hash),
            )
            # MINIMUM BAR (#153): the edited source_url must end with EXACTLY ONE
            # current, non-tombstoned row -- never zero, never two. If a concurrent
            # change slipped past the cross-source guard above, raise and let the
            # transaction ROLL BACK rather than commit a broken state. NULL
            # source_url is exempt: the one-current index does not constrain it and
            # `source_url = NULL` never matches.
            if old["source_url"] is not None:
                n_current = conn.execute(
                    "SELECT COUNT(*) FROM content "
                    "WHERE source_url = ? AND is_current = 1 AND tombstoned = 0",
                    (old["source_url"],),
                ).fetchone()[0]
                if n_current != 1:
                    raise RuntimeError(
                        f"vault_edit: source_url {old['source_url']!r} would end with "
                        f"{n_current} current rows after the swap; aborting"
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
    handler = _Handler(vault, debouncer, cfg.watcher.ignore, conn)
    observer = Observer()
    observer.schedule(handler, str(vault), recursive=True)
    observer_started = False
    try:
        _reconcile_startup(conn, vault, cfg.watcher.ignore)
        observer.start()
        observer_started = True
        logger.info("watcher started; vault=%s", vault)
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        # Graceful shutdown (#92): cancel any pending debounced timers, stop the
        # observer, and close the long-lived DB connection so the daemon doesn't
        # leak threads / file descriptors / WAL sidecars.
        debouncer.cancel_all()
        if observer_started:
            observer.stop()
            observer.join()
        conn.close()
