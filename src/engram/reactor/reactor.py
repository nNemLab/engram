"""Reactor: tail event log, dispatch handlers."""
from __future__ import annotations

import logging
import signal
import sqlite3
import time
from types import FrameType

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

# #172: tick-level failures back off exponentially (bounded) so a persistently
# failing tick does not spin forever at 1Hz.
BASE_TICK_BACKOFF_SECONDS = 1.0
MAX_TICK_BACKOFF_SECONDS = 30.0

# #172: process-level graceful shutdown flag set by signal handlers.
_SHUTDOWN_REQUESTED = False


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


def request_shutdown(signum: int | None = None, _frame: FrameType | None = None) -> None:
    """Request graceful reactor shutdown (used by SIGINT/SIGTERM handlers)."""
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = True
    if signum is None:
        logger.info("reactor shutdown requested")
    else:
        logger.info("reactor shutdown requested via signal %s", signum)


def install_signal_handlers() -> None:
    """Register SIGINT/SIGTERM handlers that trigger graceful shutdown."""
    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)


def run() -> None:
    # Own the long-lived connection's lifecycle: close it on any loop exit
    # (KeyboardInterrupt/SIGINT or a fatal error) so the daemon doesn't leak the
    # connection + WAL sidecars on shutdown (#92). common/db.get_connection
    # documents that the caller must close.
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = False
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
        in one transaction. The cursor MUST advance even if the dead-letter WRITE
        itself fails (a permanently-failing event whose DLQ write also fails would
        otherwise head-of-line-block the stream forever, #115), so fall back to a
        cursor-only advance (one DLQ record lost, logged).

    Returns True ONLY when the cursor advance was DURABLY persisted -- via either
    the dead-letter+cursor transaction or the fallback cursor-only write. If BOTH
    of those fail, returns False so the caller does NOT move the in-memory cursor
    past the event: the (already attempt-bumped) event stays pending and is
    retried from the persisted cursor on the next scheduled pass rather than being
    silently skipped/lost. A failure of the attempt-counter bump itself propagates
    to the tick handler (cursor stays put, event retried) -- unchanged from before.
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
        return True
    except Exception:
        logger.exception(
            "handler %s failed %d times for event %d AND the dead-letter write failed; "
            "trying a cursor-only advance to keep the stream unblocked (DLQ record lost)",
            evt.type, attempts, evt.id,
        )
    # The dead-letter+cursor transaction failed. Try to at least persist the cursor
    # so the exhausted event no longer blocks the stream.
    try:
        with transaction(conn):
            _write_cursor(conn, evt.id)
        logger.error(
            "handler %s failed %d times for event %d; advanced past it without a DLQ record",
            evt.type, attempts, evt.id,
        )
        return True
    except Exception:
        # Neither the DLQ write nor the fallback cursor write landed: the cursor was
        # NOT durably advanced. Do NOT report 'advanced' -- moving the in-memory
        # cursor now would skip the event without ever recording it (silent loss).
        # Leave it pending; it retries from the persisted cursor on the next pass.
        logger.exception(
            "handler %s failed %d times for event %d AND both the dead-letter write "
            "and the fallback cursor write failed; NOT advancing -- event stays "
            "pending for retry next pass",
            evt.type, attempts, evt.id,
        )
        return False


def _run_loop(conn: sqlite3.Connection) -> None:
    cursor = _read_cursor(conn)
    logger.info("reactor starting; cursor=%d handlers=%s", cursor, list(HANDLERS))
    types = list(HANDLERS.keys())
    backoff_seconds = BASE_TICK_BACKOFF_SECONDS
    while not _SHUTDOWN_REQUESTED:
        tick_failed = False
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
                    # Record the failure (retry budget / dead-letter) separately.
                    # _handle_failure returns True ONLY when the cursor was DURABLY
                    # advanced past this event; advance the in-memory cursor only
                    # then, so we never skip an event whose advance wasn't persisted.
                    if _handle_failure(conn, evt, exc):
                        cursor = evt.id  # durably advanced past it (dead-lettered)
                        continue
                    # Under budget, or the durable cursor advance failed: stop the
                    # batch and retry from the persisted cursor on the next pass
                    # (back-pressure; never a tight loop -- the loop sleeps a tick).
                    break
                else:
                    cursor = evt.id  # committed: handler effects + cursor together
        except Exception:
            tick_failed = True
            logger.exception("reactor tick failed")

        if _SHUTDOWN_REQUESTED:
            break

        if tick_failed:
            time.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, MAX_TICK_BACKOFF_SECONDS)
        else:
            backoff_seconds = BASE_TICK_BACKOFF_SECONDS
            time.sleep(BASE_TICK_BACKOFF_SECONDS)
