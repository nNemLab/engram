"""Poller circuit breaker: after CIRCUIT_BREAK_THRESHOLD consecutive failed
runs a source flips to paused=1 and emits a single source_circuit_broken
event. This is the main operational safety valve (see issue #3)."""
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c)
    from types import SimpleNamespace

    from engram.common import config as cfg_mod
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)
    yield c


class FailingAdapter:
    """Adapter whose fetch always raises a retryable httpx network error."""
    name = "failing"

    async def fetch(self, source):
        raise httpx.ConnectError("simulated network failure")
        yield  # pragma: no cover -- unreachable; makes fetch an async generator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_error_count, expect_broken",
    [(3, False), (4, True), (5, True)],
)
async def test_circuit_breaker_trips_only_at_threshold(
    conn, monkeypatch, start_error_count, expect_broken
):
    from engram.poller.adapters import ADAPTERS
    from engram.poller.poller import CIRCUIT_BREAK_THRESHOLD, poll_one

    monkeypatch.setitem(ADAPTERS, "failing", FailingAdapter())
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier, error_count) "
        "VALUES ('s1', 'Flaky', 'failing', 'https://x', '1d', 'manual', ?)",
        (start_error_count,),
    )
    src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())

    await poll_one(conn, src)

    final = conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone()
    # A failed run always increments the counter and records the error,
    # and never advances last_success_at.
    assert final["error_count"] == start_error_count + 1
    assert final["last_error"] is not None
    assert final["last_success_at"] is None

    # paused flips to 1 only once the running count reaches the threshold.
    expected_paused = (start_error_count + 1) >= CIRCUIT_BREAK_THRESHOLD
    assert expected_paused == expect_broken  # guards the parametrization itself
    assert final["paused"] == (1 if expect_broken else 0)

    # The circuit_broken event fires exactly once, and only at the threshold.
    broken = conn.execute(
        "SELECT payload FROM events WHERE type='source_circuit_broken'"
    ).fetchall()
    assert len(broken) == (1 if expect_broken else 0)
    if expect_broken:
        payload = json.loads(broken[0]["payload"])
        assert payload["source_id"] == "s1"
        assert payload["error_count"] == start_error_count + 1
