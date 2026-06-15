"""Poller circuit breaker: after CIRCUIT_BREAK_THRESHOLD consecutive failed
runs a source flips to paused=1 and emits a single source_circuit_broken
event. This is the main operational safety valve (see issue #3)."""
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

import engram

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


def _apply_full(conn):
    """Apply additional schemas (grounding, protected columns) needed by dedup.gate.

    The conn fixture already applies 001+002 via _apply(), so we only add 003+004.
    """
    for fn in ("003_grounding.sql", "004_protected.sql"):
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


@pytest.mark.asyncio
async def test_circuit_breaker_does_not_re_emit_when_already_paused(conn, monkeypatch):
    """A source that is already tripped (paused=1, count at/over threshold) and
    fails again must keep counting but must NOT re-emit source_circuit_broken:
    the event marks the 0->1 transition, not every failed poll while paused."""
    from engram.poller.adapters import ADAPTERS
    from engram.poller.poller import CIRCUIT_BREAK_THRESHOLD, poll_one

    monkeypatch.setitem(ADAPTERS, "failing", FailingAdapter())
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier, error_count, paused) "
        "VALUES ('s1', 'Flaky', 'failing', 'https://x', '1d', 'manual', ?, 1)",
        (CIRCUIT_BREAK_THRESHOLD,),
    )
    src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())

    await poll_one(conn, src)

    final = conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone()
    assert final["error_count"] == CIRCUIT_BREAK_THRESHOLD + 1  # still counts
    assert final["paused"] == 1  # stays paused
    broken = conn.execute(
        "SELECT 1 FROM events WHERE type='source_circuit_broken'"
    ).fetchall()
    assert len(broken) == 0  # no re-emit on a poll of an already-paused source


class GateFailingAdapter:
    """Adapter that yields candidates, but gate() always raises.

    The gate-error detection code should treat every tick as a source error
    (zero progress = candidates_seen>0, ingested==0, errors>0), eventually
    tripping the circuit breaker.
    """
    name = "gate_fail"
    def __init__(self, count=3):
        self.count = count
        self.calls = 0

    async def fetch(self, source):
        from engram.poller.adapters import Candidate
        self.calls += 1
        for i in range(self.count):
            yield Candidate(
                source_url=f"https://x/gate-fail/{self.calls}-{i}",
                body=f"body-{self.calls}-{i}",
                title=f"Title {self.calls}-{i}",
            )
        source["cursor"] = json.dumps({"n": self.calls})


@pytest.mark.asyncio
async def test_gate_failure_on_all_candidates_trips_breaker(conn, monkeypatch):
    """A source whose gate() raises on every candidate (zero ingests) must
    eventually trip the circuit breaker through the error_count escalation,
    exactly like an adapter-fetch failure would.  This is the core regression
    for issue #97."""
    import unittest.mock

    from engram.poller.adapters import ADAPTERS
    from engram.poller.poller import CIRCUIT_BREAK_THRESHOLD, poll_one

    monkeypatch.setitem(ADAPTERS, "gate_fail", GateFailingAdapter())

    # Insert the source once (same row re-polled each tick).
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier, error_count) "
        "VALUES ('s1', 'GateFail', 'gate_fail', 'https://x', '1d', 'manual', 0)",
    )

    call_num = [0]

    def failing_gate(*args, **kw):
        call_num[0] += 1
        raise RuntimeError(f"gate-error #{call_num[0]}")

    with unittest.mock.patch.object(engram.poller.poller, "gate", failing_gate):
        for _ in range(CIRCUIT_BREAK_THRESHOLD):  # 5 ticks
            src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
            await poll_one(conn, src)

    final = conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone()

    # Every tick: 3 candidates seen, 0 ingested, 3 errors (gate raised each time).
    assert final["error_count"] == CIRCUIT_BREAK_THRESHOLD  # 5 consecutive ticks
    assert final["paused"] == 1
    assert final["last_error"] == "gate() failed on all candidates this tick"

    # The breaker event fired exactly once (at the 0->1 transition).
    broken = conn.execute(
        "SELECT payload FROM events WHERE type='source_circuit_broken'"
    ).fetchall()
    assert len(broken) == 1
    payload = json.loads(broken[0]["payload"])
    assert payload["source_id"] == "s1"
    assert payload["error_count"] == CIRCUIT_BREAK_THRESHOLD


