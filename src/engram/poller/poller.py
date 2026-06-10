"""Poller daemon main loop. Per-tick: scan due sources, dispatch adapter,
push candidates through dedup.gate, update source state in one tx."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .. import log as event_log
from ..common.config import load_config
from ..common.db import get_connection
from ..common.time import utcnow_iso
from ..dedup import gate
from .adapters import ADAPTERS
from .schedule import parse_interval

logger = logging.getLogger("engram.poller")

CIRCUIT_BREAK_THRESHOLD = 5


def select_due(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sources "
        "WHERE paused = 0 "
        "  AND (next_poll_at IS NULL OR next_poll_at <= ?)",
        (utcnow_iso("s"),),
    ).fetchall()


def _classify_error(exc: Exception) -> tuple[bool, str]:
    """Return (retryable, short_message)."""
    try:
        import httpx
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            retryable = status >= 500
            return retryable, f"HTTP {status}: {exc.response.url}"
        if isinstance(exc, (httpx.NetworkError, httpx.TimeoutException)):
            return True, f"network: {exc.__class__.__name__}"
    except ImportError:
        pass
    return False, f"{exc.__class__.__name__}: {exc}"


async def poll_one(conn: sqlite3.Connection, source: dict[str, Any]) -> dict[str, int]:
    """Poll one source. Returns {ingested, superseded, exact_dup, blocked, errors, candidates_seen}."""
    adapter = ADAPTERS.get(source["adapter"])
    if adapter is None:
        raise ValueError(f"unknown adapter: {source['adapter']}")
    src_dict = dict(source)  # mutable copy adapter writes cursor into
    counts = {"ingested": 0, "superseded": 0, "exact_dup": 0, "blocked": 0,
              "errors": 0, "candidates_seen": 0}
    error_msg = None
    try:
        async for cand in adapter.fetch(src_dict):
            counts["candidates_seen"] += 1
            try:
                r = gate(
                    conn, body=cand.body, title=cand.title,
                    source_url=cand.source_url, source_tier=source["source_tier"],
                    confidence=0.7, kind="research", source_id=source["id"],
                )
                if r.outcome == "new":
                    counts["ingested"] += 1
                elif r.outcome == "superseded":
                    counts["superseded"] += 1
                elif r.outcome == "exact_dup":
                    counts["exact_dup"] += 1
                elif r.outcome == "supersede_blocked":
                    counts["blocked"] += 1
            except Exception:
                logger.exception("gate failed for %s", cand.source_url)
                counts["errors"] += 1
    except Exception as exc:
        retryable, msg = _classify_error(exc)
        error_msg = msg
        counts["errors"] += 1
        event_log.append(
            conn, "source_error",
            {"source_id": source["id"], "error": msg, "retryable": retryable},
            actor="poller",
        )

    interval = parse_interval(source["schedule"])
    next_at = (datetime.now(UTC) + interval).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_error_count = (source["error_count"] or 0) + 1 if error_msg else 0
    was_paused = bool(source["paused"] or 0)
    paused = 1 if new_error_count >= CIRCUIT_BREAK_THRESHOLD else (source["paused"] or 0)
    conn.execute(
        "UPDATE sources SET cursor = ?, last_polled_at = ?, "
        " last_success_at = COALESCE(?, last_success_at), "
        " next_poll_at = ?, error_count = ?, last_error = ?, paused = ?, "
        " updated_at = ? WHERE id = ?",
        (
            src_dict.get("cursor"),
            utcnow_iso("s"),
            None if error_msg else utcnow_iso("s"),
            next_at,
            new_error_count,
            error_msg,
            paused,
            utcnow_iso("s"),
            source["id"],
        ),
    )
    # Emit only on the 0->1 transition; a re-poll of an already-tripped source
    # must not re-fire the circuit-broken event.
    if paused == 1 and not was_paused:
        event_log.append(
            conn, "source_circuit_broken",
            {"source_id": source["id"], "error_count": new_error_count},
            actor="poller",
        )
    event_log.append(
        conn, "source_polled",
        {
            "source_id": source["id"],
            "candidates_seen": counts["candidates_seen"],
            "ingested": counts["ingested"],
            "superseded": counts["superseded"],
            "exact_dup": counts["exact_dup"],
            "errors": counts["errors"],
        },
        actor="poller",
    )
    conn.commit()
    return counts


async def run() -> None:
    load_config()
    conn = get_connection()
    logger.info("poller starting")
    while True:
        try:
            for src in select_due(conn):
                try:
                    counts = await poll_one(conn, dict(src))
                    logger.info("polled %s: %s", src["id"], counts)
                except Exception:
                    logger.exception("poll_one failed for %s", src["id"])
        except Exception:
            logger.exception("poller tick failed")
        await asyncio.sleep(60)
