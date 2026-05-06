import json
from pathlib import Path

import httpx
import pytest

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
    adapter = GitHubRepoAdapter(_client=httpx.AsyncClient(transport=transport))

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
    adapter = GitHubRepoAdapter(_client=httpx.AsyncClient(transport=transport))

    src = _src(cursor=json.dumps({"last_sha": "head1"}))
    cands = [c async for c in adapter.fetch(src)]
    urls = sorted(c.source_url for c in cands)
    assert urls == sorted([
        "https://github.com/docker/docs/blob/head2/docs/engine/install.md",
        "https://github.com/docker/docs/blob/head2/docs/engine/new-page.md",
    ])
    cursor = json.loads(src["cursor"])
    assert cursor["last_sha"] == "head2"
