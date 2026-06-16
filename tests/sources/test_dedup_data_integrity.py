"""Unit A data-integrity fixes in the dedup write path (#152, #153, #168).

Covers, against a DB carrying the new one-current-per-source_url unique index
(schema 007):

* #168 -- re-adding a tombstoned body RESURRECTS it (un-tombstoned, resurfaced,
  re-embedded/re-projected via an `ingested` event) with a distinct outcome,
  instead of the old bug where `INSERT OR IGNORE` no-op'd yet `ingested`/"new"
  was emitted and retrieval never surfaced the row.
* #153 -- the gate derives its outcome from the insert result (not a prior
  separate read), the unique partial index forbids two current rows for one
  source_url and the race loser is handled gracefully, and resolve_supersede
  aborts rather than leaving zero current rows when the upstream is missing.
* #152 -- the new-content path (insert + `ingested`) is one atomic transaction.
* schema 007 -- backfills pre-existing duplicate-current rows before creating
  the index, and applies cleanly through the migration runner.
"""
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from engram import dedup
from engram.common import db as common_db
from engram.dedup import content_hash

REPO = Path(__file__).resolve().parents[2]

SCHEMA_FILES = [
    "001_initial.sql",
    "002_sources_and_revisions.sql",
    "003_grounding.sql",
    "004_protected.sql",
    "005_event_hash_chain.sql",
    "006_reactor_dead_letter.sql",
    "007_unique_current_per_url.sql",
]


def _apply_schema(conn, files=SCHEMA_FILES):
    for fn in files:
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")
    _apply_schema(c)
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    yield c


def _current(conn, url):
    return conn.execute(
        "SELECT hash FROM content WHERE source_url = ? AND is_current = 1 AND tombstoned = 0",
        (url,),
    ).fetchall()


# --- #168: resurrection ------------------------------------------------------


def test_readding_tombstoned_body_resurrects_it(conn):
    r1 = dedup.gate(conn, body="cuda kernels note", kind="kb", confidence=0.5)
    assert r1.outcome == "new"
    h = r1.hash

    # Tombstone it, as a near-dup merge or a maintenance sweep would.
    conn.execute("UPDATE content SET tombstoned = 1 WHERE hash = ?", (h,))

    # Re-add the identical body: it must resurrect, not silently no-op.
    r2 = dedup.gate(conn, body="cuda kernels note", kind="kb", confidence=0.9)
    assert r2.outcome == "resurrected"
    assert r2.hash == h

    row = conn.execute(
        "SELECT tombstoned, is_current, confidence, fetched_at FROM content WHERE hash = ?",
        (h,),
    ).fetchone()
    assert row["tombstoned"] == 0  # un-tombstoned -> retrieval can surface it
    assert row["is_current"] == 1
    assert row["confidence"] == 0.9  # refreshed
    assert row["fetched_at"] is not None  # refreshed

    # Retrieval filters tombstoned=0; the row is now visible again.
    assert conn.execute(
        "SELECT hash FROM content WHERE hash = ? AND tombstoned = 0", (h,)
    ).fetchone() is not None

    # Only one content row exists (no phantom duplicate was inserted).
    assert conn.execute("SELECT COUNT(*) FROM content").fetchone()[0] == 1

    # An `ingested` event drives re-embedding (reactor) and re-projection (projector).
    ing = conn.execute(
        "SELECT payload FROM events WHERE type = 'ingested' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert json.loads(ing["payload"])["hash"] == h


def test_gate_rejects_invalid_inputs(conn):
    with pytest.raises(ValueError, match="non-empty"):
        dedup.gate(conn, body="   ", kind="kb")
    with pytest.raises(ValueError, match="confidence"):
        dedup.gate(conn, body="ok", kind="kb", confidence=1.2)
    with pytest.raises(ValueError, match="ttl_days"):
        dedup.gate(conn, body="ok", kind="kb", ttl_days=-1)


def test_live_exact_dup_is_not_resurrected(conn):
    """A non-tombstoned exact match stays an `exact_dup` no-op (no event)."""
    dedup.gate(conn, body="alpha body note", kind="kb")
    r2 = dedup.gate(conn, body="alpha body note", kind="kb")
    assert r2.outcome == "exact_dup"
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'ingested'"
    ).fetchone()[0] == 1


