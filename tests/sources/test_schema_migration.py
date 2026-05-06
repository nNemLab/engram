"""Schema migration 002: sources table + content revision columns."""
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_001 = REPO_ROOT / "schema" / "001_initial.sql"
SCHEMA_002 = REPO_ROOT / "schema" / "002_sources_and_revisions.sql"


def _apply(conn, sql_path):
    conn.executescript(sql_path.read_text())


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c, SCHEMA_001)
    _apply(c, SCHEMA_002)
    yield c
    c.close()


def test_sources_table_exists(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    expected = {
        "id", "name", "adapter", "url", "config", "schedule",
        "source_tier", "paused", "next_poll_at", "last_polled_at",
        "last_success_at", "cursor", "error_count", "last_error",
        "created_at", "updated_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_sources_due_index_exists(conn):
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='sources'"
    )}
    assert "idx_sources_due" in idx


def test_content_has_revision_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(content)")}
    assert {"revision", "is_current", "superseded_by", "source_id"}.issubset(cols)


def test_content_revision_default_is_one(conn):
    conn.execute(
        "INSERT INTO content (hash, body, kind, source_tier, confidence) "
        "VALUES ('h1', 'b', 'kb', 'manual', 0.9)"
    )
    row = conn.execute("SELECT revision, is_current FROM content WHERE hash='h1'").fetchone()
    assert row["revision"] == 1
    assert row["is_current"] == 1


def test_content_url_current_index_exists(conn):
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='content'"
    )}
    assert "idx_content_url_current" in idx
    assert "idx_content_source" in idx
