"""Poller main loop: scan due sources, dispatch adapter, gate candidates,
update source state."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    from engram.common import config as cfg_mod
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)
    yield c


class FakeAdapter:
    """Yields a fixed list of Candidates. Bumps cursor to {n: call_count} each call."""
    name = "fake"
    def __init__(self, candidates):
        self._cands = candidates
        self.calls = 0
    async def fetch(self, source):
        self.calls += 1
        for c in self._cands:
            yield c
        source["cursor"] = json.dumps({"n": self.calls})


@pytest.mark.asyncio
async def test_poll_one_runs_due_source_and_advances_state(conn, monkeypatch):
    from engram.poller.poller import poll_one
    from engram.poller.adapters import Candidate, ADAPTERS

    fake = FakeAdapter([
        Candidate(source_url="https://x/a", body="A body", title="A"),
        Candidate(source_url="https://x/b", body="B body", title="B"),
    ])
    monkeypatch.setitem(ADAPTERS, "fake", fake)

    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier) "
        "VALUES ('s1', 'Test', 'fake', 'https://x', '1d', 'manual')"
    )
    src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
    await poll_one(conn, src)

    rows = conn.execute("SELECT type FROM events WHERE type='ingested'").fetchall()
    assert len(rows) == 2

    final = conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone()
    assert final["last_polled_at"] is not None
    assert final["last_success_at"] is not None
    assert final["next_poll_at"] is not None
    assert final["error_count"] == 0
    assert json.loads(final["cursor"])["n"] == 1


@pytest.mark.asyncio
async def test_due_query_skips_paused_and_future(conn):
    from engram.poller.poller import select_due
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, paused, next_poll_at) "
        "VALUES ('past', 'p', 'fake', 'x', '1d', 0, NULL),"
        "       ('paused', 'q', 'fake', 'x', '1d', 1, NULL),"
        "       ('future', 'r', 'fake', 'x', '1d', 0, ?)",
        (future,),
    )
    due = select_due(conn)
    ids = sorted(s["id"] for s in due)
    assert ids == ["past"]
