"""#54: resolve tool for blocked-supersede contradictions (accept upstream / keep mine)."""
import json
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
    from types import SimpleNamespace
    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)
    yield c


def _blocked_state(conn, *, url="https://x/p"):
    """Drive a protected row + blocked upstream supersede; return (human, upstream) hashes."""
    from engram.dedup import content_hash, gate

    h_human = content_hash("human edit")
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, confidence, "
        "kind, revision, is_current, protected) "
        "VALUES (?, 'human edit', 'T', ?, 'vendor-doc', 0.7, 'research', 1, 1, 1)",
        (h_human, url),
    )
    conn.execute(
        "INSERT INTO vault_state (vault_path, content_hash, rendered_body, rendered_at) "
        "VALUES ('050-kb/page.md', ?, 'human edit', '2026-01-01T00:00:00Z')",
        (h_human,),
    )
    conn.commit()
    r = gate(conn, body="new upstream bytes", source_url=url,
             source_tier="vendor-doc", kind="research", actor="poller")
    assert r.outcome == "supersede_blocked"
    return h_human, content_hash("new upstream bytes")


def test_accept_upstream_makes_upstream_current(conn):
    from engram.dedup import resolve_supersede

    h_human, h_up = _blocked_state(conn)
    out = resolve_supersede(conn, h_human, "accept_upstream", actor="human")
    assert out["outcome"] == "accept_upstream"

    # Upstream is now current and unprotected; human row demoted + superseded_by upstream.
    up = conn.execute("SELECT is_current, protected FROM content WHERE hash = ?",
                      (h_up,)).fetchone()
    assert up["is_current"] == 1
    assert up["protected"] == 0
    human = conn.execute("SELECT is_current, superseded_by FROM content WHERE hash = ?",
                         (h_human,)).fetchone()
    assert human["is_current"] == 0
    assert human["superseded_by"] == h_up

    # Contradiction resolved as kept_b.
    c = conn.execute("SELECT resolved, resolution FROM contradictions").fetchone()
    assert c["resolved"] == 1
    assert c["resolution"] == "kept_b"

    # A superseded event drives the projector to re-project the vault file.
    evt = conn.execute(
        "SELECT payload FROM events WHERE type = 'superseded' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert evt is not None
    payload = json.loads(evt["payload"])
    assert payload["hash_old"] == h_human
    assert payload["hash_new"] == h_up


def test_keep_mine_resolves_kept_a(conn):
    from engram.dedup import resolve_supersede

    h_human, h_up = _blocked_state(conn)
    out = resolve_supersede(conn, h_human, "keep_mine", actor="human")
    assert out["outcome"] == "keep_mine"

    # Human row untouched: still current, still protected.
    human = conn.execute("SELECT is_current, protected, superseded_by FROM content WHERE hash = ?",
                         (h_human,)).fetchone()
    assert human["is_current"] == 1
    assert human["protected"] == 1
    assert human["superseded_by"] is None

    # Pending upstream revision tombstoned by default.
    up = conn.execute("SELECT tombstoned FROM content WHERE hash = ?", (h_up,)).fetchone()
    assert up["tombstoned"] == 1

    c = conn.execute("SELECT resolved, resolution FROM contradictions").fetchone()
    assert c["resolved"] == 1
    assert c["resolution"] == "kept_a"

    # No superseded event for keep_mine — vault file stays as the human's edit.
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type='superseded'").fetchone()[0] == 0


def test_keep_mine_can_retain_upstream_revision(conn):
    from engram.dedup import resolve_supersede

    h_human, h_up = _blocked_state(conn)
    resolve_supersede(conn, h_human, "keep_mine", tombstone_upstream=False, actor="human")
    up = conn.execute("SELECT tombstoned FROM content WHERE hash = ?", (h_up,)).fetchone()
    assert up["tombstoned"] == 0


def test_resolve_unknown_hash_errors(conn):
    from engram.dedup import resolve_supersede

    out = resolve_supersede(conn, "deadbeef", "accept_upstream", actor="human")
    assert "error" in out


def test_resolve_invalid_choice_errors(conn):
    from engram.dedup import resolve_supersede

    h_human, _ = _blocked_state(conn)
    out = resolve_supersede(conn, h_human, "frobnicate", actor="human")
    assert "error" in out


def test_resolve_already_resolved_errors(conn):
    from engram.dedup import resolve_supersede

    h_human, _ = _blocked_state(conn)
    resolve_supersede(conn, h_human, "keep_mine", actor="human")
    # Second resolve finds no unresolved contradiction.
    out = resolve_supersede(conn, h_human, "accept_upstream", actor="human")
    assert "error" in out


def test_accept_upstream_reprojects_vault_file(conn, tmp_path):
    """The superseded event emitted by accept_upstream rewrites the vault file
    in place with the upstream body, via the existing projector handler."""
    from engram import log as event_log
    from engram.dedup import resolve_supersede
    from engram.projector.projector import _handle_event

    h_human, h_up = _blocked_state(conn)
    vault = tmp_path / "vault"
    (vault / "050-kb").mkdir(parents=True)
    # The protected row already owns a vault file (the human's edited bytes).
    rel = "050-kb/page.md"
    (vault / rel).write_text("human edit")
    conn.execute("UPDATE vault_state SET vault_path = ? WHERE content_hash = ?",
                 (rel, h_human))
    conn.commit()

    resolve_supersede(conn, h_human, "accept_upstream", actor="human")

    sup_evt = list(event_log.since(conn, 0, types=["superseded"]))[-1]
    _handle_event(conn, vault, sup_evt, {"research": "050-kb"})

    text = (vault / rel).read_text()
    assert "new upstream bytes" in text
    assert "human edit" not in text
    # vault_state repointed to the upstream hash, same path.
    state = conn.execute(
        "SELECT vault_path FROM vault_state WHERE content_hash = ?", (h_up,)
    ).fetchone()
    assert state["vault_path"] == rel
