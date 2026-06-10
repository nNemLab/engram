"""#37: protect human-edited sourced rows from silent supersede clobber."""
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
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply_schema(c)
    # Patch where gate() looks load_config up (engram.dedup.load_config).
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    yield c


def test_protected_column_exists_and_defaults_zero(conn):
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(content)")}
    assert "protected" in cols
    assert cols["protected"]["dflt_value"] in ("0", 0)
    # schema_version advanced to 4
    v = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert v >= 4


def _seed_sourced_row(conn, *, hash_, body, source_url, vault_path):
    """Insert a current sourced content row + its vault_state projection."""
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, kind, revision, is_current) "
        "VALUES (?, ?, 'T', ?, 'vendor-doc', 0.7, 'research', 1, 1)",
        (hash_, body, source_url),
    )
    conn.execute(
        "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00Z')",
        (vault_path, hash_, body),
    )
    conn.commit()


def test_watcher_human_edit_sets_protected(conn, tmp_path, monkeypatch):
    from engram.dedup import content_hash
    from engram.watcher import watcher

    # Vault file on disk + matching content/vault_state row.
    vault = tmp_path / "vault"
    (vault / "050-kb").mkdir(parents=True)
    rel = "050-kb/page.md"
    (vault / rel).write_text("original sourced body")
    h = content_hash("original sourced body")
    _seed_sourced_row(conn, hash_=h, body="original sourced body",
                      source_url="https://x/p", vault_path=rel)

    # Human edits the file, then the watcher observes the change.
    (vault / rel).write_text("HUMAN edited body")
    watcher._on_change(conn, rel, str(vault / rel))

    row = conn.execute("SELECT protected, body FROM content WHERE hash = ?", (h,)).fetchone()
    assert row["protected"] == 1
    assert row["body"] == "HUMAN edited body"
    # vault_edit event still recorded.
    n = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'vault_edit'").fetchone()[0]
    assert n == 1


def _live_sourced(conn, *, body, source_url, protected):
    from engram.dedup import content_hash
    h = content_hash(body)
    conn.execute(
        "INSERT INTO content (hash, body, source_url, source_tier, confidence, "
        "kind, revision, is_current, protected) "
        "VALUES (?, ?, ?, 'vendor-doc', 0.7, 'research', 1, 1, ?)",
        (h, body, source_url, protected),
    )
    conn.commit()
    return h


def test_protected_supersede_is_blocked(conn):
    from engram.dedup import gate, content_hash
    url = "https://x/p"
    h_human = _live_sourced(conn, body="human edit", source_url=url, protected=1)

    r = gate(conn, body="new upstream bytes", source_url=url,
             source_tier="vendor-doc", kind="research", actor="poller")

    assert r.outcome == "supersede_blocked"
    # Human row stays current, never superseded.
    human = conn.execute("SELECT is_current, superseded_by FROM content WHERE hash = ?",
                         (h_human,)).fetchone()
    assert human["is_current"] == 1
    assert human["superseded_by"] is None
    # Upstream preserved as a non-current revision.
    up = conn.execute("SELECT is_current FROM content WHERE hash = ?",
                      (content_hash("new upstream bytes"),)).fetchone()
    assert up["is_current"] == 0
    # No superseded event; one contradicted event.
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='superseded'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='contradicted'").fetchone()[0] == 1
    # One unresolved contradiction linking human -> upstream.
    c = conn.execute("SELECT hash_a, hash_b, resolved FROM contradictions").fetchall()
    assert len(c) == 1
    assert c[0]["hash_a"] == h_human
    assert c[0]["hash_b"] == content_hash("new upstream bytes")
    assert c[0]["resolved"] == 0


def test_unprotected_row_still_supersedes(conn):
    from engram.dedup import gate, content_hash
    url = "https://x/q"
    h_old = _live_sourced(conn, body="v1", source_url=url, protected=0)

    r = gate(conn, body="v2", source_url=url, source_tier="vendor-doc",
             kind="research", actor="poller")

    assert r.outcome == "superseded"
    old = conn.execute("SELECT is_current, superseded_by FROM content WHERE hash = ?",
                       (h_old,)).fetchone()
    assert old["is_current"] == 0
    assert old["superseded_by"] == content_hash("v2")
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='superseded'").fetchone()[0] == 1


def test_identical_repoll_of_protected_is_exact_dup(conn):
    from engram.dedup import gate
    url = "https://x/p"
    _live_sourced(conn, body="human edit", source_url=url, protected=1)
    # First changed upstream blocks.
    gate(conn, body="upstream A", source_url=url, source_tier="vendor-doc",
         kind="research", actor="poller")
    # Re-poll with the SAME upstream bytes -> exact_dup, no new contradiction.
    r = gate(conn, body="upstream A", source_url=url, source_tier="vendor-doc",
             kind="research", actor="poller")
    assert r.outcome == "exact_dup"
    assert conn.execute("SELECT COUNT(*) FROM contradictions").fetchone()[0] == 1


def test_changed_upstream_updates_single_contradiction(conn):
    from engram.dedup import gate, content_hash
    url = "https://x/p"
    h_human = _live_sourced(conn, body="human edit", source_url=url, protected=1)
    gate(conn, body="upstream A", source_url=url, source_tier="vendor-doc",
         kind="research", actor="poller")
    gate(conn, body="upstream B", source_url=url, source_tier="vendor-doc",
         kind="research", actor="poller")
    # Still exactly one unresolved contradiction, hash_b advanced to the newest.
    rows = conn.execute("SELECT hash_a, hash_b FROM contradictions WHERE resolved = 0").fetchall()
    assert len(rows) == 1
    assert rows[0]["hash_a"] == h_human
    assert rows[0]["hash_b"] == content_hash("upstream B")


def test_poll_one_counts_blocked(conn, monkeypatch):
    import asyncio
    from engram.poller import poller
    from engram.poller.adapters import Candidate

    url = "https://x/p"
    _live_sourced(conn, body="human edit", source_url=url, protected=1)
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, schedule, source_tier) "
        "VALUES ('s1', 'fake', 'fake', ?, '7d', 'vendor-doc')",
        (url,),
    )
    conn.commit()

    class FakeAdapter:
        name = "fake"
        async def fetch(self, source):
            yield Candidate(body="new upstream", title="T", source_url=url)

    monkeypatch.setitem(poller.ADAPTERS, "fake", FakeAdapter())
    source = {"id": "s1", "adapter": "fake", "schedule": "7d",
              "source_tier": "vendor-doc", "error_count": 0, "paused": 0, "cursor": None}

    counts = asyncio.run(poller.poll_one(conn, source))
    assert counts["blocked"] == 1
    assert counts["superseded"] == 0
    assert counts["ingested"] == 0
