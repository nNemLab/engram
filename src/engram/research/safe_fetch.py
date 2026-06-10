"""SSRF-guarded HTTP fetching for the research tools.

URLs reaching the fetchers come from SearXNG results or the agent, so host and
scheme are attacker-influenceable — especially via redirects. Every hop is
validated before the request is sent: scheme pinned to http/https, and every
address the host resolves to must be globally routable (no loopback, RFC1918,
link-local, CGN, etc.).
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

_MAX_REDIRECTS = 5


class UnsafeURLError(ValueError):
    """URL points at a non-public destination (or uses a non-http scheme)."""


def assert_public_url(url: str) -> None:
    """Raise UnsafeURLError unless `url` is http(s) to a publicly-routable host.

    Hostnames are resolved and *every* returned address must be global —
    a single private A/AAAA record rejects the URL.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"refusing non-http(s) URL: {url}")
    host = parts.hostname
    if not host:
        raise UnsafeURLError(f"URL has no host: {url}")

    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parts.port or 0, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise UnsafeURLError(f"cannot resolve host {host!r}: {e}") from e
        addrs = [ipaddress.ip_address(info[4][0]) for info in infos]

    if not addrs:
        raise UnsafeURLError(f"host {host!r} resolved to no addresses")
    for ip in addrs:
        if not ip.is_global:
            raise UnsafeURLError(f"refusing non-public address {ip} for {url}")


def _next_hop(url: str, response: httpx.Response) -> str | None:
    if response.is_redirect and "location" in response.headers:
        return str(httpx.URL(url).join(response.headers["location"]))
    return None


def get(url: str, *, timeout: float = 25.0, headers: dict[str, str] | None = None,
        max_redirects: int = _MAX_REDIRECTS,
        transport: httpx.BaseTransport | None = None) -> httpx.Response:
    """httpx.get with the SSRF guard applied to the URL and every redirect hop."""
    with httpx.Client(transport=transport, timeout=timeout, headers=headers) as client:
        for _ in range(max_redirects + 1):
            assert_public_url(url)
            r = client.get(url, follow_redirects=False)
            nxt = _next_hop(url, r)
            if nxt is None:
                return r
            url = nxt
    raise UnsafeURLError(f"too many redirects fetching {url}")


async def get_async(client: httpx.AsyncClient, url: str, *, timeout: float = 12.0,
                    headers: dict[str, str] | None = None,
                    max_redirects: int = _MAX_REDIRECTS) -> httpx.Response:
    """Async variant; DNS validation runs in a thread to keep the loop free."""
    for _ in range(max_redirects + 1):
        await asyncio.to_thread(assert_public_url, url)
        r = await client.get(url, timeout=timeout, headers=headers, follow_redirects=False)
        nxt = _next_hop(url, r)
        if nxt is None:
            return r
        url = nxt
    raise UnsafeURLError(f"too many redirects fetching {url}")
