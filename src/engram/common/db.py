"""SQLite connection. Loads sqlite-vec, applies schema, ensures vec0 table."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from .config import load_config

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
    return conn


def init_schema(conn: sqlite3.Connection, embed_dim: int = 384) -> None:
    sql = SCHEMA_PATH.read_text()
    conn.executescript(sql)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0("
        f"content_hash TEXT PRIMARY KEY, embedding FLOAT[{embed_dim}])"
    )
    _apply_pending_migrations(conn)


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
