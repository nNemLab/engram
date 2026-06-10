from engram.poller.adapters import matches_globs


def test_no_filters_passes_everything():
    assert matches_globs("/anything", include=[], exclude=[]) is True


def test_include_matches():
    assert matches_globs("/engine/install/", include=["*/engine/*"], exclude=[]) is True


def test_include_does_not_match():
    assert matches_globs("/desktop/", include=["*/engine/*"], exclude=[]) is False


def test_exclude_overrides_include():
    assert matches_globs(
        "/desktop/install/macos/",
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
    assert matches_globs(
        "https://docs.docker.com/engine/install/linux/",
        include=["*/engine/*"],
        exclude=[],
    ) is True