# --- #153: derive-from-rowcount / TOCTOU ------------------------------------


def test_insert_content_returns_false_on_duplicate(conn):
    """insert_content reports whether it inserted, so the gate derives the
    outcome from the write itself rather than a prior separate read (#153)."""
    assert dedup.insert_content(conn, body="dup body", kind="kb") is True
    assert dedup.insert_content(conn, body="dup body", kind="kb") is False


def test_duplicate_insert_emits_single_ingested(conn):
    r1 = dedup.gate(conn, body="exactly the same body", kind="kb")
    r2 = dedup.gate(conn, body="exactly the same body", kind="kb")
    assert r1.outcome == "new"
    assert r2.outcome == "exact_dup"
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'ingested'"
    ).fetchone()[0] == 1


# --- #153: one-current-per-source_url unique index --------------------------


def test_unique_index_rejects_second_current_row_for_one_url(conn):
    url = "https://example.com/page"
    dedup.gate(conn, body="v1", source_url=url, kind="research", source_tier="vendor-doc")
    h2 = content_hash("v2 manual current")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO content (hash, body, source_url, source_tier, kind, revision, is_current) "
            "VALUES (?, 'v2 manual current', ?, 'vendor-doc', 'research', 2, 1)",
            (h2, url),
        )


def test_unique_index_allows_multiple_null_source_url_current(conn):
    """Agent rows (NULL source_url) are exempt -- NULLs are distinct in the index."""
    dedup.gate(conn, body="agent note one", kind="kb")
    dedup.gate(conn, body="agent note two", kind="kb")
    assert conn.execute(
        "SELECT COUNT(*) FROM content WHERE source_url IS NULL AND is_current = 1"
    ).fetchone()[0] == 2


def test_supersede_keeps_exactly_one_current_with_index(conn):
    """The reordered supersede (insert non-current -> demote old -> promote new)
    never trips the unique index and leaves exactly one current row."""
    url = "https://example.com/p"
    dedup.gate(conn, body="v1", source_url=url, kind="research", source_tier="vendor-doc")
    r2 = dedup.gate(conn, body="v2", source_url=url, kind="research", source_tier="vendor-doc")
    assert r2.outcome == "superseded"
    current = _current(conn, url)
    assert len(current) == 1
    assert current[0]["hash"] == r2.hash


