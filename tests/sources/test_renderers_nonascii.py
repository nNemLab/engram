"""#159: renderer frontmatter must not escape non-ASCII characters.

When a vault file has a non-ASCII title (e.g. Japanese, Cyrillic), the
YAML frontmatter dumped by _frontmatter must keep the characters literal
rather than escaping them to \\uXXXX sequences.  This keeps human-readable
vault output intact and avoids hash drift when locale encoding differs.
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
def conn(tmp_path):
    db = tmp_path / "test.sqlite"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    _apply_schema(c)
    yield c


def test_nonascii_title_preserved_in_frontmatter(conn):
    """Non-ASCII title must appear literally in frontmatter, not as \\uXXXX."""
    from engram.projector import renderers

    row = conn.execute(
        "SELECT 'abc123' as hash, 'body' as body, '日本語タイトル' as title, "
        "'https://例.jp' as source_url, 'vendor-doc' as source_tier, "
        "0.9 as confidence, 365 as ttl_days, 'kb' as kind, "
        "'2026-01-01T00:00:00Z' as fetched_at"
    ).fetchone()

    path, body = renderers.render_kb(row, "010-kb")

    # The path itself may use safe-slugified title, but frontmatter must
    # contain the *original* title literally.
    assert "日本語タイトル" in body, (
        "Frontmatter must contain non-ASCII title literally, not escaped"
    )
    assert "source_url" in body


def test_nonascii_url_preserved_in_frontmatter(conn):
    """Non-ASCII characters in source_url must not be unicode-escaped."""
    from engram.projector import renderers

    row = conn.execute(
        "SELECT 'def456' as hash, 'body' as body, 'Cyrillic test' as title, "
        "'https://пример.ru/path' as source_url, 'web' as source_tier, "
        "0.85 as confidence, 180 as ttl_days, 'kb' as kind, "
        "'2026-06-15T00:00:00Z' as fetched_at"
    ).fetchone()

    path, body = renderers.render_kb(row, "010-kb")

    assert "пример" in body, (
        "Non-ASCII in source_url must appear literally in frontmatter"
    )
    # Verify no \\uXXXX escaping on those characters
    for ch in "пример":
        assert r"\u" not in body[body.index("пример") - 5: body.index("пример") + 6], (
            f"Character {ch!r} was unicode-escaped"
        )
