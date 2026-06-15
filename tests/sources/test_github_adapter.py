import json
from pathlib import Path

import httpx
import pytest

from engram.poller.adapters import github_repo as gh_adapter
from engram.poller.adapters.github_repo import GitHubRepoAdapter

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures"


def _src(**overrides):
    base = {
        "id": "docker-docs",
        "url": "https://github.com/docker/docs",
        "config": json.dumps({"include": ["docs/engine/**"], "branch": "main"}),
        "cursor": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_first_run_walks_tree_and_filters(monkeypatch):
    """No cursor → walks tree at HEAD, applies include glob."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")  # hermetic: skip gh keyring lookup
    tree = (FIX / "github_tree_response.json").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/repos/docker/docs/branches/main":
            return httpx.Response(200, json={"commit": {"sha": "head1"}})
        if p == "/repos/docker/docs/git/trees/head1":
            return httpx.Response(200, text=tree, headers={"content-type": "application/json"})
        if p.startswith("/repos/docker/docs/contents/docs/engine/install.md"):
            return httpx.Response(200, text="install body",
                                  headers={"content-type": "text/plain"})
        if p.startswith("/repos/docker/docs/contents/docs/engine/upgrade.md"):
            return httpx.Response(200, text="upgrade body",
                                  headers={"content-type": "text/plain"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    adapter = GitHubRepoAdapter(_transport=transport)

    src = _src()
    cands = [c async for c in adapter.fetch(src)]
    paths = sorted(c.source_url for c in cands)
    assert paths == sorted([
        "https://github.com/docker/docs/blob/head1/docs/engine/install.md",
        "https://github.com/docker/docs/blob/head1/docs/engine/upgrade.md",
    ])
    cursor = json.loads(src["cursor"])
    assert cursor["last_sha"] == "head1"


@pytest.mark.asyncio
async def test_subsequent_run_uses_compare(monkeypatch):
    """With cursor present, adapter calls compare-API and only fetches changed files."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")  # hermetic: skip gh keyring lookup
    cmp_resp = (FIX / "github_compare_response.json").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/repos/docker/docs/branches/main":
            return httpx.Response(200, json={"commit": {"sha": "head2"}})
        if p == "/repos/docker/docs/compare/head1...head2":
            return httpx.Response(200, text=cmp_resp,
                                  headers={"content-type": "application/json"})
        if "/contents/docs/engine/install.md" in p:
            return httpx.Response(200, text="updated install")
        if "/contents/docs/engine/new-page.md" in p:
            return httpx.Response(200, text="new page body")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    adapter = GitHubRepoAdapter(_transport=transport)

    src = _src(cursor=json.dumps({"last_sha": "head1"}))
    cands = [c async for c in adapter.fetch(src)]
    urls = sorted(c.source_url for c in cands)
    assert urls == sorted([
        "https://github.com/docker/docs/blob/head2/docs/engine/install.md",
        "https://github.com/docker/docs/blob/head2/docs/engine/new-page.md",
    ])
    cursor = json.loads(src["cursor"])
    assert cursor["last_sha"] == "head2"


# ----- token resolution: env -> gh keyring -> anonymous ------------------

def test_resolve_token_prefers_env(monkeypatch):
    """Explicit GITHUB_TOKEN wins and gh is never consulted."""
    monkeypatch.setenv("GITHUB_TOKEN", "env-tok")

    def _no_gh(_name):
        raise AssertionError("gh lookup must be skipped when GITHUB_TOKEN is set")

    monkeypatch.setattr(gh_adapter.shutil, "which", _no_gh)
    assert gh_adapter._resolve_token() == ("env-tok", "env")


def test_resolve_token_falls_back_to_gh_keyring(monkeypatch):
    """No env var → read the token from the gh credential store."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gh_adapter.shutil, "which", lambda _name: "/usr/bin/gh")

    class _Proc:
        returncode = 0
        stdout = "keyring-tok\n"

    monkeypatch.setattr(gh_adapter.subprocess, "run", lambda *a, **k: _Proc())
    assert gh_adapter._resolve_token() == ("keyring-tok", "gh")


def test_resolve_token_anonymous_when_no_gh(monkeypatch):
    """No env var and no gh binary → anonymous."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gh_adapter.shutil, "which", lambda _name: None)
    assert gh_adapter._resolve_token() == (None, "none")


def test_resolve_token_anonymous_when_gh_fails(monkeypatch):
    """gh present but errors/unauthenticated → anonymous, never raises."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gh_adapter.shutil, "which", lambda _name: "/usr/bin/gh")

    def _boom(*a, **k):
        raise OSError("gh exploded")

    monkeypatch.setattr(gh_adapter.subprocess, "run", _boom)
    assert gh_adapter._resolve_token() == (None, "none")


def test_resolve_token_anonymous_when_gh_returns_nonzero(monkeypatch):
    """gh runs but exits non-zero (not logged in) → anonymous."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gh_adapter.shutil, "which", lambda _name: "/usr/bin/gh")

    class _Proc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(gh_adapter.subprocess, "run", lambda *a, **k: _Proc())
    assert gh_adapter._resolve_token() == (None, "none")