def test_concurrent_supersede_resolves_to_one_current(tmp_path, monkeypatch):
    """Two connections racing a supersede on the same source_url: the loser trips
    the unique index / loses its WAL snapshot, is caught and retried, and the end
    state is exactly ONE current row -- no crash, no two current rows (#153)."""
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)

    db = tmp_path / "race.sqlite"
    setup = sqlite3.connect(db, isolation_level=None)
    setup.row_factory = sqlite3.Row
    setup.execute("PRAGMA foreign_keys = ON")
    _apply_schema(setup)
    url = "https://example.com/race"
    h0 = content_hash("v0 seed")
    setup.execute(
        "INSERT INTO content (hash, body, source_url, source_tier, kind, revision, is_current) "
        "VALUES (?, 'v0 seed', ?, 'vendor-doc', 'research', 1, 1)",
        (h0, url),
    )
    setup.close()

    def _open():
        c = sqlite3.connect(db, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA busy_timeout = 5000")
        return c

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(body: str) -> None:
        c = _open()
        try:
            barrier.wait()
            dedup.gate(c, body=body, source_url=url, kind="research", source_tier="vendor-doc")
        except BaseException as exc:  # noqa: BLE001 - surface any thread failure
            errors.append(exc)
        finally:
            c.close()

    threads = [
        threading.Thread(target=worker, args=("revision A",)),
        threading.Thread(target=worker, args=("revision B",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"a racing gate crashed instead of retrying: {errors!r}"
    check = _open()
    current = _current(check, url)
    assert len(current) == 1, f"expected exactly one current row, got {len(current)}"
    check.close()


def test_supersede_insert_ignored_does_not_zero_out_source_url(conn, monkeypatch):
    """BLOCKING: in the supersede path, if `insert_content` reports it did NOT
    insert our row (its hash already exists -- a concurrent writer committed it,
    possibly for a DIFFERENT source_url), the gate must NOT blindly demote-old +
    promote-by-hash. Doing so would leave THIS source_url with ZERO current rows
    (and could promote a row that belongs to another source_url). The fix re-
    resolves from fresh state instead, so the source_url always ends with EXACTLY
    one current row.

    This drives that branch deterministically by making the first supersede insert
    report `False` (the WAL snapshot rules make the literal cross-source_url commit
    intercept itself via BUSY_SNAPSHOT, so we simulate the report directly). It is
    a regression guard: WITHOUT the `if not inserted: re-resolve` check, the demote
    then references a hash that was never inserted and the swap fails outright; WITH
    it, the gate retries and completes cleanly with one current row.
    """
    url_a = "https://example.com/A"
    live_a = content_hash("old body for A")
    conn.execute(
        "INSERT INTO content (hash, body, source_url, source_tier, kind, revision, is_current) "
        "VALUES (?, 'old body for A', ?, 'vendor-doc', 'research', 1, 1)",
        (live_a, url_a),
    )

    new_body = "fresh body written to A"
    h = content_hash(new_body)

    retries = {"n": 0}
    monkeypatch.setattr(
        dedup, "_on_gate_retry", lambda: retries.__setitem__("n", retries["n"] + 1)
    )

    # First supersede insert reports it was ignored (hash already exists); later
    # calls delegate to the real insert so the retry can complete.
    real_insert = dedup.insert_content
    calls = {"n": 0}

    def fake_insert(c, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return False  # simulate INSERT OR IGNORE no-op'ing on a pre-existing hash
        return real_insert(c, **kwargs)

    monkeypatch.setattr(dedup, "insert_content", fake_insert)

    result = dedup.gate(conn, body=new_body, source_url=url_a, kind="research",
                        source_tier="vendor-doc")

    # The contention-retry path fired (deterministic proof, not timing-based).
    assert retries["n"] >= 1, "the contention-retry path did not fire"
    # The gate re-resolved and completed cleanly rather than erroring or zeroing out.
    assert result.outcome == "superseded"
    # The invariant holds: EXACTLY one current row for the source_url, never zero.
    current_a = _current(conn, url_a)
    assert len(current_a) == 1, f"source_url A must keep exactly one current row, got {len(current_a)}"
    assert current_a[0]["hash"] == h
    # No hash was promoted under the wrong source_url -- the new current row is the
    # one we actually wrote for url_a, and the old row was demoted, not orphaned.
    assert conn.execute(
        "SELECT source_url FROM content WHERE hash = ?", (h,)
    ).fetchone()["source_url"] == url_a
    old = conn.execute(
        "SELECT is_current, superseded_by FROM content WHERE hash = ?", (live_a,)
    ).fetchone()
    assert old["is_current"] == 0
    assert old["superseded_by"] == h


def test_find_near_checks_multiple_candidates_and_threshold_inclusive(conn):
    q = b"embedding"
    first = {"content_hash": "stale", "distance": 0.1}
    second = {"content_hash": "live", "distance": 0.2}

    class Cur:
        def fetchall(self):
            return [first, second]

    class FakeConn:
        def execute(self, sql, params=()):
            if "FROM embeddings" in sql:
                return Cur()
            h = params[0]
            if h == "live":
                return type("R", (), {"fetchone": lambda self: {"ok": 1}})()
            return type("R", (), {"fetchone": lambda self: None})()

    out = dedup.find_near(FakeConn(), q, threshold=0.98)
    assert out is not None
    assert out[0] == "live"
    assert out[1] >= 0.98


def test_merged_event_includes_similarity_and_contradicted_keeps_correlation_id(conn, monkeypatch):
    dedup.gate(conn, body="v1", source_url="https://x/protected", kind="research", source_tier="vendor-doc")
    conn.execute("UPDATE content SET protected = 1 WHERE source_url = 'https://x/protected'")
    dedup.gate(
        conn,
        body="v2",
        source_url="https://x/protected",
        kind="research",
        source_tier="vendor-doc",
        correlation_id="cid-123",
    )
    contradicted = conn.execute(
        "SELECT payload, correlation_id FROM events WHERE type='contradicted' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert contradicted["correlation_id"] == "cid-123"

    monkeypatch.setattr(dedup, "find_near", lambda *_a, **_k: ("kept-hash", 0.987))
    out = dedup.gate(conn, body="near body", kind="kb", embedding=b"emb", correlation_id="cid-m")
    assert out.outcome == "near_dup"
    merged = conn.execute("SELECT payload FROM events WHERE type='merged' ORDER BY id DESC LIMIT 1").fetchone()
    payload = json.loads(merged["payload"])
    assert payload["similarity"] == pytest.approx(0.987)


def test_resolve_keep_mine_reraises_non_missing_embeddings_table_errors(conn):
    h_human = content_hash("human")
    h_up = content_hash("upstream")
    conn.execute(
        "INSERT INTO content (hash, body, source_url, source_tier, kind, revision, is_current, protected) "
        "VALUES (?, 'human', 'https://x/u', 'vendor-doc', 'research', 1, 1, 1)",
        (h_human,),
    )
    conn.execute(
        "INSERT INTO content (hash, body, source_url, source_tier, kind, revision, is_current) "
        "VALUES (?, 'upstream', 'https://x/u', 'vendor-doc', 'research', 2, 0)",
        (h_up,),
    )
    conn.execute(
        "INSERT INTO contradictions (hash_a, hash_b, detected_by) VALUES (?, ?, 'poller')",
        (h_human, h_up),
    )

    # Replace the optional embeddings table with a non-writable view so
    # DELETE hits an OperationalError that is NOT "no such table".
    conn.execute("DROP TABLE IF EXISTS embeddings")
    conn.execute("CREATE VIEW embeddings AS SELECT hash AS content_hash FROM content")

    with pytest.raises(sqlite3.OperationalError, match="view"):
        dedup.resolve_supersede(conn, h_human, "keep_mine", tombstone_upstream=True)


def test_supersede_path_invariant_one_current_after_normal_swap(conn):
    """Companion to the blocking case: the normal (uncontended) supersede leaves
    exactly one current row and emits the event -- the invariant guard does not
    interfere with the happy path."""
    url = "https://example.com/normal"
    dedup.gate(conn, body="rev one", source_url=url, kind="research", source_tier="vendor-doc")
    r2 = dedup.gate(conn, body="rev two", source_url=url, kind="research", source_tier="vendor-doc")
    assert r2.outcome == "superseded"
    current = _current(conn, url)
    assert len(current) == 1
    assert current[0]["hash"] == r2.hash
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'superseded'"
    ).fetchone()[0] == 1


# --- #153: resolve_supersede must never leave zero current rows -------------


def test_resolve_accept_upstream_missing_upstream_aborts(conn):
    url = "https://x/p"
    h_human = content_hash("human edit")
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current, protected) "
        "VALUES (?, 'human edit', 'T', ?, 'vendor-doc', 0.7, 'research', 1, 1, 1)",
        (h_human, url),
    )
    # Simulate a corrupted/missing upstream revision: a contradiction whose
    # hash_b row is absent from content. The hash_b FK (ON DELETE CASCADE) makes
    # this impossible to reach by deleting the upstream normally, so insert the
    # dangling contradiction with FK enforcement briefly off -- exactly the
    # storage corruption the rowcount guard must defend against.
    missing = "deadbeef" * 8
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO contradictions (hash_a, hash_b, detected_by) VALUES (?, ?, 'poller')",
        (h_human, missing),
    )
    conn.execute("PRAGMA foreign_keys = ON")

    result = dedup.resolve_supersede(conn, h_human, "accept_upstream")
    assert "error" in result, f"expected an error dict, got {result!r}"

    # Never zero current rows: the human row stays the single current revision.
    current = _current(conn, url)
    assert len(current) == 1
    assert current[0]["hash"] == h_human
    # The contradiction is NOT resolved (the whole resolve rolled back).
    assert conn.execute(
        "SELECT resolved FROM contradictions WHERE hash_a = ?", (h_human,)
    ).fetchone()["resolved"] == 0


def test_resolve_accept_upstream_foreign_source_upstream_aborts(conn):
    """BLOCKING (cross-source lineage): a contradiction whose upstream hash_b
    points to a row owned by a DIFFERENT source_url must NOT promote that foreign
    row. accept_upstream would otherwise demote the human row of source A and
    promote source B's row, leaving A with ZERO current rows and mutating B. The
    lineage check + exactly-one-current assertion abort instead.
    """
    url_a = "https://x/A"
    url_b = "https://x/B"
    h_human = content_hash("human edit on A")
    h_foreign = content_hash("content owned by B")
    # Protected human row, current under source A.
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current, protected) "
        "VALUES (?, 'human edit on A', 'T', ?, 'vendor-doc', 0.7, 'research', 1, 1, 1)",
        (h_human, url_a),
    )
    # A foreign row, current+protected under a DIFFERENT source B.
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current, protected) "
        "VALUES (?, 'content owned by B', 'T', ?, 'vendor-doc', 0.7, 'research', 1, 1, 1)",
        (h_foreign, url_b),
    )
    # A contradiction (hash_a=human@A, hash_b=foreign@B). The hash_b FK is
    # satisfied because B's row exists, so this is a normal insert.
    conn.execute(
        "INSERT INTO contradictions (hash_a, hash_b, detected_by) VALUES (?, ?, 'poller')",
        (h_human, h_foreign),
    )

    result = dedup.resolve_supersede(conn, h_human, "accept_upstream")
    assert "error" in result, f"expected an error dict, got {result!r}"

    # Source A never goes to zero current rows: the human row stays its sole current.
    current_a = _current(conn, url_a)
    assert len(current_a) == 1, "source A must never end with zero current rows"
    assert current_a[0]["hash"] == h_human
    # The foreign B row is NOT promoted/mutated under the wrong source_url: still
    # current under B, still protected, never demoted.
    b_row = conn.execute(
        "SELECT source_url, is_current, protected, superseded_by FROM content WHERE hash = ?",
        (h_foreign,),
    ).fetchone()
    assert b_row["source_url"] == url_b
    assert b_row["is_current"] == 1
    assert b_row["protected"] == 1
    assert b_row["superseded_by"] is None
    # The human row was not demoted and the contradiction was not resolved.
    human = conn.execute(
        "SELECT is_current, superseded_by FROM content WHERE hash = ?", (h_human,)
    ).fetchone()
    assert human["is_current"] == 1
    assert human["superseded_by"] is None
    assert conn.execute(
        "SELECT resolved FROM contradictions WHERE hash_a = ?", (h_human,)
    ).fetchone()["resolved"] == 0


