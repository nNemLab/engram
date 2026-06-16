"""#160: schema migrations apply atomically and cross-process-exclusively.

`_apply_pending_migrations` used `conn.executescript`, which implicitly COMMITs
before running and wraps nothing -- a multi-statement migration that failed
partway (e.g. schema 002's four `ALTER TABLE` + trailing version `INSERT`) left
the earlier statements committed. The next start re-ran the file and the
non-idempotent `ALTER TABLE ... ADD COLUMN` failed with 'duplicate column',
crashing the daemons. Separately, every connection open re-ran the
version-check + apply, so the 8+ daemon entry points racing on first launch
after a version bump double-applied the same migration.

These tests pin both fixes:

* **Atomic** -- each migration (its statements *and* its `schema_version` row)
  applies all-or-nothing inside one explicit BEGIN/COMMIT, ROLLBACK on any
  error. A failure leaves no partial `ALTER` and the version unchanged, so a
  re-run is deterministic rather than a spurious 'duplicate column' crash.
* **Cross-process exclusive** -- racing opens serialize behind an exclusive
  lock and re-read `schema_version` inside it, so the loser applies nothing and
  each migration runs exactly once (no 'duplicate column').
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from engram.common import db


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _apply_base(conn: sqlite3.Connection) -> None:
    """Stand in for schema 001: the version tracker + a table to migrate."""
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version INTEGER PRIMARY KEY, applied_at TEXT);"
        "CREATE TABLE IF NOT EXISTS base (id INTEGER PRIMARY KEY);"
        "INSERT OR IGNORE INTO schema_version (version) VALUES (1);"
    )


def _version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("PRAGMA table_info(base)")}


# --- statement splitter -----------------------------------------------------


def test_split_sql_statements_respects_comments_and_strings():
    """`;` inside line comments, block comments, and string literals is not a
    statement boundary; the two real statements split out intact."""
    script = (
        "-- leading comment; with a semicolon inside it\n"
        "CREATE TABLE t (a TEXT DEFAULT 'x;y');\n"
        "/* block; comment */ INSERT INTO t (a) VALUES ('p;q'); -- trailing\n"
        "\n"
    )
    stmts = db._split_sql_statements(script)
    assert len(stmts) == 2
    assert "CREATE TABLE t" in stmts[0]
    assert "INSERT INTO t" in stmts[1]


def test_split_sql_statements_handles_escaped_quotes():
    """A doubled `''` inside a string literal does not end the literal."""
    stmts = db._split_sql_statements("INSERT INTO t (a) VALUES ('it''s; fine');")
    assert len(stmts) == 1
    assert "it''s; fine" in stmts[0]


def test_split_sql_statements_drops_comment_only_fragments():
    """Comment-only / whitespace-only fragments are never handed to execute."""
    assert db._split_sql_statements("-- just a comment\n") == []
    assert db._split_sql_statements("   \n\n") == []
    assert db._split_sql_statements("") == []


def test_split_sql_statements_raises_on_unterminated_statement():
    """A trailing statement with no terminating `;` is malformed migration input
    and raises rather than being silently dropped or mis-applied."""
    with pytest.raises(ValueError, match="unterminated"):
        db._split_sql_statements("SELECT 1")
    with pytest.raises(ValueError, match="unterminated"):
        db._split_sql_statements(
            "INSERT INTO base (id) VALUES (1);\nALTER TABLE base ADD COLUMN x TEXT"
        )


def test_split_sql_statements_keeps_compound_trigger_intact():
    """A `CREATE TRIGGER ... BEGIN <body with internal ';'> END;` is ONE
    statement -- the internal semicolons must not split it."""
    script = (
        "CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER);\n"
        "CREATE TRIGGER t_ai AFTER INSERT ON t\n"
        "BEGIN\n"
        "    UPDATE t SET n = 1 WHERE id = NEW.id;\n"
        "    UPDATE t SET n = 2 WHERE id = NEW.id;\n"
        "END;\n"
    )
    stmts = db._split_sql_statements(script)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE t")
    assert stmts[1].startswith("CREATE TRIGGER t_ai")
    assert stmts[1].rstrip().endswith("END;")
    # The whole trigger body (both internal statements) is in the one chunk.
    assert stmts[1].count("UPDATE t SET") == 2


# --- atomic apply -----------------------------------------------------------


def test_failed_migration_rolls_back_fully(tmp_path, monkeypatch):
    """A migration failing partway rolls back wholesale -- no partial ALTER, the
    version row unchanged, and a re-run stays the same deterministic failure
    (not the issue's spurious 'duplicate column' crash)."""
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    monkeypatch.setattr(db, "SCHEMA_DIR", schema_dir)

    # 1st statement alters the table; the 2nd fails (duplicate column) BEFORE
    # the version row is written. executescript would commit the 1st ALTER;
    # atomic apply must undo it.
    (schema_dir / "002_add.sql").write_text(
        "-- 002: add a column, then a deliberately failing statement.\n"
        "ALTER TABLE base ADD COLUMN col_a TEXT;\n"
        "ALTER TABLE base ADD COLUMN col_a TEXT;  -- duplicate -> error\n"
        "INSERT OR IGNORE INTO schema_version (version) VALUES (2);\n"
    )

    db_path = tmp_path / "m.sqlite"
    conn = _open(db_path)
    _apply_base(conn)

    with pytest.raises(sqlite3.OperationalError):
        db._apply_pending_migrations(conn)

    assert "col_a" not in _columns(conn), "partial ALTER survived the rollback"
    assert _version(conn) == 1, "version advanced despite the failure"
    conn.close()

    # Re-run from a fresh connection: still the original failure, still clean.
    conn2 = _open(db_path)
    with pytest.raises(sqlite3.OperationalError):
        db._apply_pending_migrations(conn2)
    assert "col_a" not in _columns(conn2)
    assert _version(conn2) == 1
    conn2.close()


def test_corrected_migration_applies_after_rollback(tmp_path, monkeypatch):
    """Once the broken migration is corrected, re-run applies it cleanly to v2 --
    the earlier rollback left no residue to trip over."""
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    monkeypatch.setattr(db, "SCHEMA_DIR", schema_dir)
    migration = schema_dir / "002_add.sql"

    migration.write_text(
        "ALTER TABLE base ADD COLUMN col_a TEXT;\n"
        "ALTER TABLE base ADD COLUMN col_a TEXT;  -- duplicate -> error\n"
        "INSERT OR IGNORE INTO schema_version (version) VALUES (2);\n"
    )
    db_path = tmp_path / "fix.sqlite"
    conn = _open(db_path)
    _apply_base(conn)
    with pytest.raises(sqlite3.OperationalError):
        db._apply_pending_migrations(conn)

    # Correct the migration and re-run.
    migration.write_text(
        "ALTER TABLE base ADD COLUMN col_a TEXT;\n"
        "INSERT OR IGNORE INTO schema_version (version) VALUES (2);\n"
    )
    db._apply_pending_migrations(conn)
    assert _version(conn) == 2
    assert "col_a" in _columns(conn)
    conn.close()


def test_multi_statement_migration_commits_all_or_nothing(tmp_path, monkeypatch):
    """A valid multi-statement migration commits every statement plus its
    version row together."""
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    monkeypatch.setattr(db, "SCHEMA_DIR", schema_dir)
    (schema_dir / "002_add.sql").write_text(
        "-- 002: several columns at once.\n"
        "ALTER TABLE base ADD COLUMN col_a TEXT;\n"
        "ALTER TABLE base ADD COLUMN col_b TEXT;\n"
        "ALTER TABLE base ADD COLUMN col_c TEXT;\n"
        "INSERT OR IGNORE INTO schema_version (version) VALUES (2);\n"
    )
    db_path = tmp_path / "multi.sqlite"
    conn = _open(db_path)
    _apply_base(conn)
    db._apply_pending_migrations(conn)
    assert _version(conn) == 2
    assert {"col_a", "col_b", "col_c"} <= _columns(conn)
    conn.close()


def test_compound_trigger_migration_applies_atomically(tmp_path, monkeypatch):
    """A migration containing a compound `CREATE TRIGGER ... BEGIN ...; ...; END;`
    applies cleanly and the trigger fires -- proving the runner no longer
    mis-splits a body whose internal semicolons are not statement boundaries.
    The old hand-rolled `;` scanner would have cut the trigger into broken
    fragments and the migration would have failed."""
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    monkeypatch.setattr(db, "SCHEMA_DIR", schema_dir)
    (schema_dir / "002_trigger.sql").write_text(
        "-- 002: a compound statement whose body has internal semicolons.\n"
        "ALTER TABLE base ADD COLUMN touched INTEGER NOT NULL DEFAULT 0;\n"
        "CREATE TABLE audit (id INTEGER PRIMARY KEY, note TEXT);\n"
        "CREATE TRIGGER base_ai AFTER INSERT ON base\n"
        "BEGIN\n"
        "    INSERT INTO audit (note) VALUES ('inserted');\n"
        "    UPDATE base SET touched = 1 WHERE id = NEW.id;\n"
        "END;\n"
        "INSERT OR IGNORE INTO schema_version (version) VALUES (2);\n"
    )
    db_path = tmp_path / "trig.sqlite"
    conn = _open(db_path)
    _apply_base(conn)
    db._apply_pending_migrations(conn)

    assert _version(conn) == 2
    trig = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='base_ai'"
    ).fetchone()
    assert trig is not None, "trigger was mis-split and never created"

    # The trigger fires: inserting a row writes an audit row AND flips touched,
    # proving both internal statements of the compound body survived intact.
    conn.execute("INSERT INTO base (id) VALUES (1)")
    assert conn.execute("SELECT note FROM audit").fetchone()["note"] == "inserted"
    assert conn.execute("SELECT touched FROM base WHERE id = 1").fetchone()["touched"] == 1
    conn.close()


