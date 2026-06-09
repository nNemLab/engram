"""GitHub-repo adapter: tracks a public repo by branch HEAD; uses the compare
API for incremental updates after the first walk."""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

from . import Candidate, matches_globs, register

logger = logging.getLogger("engram.poller.github_repo")

_REPO_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+?)/?(?:$|\.git$)")


def _parse_repo(url: str) -> tuple[str, str]:
    m = _REPO_RE.match(url)
    if not m:
        raise ValueError(f"not a github.com/<org>/<repo> URL: {url}")
    return m.group(1), m.group(2)


class GitHubRepoAdapter:
    name = "github-repo"

    _API_BASE = "https://api.github.com"

    def __init__(
        self,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "engram/0.1 (+source-poller)",
    ) -> None:
        token = os.environ.get("GITHUB_TOKEN")
        headers: dict[str, str] = {"user-agent": user_agent,
                                    "accept": "application/vnd.github+json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        kwargs: dict = dict(base_url=self._API_BASE, headers=headers, timeout=30.0)
        if _transport is not None:
            kwargs["transport"] = _transport
        self._client = httpx.AsyncClient(**kwargs)

    async def fetch(self, source: dict) -> AsyncIterator[Candidate]:
        cfg = json.loads(source.get("config") or "{}")
        include = cfg.get("include", [])
        exclude = cfg.get("exclude", [])
        branch = cfg.get("branch", "main")
        cursor = json.loads(source.get("cursor") or "{}")
        last_sha: str | None = cursor.get("last_sha")
        owner, repo = _parse_repo(source["url"])

        head_resp = await self._client.get(f"/repos/{owner}/{repo}/branches/{branch}")
        head_resp.raise_for_status()
        head_sha = head_resp.json()["commit"]["sha"]

        if last_sha and last_sha != head_sha:
            paths = await self._changed_paths(owner, repo, last_sha, head_sha)
        elif last_sha == head_sha:
            paths = []
        else:
            paths = await self._tree_paths(owner, repo, head_sha)

        for path in paths:
            if not matches_globs(path, include, exclude):
                continue
            body = await self._fetch_file(owner, repo, head_sha, path)
            if body is None:
                continue
            url = f"https://github.com/{owner}/{repo}/blob/{head_sha}/{path}"
            yield Candidate(
                source_url=url,
                body=body,
                title=path.rsplit("/", 1)[-1],
                fetched_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                metadata={"sha": head_sha, "path": path},
            )

        source["cursor"] = json.dumps({"last_sha": head_sha})

    async def _tree_paths(self, owner: str, repo: str, sha: str) -> list[str]:
        r = await self._client.get(f"/repos/{owner}/{repo}/git/trees/{sha}",
                                    params={"recursive": "1"})
        r.raise_for_status()
        data = r.json()
        return [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"]

    async def _changed_paths(self, owner: str, repo: str, base: str, head: str) -> list[str]:
        r = await self._client.get(f"/repos/{owner}/{repo}/compare/{base}...{head}")
        r.raise_for_status()
        data = r.json()
        return [
            f["filename"] for f in data.get("files", [])
            if f.get("status") in ("added", "modified", "renamed")
        ]

    async def _fetch_file(self, owner: str, repo: str, sha: str, path: str) -> str | None:
        r = await self._client.get(
            f"/repos/{owner}/{repo}/contents/{path}", params={"ref": sha},
            headers={"accept": "application/vnd.github.v3.raw"},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text


register(GitHubRepoAdapter())