@pytest.mark.asyncio
async def test_gate_failure_with_successful_ingest_does_not_trip(conn, monkeypatch):
    """A source whose gate() sometimes raises but at least one candidate still
    ingests (outcome=="new") must NOT count as a source error this tick —
    the zero-progress guard (ingested>0) prevents false trips."""
    import unittest.mock
    from types import SimpleNamespace

    from engram.dedup import gate as real_gate
    from engram.poller.adapters import ADAPTERS
    from engram.poller.poller import poll_one

    # real_gate calls load_config() + queries the content table; provide
    # config and apply the full schema (poller_loop.py style).
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    _apply_full(conn)

    monkeypatch.setitem(ADAPTERS, "gate_fail", GateFailingAdapter())

    # Insert the source once.
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier, error_count) "
        "VALUES ('s1', 'PartialFail', 'gate_fail', 'https://x', '1d', 'manual', 0)",
    )

    call_num = [0]

    def selective_gate(*args, **kw):
        call_num[0] += 1
        # Raise on odd-numbered calls, succeed on even (simulating mixed results).
        # With 3 candidates per tick and call_num starting at 0:
        #   Tick 1: calls 0,1,2 → success, raise, success → 2 inverts → NOT an error tick
        #   Tick 2: calls 3,4,5 → raise, success, raise → 1 invert → NOT an error tick
        if call_num[0] % 2 == 1:  # odd = raise
            raise RuntimeError(f"gate-error #{call_num[0]}")
        return real_gate(*args, **kw)

    with unittest.mock.patch.object(engram.poller.poller, "gate", selective_gate):
        src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
        await poll_one(conn, src)

    final = conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone()

    # The first tick: call_num 0,1,2 → 2 successes + 1 gate error → ingested>0
    # so new_error_count=0 (zero-progress guard).  error_count stays 0.
    assert final["error_count"] == 0
    assert final["paused"] == 0


@pytest.mark.asyncio
async def test_gate_failure_all_candidates_stays_quiet_until_threshold(conn, monkeypatch):
    """Gate-failure counts must accumulate across ticks *only* when every tick
    has zero inverts.  If a single tick ever gets >=1 invert, error_count
    resets to 0 (same behaviour as fetch-error path, just for gate failures)."""
    import unittest.mock
    from types import SimpleNamespace

    from engram.dedup import gate as real_gate
    from engram.poller.adapters import ADAPTERS
    from engram.poller.poller import poll_one

    # real_gate calls load_config() + queries the content table; provide
    # config and apply the full schema.
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    _apply_full(conn)

    monkeypatch.setitem(ADAPTERS, "gate_fail", GateFailingAdapter())

    # Insert the source once.
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier, error_count) "
        "VALUES ('s1', 'MixedThenFail', 'gate_fail', 'https://x', '1d', 'manual', 0)",
    )

    call_num = [0]
    pure_fail = [False]  # flips True after mixed ticks

    def mixed_then_fail(*args, **kw):
        call_num[0] += 1
        if pure_fail[0]:
            # Pure-fail mode: every call raises → zero inverts.
            raise RuntimeError(f"pure-fail #{call_num[0]}")
        # Mixed mode: raise on odd calls, real_gate on even calls.
        if call_num[0] % 2 == 1:
            raise RuntimeError(f"gate-error #{call_num[0]}")
        return real_gate(*args, **kw)

    with unittest.mock.patch.object(engram.poller.poller, "gate", mixed_then_fail):
        # --- Mixed ticks: error_count stays at 0 because ingested>0 each tick ---

        # Tick 1: calls 0,1,2 → success(0), raise(1), success(2) → ingested=2
        src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
        await poll_one(conn, src)
        assert conn.execute("SELECT error_count FROM sources WHERE id='s1'").fetchone()[
            "error_count"
        ] == 0

        # Tick 2: calls 3,4,5 → raise(3), success(4), raise(5) → ingested=1
        src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
        await poll_one(conn, src)
        assert conn.execute("SELECT error_count FROM sources WHERE id='s1'").fetchone()[
            "error_count"
        ] == 0

        # Tick 3: calls 6,7,8 → success(6), raise(7), success(8) → ingested=2
        src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
        await poll_one(conn, src)
        assert conn.execute("SELECT error_count FROM sources WHERE id='s1'").fetchone()[
            "error_count"
        ] == 0

        # --- Bump base error_count, then flip to pure-fail mode ---
        conn.execute("UPDATE sources SET error_count = 3 WHERE id='s1'")
        pure_fail[0] = True  # every subsequent gate() call raises

        # --- 4 pure-fail ticks: error_count accumulates from 3 → 7 → threshold hit ---
        for tick_idx in range(4):
            src = dict(conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone())
            counts = await poll_one(conn, src)
            expected_error = 3 + (tick_idx + 1)
            assert counts["candidates_seen"] == 3
            assert counts["ingested"] == 0
            assert counts["errors"] == 3  # 3 gate failures this tick
            final = conn.execute(
                "SELECT error_count, paused FROM sources WHERE id='s1'"
            ).fetchone()
            assert final["error_count"] == expected_error
            if tick_idx == 1:
                # After 5 consecutive errors (3+2), circuit should trip.
                assert final["paused"] == 1

        final = conn.execute("SELECT * FROM sources WHERE id='s1'").fetchone()
        assert final["error_count"] == 7
        assert final["paused"] == 1
