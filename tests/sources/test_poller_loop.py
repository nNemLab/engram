"""Poller main loop: scan due sources, dispatch adapter, gate candidates,
update source state."""
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql",
               "003_grounding.sql", "004_protected.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c)
    # Patch where gate() looks the symbol up (engram.dedup.load_config), not
    # where it's defined — robust even if dedup is imported before this runs.
    from types import SimpleNamespace

    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
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
    from engram.poller.adapters import ADAPTERS, Candidate
    from engram.poller.poller import poll_one

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

    # source_id is threaded through dedup.gate at insert time (not patched in a
    # post-hoc UPDATE), so every ingested content row carries it.
    tagged = conn.execute(
        "SELECT COUNT(*) AS c FROM content WHERE source_id = 's1'"
    ).fetchone()
    assert tagged["c"] == 2

    final = conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone()
    assert final["last_polled_at"] is not None
    assert final["last_success_at"] is not None
    assert final["next_poll_at"] is not None
    assert final["error_count"] == 0
    assert json.loads(final["cursor"])["n"] == 1


@pytest.mark.asyncio
async def test_slow_source_does_not_block_others(conn, monkeypatch):
    """One source parked mid-fetch must not stall the other due sources in the
    same tick: the fast sources complete while the slow one is still blocked."""
    import asyncio

    from engram.poller.adapters import ADAPTERS, Candidate
    from engram.poller.poller import _poll_due

    release = asyncio.Event()

    class SlowAdapter:
        name = "slow"
        async def fetch(self, source):
            await release.wait()  # park until the test releases it
            yield Candidate(source_url="https://x/slow", body="S body", title="S")

    fast = FakeAdapter([Candidate(source_url="https://x/fast", body="F body", title="F")])
    monkeypatch.setitem(ADAPTERS, "fast", fast)
    monkeypatch.setitem(ADAPTERS, "slow", SlowAdapter())

    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier) "
        "VALUES ('slow1', 'Slow', 'slow', 'https://x', '1d', 'manual'),"
        "       ('fast1', 'Fast1', 'fast', 'https://x', '1d', 'manual'),"
        "       ('fast2', 'Fast2', 'fast', 'https://x', '1d', 'manual')"
    )
    due = [dict(r) for r in conn.execute("SELECT * FROM sources").fetchall()]

    task = asyncio.create_task(_poll_due(conn, due))
    # Yield repeatedly so the fast coroutines run to completion while the slow
    # one stays parked on `release`.
    done = 0
    for _ in range(100):
        await asyncio.sleep(0)
        done = conn.execute(
            "SELECT COUNT(*) AS c FROM sources "
            "WHERE id IN ('fast1', 'fast2') AND last_polled_at IS NOT NULL"
        ).fetchone()["c"]
        if done == 2:
            break

    # Both fast sources finished even though the slow one is still blocked.
    assert done == 2
    assert not task.done()
    slow = conn.execute("SELECT last_polled_at FROM sources WHERE id='slow1'").fetchone()
    assert slow["last_polled_at"] is None

    # Release the slow source; the tick then completes cleanly.
    release.set()
    await task
    slow = conn.execute("SELECT last_polled_at FROM sources WHERE id='slow1'").fetchone()
    assert slow["last_polled_at"] is not None


