"""Shared UTC timestamp formatting.

One helper so the poller, the sources MCP tools, and the event log all emit
ISO-8601 'Z' timestamps the same way, matching what SQLite writes via
strftime('%Y-%m-%dT%H:%M:%fZ', 'now').
"""
from __future__ import annotations

from datetime import UTC, datetime


def utcnow_iso(precision: str = "ms") -> str:
    """Current UTC time as an ISO-8601 'Z'-suffixed string.

    precision="ms" -> 'YYYY-MM-DDTHH:MM:SS.mmmZ' (millisecond, matches the DB
                      default written by strftime('%Y-%m-%dT%H:%M:%fZ', 'now')).
    precision="s"  -> 'YYYY-MM-DDTHH:MM:SSZ' (whole seconds).
    """
    now = datetime.now(UTC)
    if precision == "s":
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if precision == "ms":
        return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    raise ValueError(f"unknown precision: {precision!r} (expected 'ms' or 's')")
