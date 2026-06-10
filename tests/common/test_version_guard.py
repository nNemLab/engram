"""Schema/version compatibility guard (#43 detection + version management)."""
from pathlib import Path

import pytest

from engram.common import db as dbmod
from engram.common.db import (
    IncompatibleDatabaseError,
    _code_schema_version,
    _db_schema_version,
    _embeddings_table_dim,
    init_schema,
)


def _fresh(tmp_path, embed_dim=384):
    conn = dbmod._connect(Path(tmp_path) / "db.sqlite")
    init_schema(conn, embed_dim)  # runs the guard; should not raise on a fresh DB
    return conn


def test_code_schema_version_tracks_files(tmp_path):
    # The repo ships through migration 004; the helper must reflect that.
    assert _code_schema_version() >= 4


def test_fresh_db_passes(tmp_path):
    conn = _fresh(tmp_path)
    assert _db_schema_version(conn) == _code_schema_version()
    assert _embeddings_table_dim(conn) == 384


def test_db_ahead_of_code_is_refused(tmp_path):
    conn = _fresh(tmp_path)
    # Simulate a database migrated by a newer engram.
    conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                 (_code_schema_version() + 5,))
    with pytest.raises(IncompatibleDatabaseError, match="newer version"):
        init_schema(conn, 384)


def test_embed_dim_mismatch_is_refused(tmp_path):
    conn = _fresh(tmp_path, embed_dim=384)
    # Re-init with a different configured dim; the existing vec0 table stays 384.
    with pytest.raises(IncompatibleDatabaseError, match="dimension mismatch"):
        init_schema(conn, 768)


def test_skip_env_bypasses_guard(tmp_path, monkeypatch):
    conn = _fresh(tmp_path)
    conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                 (_code_schema_version() + 5,))
    monkeypatch.setenv("ENGRAM_SKIP_VERSION_CHECK", "1")
    init_schema(conn, 384)  # bypassed: no raise


def test_embeddings_dim_none_when_table_absent(tmp_path):
    conn = dbmod._connect(Path(tmp_path) / "empty.sqlite")
    assert _embeddings_table_dim(conn) is None
