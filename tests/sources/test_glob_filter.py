from engram.poller.adapters import matches_globs


def test_no_filters_passes_everything():
    assert matches_globs("/anything", include=[], exclude=[]) is True


def test_include_matches():
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


def test_middle_double_star_stays_segment_aware():
    """a/**/b must match a/b, a/x/b, a/x/y/b — but NOT a/xb or a/x/yb."""
    assert matches_globs("a/b", include=["a/**/b"], exclude=[]) is True
    assert matches_globs("a/x/b", include=["a/**/b"], exclude=[]) is True
    assert matches_globs("a/x/y/b", include=["a/**/b"], exclude=[]) is True
    assert matches_globs("a/xb", include=["a/**/b"], exclude=[]) is False
    assert matches_globs("a/x/yb", include=["a/**/b"], exclude=[]) is False


def test_single_star_stays_in_segment():
    """"docs/*.md" matches "docs/x.md" but NOT "docs/a/b/c.md"."""
    assert matches_globs("docs/x.md", include=["docs/*.md"], exclude=[]) is True
    assert matches_globs("docs/a/b/c.md", include=["docs/*.md"], exclude=[]) is False


def test_double_star_crosses_segments():
    """"docs/**/*.md" matches deeply nested paths."""
    assert matches_globs("docs/a/b/c.md", include=["docs/**/*.md"], exclude=[]) is True
