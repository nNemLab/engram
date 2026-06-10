"""Shared fixtures for rag-core tests."""
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCHEMA = ("001_initial.sql", "002_sources_and_revisions.sql", "003_grounding.sql")


def fresh_conn(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "t.sqlite")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    for fn in SCHEMA:
        c.executescript((REPO / "schema" / fn).read_text())
    return c
