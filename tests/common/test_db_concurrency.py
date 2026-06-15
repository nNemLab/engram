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
from pathlib import Path

import pytest

from engram.common.db import transaction


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
