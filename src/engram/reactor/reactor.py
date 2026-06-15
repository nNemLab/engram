"""Reactor: tail event log, dispatch handlers."""
from __future__ import annotations

import logging
import sqlite3
import time

from .. import log as event_log
from ..common.db import get_connection
from ..common.time import utcnow_iso
from .handlers import HANDLERS

logger = logging.getLogger("engram.reactor")
CURSOR_KEY = "reactor"

# Retry budget for the handler-failure class (#115). A parseable event whose
# handler raises is retried once per poll cycle; after this many failed attempts
# it is dead-lettered and the cursor advances past it, so a deterministically
# (permanently) failing event can't head-of-line-block the stream forever. The
# poison path (#84) is a different class (unparseable payloads) and is unaffected.
MAX_HANDLER_ATTEMPTS = 5


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


def _bump_attempts(conn: sqlite3.Connection, evt: event_log.Event, exc: Exception) -> int:
    """Record one more handler-failure attempt for `evt`; return the new count."""
    conn.execute(
        "INSERT INTO reactor_attempts (event_id, attempts, last_error, last_attempt_ts) "
        "VALUES (?, 1, ?, ?) "
        "ON CONFLICT(event_id) DO UPDATE SET attempts = attempts + 1, "
        "last_error = excluded.last_error, last_attempt_ts = excluded.last_attempt_ts",
        (evt.id, repr(exc), utcnow_iso("ms")),
    )
    row = conn.execute(
        "SELECT attempts FROM reactor_attempts WHERE event_id = ?", (evt.id,)
    ).fetchone()
    return int(row["attempts"])


def _clear_attempts(conn: sqlite3.Connection, event_id: int) -> None:
    """Drop a per-event attempt counter once the event has been handled."""
    conn.execute("DELETE FROM reactor_attempts WHERE event_id = ?", (event_id,))


def _dead_letter(
    conn: sqlite3.Connection, evt: event_log.Event, attempts: int, exc: Exception
) -> None:
    """Move a budget-exhausted event into the dead_letter table and drop its counter."""
    conn.execute(
        "INSERT OR IGNORE INTO dead_letter "
        "(event_id, event_type, attempts, error, dead_lettered_ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (evt.id, evt.type, attempts, repr(exc), utcnow_iso("ms")),
    )
    _clear_attempts(conn, evt.id)


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
    cursor = _read_cursor(conn)
    logger.info("reactor starting; cursor=%d handlers=%s", cursor, list(HANDLERS))
    types = list(HANDLERS.keys())
    while True:
        try:
            last = cursor
            for evt in event_log.since(conn, cursor, types=types, yield_poison=True):
                if evt.poison:
                    # Dead-letter an un-parseable payload and advance past it so
                    # one corrupt row can't freeze the loop and drop every later
                    # event (#84).
                    logger.warning(
                        "reactor skipping poison event id=%d (unparseable payload)",
                        evt.id,
                    )
                    last = evt.id
                    continue
                handler = HANDLERS.get(evt.type)
                if handler:
                    try:
                        handler(conn, evt)
                    except Exception as exc:
                        # A parseable event whose handler raised. Track attempts so
                        # transient failures retry (preserving #111's no-silent-drop
                        # guarantee) but a deterministic failure can't block forever.
                        attempts = _bump_attempts(conn, evt, exc)
                        if attempts >= MAX_HANDLER_ATTEMPTS:
                            # Budget exhausted: record it and advance past the event
                            # so later events are no longer head-of-line-blocked.
                            # The cursor MUST advance even if the dead-letter WRITE
                            # itself fails (DB error/lock/schema drift); otherwise the
                            # exception would escape to the tick-level handler, the
                            # cursor would stay put, and this same event would be
                            # retried forever -- re-creating the exact head-of-line
                            # block we're fixing. Losing/logging one DLQ record is
                            # strictly better than wedging the whole reactor.
                            try:
                                _dead_letter(conn, evt, attempts, exc)
                                logger.error(
                                    "handler %s failed %d times for event %d; "
                                    "dead-lettering and advancing past it",
                                    evt.type, attempts, evt.id,
                                )
                            except Exception:
                                logger.exception(
                                    "handler %s failed %d times for event %d AND the "
                                    "dead-letter write failed; advancing past it anyway "
                                    "to keep the stream unblocked (DLQ record lost)",
                                    evt.type, attempts, evt.id,
                                )
                            last = evt.id
                            continue
                        # Under budget: stop the batch and retry on the next tick
                        # (cursor stays at the last successful event, as in #111).
                        logger.exception(
                            "handler %s failed for event %d (attempt %d/%d); "
                            "will retry next tick",
                            evt.type, evt.id, attempts, MAX_HANDLER_ATTEMPTS,
                        )
                        break
                    else:
                        _clear_attempts(conn, evt.id)  # success → reset retry budget
                        last = evt.id  # advance cursor only after successful dispatch
                else:
                    last = evt.id  # no handler → still safe to advance
            if last != cursor:
                _write_cursor(conn, last)
                cursor = last
        except Exception:
            logger.exception("reactor tick failed")
        time.sleep(1)
