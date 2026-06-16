"""GitHub-repo adapter: tracks a public repo by branch HEAD; uses the compare
API for incremental updates after the first walk."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator

import httpx

from . import Candidate, matches_globs, register
from ._http import AsyncRateLimiter, request_with_retry

logger = logging.getLogger("engram.poller.github_repo")

# Default inter-request spacing for GitHub API calls. 0 = no proactive spacing;
# politeness is reactive -- every request flows through request_with_retry, which
# honors Retry-After and the X-RateLimit-* primary-rate-limit headers. Override
# per source with config `request_interval_ms` to also space requests.
DEFAULT_INTERVAL_MS = 0

_REPO_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+?)/?(?:$|\.git$)")

def _next_page_url(link_header: str) -> str | None:
    """Parse a GitHub ``Link`` header and return the URL for ``rel=next``, or
    ``None`` when there is no next page."""
    if not link_header:
        return None

    for segment in link_header.split(","):
        parts = [part.strip() for part in segment.split(";")]
        if not parts:
            continue

        url_part = parts[0]
        rel: str | None = None
        for param in parts[1:]:
            if "=" not in param:
                continue
            key, value = param.split("=", 1)
            if key.strip().lower() == "rel":
                rel = value.strip().strip('"')
                break

        if rel == "next" and url_part:
            return url_part.strip("<>")

    return None


_AUTH_SOURCE_LABEL = {
    "env": "GITHUB_TOKEN env var",
    "gh": "gh CLI keyring",
    "none": "anonymous (60 req/hr)",
}


def _parse_repo(url: str) -> tuple[str, str]:
    m = _REPO_RE.match(url)
    if not m:
        raise ValueError(f"not a github.com/<org>/<repo> URL: {url}")
    return m.group(1), m.group(2)


def _resolve_token() -> tuple[str | None, str]:
    """Resolve a GitHub API token: GITHUB_TOKEN env -> gh keyring -> anonymous.

    An explicit ``GITHUB_TOKEN`` wins so containers/CI can set it directly. When
    absent (e.g. a host that keeps its token in the ``gh`` credential store
    rather than the environment), fall back to ``gh auth token``. If neither is
    available, return ``None`` and the adapter polls anonymously.

    Returns ``(token, source)`` with ``source`` in {"env", "gh", "none"}.
    """
    env_token = os.environ.get("GITHUB_TOKEN")
    if env_token:
        return env_token, "env"

    gh = shutil.which("gh")
    if gh:
        # Strip GH_TOKEN/GITHUB_TOKEN so gh reads its credential store (keyring)
        # rather than echoing an env var back.
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        try:
            proc = subprocess.run(
                [gh, "auth", "token"],
                capture_output=True, text=True, timeout=5, env=clean_env,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            tok = proc.stdout.strip()
            if tok:
                return tok, "gh"

    return None, "none"


class GitHubRepoAdapter:
    name = "github-repo"

    _API_BASE = "https://api.github.com"

    def __init__(
        self,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "engram/0.1 (+source-poller)",
    ) -> None:
        # Resolve the token lazily on first fetch: this adapter is instantiated
        # at import time (self-registration), and token resolution may shell out
        # to `gh`, which must not run during import/test collection.
        self._transport = _transport
        self._user_agent = user_agent
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            token, source = _resolve_token()
            logger.info("github-repo auth: %s", _AUTH_SOURCE_LABEL[source])
            headers: dict[str, str] = {"user-agent": self._user_agent,
                                        "accept": "application/vnd.github+json"}
            if token:
                headers["authorization"] = f"Bearer {token}"
            kwargs: dict = dict(base_url=self._API_BASE, headers=headers, timeout=30.0)
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx client if one was lazily created (#92)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(
        self,
        url: str,
        rate_limiter: AsyncRateLimiter,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GitHub GET routed through the shared politeness/rate-limit helper so
        every call honors Retry-After and the X-RateLimit-* headers."""
        assert self._client is not None  # _ensure_client() ran in fetch()
        return await request_with_retry(
            self._client, url, params=params, headers=headers, rate_limiter=rate_limiter,
        )

    async def fetch(self, source: dict) -> AsyncIterator[Candidate]:
        self._ensure_client()
        cfg = json.loads(source.get("config") or "{}")
        include = cfg.get("include", [])
        exclude = cfg.get("exclude", [])
        branch = cfg.get("branch", "main")
        interval_ms = int(cfg.get("request_interval_ms", DEFAULT_INTERVAL_MS))
        rate_limiter = AsyncRateLimiter(interval_ms=interval_ms)
        cursor = json.loads(source.get("cursor") or "{}")
        last_sha: str | None = cursor.get("last_sha")
        owner, repo = _parse_repo(source["url"])

        head_resp = await self._get(f"/repos/{owner}/{repo}/branches/{branch}", rate_limiter)
        head_resp.raise_for_status()
        head_sha = head_resp.json()["commit"]["sha"]

        if last_sha and last_sha != head_sha:
            paths = await self._changed_paths(owner, repo, last_sha, head_sha, rate_limiter)
        elif last_sha == head_sha:
            paths = []
        else:
            paths = await self._tree_paths(owner, repo, head_sha, rate_limiter)

        for path in paths:
            if not matches_globs(path, include, exclude):
                continue
            body = await self._fetch_file(owner, repo, head_sha, path, rate_limiter)
            if body is None:
                continue
            url = f"https://github.com/{owner}/{repo}/blob/{head_sha}/{path}"
            yield Candidate(
                source_url=url,
                body=body,
                title=path.rsplit("/", 1)[-1],
            )

        source["cursor"] = json.dumps({"last_sha": head_sha})

    async def _tree_paths(
        self, owner: str, repo: str, sha: str, rate_limiter: AsyncRateLimiter,
    ) -> list[str]:
        """Walk the repository tree recursively with one request per subtree.

        Returns only blob paths (not dirs, symlinks, or submodules).
        """
        queue: list[tuple[str, str]] = [(sha, "")]   # (tree_sha, prefix)
        all_blobs: list[str] = []

        while queue:
            tree_sha, prefix = queue.pop()

            r = await self._get(
                f"/repos/{owner}/{repo}/git/trees/{tree_sha}", rate_limiter,
            )
            r.raise_for_status()
            data = r.json()

            if data.get("truncated"):
                raise RuntimeError(
                    "GitHub tree response was truncated; refusing to advance "
                    "cursor past unseen files"
                )

            for entry in data.get("tree", []):
                if entry["type"] == "blob":
                    child_path = (
                        f"{prefix}/{entry['path']}" if prefix else entry["path"]
                    )
                    all_blobs.append(child_path)
                elif entry["type"] == "tree":
                    child_path = (
                        f"{prefix}/{entry['path']}" if prefix else entry["path"]
                    )
                    queue.append((entry["sha"], child_path))

        return all_blobs

    async def _changed_paths(
        self, owner: str, repo: str, base: str, head: str, rate_limiter: AsyncRateLimiter,
    ) -> list[str]:
        """Drain ALL pages of the compare API (follows ``Link`` headers).
        The GitHub compare API caps each response at 3 000 files; the pagination
        Link header tells us where to fetch next.  We follow it until exhausted
        so no changed files are silently skipped before the cursor advances."""
        all_files: list[str] = []
        url = f"/repos/{owner}/{repo}/compare/{base}...{head}"
        while url:
            r = await self._get(url, rate_limiter)
            r.raise_for_status()
            data = r.json()
            all_files.extend(
                f["filename"] for f in data.get("files", [])
                if f.get("status") in ("added", "modified", "renamed")
            )
            url = _next_page_url(r.headers.get("link", ""))
        return all_files

    async def _fetch_file(
        self, owner: str, repo: str, sha: str, path: str, rate_limiter: AsyncRateLimiter,
    ) -> str | None:
        r = await self._get(
            f"/repos/{owner}/{repo}/contents/{path}", rate_limiter, params={"ref": sha},
            headers={"accept": "application/vnd.github.v3.raw"},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text


register(GitHubRepoAdapter())
