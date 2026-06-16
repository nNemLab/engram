import sqlite3

from engram import log as event_log


def _conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            payload TEXT,
            actor TEXT,
            correlation_id TEXT
        );
        """
    )
    return conn


def test_since_yield_poison_flags_non_text_payload_type_error(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO events (ts, type, payload, actor, correlation_id) VALUES (?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00.000Z", "ingested", None, None, None),
    )
    conn.commit()

    evt = list(event_log.since(conn, 0, yield_poison=True))[0]
    assert evt.id == 1
    assert evt.poison is True
    assert evt.payload == {}