# --- cross-process exclusive apply ------------------------------------------


def test_concurrent_opens_apply_migration_exactly_once(tmp_path, monkeypatch):
    """Racing opens do not double-apply: the non-idempotent ADD COLUMN would
    raise 'duplicate column' on a second apply, so an error-free run with the
    version advanced exactly once proves only one opener applied it."""
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    monkeypatch.setattr(db, "SCHEMA_DIR", schema_dir)
    (schema_dir / "002_add.sql").write_text(
        "ALTER TABLE base ADD COLUMN col_a TEXT;\n"
        "INSERT OR IGNORE INTO schema_version (version) VALUES (2);\n"
    )

    db_path = tmp_path / "race.sqlite"
    seed = _open(db_path)
    _apply_base(seed)
    seed.close()

    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker() -> None:
        conn = _open(db_path)
        try:
            barrier.wait()  # maximize the race onto the pending migration
            db._apply_pending_migrations(conn)
        except BaseException as exc:  # noqa: BLE001 - surface any thread failure
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"racing opens raised (double-apply?): {errors!r}"
    check = _open(db_path)
    assert _version(check) == 2
    assert "col_a" in _columns(check)
    applied = check.execute(
        "SELECT COUNT(*) AS c FROM schema_version WHERE version = 2"
    ).fetchone()["c"]
    assert applied == 1
    check.close()


