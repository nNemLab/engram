"""#84: a single un-parseable event payload must not freeze the projector.

A poison row (corrupt JSON payload) sitting between two good events must be
dead-lettered and skipped: the cursor advances past it and later good events
are still processed, instead of the loop restarting from the same poison row
forever and silently dropping everything after it.
"""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql",
               "003_grounding.sql", "004_protected.sql",
               "005_event_hash_chain.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c)
    yield c


def _seed_content(conn, h, body):
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, kind, revision, is_current) "
        "VALUES (?, ?, ?, 'https://x/p', 'vendor-doc', 0.7, 'research', 1, 1)",
        (h, body, body),
    )


class _StopTick(Exception):
    """Sentinel raised from a patched time.sleep to end run() after one tick."""


def test_run_skips_poison_event_between_good_events(conn, tmp_path, monkeypatch):
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.projector import projector as pmod

    vault = tmp_path / "vault"
    h_a = content_hash("body A")
    h_b = content_hash("body B")
    _seed_content(conn, h_a, "body A")
    _seed_content(conn, h_b, "body B")

    # event order: good (A) -> poison -> good (B)
    event_log.append(conn, "ingested", {"hash": h_a})
    poison_id = event_log.append(conn, "ingested", {"hash": "unused"})
    conn.execute("UPDATE events SET payload = ? WHERE id = ?", ("{not valid json", poison_id))
    last_id = event_log.append(conn, "ingested", {"hash": h_b})
    conn.commit()

    fake_cfg = SimpleNamespace(
        paths=SimpleNamespace(vault=vault),
        projector=SimpleNamespace(poll_interval=0, kind_dirs={"research": "030-research"}),
    )
    monkeypatch.setattr(pmod, "load_config", lambda: fake_cfg)
    monkeypatch.setattr(pmod, "get_connection", lambda: conn)

    def _stop(_):
        raise _StopTick

    # time.sleep runs after each tick's processing; raising ends the loop once
    # the single batch of events has been drained.
    monkeypatch.setattr(pmod.time, "sleep", _stop)
    with pytest.raises(_StopTick):
        pmod.run()

    # The later good event (B) was still processed despite the poison row.
    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE content_hash = ?", (h_b,)
    ).fetchone() is not None

    # The cursor advanced PAST the poison row (loop is not stuck).
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'projector'"
    ).fetchone()["last_event_id"]
    assert cursor == last_id
