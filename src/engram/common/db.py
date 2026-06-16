"""SQLite connection. Loads sqlite-vec, applies schema, ensures vec0 table."""
from __future__ import annotations

import fcntl
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


def open_readonly_connection(path: Path) -> sqlite3.Connection:
    """Open a short-lived worker-thread connection for read-heavy daemon paths.

    This applies the same sqlite setup as the daemon's main connection
    (sqlite-vec loaded, Row factory, WAL/foreign_keys/synchronous/busy_timeout)
    but intentionally does NOT run schema init/migrations. Callers use it for
    request-scoped reads against an already-initialized database.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
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


def _connect(db_path: Path) -> sqlite3.Connection:
    return open_readonly_connection(db_path)


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


# Serializes migration apply between threads of THIS process when the database
# has no on-disk file to flock (an in-memory/temp DB). For a real file the
# cross-process flock below already excludes concurrent threads too (each open()
# is a distinct open file description), so this is only the in-memory fallback.
_MIGRATION_THREAD_LOCK = threading.Lock()


# Matches an SQL line comment (`-- ...` to EOL) or block comment (`/* ... */`).
# Used ONLY to decide whether trailing post-last-`;` leftover is droppable
# comments vs. a genuine unterminated statement -- NOT for statement splitting,
# which defers entirely to sqlite3.complete_statement below.
_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _is_blank_or_comments(text: str) -> bool:
    """True if `text` holds only whitespace and SQL comments (no executable token)."""
    return not _SQL_COMMENT_RE.sub("", text).strip()


def _split_sql_statements(script: str) -> list[str]:
    """Split a migration script into individually-executable statements.

    `conn.executescript` implicitly COMMITs before it runs and wraps nothing, so
    a multi-statement migration that fails partway leaves the earlier statements
    committed (#160). To apply a migration atomically we run each statement via
    `conn.execute` inside one explicit BEGIN/COMMIT, which means splitting the
    script into single statements first.

    Statement boundaries come from SQLite's own completeness rule
    (`sqlite3.complete_statement`), NOT a hand-rolled `;` scanner. That matters
    because a `;` is not a terminator inside string literals, double-quoted
    identifiers, `--`/`/* */` comments, AND -- crucially -- inside a compound
    `CREATE TRIGGER ... BEGIN <body; with; internal; semicolons> END;` (or
    `CREATE VIEW`) body. complete_statement understands all of these, so a
    future trigger/view migration is kept intact instead of mis-split into
    broken fragments. We append characters and close a statement the moment the
    buffer (which just ended at a `;`) is a complete statement, so each returned
    chunk is exactly one statement, its comments included and `strip`ped.

    Trailing content after the final `;` that is only whitespace/comments (a
    license header, a closing remark) is dropped. Any other leftover is an
    unterminated statement and raises ValueError rather than being silently
    dropped, so a malformed migration fails loudly.
    """
    statements: list[str] = []
    buf = ""
    for ch in script:
        buf += ch
        # A statement can only complete at a `;`; gate the (relatively costly)
        # completeness check on that so we don't call it per character.
        if ch == ";" and sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip() and not _is_blank_or_comments(buf):
        raise ValueError(
            "migration script ends with an unterminated SQL statement "
            f"(missing ';'): {buf.strip()[:120]!r}"
        )
    return statements


def _pending_migrations(conn: sqlite3.Connection) -> list[tuple[int, Path]]:
    """Migration files whose version is past the highest applied, sorted ascending."""
    current = _db_schema_version(conn)
    pending: list[tuple[int, Path]] = []
    for p in sorted(SCHEMA_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        try:
            version = int(p.name.split("_", 1)[0])
        except ValueError:
            continue
        if version > current:
            pending.append((version, p))
    return pending


def _apply_one_migration(conn: sqlite3.Connection, path: Path) -> None:
    """Apply one migration file atomically: all statements + its version row, or none.

    Replaces `executescript` (which auto-commits each statement) with explicit
    statement-by-statement execution inside one BEGIN/COMMIT, ROLLBACK on any
    error. The migration's own `INSERT ... schema_version` is part of the same
    transaction, so version and schema advance together or not at all (#160).
    """
    statements = _split_sql_statements(path.read_text())
    conn.execute("BEGIN")
    try:
        for stmt in statements:
            conn.execute(stmt)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migration_lock_path(conn: sqlite3.Connection) -> Path | None:
    """Lock-file path beside the main database file, or None for an in-memory DB."""
    row = conn.execute("PRAGMA database_list").fetchone()
    main_file = row[2] if row else ""  # (seq, name, file)
    if not main_file:  # :memory: / temp DB -- no cross-process file to guard
        return None
    return Path(main_file + ".migrate.lock")


@contextmanager
def _migration_lock(conn: sqlite3.Connection) -> Iterator[None]:
    """Hold an exclusive lock guarding migration apply across processes (#160).

    `connect`/`get_connection` run `_apply_pending_migrations` on EVERY open, and
    8+ daemon entry points open connections at startup. On the first launch
    after a version bump they all see the same pending migration and would race
    to apply it -- the non-idempotent `ALTER ... ADD COLUMN` then fails with
    'duplicate column' for every loser. An exclusive `flock` on a lock file
    beside the database serializes the apply across processes (and across this
    process's threads, since each open file description flocks independently);
    callers re-read `schema_version` inside the lock so the loser applies
    nothing. In-memory/temp DBs have no file -- fall back to a process-wide
    thread lock (such DBs are never shared across processes anyway).
    """
    path = _migration_lock_path(conn)
    if path is None:
        with _MIGRATION_THREAD_LOCK:
            yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _apply_pending_migrations(conn: sqlite3.Connection) -> None:
    """Apply any schema/NNN_*.sql files past the highest applied version.

    Migration files are NOT idempotent in general (SQLite has no `ADD COLUMN
    IF NOT EXISTS`), so we gate strictly on the schema_version table; each
    migration inserts its own version row. Two invariants (#160):

    * **Atomic** -- each file applies all-or-nothing (`_apply_one_migration`).
    * **Cross-process exclusive** -- the version check + apply runs under an
      exclusive lock, with `schema_version` RE-READ inside the lock, so racing
      opens never double-apply the same migration.
    """
    # Cheap pre-check outside the lock: skip the lock entirely on the common
    # already-migrated path. Re-checked under the lock below, so a process that
    # passes this check but loses the race still applies nothing.
    if not _pending_migrations(conn):
        return
    with _migration_lock(conn):
        for _version, path in _pending_migrations(conn):
            _apply_one_migration(conn, path)


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
    Only the exact value "1" disables the guard; "0"/"false"/anything else leaves
    it armed, so a stray non-empty value can't silently bypass the check.
    """
    if os.environ.get(_SKIP_VERSION_CHECK) == "1":
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
