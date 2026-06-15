"""#83: multi-statement write paths in the dedup gate must be atomic.

The shared connection is autocommit (isolation_level=None), so `conn.commit()`
is a no-op and each statement would commit on its own. A failure partway through
a supersede sequence must therefore ROLL BACK, leaving exactly ONE is_current
row for the source_url -- never two (the new + the not-yet-cleared old) and never
zero. These tests inject a failure at the event-append step (the last statement
of each sequence) and assert the invariant holds.
"""
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply_schema(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql",
               "003_grounding.sql", "004_protected.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply_schema(c)
    from types import SimpleNamespace

    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    yield c


def _current_rows(conn, url):
    return conn.execute(
        "SELECT hash FROM content WHERE source_url = ? AND is_current = 1 AND tombstoned = 0",
        (url,),
    ).fetchall()


def test_supersede_failure_rolls_back_to_exactly_one_current(conn, monkeypatch):
    from engram import dedup

    url = "https://example.com/page"
    r1 = dedup.gate(conn, body="v1", source_url=url, kind="research",
                    source_tier="vendor-doc")

    # Inject a failure at the final (event-append) step of the supersede sequence.
    def boom(*a, **k):
        raise RuntimeError("injected mid-supersede failure")

    monkeypatch.setattr(dedup.event_log, "append", boom)
    with pytest.raises(RuntimeError, match="injected"):
        dedup.gate(conn, body="v2 changed", source_url=url, kind="research",
                   source_tier="vendor-doc")

    # ROLLBACK: exactly one current row, and it is still the original v1.
    current = _current_rows(conn, url)
    assert len(current) == 1
    assert current[0]["hash"] == r1.hash
    # The new revision was never persisted, and the old row was not demoted.
    assert conn.execute(
        "SELECT COUNT(*) FROM content WHERE source_url = ?", (url,)
    ).fetchone()[0] == 1
    old = conn.execute(
        "SELECT is_current, superseded_by FROM content WHERE hash = ?", (r1.hash,)
    ).fetchone()
    assert old["is_current"] == 1
    assert old["superseded_by"] is None


def test_supersede_success_still_leaves_one_current(conn):
    """Sanity: the happy path remains a clean single-current swap."""
    from engram import dedup

    url = "https://example.com/page"
    dedup.gate(conn, body="v1", source_url=url, kind="research", source_tier="vendor-doc")
    r2 = dedup.gate(conn, body="v2", source_url=url, kind="research", source_tier="vendor-doc")
    current = _current_rows(conn, url)
    assert len(current) == 1
    assert current[0]["hash"] == r2.hash


def _blocked_state(conn, *, url="https://x/p"):
    """Drive a protected row + blocked upstream supersede; return (human, upstream)."""
    from engram.dedup import content_hash, gate

    h_human = content_hash("human edit")
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current, protected) "
        "VALUES (?, 'human edit', 'T', ?, 'vendor-doc', 0.7, 'research', 1, 1, 1)",
        (h_human, url),
    )
    r = gate(conn, body="new upstream bytes", source_url=url,
             source_tier="vendor-doc", kind="research", actor="poller")
    assert r.outcome == "supersede_blocked"
    return h_human, content_hash("new upstream bytes")


def test_resolve_accept_upstream_failure_rolls_back_to_one_current(conn, monkeypatch):
    from engram import dedup

    url = "https://x/p"
    h_human, h_up = _blocked_state(conn, url=url)

    def boom(*a, **k):
        raise RuntimeError("injected mid-resolve failure")

    monkeypatch.setattr(dedup.event_log, "append", boom)
    with pytest.raises(RuntimeError, match="injected"):
        dedup.resolve_supersede(conn, h_human, "accept_upstream", actor="human")

    # ROLLBACK: human row stays current+protected, upstream stays non-current,
    # so there is still exactly one current row for the url (never two, never zero).
    current = _current_rows(conn, url)
    assert len(current) == 1
    assert current[0]["hash"] == h_human
    human = conn.execute(
        "SELECT is_current, protected, superseded_by FROM content WHERE hash = ?",
        (h_human,),
    ).fetchone()
    assert human["is_current"] == 1
    assert human["protected"] == 1
    assert human["superseded_by"] is None
    up = conn.execute("SELECT is_current FROM content WHERE hash = ?", (h_up,)).fetchone()
    assert up["is_current"] == 0
    # Contradiction was not marked resolved.
    contradiction = conn.execute(
        "SELECT resolved FROM contradictions WHERE hash_a = ?", (h_human,)
    ).fetchone()
    assert contradiction["resolved"] == 0


def test_supersede_blocked_failure_rolls_back(conn, monkeypatch):
    """The protected branch (insert non-current revision + record contradiction)
    must also be atomic: a failure leaves no orphan revision and no contradiction."""
    from engram import dedup

    url = "https://x/blocked"
    h_human = dedup.content_hash("human edit")
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current, protected) "
        "VALUES (?, 'human edit', 'T', ?, 'vendor-doc', 0.7, 'research', 1, 1, 1)",
        (h_human, url),
    )

    def boom(*a, **k):
        raise RuntimeError("injected mid-block failure")

    monkeypatch.setattr(dedup.event_log, "append", boom)
    with pytest.raises(RuntimeError, match="injected"):
        dedup.gate(conn, body="new upstream bytes", source_url=url,
                   source_tier="vendor-doc", kind="research", actor="poller")

    # Only the original human row exists; no orphan non-current revision, no contradiction.
    assert conn.execute(
        "SELECT COUNT(*) FROM content WHERE source_url = ?", (url,)
    ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM contradictions").fetchone()[0] == 0
    assert _current_rows(conn, url)[0]["hash"] == h_human
