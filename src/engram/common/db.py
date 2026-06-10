"""SQLite connection. Loads sqlite-vec, applies schema, ensures vec0 table."""
from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from .. import __version__
from .config import load_config

# Set to bypass the compatibility guard (recovery only — may corrupt data).
_SKIP_VERSION_CHECK = "ENGRAM_SKIP_VERSION_CHECK"


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
    return conn


def init_schema(conn: sqlite3.Connection, embed_dim: int = 384) -> None:
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
