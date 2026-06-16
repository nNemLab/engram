"""Regression tests for reactor robustness fixes from issue #172.

Covers:
  1) retrieved-event emission de-dupe (stale_marked / refresh_requested),
  2) staleness_score monotonicity,
  3) tick-level exponential backoff + reset on success,
  4) SIGINT/SIGTERM graceful shutdown wiring.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
import sqlite_vec

from engram.common.db import init_schema
from engram.reactor import handlers as hmod
from engram.reactor import reactor as rmod

DIM = 4


class _StopLoop(Exception):
    """Sentinel used by patched time.sleep to stop infinite loops in tests."""


class _SpyConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.sqlite", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    init_schema(c, embed_dim=DIM)
    return c


def _cfg(*, threshold=0.5):
    return SimpleNamespace(
        rag=SimpleNamespace(chunk_size_tokens=512, chunk_overlap_tokens=64, near_dup_threshold=0.92),
        reactor=SimpleNamespace(retrieval_staleness_threshold=threshold),
    )


def _insert_stale_row(
    conn,
    h,
    *,
    source_url="https://example.test/doc",
    staleness_score=0.0,
    fetched_at="2000-01-01T00:00:00Z",
    ttl_days=1,
):
    conn.execute(
        "INSERT INTO content (hash, title, body, source_url, source_tier, confidence, kind, "
        "tombstoned, fetched_at, ttl_days, staleness_score) "
        "VALUES (?, 't', 'b', ?, 'manual', 0.8, 'kb', 0, ?, ?, ?)",
        (h, source_url, fetched_at, ttl_days, staleness_score),
    )


def test_on_retrieved_dedupes_stale_and_refresh_events(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _insert_stale_row(conn, "h1")
    monkeypatch.setattr(hmod, "load_config", lambda *a, **k: _cfg())

    evt = SimpleNamespace(type="retrieved", payload={"hashes": ["h1"], "query": "q", "count": 1}, id=1)
    hmod.on_retrieved(conn, evt)
    hmod.on_retrieved(conn, evt)  # second qualifying retrieval should not re-emit

    assert conn.execute("SELECT count(*) FROM events WHERE type='stale_marked'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM events WHERE type='refresh_requested'").fetchone()[0] == 1


def test_on_retrieved_staleness_score_is_monotonic(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    # Existing high score should not be reduced by a lower newly computed score.
    _insert_stale_row(conn, "h1", source_url=None, staleness_score=0.9, ttl_days=100_000)
    monkeypatch.setattr(hmod, "load_config", lambda *a, **k: _cfg(threshold=0.01))

    evt = SimpleNamespace(type="retrieved", payload={"hashes": ["h1"], "query": "q", "count": 1}, id=1)
    hmod.on_retrieved(conn, evt)

    score = conn.execute("SELECT staleness_score FROM content WHERE hash='h1'").fetchone()[0]
    assert score == pytest.approx(0.9)


def test_tick_backoff_exponential_and_resets_after_success(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(rmod, "_read_cursor", lambda _conn: 0)
    monkeypatch.setattr(rmod, "_SHUTDOWN_REQUESTED", False)

    attempts = {"n": 0}

    def _flaky_since(_conn, _cursor, *, types=None, yield_poison=True):
        attempts["n"] += 1
        if attempts["n"] in (1, 2, 4):
            raise RuntimeError("tick failure")
        return []

    sleeps: list[float] = []

    def _sleep(seconds: float):
        sleeps.append(seconds)
        if len(sleeps) >= 4:
            raise _StopLoop

    monkeypatch.setattr(rmod.event_log, "since", _flaky_since)
    monkeypatch.setattr(rmod.time, "sleep", _sleep)

    with pytest.raises(_StopLoop):
        rmod._run_loop(conn)

    # Fail, fail, success, fail -> sleep durations: 1, 2, 1, 1 (reset after success).
    assert sleeps == [1.0, 2.0, 1.0, 1.0]


def test_install_signal_handlers_and_graceful_shutdown(monkeypatch):
    signals = []

    def _record_signal(sig, handler):
        signals.append((sig, handler))

    monkeypatch.setattr(rmod.signal, "signal", _record_signal)
    rmod.install_signal_handlers()

    assert [sig for sig, _ in signals] == [rmod.signal.SIGINT, rmod.signal.SIGTERM]
    assert all(handler is rmod.request_shutdown for _, handler in signals)

    # Signal-triggered run-loop exit still closes the owned connection.
    spy = _SpyConn()
    monkeypatch.setattr(rmod, "get_connection", lambda: spy)

    def _loop_and_shutdown(_conn):
        rmod.request_shutdown()

    monkeypatch.setattr(rmod, "_run_loop", _loop_and_shutdown)
    rmod.run()
    assert spy.closed is True


def test_reactor_main_installs_handlers_then_runs(monkeypatch):
    from engram.reactor import __main__ as mainmod

    calls = []
    monkeypatch.setattr(mainmod, "install_signal_handlers", lambda: calls.append("handlers"))
    monkeypatch.setattr(mainmod, "run", lambda: calls.append("run"))

    mainmod.main()

    assert calls == ["handlers", "run"]
