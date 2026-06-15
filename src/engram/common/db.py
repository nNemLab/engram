"""SQLite connection. Loads sqlite-vec, applies schema, ensures vec0 table."""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from .. import __version__
from .config import load_config

# Set to bypass the compatibility guard (recovery only — may corrupt data).
_SKIP_VERSION_CHECK = "ENGRAM_SKIP_VERSION_CHECK"

# Process-wide lock serializing every access to the shared long-lived connection
# (#83). The connection opened by `get_connection` is shared across threads
# (check_same_thread=False): the MCP server fans tool calls out over
# `asyncio.to_thread`, and the watcher debouncer fires from `threading.Timer`
# daemon threads. A single sqlite connection has ONE transaction state, so
# concurrent use lets one thread's autocommit write land inside another thread's
# open BEGIN, and concurrent writers race into `database is locked`. Holding this
# lock around each logical operation (see `db_lock`) and wrapping multi-statement
# writes in `transaction` serializes all access. RLock so a logical op that has
# already taken the lock can still open a `transaction` without deadlocking.
_DB_LOCK = threading.RLock()


def db_lock() -> threading.RLock:
    """The process-wide lock serializing access to the shared connection (#83).

    Acquire it around a whole logical operation that touches the shared
    connection from a worker/daemon thread (MCP tool dispatch, watcher change
    handling) so no two threads ever drive the single connection concurrently.
    """
    return _DB_LOCK


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a multi-statement write atomically under the shared DB lock (#83).

    The shared connection is opened in autocommit mode (`isolation_level=None`),
    so `conn.commit()` is a no-op and each statement commits on its own — a crash
    or error mid-sequence can leave invariants half-applied (e.g. two `is_current`
    rows for one `source_url`). This wraps the sequence in an explicit
    BEGIN ... COMMIT and ROLLBACKs on any error, mirroring the pattern in
    `maintenance.reembed`. The lock is held for the whole transaction so another
    thread's writes can't interleave into this connection's open transaction.

    If a transaction is already open on the connection (a caller's outer
    transaction, or sqlite3's legacy implicit-transaction mode), this joins it
    rather than issuing a nested BEGIN — which SQLite forbids — leaving the
    outermost scope to commit or roll back. On the production connection
    (`isolation_level=None`) no transaction is ever implicitly open, so this
    always drives a real BEGIN/COMMIT/ROLLBACK.
    """
    with _DB_LOCK:
        own = not conn.in_transaction
        if own:
            conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            if own:
                conn.execute("ROLLBACK")
            raise
        else:
            if own:
                conn.execute("COMMIT")


class _LockingCursor:
    """A fully-buffered view of one statement's result.

    `LockingConnection` runs each statement to completion and drains its rows
    while holding the DB lock, then hands back this cursor. Because the cursor is
    never stepped against SQLite outside the lock, no other thread's statement
    can interleave with a half-iterated result on the shared connection (the race
    #112 closed) -- while the lock is free between statements so a handler's
    non-DB work runs concurrently (#113). It mirrors the slice of the
    `sqlite3.Cursor` API the codebase actually uses against the shared
    connection: row iteration, `fetchone`/`fetchmany`/`fetchall`, and the
    `lastrowid`/`rowcount`/`description` attributes.
    """

    def __init__(
        self,
        rows: list,
        *,
        lastrowid: int | None,
        rowcount: int,
        description: object,
    ) -> None:
        self._rows = rows
        self._pos = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount
        self.description = description

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int | None = None):
        if size is None:
            size = len(self._rows) - self._pos
        end = min(self._pos + max(size, 0), len(self._rows))
        rows = self._rows[self._pos:end]
        self._pos = end
        return rows

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        if self._pos >= len(self._rows):
            raise StopIteration
        row = self._rows[self._pos]
        self._pos += 1
        return row


def _drain(cur: sqlite3.Cursor) -> _LockingCursor:
    """Materialize a freshly-executed cursor's rows + metadata (lock held)."""
    return _LockingCursor(
        cur.fetchall(),
        lastrowid=cur.lastrowid,
        rowcount=cur.rowcount,
        description=cur.description,
    )


class LockingConnection:
    """Lock-serialized proxy over the shared SQLite connection (#83, #113).

    The MCP server shares one autocommit connection across `asyncio.to_thread`
    worker threads. A single sqlite3 connection has one statement/transaction
    state, so two threads driving it at once interleave writes and race into
    `database is locked` (#83). PR #112 closed that by holding the process-wide
    DB lock around the ENTIRE tool handler -- but that also serialized the
    handlers' non-DB work (research network fetches, playbook subprocesses), a
    throughput regression (#113).

    This proxy narrows the lock to the DB-touching regions. Every access to the
    underlying connection -- reads AND writes, not just `transaction()` -- runs
    under the process-wide lock, and each statement is run to completion and its
    rows drained while the lock is held (see `_drain`), so a cursor is never
    stepped against SQLite outside the lock. Between statements the lock is free,
    so non-DB work in a handler runs concurrently and tool calls overlap. The
    lock is the same reentrant RLock `transaction()` takes, so a `transaction()`
    opened inside a handler (which then runs its own statements through this
    proxy) re-enters without deadlock.

    Hand every tool handler this proxy rather than the bare connection so that
    no code path can touch the shared connection without the lock.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql, parameters=(), /) -> _LockingCursor:
        with _DB_LOCK:
            return _drain(self._conn.execute(sql, parameters))

    def executemany(self, sql, seq_of_parameters, /) -> _LockingCursor:
        with _DB_LOCK:
            return _drain(self._conn.executemany(sql, seq_of_parameters))

    def executescript(self, sql_script, /) -> _LockingCursor:
        with _DB_LOCK:
            return _drain(self._conn.executescript(sql_script))

    def commit(self) -> None:
        with _DB_LOCK:
            self._conn.commit()

    def rollback(self) -> None:
        with _DB_LOCK:
            self._conn.rollback()

    def close(self) -> None:
        with _DB_LOCK:
            self._conn.close()

    @property
    def in_transaction(self) -> bool:
        with _DB_LOCK:
            return self._conn.in_transaction

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self._conn.row_factory = value


class IncompatibleDatabaseError(RuntimeError):
    """The on-disk database is incompatible with this build of engram."""

# Schema ships inside the wheel at engram/schema (force-included in pyproject) for
# non-editable installs (e.g. the Docker image); editable/dev installs fall back to
# the repo-root schema/ dir.
_PKG_SCHEMA = Path(__file__).resolve().parent.parent / "schema"
_REPO_SCHEMA = Path(__file__).resolve().parents[3] / "schema"
SCHEMA_DIR = _PKG_SCHEMA if _PKG_SCHEMA.exists() else _REPO_SCHEMA
SCHEMA_PATH = SCHEMA_DIR / "001_initial.sql"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    # Absorb cross-process writer contention (MCP server vs. watcher/daemons each
    # hold their own connection to the same file): wait for the WAL write lock
    # instead of immediately raising `database is locked` (#83).
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection, embed_dim: int = 384) -> None:
    """Apply the base schema and vec0 embeddings table.

    The embed_dim guard is authoritative in config.py (RagConfig.__post_init__),
    but we repeat the sanity check here as defense-in-depth so that any
    direct callers of init_schema also get caught early.
    """
    if not isinstance(embed_dim, int) or isinstance(embed_dim, bool):
        raise ValueError(
            f"embed_dim must be a positive integer, got {embed_dim!r} "
            f"(type {type(embed_dim).__name__})."
        )
    if embed_dim <= 0 or embed_dim > 8192:
        raise ValueError(
            f"embed_dim must be between 1 and 8192, got {embed_dim}."
        )
    sql = SCHEMA_PATH.read_text()
    conn.executescript(sql)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0("
        f"content_hash TEXT PRIMARY KEY, embedding FLOAT[{embed_dim}])"
    )
    _apply_pending_migrations(conn)
    check_compatibility(conn, embed_dim)


def _apply_pending_migrations(conn: sqlite3.Connection) -> None:
    """Apply any schema/NNN_*.sql files past the highest applied version.

    Migration files are NOT idempotent in general (SQLite has no `ADD COLUMN
    IF NOT EXISTS`), so we gate strictly on the schema_version table. Each
    migration is expected to insert its own version row.
    """
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = int(row["v"] or 0) if row else 0
    pending: list[tuple[int, Path]] = []
    for p in sorted(SCHEMA_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        try:
            version = int(p.name.split("_", 1)[0])
        except ValueError:
            continue
        if version > current:
            pending.append((version, p))
    for version, path in pending:
        conn.executescript(path.read_text())


def _code_schema_version() -> int:
    """Highest migration version this build ships (from `schema/NNN_*.sql`)."""
    versions: list[int] = []
    for p in SCHEMA_DIR.glob("[0-9][0-9][0-9]_*.sql"):
        try:
            versions.append(int(p.name.split("_", 1)[0]))
        except ValueError:
            continue
    return max(versions, default=0)


def _db_schema_version(conn: sqlite3.Connection) -> int:
    """Highest schema version recorded in the database (0 if uninitialized)."""
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not has:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def _embeddings_table_dim(conn: sqlite3.Connection) -> int | None:
    """Vector dimension of the existing `embeddings` table, or None if absent."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'embeddings'").fetchone()
    if not row or not row["sql"]:
        return None
    m = re.search(r"FLOAT\[(\d+)\]", row["sql"])
    return int(m.group(1)) if m else None


def check_compatibility(conn: sqlite3.Connection, embed_dim: int) -> None:
    """Refuse to operate on a database this build can't safely handle.

    Two axes:

    * **Schema version** — if the database was migrated by a newer engram (its
      schema version exceeds the highest migration this build ships), older code
      could silently misread relocated columns. Fail loud instead of risking it.
    * **Embedding dimension** — if the configured model's dimension no longer
      matches the existing `vec0` table, retrieval is silently broken until the
      corpus is re-embedded (see issue #43).

    Bypass with `ENGRAM_SKIP_VERSION_CHECK=1` (recovery only — may corrupt data).
    """
    if os.environ.get(_SKIP_VERSION_CHECK):
        return
    code_ver = _code_schema_version()
    db_ver = _db_schema_version(conn)
    if db_ver > code_ver:
        raise IncompatibleDatabaseError(
            f"This database is at schema v{db_ver}, but engram v{__version__} only "
            f"understands schema up to v{code_ver} — it was likely created by a newer "
            f"version. Upgrade engram, or restore a compatible snapshot with `eos-restore`. "
            f"(Set {_SKIP_VERSION_CHECK}=1 to bypass — may corrupt data.)"
        )
    table_dim = _embeddings_table_dim(conn)
    if table_dim is not None and table_dim != embed_dim:
        raise IncompatibleDatabaseError(
            f"Embedding dimension mismatch: the database's vector table is {table_dim}-dim, "
            f"but the configured embedding model (rag.embed_dim={embed_dim}) produces "
            f"{embed_dim}-dim vectors. Re-embed the corpus at the new dimension (see issue "
            f"#43), or revert rag.embed_dim / rag.embed_model. "
            f"(Set {_SKIP_VERSION_CHECK}=1 to bypass.)"
        )


def version_report() -> dict:
    """Version + compatibility status for the configured database (bypasses the guard)."""
    cfg = load_config()
    report: dict = {
        "app_version": __version__,
        "schema_code": _code_schema_version(),
        "embed_dim_config": cfg.rag.embed_dim,
        "db_path": str(cfg.db_path),
        "initialized": cfg.db_path.exists(),
        "schema_db": 0,
        "embed_dim_table": None,
    }
    if not report["initialized"]:
        return report
    conn = _connect(cfg.db_path)
    try:
        report["schema_db"] = _db_schema_version(conn)
        report["embed_dim_table"] = _embeddings_table_dim(conn)
    finally:
        conn.close()
    return report


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    cfg = load_config()
    conn = _connect(cfg.db_path)
    try:
        init_schema(conn, cfg.rag.embed_dim)
        yield conn
    finally:
        conn.close()


def get_connection() -> sqlite3.Connection:
    """Long-lived connection for daemons. Caller closes."""
    cfg = load_config()
    conn = _connect(cfg.db_path)
    init_schema(conn, cfg.rag.embed_dim)
    return conn