@pytest.mark.asyncio
async def test_concurrent_sources_do_not_share_adapter_rate_limiter(monkeypatch):
    """Adapters are process-wide singletons in ADAPTERS; the poller now polls
    sources concurrently. Per-fetch mutable state (the rate limiter) must be a
    local, not stored on self, or two concurrent sources sharing the one adapter
    instance clobber each other's limiter mid-run."""
    import asyncio
    from collections import Counter

    import httpx

    import engram.poller.adapters.sitemap as sm
    from engram.poller.adapters.sitemap import SitemapAdapter

    sitemap_xml = (REPO / "tests" / "fixtures" / "sitemap_minimal.xml").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml,
                                  headers={"content-type": "application/xml"})
        return httpx.Response(200, text="<html><body>page</body></html>",
                              headers={"content-type": "text/html", "etag": '"x"'})

    # ONE adapter instance, shared by both sources (mirrors the ADAPTERS singleton).
    adapter = SitemapAdapter(_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    # Record the interval of whichever limiter each page fetch actually uses.
    recorded: list[float] = []
    real_fetch = sm.fetch_with_politeness

    async def recording_fetch(client, url, *, rate_limiter, **kw):
        recorded.append(rate_limiter._interval)
        return await real_fetch(client, url, rate_limiter=rate_limiter, **kw)

    monkeypatch.setattr(sm, "fetch_with_politeness", recording_fetch)

    # Barrier forces both sources to finish creating their limiter (and collecting
    # URLs) before EITHER starts fetching pages. With a shared-self limiter the
    # second source's assignment would have clobbered the first's by then.
    barrier = asyncio.Barrier(2)
    orig_collect = SitemapAdapter._collect_urls

    async def collect_with_barrier(self, sitemap_url, _depth=0):
        urls = await orig_collect(self, sitemap_url, _depth)
        if _depth == 0:
            await barrier.wait()
        return urls

    monkeypatch.setattr(SitemapAdapter, "_collect_urls", collect_with_barrier)

    base_cfg = {"include": ["**/engine/**"], "exclude": ["**/macos/**"]}
    src_a = {"id": "a", "url": "https://docs.example.com/sitemap.xml",
             "config": json.dumps({**base_cfg, "request_interval_ms": 100}), "cursor": None}
    src_b = {"id": "b", "url": "https://docs.example.com/sitemap.xml",
             "config": json.dumps({**base_cfg, "request_interval_ms": 999}), "cursor": None}

    async def drain(src):
        return [c async for c in adapter.fetch(src)]

    res_a, res_b = await asyncio.gather(drain(src_a), drain(src_b))

    # Each source yields its two engine pages.
    assert len(res_a) == 2
    assert len(res_b) == 2
    # Source A's two fetches use a 0.1s limiter, B's use a 0.999s one. A shared
    # limiter would skew this multiset (e.g. all four at one interval).
    assert Counter(recorded) == Counter({100 / 1000: 2, 999 / 1000: 2})


@pytest.mark.asyncio
async def test_due_query_skips_paused_and_future(conn):
    from engram.poller.poller import select_due
    future = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
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


@pytest.mark.asyncio
async def test_poll_one_reads_confidence_kind_from_source_config(conn, monkeypatch):
    """Per-source config overrides confidence and kind; defaults apply when absent."""
    from engram.poller.adapters import ADAPTERS, Candidate
    from engram.poller.poller import poll_one

    captured: list[dict] = []

    def mock_gate(conn, **kw):
        captured.append(kw)
        from types import SimpleNamespace
        return SimpleNamespace(outcome="new")

    monkeypatch.setattr("engram.poller.poller.gate", mock_gate)

    # Source with explicit config values
    fake = FakeAdapter([Candidate(source_url="https://x/a", body="A body", title="A")])
    monkeypatch.setitem(ADAPTERS, "fake", fake)
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier, config) "
        "VALUES ('s1', 'Test', 'fake', 'https://x', '1d', 'manual', "
        "'{\"confidence\": 0.95, \"kind\": \"article\"}')",
    )
    src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
    await poll_one(conn, src)

    assert captured[0]["confidence"] == 0.95
    assert captured[0]["kind"] == "article"


@pytest.mark.asyncio
async def test_poll_one_defaults_confidence_kind_when_omitted(conn, monkeypatch):
    """When config omits confidence/kind, defaults (0.7, "research") apply."""
    from engram.poller.adapters import ADAPTERS, Candidate
    from engram.poller.poller import poll_one

    captured: list[dict] = []

    def mock_gate(conn, **kw):
        captured.append(kw)
        from types import SimpleNamespace
        return SimpleNamespace(outcome="new")

    monkeypatch.setattr("engram.poller.poller.gate", mock_gate)

    fake = FakeAdapter([Candidate(source_url="https://x/a", body="A body", title="A")])
    monkeypatch.setitem(ADAPTERS, "fake", fake)
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier, config) "
        "VALUES ('s1', 'Test', 'fake', 'https://x', '1d', 'manual', '{}')",
    )
    src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
    await poll_one(conn, src)

    assert captured[0]["confidence"] == 0.7
    assert captured[0]["kind"] == "research"
