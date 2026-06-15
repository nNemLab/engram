"""Reactor: tail event log, dispatch handlers."""
from __future__ import annotations

import logging
import sqlite3
import time

from .. import log as event_log
from ..common.db import get_connection, transaction
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


def _handle_failure(
    conn: sqlite3.Connection, evt: event_log.Event, exc: Exception
) -> bool:
    """Record a handler failure and decide how the loop proceeds.

    The handler's own side-effects already rolled back with its per-event
    transaction (#153). This persists the retry-budget bookkeeping in a SEPARATE
    transaction so it survives that rollback:

      * under budget -> return False so the caller stops the batch and retries the
        event on the next tick (the cursor stays at the last fully-processed
        event, preserving #111's no-silent-drop back-pressure);
      * budget exhausted -> dead-letter the event AND advance the cursor PAST it
        in one transaction, returning True. The cursor MUST advance even if the
        dead-letter WRITE itself fails, else a permanently-failing event whose DLQ
        write also fails would head-of-line-block the stream forever (#115); fall
        back to a cursor-only advance in that case (one DLQ record lost, logged).

    A failure of the attempt-counter bump itself propagates to the tick handler
    (cursor stays put, event retried next tick) -- unchanged from before.
    """
    with transaction(conn):
        attempts = _bump_attempts(conn, evt, exc)
    if attempts < MAX_HANDLER_ATTEMPTS:
        logger.error(
            "handler %s failed for event %d (attempt %d/%d); will retry next tick",
            evt.type, evt.id, attempts, MAX_HANDLER_ATTEMPTS, exc_info=exc,
        )
        return False
    try:
        with transaction(conn):
            _dead_letter(conn, evt, attempts, exc)
            _write_cursor(conn, evt.id)
        logger.error(
            "handler %s failed %d times for event %d; dead-lettering and advancing past it",
            evt.type, attempts, evt.id,
        )
    except Exception:
        logger.exception(
            "handler %s failed %d times for event %d AND the dead-letter write failed; "
            "advancing past it anyway to keep the stream unblocked (DLQ record lost)",
            evt.type, attempts, evt.id,
        )
        try:
            with transaction(conn):
                _write_cursor(conn, evt.id)
        except Exception:
            logger.exception("reactor: cursor advance also failed for event %d", evt.id)
    return True


def _run_loop(conn: sqlite3.Connection) -> None:
    cursor = _read_cursor(conn)
    logger.info("reactor starting; cursor=%d handlers=%s", cursor, list(HANDLERS))
    types = list(HANDLERS.keys())
    while True:
        try:
            # Materialize the batch before processing: each event below COMMITs its
            # own transaction, so we must not iterate a live cursor over `events`
            # while writing to that table.
            for evt in list(event_log.since(conn, cursor, types=types, yield_poison=True)):
                if evt.poison:
                    # Dead-letter an un-parseable payload and advance past it so
                    # one corrupt row can't freeze the loop and drop every later
                    # event (#84). No handler effects to bind -- the single cursor
                    # write is atomic on its own.
                    logger.warning(
                        "reactor skipping poison event id=%d (unparseable payload)",
                        evt.id,
                    )
                    _write_cursor(conn, evt.id)
                    cursor = evt.id
                    continue
                handler = HANDLERS.get(evt.type)
                if handler is None:
                    _write_cursor(conn, evt.id)  # no handler → safe to advance
                    cursor = evt.id
                    continue
                try:
                    # #153: the handler's side-effects AND this event's cursor
                    # advance commit in ONE transaction (the handler's own
                    # `with transaction` joins this outer one). A crash mid-handler,
                    # or before the cursor persists, rolls BOTH back -- so the cursor
                    # never advances past an event whose effects didn't fully land,
                    # and no completed non-idempotent event (merged / stale_marked /
                    # refresh_requested) is replayed/re-emitted on restart.
                    with transaction(conn):
                        handler(conn, evt)
                        _clear_attempts(conn, evt.id)  # success → reset retry budget
                        _write_cursor(conn, evt.id)
                except Exception as exc:
                    # The handler's effects rolled back with the transaction above.
                    # Record the failure (retry budget / dead-letter) separately and
                    # either advance past the event (dead-lettered) or stop the batch
                    # to retry it on the next tick.
                    if _handle_failure(conn, evt, exc):
                        cursor = evt.id  # dead-lettered: advanced past it
                        continue
                    break  # under budget: retry on the next tick
                else:
                    cursor = evt.id  # committed: handler effects + cursor together
        except Exception:
            logger.exception("reactor tick failed")
        time.sleep(1)
