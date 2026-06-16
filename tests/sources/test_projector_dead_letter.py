"""#158: projector retry budget + dead-letter for deterministically-failing handlers.

A parseable event whose projection path raises should be retried once per poll
cycle up to ``MAX_HANDLER_ATTEMPTS``. Once exhausted, the event is moved to
``projector_dead_letter`` and the projector cursor advances so later events still
project (no permanent head-of-line block).
"""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in (
        "001_initial.sql",
        "002_sources_and_revisions.sql",
        "003_grounding.sql",
        "004_protected.sql",
        "005_event_hash_chain.sql",
        "006_reactor_dead_letter.sql",
        "007_unique_current_per_url.sql",
        "008_projector_dead_letter.sql",
    ):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply(c)
    yield c


def _seed_content(conn, h, body, source_url):
    conn.execute(
        "INSERT INTO content (hash, body, title, source_url, source_tier, "
        "confidence, kind, revision, is_current) "
        "VALUES (?, ?, ?, ?, 'vendor-doc', 0.7, 'research', 1, 1)",
        (h, body, body, source_url),
    )


class _StopTick(Exception):
    """Sentinel raised from patched time.sleep to end run() after N ticks."""


def _stopper(max_ticks):
    count = [0]

    def _stop(_):
        count[0] += 1
        if count[0] >= max_ticks:
            raise _StopTick

    return _stop


def test_generic_handler_failure_dead_letters_and_stream_advances(conn, tmp_path, monkeypatch):
    from engram import log as event_log
    from engram.dedup import content_hash
    from engram.projector import projector as pmod

    vault = tmp_path / "vault"
    h_fail = content_hash("body fail")
    h_ok = content_hash("body ok")
    _seed_content(conn, h_fail, "body fail", "https://x/fail")
    _seed_content(conn, h_ok, "body ok", "https://x/ok")

    fail_id = event_log.append(conn, "ingested", {"hash": h_fail})
    ok_id = event_log.append(conn, "ingested", {"hash": h_ok})
    conn.commit()

    fake_cfg = SimpleNamespace(
        paths=SimpleNamespace(vault=vault),
        projector=SimpleNamespace(poll_interval=0, kind_dirs={"research": "030-research"}),
    )
    monkeypatch.setattr(pmod, "load_config", lambda: fake_cfg)
    monkeypatch.setattr(pmod, "MAX_HANDLER_ATTEMPTS", 3)

    calls = []
    real_handle = pmod._handle_event

    def _failing_handle(conn, vault, evt, kind_dirs):
        calls.append(evt.id)
        if evt.id == fail_id:
            raise RuntimeError("renderer crashed")
        return real_handle(conn, vault, evt, kind_dirs)

    monkeypatch.setattr(pmod, "_handle_event", _failing_handle)
    monkeypatch.setattr(pmod.time, "sleep", _stopper(3))

    with pytest.raises(_StopTick):
        pmod._run_loop(conn)

    # Deterministically-failing event retried exactly up to the configured budget.
    assert calls.count(fail_id) == 3
    # Later event still projected once the poison event is dead-lettered.
    assert ok_id in calls
    assert conn.execute(
        "SELECT 1 FROM vault_state WHERE content_hash = ?", (h_ok,)
    ).fetchone() is not None

    row = conn.execute(
        "SELECT event_id, event_type, attempts, error, dead_lettered_ts "
        "FROM projector_dead_letter WHERE event_id = ?",
        (fail_id,),
    ).fetchone()
    assert row is not None
    assert row["event_id"] == fail_id
    assert row["event_type"] == "ingested"
    assert row["attempts"] == 3
    assert "renderer crashed" in row["error"]
    assert row["dead_lettered_ts"]

    # Attempt bookkeeping is cleared once dead-lettered.
    assert conn.execute(
        "SELECT count(*) FROM projector_attempts WHERE event_id = ?", (fail_id,)
    ).fetchone()[0] == 0

    # Cursor advanced past the dead-lettered event all the way to the later good event.
    cursor = conn.execute(
        "SELECT last_event_id FROM daemon_cursors WHERE name = 'projector'"
    ).fetchone()["last_event_id"]
    assert cursor == ok_id
