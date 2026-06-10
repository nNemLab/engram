"""Sourced content (non-null source_url + source_id) renders to a stable
URL-derived path so successive revisions overwrite the same file. Non-sourced
content keeps the existing title-slug-hash scheme.
"""

from engram.projector.renderers import render_kb


def _row(**fields):
    """Build a sqlite3.Row-like mapping (dict suffices since render_kb only indexes by name)."""
    defaults = dict(
        hash="hashabcdef0123456789",
        title=None,
        source_url=None,
        source_tier="manual",
        fetched_at=None,
        confidence=0.5,
        ttl_days=None,
        kind="kb",
        body="hello",
        source_id=None,
    )
    defaults.update(fields)
    # render_kb uses row["k"] subscript; dict satisfies that.
    return defaults


def test_non_sourced_uses_title_hash_path():
    row = _row(title="My Note", hash="abcdef0123" * 6)
    path, _ = render_kb(row, "050-kb")
    assert path == "050-kb/my-note-abcdef01.md"


def test_sourced_uses_url_derived_path():
    row = _row(
        title="Engine Install",
        source_url="https://docs.docker.com/engine/install/linux/",
        source_id="docker-docs-linux",
        kind="research",
    )
    path, _ = render_kb(row, "030-research")
    # URL path tail "linux" → slug; source_id first 8 chars → "docker-d"
    assert path == "030-research/linux-docker-d.md"


def test_sourced_two_revisions_same_path():
    r1 = _row(
        hash="rev1hash" * 8,
        source_url="https://example.com/foo/bar/",
        source_id="example-src",
        title="First",
    )
    r2 = _row(
        hash="rev2hash" * 8,
        source_url="https://example.com/foo/bar/",
        source_id="example-src",
        title="Second",
    )
    p1, _ = render_kb(r1, "030-research")
    p2, _ = render_kb(r2, "030-research")
    assert p1 == p2  # stable across revisions