# ----- truncation handling -------------------------------------------------

def test_next_page_url_parses_link_header():
    """Link header with rel=next → URL extracted; no Link → None."""
    link = '<https://api.github.com/repositories/1/items?page=2>; rel="next"'
    assert gh_adapter._next_page_url(link) == "https://api.github.com/repositories/1/items?page=2"

    assert gh_adapter._next_page_url("") is None
    assert gh_adapter._next_page_url(None) is None

    # rel=last only — no next page
    link2 = '<https://api.github.com/repositories/1/items?page=3>; rel="last"'
    assert gh_adapter._next_page_url(link2) is None

    # Multiple links with next in the middle
    link3 = (
        '<https://api.github.com/repositories/1/items?page=1>; rel="first", '
        '<https://api.github.com/repositories/1/items?page=2>; rel="next", '
        '<https://api.github.com/repositories/1/items?page=3>; rel="last"'
    )
    assert gh_adapter._next_page_url(link3) == "https://api.github.com/repositories/1/items?page=2"


@pytest.mark.asyncio
async def test_tree_walk_drains_truncated_pages(monkeypatch):
    """When the tree API returns truncated entries, pagination follows them
    until no more truncated pages remain, so NO files are skipped."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/repos/docker/docs/branches/main":
            return httpx.Response(200, json={"commit": {"sha": "head1"}})
        if p == "/repos/docker/docs/git/trees/head1":
            # Root commit tree: one subdirectory (docs) + root readme
            return httpx.Response(200, text=json.dumps({
                "sha": "head1",
                "tree": [
                    {"path": "docs", "type": "tree", "sha": "docsha"},
                    {"path": "README.md", "type": "blob", "sha": "blob0"},
                ],
                "truncated": False,
            }), headers={"content-type": "application/json"})
        if p == "/repos/docker/docs/git/trees/docsha":
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First page: truncated, returns 2 dirs + 1 trailing blob
                return httpx.Response(200, text=json.dumps({
                    "sha": "docsha",
                    "tree": [
                        {"path": "engine", "type": "tree", "sha": "engsha"},
                        {"path": "setup", "type": "tree", "sha": "setp"},
                        {"path": "top.txt", "type": "blob", "sha": "blob_top"},
                    ],
                    "truncated": True,
                }), headers={"content-type": "application/json"})
            else:
                # Second page (paginated from last entry's sha): more dirs/files
                return httpx.Response(200, text=json.dumps({
                    "sha": "docsha",
                    "tree": [
                        {"path": "web", "type": "tree", "sha": "webs"},
                        {"path": "index.html", "type": "blob", "sha": "idx1"},
                    ],
                    "truncated": False,
                }), headers={"content-type": "application/json"})
        if p == "/repos/docker/docs/git/trees/blob_top":
            # Pagination cursor that happens to be a blob sha — GitHub still
            # returns the next batch of entries from the same parent dir.
            return httpx.Response(200, text=json.dumps({
                "sha": "docsha",
                "tree": [
                    {"path": "web", "type": "tree", "sha": "webs"},
                    {"path": "index.html", "type": "blob", "sha": "idx1"},
                ],
                "truncated": False,
            }), headers={"content-type": "application/json"})
        if p.startswith("/repos/docker/docs/git/trees/engsha"):
            return httpx.Response(200, text=json.dumps({
                "sha": "engsha",
                "tree": [
                    {"path": "install.md", "type": "blob", "sha": "b1"},
                ],
                "truncated": False,
            }), headers={"content-type": "application/json"})
        if p.startswith("/repos/docker/docs/git/trees/setp"):
            return httpx.Response(200, text=json.dumps({
                "sha": "setp",
                "tree": [
                    {"path": "install.md", "type": "blob", "sha": "b2"},
                ],
                "truncated": False,
            }), headers={"content-type": "application/json"})
        if p.startswith("/repos/docker/docs/git/trees/webs"):
            return httpx.Response(200, text=json.dumps({
                "sha": "webs",
                "tree": [
                    {"path": "index.html", "type": "blob", "sha": "b3"},
                ],
                "truncated": False,
            }), headers={"content-type": "application/json"})
        # Fetch-file endpoints
        if "/contents/README.md" in p:
            return httpx.Response(200, text="readme body",
                                  headers={"content-type": "text/plain"})
        if "/contents/docs/top.txt" in p:
            return httpx.Response(200, text="top file",
                                  headers={"content-type": "text/plain"})
        if "/contents/docs/engine/install.md" in p:
            return httpx.Response(200, text="engine install",
                                  headers={"content-type": "text/plain"})
        if "/contents/docs/setup/install.md" in p:
            return httpx.Response(200, text="setup install",
                                  headers={"content-type": "text/plain"})
        if "/contents/docs/web/index.html" in p:
            return httpx.Response(200, text="web index",
                                  headers={"content-type": "text/plain"})
        if "/contents/docs/index.html" in p:
            return httpx.Response(200, text="index",
                                  headers={"content-type": "text/plain"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    adapter = GitHubRepoAdapter(_transport=transport)
    # Broad include so ALL tree blobs pass the filter
    src = _src(config=json.dumps({"include": ["**"], "branch": "main"}))
    cands = [c async for c in adapter.fetch(src)]
    # Should have fetched README + all blobs from all paginated dirs
    paths = sorted(c.source_url for c in cands)
    expected_urls = sorted([
        "https://github.com/docker/docs/blob/head1/README.md",
        "https://github.com/docker/docs/blob/head1/docs/engine/install.md",
        "https://github.com/docker/docs/blob/head1/docs/index.html",
        "https://github.com/docker/docs/blob/head1/docs/setup/install.md",
        "https://github.com/docker/docs/blob/head1/docs/top.txt",
        "https://github.com/docker/docs/blob/head1/docs/web/index.html",
    ])
    assert paths == expected_urls



@pytest.mark.asyncio
async def test_compare_pagates_across_link_header(monkeypatch):
    """When the compare API returns multiple pages (via Link header),
    all changed files are collected before the cursor advances."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        # Match the compare endpoint regardless of query-string page param
        is_compare = p == "/repos/docker/docs/compare/head1...head2"
        if p == "/repos/docker/docs/branches/main":
            return httpx.Response(200, json={"commit": {"sha": "head2"}})
        if is_compare:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First page with Link header pointing to page 2
                return httpx.Response(200,
                    text=json.dumps({
                        "files": [
                            {"filename": "docs/engine/install.md", "status": "modified"},
                            {"filename": "docs/engine/new-page.md", "status": "added"},
                        ],
                        "merge_base_commit": {"sha": "head1"},
                        "commits": [{"sha": "head2"}],
                    }),
                    headers={
                        "content-type": "application/json",
                        "link": '<https://api.github.com/repos/docker/docs/compare/head1...head2?page=2>; rel="next"',
                    },
                )
            else:
                # Second page (no Link header → end of pagination)
                return httpx.Response(200,
                    text=json.dumps({
                        "files": [
                            {"filename": "docs/engine/desktop.md", "status": "modified"},
                        ],
                        "merge_base_commit": {"sha": "head1"},
                        "commits": [{"sha": "head2"}],
                    }),
                    headers={"content-type": "application/json"},
                )
        if "/contents/docs/engine/install.md" in p:
            return httpx.Response(200, text="updated install")
        if "/contents/docs/engine/new-page.md" in p:
            return httpx.Response(200, text="new page body")
        if "/contents/docs/engine/desktop.md" in p:
            return httpx.Response(200, text="desktop doc")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    adapter = GitHubRepoAdapter(_transport=transport)
    # Broad include so ALL changed files pass the filter
    src = _src(config=json.dumps({"include": ["docs/**"], "branch": "main"}),
               cursor=json.dumps({"last_sha": "head1"}))
    cands = [c async for c in adapter.fetch(src)]
    # Debug: verify pagination was exercised
    assert call_count["n"] == 2, f"Expected 2 compare API calls, got {call_count['n']}"
    urls = sorted(c.source_url for c in cands)
    assert urls == sorted([
        "https://github.com/docker/docs/blob/head2/docs/engine/desktop.md",
        "https://github.com/docker/docs/blob/head2/docs/engine/install.md",
        "https://github.com/docker/docs/blob/head2/docs/engine/new-page.md",
    ])
    # Cursor was advanced after full pagination
    cursor = json.loads(src["cursor"])
    assert cursor["last_sha"] == "head2"


@pytest.mark.asyncio
async def test_no_cursor_when_compare_first_page_missing(monkeypatch):
    """Edge case: if the first compare response has no files and no Link,
    cursor still advances (empty set of changed files is valid)."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/repos/docker/docs/branches/main":
            return httpx.Response(200, json={"commit": {"sha": "head2"}})
        if p == "/repos/docker/docs/compare/head1...head2":
            return httpx.Response(200,
                text=json.dumps({
                    "files": [],
                    "merge_base_commit": {"sha": "head1"},
                    "commits": [{"sha": "head2"}],
                }),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    adapter = GitHubRepoAdapter(_transport=transport)
    src = _src(cursor=json.dumps({"last_sha": "head1"}))
    cands = [c async for c in adapter.fetch(src)]
    assert cands == []
    cursor = json.loads(src["cursor"])
    assert cursor["last_sha"] == "head2"
