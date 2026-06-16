"""Deterministic source-health observability.

A read-only view over the existing `sources` and `content` tables. Pure
SQL/Python — no network, no LLM. Every derived field is computed from columns
that already exist, so the result is reproducible for a given DB state.

ISO-8601 UTC 'Z' timestamps sort lexicographically, so plain string comparison
is used for liveness/overdue checks (matching `engram.common.time.utcnow_iso`).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..common.time import utcnow_iso


def _derive_status(
    *,
    paused: bool,
    overdue: bool,
    error_count: int,
    last_polled_at: str | None,
    last_success_at: str | None,
) -> str:
    """Derive a single status label.

    erroring  : has errors AND we have not had a success since the last poll
                (last_success_at is null, or older than last_polled_at).
    overdue   : past its next_poll_at and not paused.
    paused    : paused.
    ok        : everything else.
    """
    poll_dt = _parse_iso_z(last_polled_at)
    succ_dt = _parse_iso_z(last_success_at)
    success_is_stale = succ_dt is None or (poll_dt is not None and succ_dt < poll_dt)
    if error_count > 0 and success_is_stale:
        return "erroring"
    if overdue:
        return "overdue"
    if paused:
        return "paused"
    return "ok"


def _parse_iso_z(ts: str | None) -> datetime | None:
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def source_health(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return a per-source health record for every row in `sources`.

    Each record carries identity/config, liveness, and deterministically
    derived fields (overdue, content counts, dup_ratio, last_new_content_at,
    status). Ordered by source id for stable output.
    """
    now = utcnow_iso()
    now_dt = _parse_iso_z(now)
    records: list[dict[str, Any]] = []

    rows = conn.execute(
        "SELECT id, name, adapter, paused, source_tier, schedule, "
        "       last_polled_at, last_success_at, next_poll_at, "
        "       error_count, last_error "
        "FROM sources ORDER BY id"
    ).fetchall()

    for row in rows:
        d = dict(row)
        paused = bool(d["paused"])
        next_poll_at = d["next_poll_at"]

        next_poll_dt = _parse_iso_z(next_poll_at)
        overdue = (
            not paused
            and next_poll_dt is not None
            and now_dt is not None
            and next_poll_dt < now_dt
        )

        counts = conn.execute(
            "SELECT COUNT(*) AS total, "
            "       SUM(CASE WHEN is_current = 1 THEN 1 ELSE 0 END) AS current, "
            "       MAX(fetched_at) AS last_new "
            "FROM content WHERE source_id = ?",
            (d["id"],),
        ).fetchone()
        content_total = counts["total"]
        content_current = counts["current"] or 0
        last_new_content_at = counts["last_new"]

        dup_ratio = (
            round(1 - (content_current / content_total), 3)
            if content_total > 0
            else 0.0
        )

        status = _derive_status(
            paused=paused,
            overdue=overdue,
            error_count=d["error_count"] or 0,
            last_polled_at=d["last_polled_at"],
            last_success_at=d["last_success_at"],
        )

        records.append({
            "id": d["id"],
            "name": d["name"],
            "adapter": d["adapter"],
            "paused": paused,
            "source_tier": d["source_tier"],
            "schedule": d["schedule"],
            "last_polled_at": d["last_polled_at"],
            "last_success_at": d["last_success_at"],
            "next_poll_at": next_poll_at,
            "error_count": d["error_count"] or 0,
            "last_error": d["last_error"],
            "overdue": overdue,
            "content_total": content_total,
            "content_current": content_current,
            "dup_ratio": dup_ratio,
            "last_new_content_at": last_new_content_at,
            "status": status,
        })

    return records
