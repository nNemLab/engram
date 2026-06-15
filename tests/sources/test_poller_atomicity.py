"""Unit D (#152): poll_one's source-state update + its events are atomic.

The per-tick `UPDATE sources SET ...` plus the circuit-broken transition and the
`source_polled` event are wrapped in one `common.db.transaction()`, so a tick
can't advance source state without its events (or emit events without persisting
the state). An injected failure at an event append rolls back the whole unit.
"""
import sqlite3
from pathlib import Path

import pytest

import engram.poller.poller as pmod
from engram.poller.adapters import ADAPTERS
from engram.poller.poller import poll_one

REPO = Path(__file__).resolve().parents[2]


def _conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.sqlite", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql",
               "003_grounding.sql", "004_protected.sql"):
        c.executescript((REPO / "schema" / fn).read_text())
    return c


class _EmptyAdapter:
    """Yields no candidates -- isolates the source-state update from the gate."""
    name = "fake"

    async def fetch(self, source):
        return
        yield  # pragma: no cover - makes this an async generator


@pytest.mark.asyncio
async def test_poll_one_state_update_atomic_on_event_failure(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setitem(ADAPTERS, "fake", _EmptyAdapter())
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier) "
        "VALUES ('s1', 'Test', 'fake', 'https://x', '1d', 'manual')"
    )
    before = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())

    # Blow up at the source_polled append (inside the state-update transaction).
    def _boom(*a, **k):
        raise RuntimeError("event append exploded")

    monkeypatch.setattr(pmod.event_log, "append", _boom)

    src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
    with pytest.raises(RuntimeError, match="exploded"):
        await poll_one(conn, src)

    # The UPDATE sources rolled back with the failed event: source state unchanged.
    after = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
    assert after == before
    # No events were emitted (neither source_polled nor any partial).
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