def _mp_apply(barrier, db_path_str: str) -> None:
    """Worker (separate process): open the shared DB and apply pending migrations.

    Runs in a forked child, so the monkeypatched `db.SCHEMA_DIR` is inherited. A
    raise (e.g. 'duplicate column' from a double-apply) gives a non-zero
    exitcode, which the parent asserts against.
    """
    conn = sqlite3.connect(db_path_str, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        barrier.wait()  # release all processes onto the apply together
        db._apply_pending_migrations(conn)
    finally:
        conn.close()


@pytest.mark.skipif(
    "fork" not in __import__("multiprocessing").get_all_start_methods(),
    reason="needs the fork start method to inherit the monkeypatched SCHEMA_DIR",
)
def test_concurrent_processes_apply_migration_exactly_once(tmp_path, monkeypatch):
    """True cross-process proof of the flock: separate PROCESSES racing on the
    same DB apply the non-idempotent migration exactly once. A double-apply
    would raise 'duplicate column' in a child and surface as a non-zero
    exitcode."""
    import multiprocessing as mp

    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    monkeypatch.setattr(db, "SCHEMA_DIR", schema_dir)
    (schema_dir / "002_add.sql").write_text(
        "ALTER TABLE base ADD COLUMN col_a TEXT;\n"
        "INSERT OR IGNORE INTO schema_version (version) VALUES (2);\n"
    )

    db_path = tmp_path / "race_mp.sqlite"
    seed = _open(db_path)
    _apply_base(seed)
    seed.close()

    ctx = mp.get_context("fork")
    n = 4
    barrier = ctx.Barrier(n)
    procs = [
        ctx.Process(target=_mp_apply, args=(barrier, str(db_path))) for _ in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    exitcodes = [p.exitcode for p in procs]
    assert all(code == 0 for code in exitcodes), (
        f"a racing process failed (double-apply?): exitcodes={exitcodes}"
    )
    check = _open(db_path)
    assert _version(check) == 2
    assert "col_a" in _columns(check)
    applied = check.execute(
        "SELECT COUNT(*) AS c FROM schema_version WHERE version = 2"
    ).fetchone()["c"]
    assert applied == 1
    check.close()