# --- schema 007: backfill + clean apply -------------------------------------


def test_migration_007_backfills_duplicate_current_rows(tmp_path):
    """007 demotes pre-existing duplicate-current rows (keeping the canonical
    one), then creates the unique index -- and applies cleanly via the runner."""
    db = tmp_path / "m7.sqlite"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Apply everything up to (but not including) 007: no unique index yet.
    _apply_schema(conn, SCHEMA_FILES[:-1])

    url = "https://example.com/dup"
    for rev, body in [(1, "a"), (2, "b"), (3, "c")]:
        conn.execute(
            "INSERT INTO content (hash, body, source_url, source_tier, kind, revision, is_current) "
            "VALUES (?, ?, ?, 'vendor-doc', 'research', ?, 1)",
            (content_hash(body), body, url, rev),
        )
    # A NULL-source_url current row must survive the backfill untouched.
    h_null = content_hash("agent note")
    conn.execute(
        "INSERT INTO content (hash, body, kind, revision, is_current) "
        "VALUES (?, 'agent note', 'kb', 1, 1)",
        (h_null,),
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM content WHERE source_url = ? AND is_current = 1", (url,)
    ).fetchone()[0] == 3

    # Apply 007 atomically via B's migration runner.
    common_db._apply_one_migration(conn, REPO / "schema" / "007_unique_current_per_url.sql")

    # Exactly one current row survives: the canonical (highest-revision) one.
    current = conn.execute(
        "SELECT hash, revision FROM content WHERE source_url = ? AND is_current = 1", (url,)
    ).fetchall()
    assert len(current) == 1
    assert current[0]["revision"] == 3
    assert current[0]["hash"] == content_hash("c")
    # The NULL-source_url current row is untouched.
    assert conn.execute(
        "SELECT is_current FROM content WHERE hash = ?", (h_null,)
    ).fetchone()["is_current"] == 1
    # The index now exists and enforces the invariant.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO content (hash, body, source_url, source_tier, kind, revision, is_current) "
            "VALUES (?, 'd', ?, 'vendor-doc', 'research', 4, 1)",
            (content_hash("d"), url),
        )
    # schema_version advanced.
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 7
    conn.close()
