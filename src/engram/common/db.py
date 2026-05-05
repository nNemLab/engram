"""SQLite connection. Loads sqlite-vec, applies schema, ensures vec0 table."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import sqlite_vec

from .config import load_config

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "001_initial.sql"


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
