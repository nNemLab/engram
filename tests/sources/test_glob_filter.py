from engram.poller.adapters import matches_globs


def test_no_filters_passes_everything():
    assert matches_globs("/anything", include=[], exclude=[]) is True


def test_include_matches():
    # * matches within one segment; two wildcards + middle segment
    assert matches_globs("foo/engine/bar", include=["*/engine/*"], exclude=[]) is True


def test_include_does_not_match():
    assert matches_globs("foo/desktop/", include=["*/engine/*"], exclude=[]) is False


def test_exclude_overrides_include():
    assert matches_globs(
        "desktop/install/macos/",
        include=["*/install/*"],
        exclude=["*/macos/*"],
    ) is False


def test_double_star_matches_path_segments():
    assert matches_globs(
        "docs/engine/deep/nested/install.md",
        include=["docs/engine/**"],
        exclude=[],
    ) is True


def test_url_filter_handles_full_url():
    # Segment-aware globs need ** to span multi-segment URLs with :// etc.
    assert matches_globs(
        "https://docs.docker.com/engine/install/linux/",
        include=["**/engine/**"],
        exclude=[],
    ) is True


# --- Segment-aware scoping (fix #171) -------------------------------------


def test_single_star_stays_in_segment():
    """"docs/*.md" matches "docs/x.md" but NOT "docs/a/b/c.md"."""
    assert matches_globs("docs/x.md", include=["docs/*.md"], exclude=[]) is True
    assert matches_globs("docs/a/b/c.md", include=["docs/*.md"], exclude=[]) is False


def test_double_star_crosses_segments():
    """"docs/**/*.md" matches deeply nested paths."""
    assert matches_globs("docs/a/b/c.md", include=["docs/**/*.md"], exclude=[]) is True


def test_single_star_no_match_different_segment():
    """Single * matches within one segment only."""
    assert matches_globs("docs/sub/x.md", include=["docs/*.md"], exclude=[]) is False
    assert matches_globs("docs/x/y.md", include=["docs/*.md"], exclude=[]) is False


def test_double_star_matches_single_segment():
    """** also matches zero segments, so docs/**/*.md matches docs/x.md too."""
    assert matches_globs("docs/x.md", include=["docs/**/*.md"], exclude=[]) is True


def test_exclude_single_star_stays_in_segment():
    """Exclusion also uses segment-aware scoping."""
    assert matches_globs("docs/a/b/c.md", include=["docs/**"], exclude=["docs/a/*.md"]) is True
    assert matches_globs("docs/a/x.md", include=["docs/**"], exclude=["docs/a/*.md"]) is False
