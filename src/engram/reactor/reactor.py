"""Reactor: tail event log, dispatch handlers."""
from __future__ import annotations

import logging
import sqlite3
import time

from .. import log as event_log
from ..common.db import get_connection
from .handlers import HANDLERS

logger = logging.getLogger("engram.reactor")
CURSOR_KEY = "reactor"


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


def run() -> None:
    conn = get_connection()
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
                        last = evt.id  # advance cursor only after successful dispatch
                    except Exception:
                        logger.exception("handler %s failed for event %d", evt.type, evt.id)
                        break  # stop batch on failure; cursor stays at last successful event
                else:
                    last = evt.id  # no handler → still safe to advance
            if last != cursor:
                _write_cursor(conn, last)
                cursor = last
        except Exception:
            logger.exception("reactor tick failed")
        time.sleep(1)
