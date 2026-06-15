"""#83: the shared autocommit connection must be serialized across threads.

`common.db.transaction` wraps a multi-statement write in BEGIN ... COMMIT under a
process-wide RLock so that:

  * a failure mid-sequence ROLLs BACK (no half-applied writes), and
  * concurrent writers from multiple threads driving the SAME connection are
    serialized -- no interleaving and no `database is locked`.

The watcher (Timer daemon threads) and MCP server (asyncio.to_thread workers)
both share one long-lived connection, which is exactly this scenario.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from engram.common.db import LockingConnection, db_lock, transaction


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def test_transaction_rolls_back_on_error(tmp_path):
    conn = _open(tmp_path / "rollback.sqlite")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="boom"):
        with transaction(conn):
            conn.execute("INSERT INTO t (id) VALUES (1)")
            raise RuntimeError("boom")

    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_transaction_commits_on_success(tmp_path):
    conn = _open(tmp_path / "commit.sqlite")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with transaction(conn):
        conn.execute("INSERT INTO t (id) VALUES (1)")
        conn.execute("INSERT INTO t (id) VALUES (2)")

    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_concurrent_writers_are_serialized_without_database_locked(tmp_path):
    """Many threads hammer one shared connection through `transaction`.

    Each write reads the current row count then inserts a row stamped with that
    pre-insert count. If two transactions interleaved, two rows would carry the
    same stamp; if the connection raced, sqlite would raise `database is locked`
    or `cannot start a transaction within a transaction`. With the lock, the
    stamps form the exact contiguous sequence 0..N-1 and nothing raises.
    """
    conn = _open(tmp_path / "concurrent.sqlite")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, stamp INTEGER)")

    n_threads = 8
    writes_per_thread = 25
    total = n_threads * writes_per_thread
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()  # maximize contention: all threads start together
            for _ in range(writes_per_thread):
                with transaction(conn):
                    stamp = conn.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"]
                    conn.execute("INSERT INTO t (stamp) VALUES (?)", (stamp,))
        except BaseException as exc:  # noqa: BLE001 - surface any thread failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"writer threads raised: {errors!r}"
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == total
    stamps = [r["stamp"] for r in conn.execute("SELECT stamp FROM t ORDER BY id")]
    # No interleaving: each pre-insert count is unique and contiguous.
    assert sorted(stamps) == list(range(total))


# --- #113: narrowed lock via LockingConnection ------------------------------
#
# PR #112 closed the cross-thread race by holding the process-wide DB lock
# around the ENTIRE MCP tool handler, which also serialized handlers' non-DB
# work (network fetch, subprocess) -- a throughput regression. `LockingConnection`
# narrows the lock to the DB-touching regions: every access to the shared
# connection still runs under the lock and each statement is fully drained while
# the lock is held (so a cursor is never stepped outside the lock -- the #112
# invariant), but the lock is free between statements so non-DB work overlaps.


def test_locking_connection_serializes_concurrent_statements(tmp_path):
    """Bare single-statement reads/writes through the proxy stay serialized.

    Handlers issue bare `conn.execute(...)` (no `transaction()`); the proxy must
    still serialize every C-API access so concurrent threads never race into
    `database is locked` and every write lands. Each thread writes a disjoint id
    range, so the only way a row goes missing is a dropped/raced statement.
    """
    raw = _open(tmp_path / "proxy.sqlite")
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn = LockingConnection(raw)

    n_threads = 8
    per_thread = 25
    total = n_threads * per_thread
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker(base: int) -> None:
        try:
            barrier.wait()  # maximize contention
            for i in range(per_thread):
                conn.execute("INSERT INTO t (id) VALUES (?)", (base + i,))
                conn.execute("SELECT COUNT(*) FROM t").fetchone()
        except BaseException as exc:  # noqa: BLE001 - surface any thread failure
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(k * per_thread,)) for k in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"proxy access raised under concurrency: {errors!r}"
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == total


def test_transaction_through_locking_connection_is_atomic(tmp_path):
    """`transaction()` still serializes multi-statement RMW through the proxy.

    The proxy takes the same reentrant RLock `transaction()` does, so a
    transaction opened on a proxied connection re-enters without deadlock and
    the read-modify-write stays atomic: stamps form the exact contiguous
    sequence 0..N-1 with no interleaving and nothing raises.
    """
    raw = _open(tmp_path / "proxy_txn.sqlite")
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, stamp INTEGER)")
    conn = LockingConnection(raw)

    n_threads = 8
    writes_per_thread = 25
    total = n_threads * writes_per_thread
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(writes_per_thread):
                with transaction(conn):
                    stamp = conn.execute("SELECT COUNT(*) AS c FROM t").fetchone()["c"]
                    conn.execute("INSERT INTO t (stamp) VALUES (?)", (stamp,))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"writer threads raised: {errors!r}"
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == total
    stamps = [r["stamp"] for r in conn.execute("SELECT stamp FROM t ORDER BY id")]
    assert sorted(stamps) == list(range(total))


def test_locking_connection_releases_lock_between_statements(tmp_path):
    """Non-DB work between DB calls runs with the lock free (#113).

    Two threads each touch the DB, do non-DB work (sleep) *without* the lock,
    then touch the DB again. Under #112's whole-handler lock the sleeps would
    serialize (~2*delay); with the narrowed lock they overlap (~1*delay).
    """
    raw = _open(tmp_path / "proxy_overlap.sqlite")
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn = LockingConnection(raw)

    delay = 0.3
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            conn.execute("INSERT INTO t (id) VALUES (?)", (i,))  # DB touch (locked)
            barrier.wait()
            time.sleep(delay)  # simulated non-DB handler work -- must not hold the lock
            conn.execute("SELECT COUNT(*) FROM t").fetchone()  # DB touch (locked)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    assert errors == [], f"proxy access raised: {errors!r}"
    # Serialized non-DB work would take >= 2*delay; overlap stays well under it.
    assert elapsed < delay * 1.8, f"non-DB work was serialized (elapsed={elapsed:.3f}s)"


def test_locking_connection_lock_is_free_during_non_db_work(tmp_path):
    """While a thread is between DB statements, another thread can take the lock.

    A direct (non-timing) check of the same property: the proxy does not hold
    the process-wide DB lock across non-DB regions, so an independent thread can
    acquire it.
    """
    raw = _open(tmp_path / "proxy_free.sqlite")
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn = LockingConnection(raw)

    conn.execute("INSERT INTO t (id) VALUES (1)")  # a DB statement just completed
    # The lock must be released now -- a fresh thread can grab it without blocking.
    acquired: list[bool] = []

    def grabber() -> None:
        lock = db_lock()
        got = lock.acquire(blocking=False)
        acquired.append(got)
        if got:
            lock.release()

    t = threading.Thread(target=grabber)
    t.start()
    t.join()
    assert acquired == [True]


# --- #114: nested transaction() ownership -----------------------------------
#
# `transaction()` gates COMMIT/ROLLBACK on `own = not conn.in_transaction`, so an
# inner scope that joins an outer transaction never commits or rolls it back.
# These tests exercise that path explicitly: the production connection uses
# `isolation_level=None`, so no implicit transaction is ever open — the only way
# to have a pre-existing transaction is to enter an outer `with transaction()`
# first.


def test_nested_transaction_inner_does_not_commit_outer(tmp_path):
    """Inner `transaction(conn)` joins the outer; only the outer commits.

    An outer scope opens a transaction, writes rows, then enters an inner
    `transaction()` that also succeeds. The inner scope's COMMIT/ROLLBACK are
    guarded by `own = False`, so neither fires — the inner scope leaves the
    outer's transaction intact, and the outer scope still controls whether the
    data is committed.
    """
    conn = _open(tmp_path / "nested.sqlite")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    # Enter outer transaction (own=True → issues BEGIN).
    with transaction(conn):
        conn.execute("INSERT INTO t (id) VALUES (10)")

        # Inner transaction joins (own=False → no BEGIN issued).
        with transaction(conn):
            conn.execute("INSERT INTO t (id) VALUES (20)")
            # Inner succeeds → no COMMIT issued (own=False).  Outer still open.

        # Back in outer scope — inner did not commit, outer still controls.

    # Now the outer scope commits: both rows are durable.
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_nested_transaction_inner_exception_does_not_rollback_outer(tmp_path):
    """Inner `transaction(conn)` raises — it must NOT roll back the outer's work.

    The outer scope has already written rows under its own `BEGIN`. When the
    inner scope raises, its exception handler sees `own = False` and does NOT
    issue ROLLBACK; the outer scope retains full control of the transaction.
    """
    conn = _open(tmp_path / "nested_raise.sqlite")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with transaction(conn):
        conn.execute("INSERT INTO t (id) VALUES (30)")
        inner_raised = False
        try:
            with transaction(conn):
                conn.execute("INSERT INTO t (id) VALUES (40)")
                raise RuntimeError("inner boom")
        except RuntimeError:
            inner_raised = True

        assert inner_raised
        # Outer scope's rows must still be there — the inner error did NOT
        # roll back the outer's transaction.  Both rows are visible within
        # the still-open outer transaction.
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2

    # Outer scope commits: both rows are durable.
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2

    # A fresh connection confirms: both ids 30 and 40 survived.
    fresh = _open(tmp_path / "nested_raise.sqlite")
    ids = [r[0] for r in fresh.execute("SELECT id FROM t ORDER BY id").fetchall()]
    assert ids == [30, 40]


def test_nested_transaction_inner_exception_outer_rollback(tmp_path):
    """Outer `transaction(conn)` rolls back on its own error — data never commits.

    Confirms the full outer-on-error path: the outer scope raises and its
    ROLLBACK clears all its work, including rows written inside a nested inner
    scope that never committed.
    """
    conn = _open(tmp_path / "nested_outer_rb.sqlite")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="outer boom"):
        with transaction(conn):
            conn.execute("INSERT INTO t (id) VALUES (50)")
            with transaction(conn):
                conn.execute("INSERT INTO t (id) VALUES (60)")
            raise RuntimeError("outer boom")

    # Both rows — outer's AND inner's — were rolled back.
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_top_level_transaction_commits_on_success(tmp_path):
    """A top-level (own=True) transaction commits normally — baseline."""
    conn = _open(tmp_path / "top_level_commit.sqlite")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with transaction(conn):
        conn.execute("INSERT INTO t (id) VALUES (100)")

    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_top_level_transaction_rolls_back_on_error(tmp_path):
    """A top-level (own=True) transaction rolls back normally — baseline."""
    conn = _open(tmp_path / "top_level_rb.sqlite")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="boom"):
        with transaction(conn):
            conn.execute("INSERT INTO t (id) VALUES (200)")
            raise RuntimeError("boom")

    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_nested_transaction_through_locking_connection(tmp_path):
    """Nested `transaction(conn)` with a `LockingConnection` proxy follows same rules.

    Repeats the ownership semantics under the proxy that real tool handlers use,
    ensuring the inner scope does not commit or roll back the outer's work.
    """
    raw = _open(tmp_path / "nested_proxy.sqlite")
    raw.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn = LockingConnection(raw)

    with transaction(conn):
        conn.execute("INSERT INTO t (id) VALUES (70)")

        with transaction(conn):
            conn.execute("INSERT INTO t (id) VALUES (80)")
            # Inner succeeds → no COMMIT issued (own=False).  Outer still open.

        # Back in outer scope — inner did not commit, outer still controls.

    # Outer scope commits: both rows visible.
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
