"""SSRF-guarded HTTP fetching for the research tools.

URLs reaching the fetchers come from SearXNG results or the agent, so host and
scheme are attacker-influenceable — especially via redirects. Every hop is
validated before the request is sent: scheme pinned to http/https, and every
address the host resolves to must be globally routable (no loopback, RFC1918,
link-local, CGN, etc.).

DNS-rebinding (TOCTOU) defence: validating the hostname and *then* issuing the
request against that same hostname lets httpx/httpcore re-resolve it
independently at connect time, so a rebinding name can answer with a public IP
during validation and a private one (127.0.0.1 / 169.254.169.254 / RFC1918) at
connect. To close that window the host is resolved exactly once; the returned
address(es) are validated; and the connection is *pinned* to a validated IP —
the request targets the IP literal directly while the original hostname is
preserved for the ``Host`` header and the TLS SNI, so certificate verification
still runs against the hostname. No second DNS lookup can occur between check
and connect. Re-validation runs on every redirect hop.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

_MAX_REDIRECTS = 5
_DEFAULT_PORTS = {"http": 80, "https": 443}

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UnsafeURLError(ValueError):
    """URL points at a non-public destination (or uses a non-http scheme)."""


def _resolve(url: str) -> tuple[str, str, int | None, list[_IPAddress], bool]:
    """Validate scheme/host and resolve `url` to IP address(es).

    Returns ``(scheme, host, port, addrs, host_is_ip_literal)``. Raises
    UnsafeURLError on a non-http(s) scheme, a missing host, or a resolution
    failure. Does *not* check global-routability — that is `_reject_non_global`.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"refusing non-http(s) URL: {url}")
    host = parts.hostname
    if not host:
        raise UnsafeURLError(f"URL has no host: {url}")

    try:
        addrs = [ipaddress.ip_address(host)]
        is_literal = True
    except ValueError:
        is_literal = False
        try:
            infos = socket.getaddrinfo(host, parts.port or 0, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise UnsafeURLError(f"cannot resolve host {host!r}: {e}") from e
        addrs = [ipaddress.ip_address(info[4][0]) for info in infos]

    if not addrs:
        raise UnsafeURLError(f"host {host!r} resolved to no addresses")
    return parts.scheme, host, parts.port, addrs, is_literal


def _reject_non_global(addrs: list[_IPAddress], url: str) -> None:
    for ip in addrs:
        if not ip.is_global:
            raise UnsafeURLError(f"refusing non-public address {ip} for {url}")


def assert_public_url(url: str) -> None:
    """Raise UnsafeURLError unless `url` is http(s) to a publicly-routable host.

    Hostnames are resolved and *every* returned address must be global —
    a single private A/AAAA record rejects the URL.
    """
    _, _, _, addrs, _ = _resolve(url)
    _reject_non_global(addrs, url)


def _pin_to_validated_ip(
    url: str,
) -> tuple[str, dict[str, str] | None, dict[str, str] | None]:
    """Validate `url` and return ``(connect_url, headers, extensions)`` that pin
    the connection to a validated IP, closing the DNS-rebinding window.

    For an IP-literal host the URL is returned unchanged (no DNS, nothing to
    pin). For a hostname the connect URL targets a validated IP literal, with
    the original host carried in the ``Host`` header and the TLS ``sni_hostname``
    so certificate verification still runs against the hostname. Raises
    UnsafeURLError if any resolved address is not globally routable.
    """
    scheme, host, port, addrs, is_literal = _resolve(url)
    _reject_non_global(addrs, url)
    if is_literal:
        return url, None, None

    ip = addrs[0]
    connect_url = str(httpx.URL(url).copy_with(host=str(ip)))
    default_port = _DEFAULT_PORTS[scheme]
    host_header = host if port in (None, default_port) else f"{host}:{port}"
    return connect_url, {"Host": host_header}, {"sni_hostname": host}


def _next_hop(url: str, response: httpx.Response) -> str | None:
    if response.is_redirect and "location" in response.headers:
        return str(httpx.URL(url).join(response.headers["location"]))
    return None


def get(url: str, *, timeout: float = 25.0, headers: dict[str, str] | None = None,
        max_redirects: int = _MAX_REDIRECTS,
        transport: httpx.BaseTransport | None = None) -> httpx.Response:
    """httpx.get with the SSRF guard applied to the URL and every redirect hop.

    Each hop is resolved + validated once and the connection is pinned to the
    validated IP (see module docstring) so the hostname cannot be re-resolved to
    a private address between validation and connect.
    """
    with httpx.Client(transport=transport, timeout=timeout, headers=headers) as client:
        for _ in range(max_redirects + 1):
            connect_url, pin_headers, extensions = _pin_to_validated_ip(url)
            r = client.get(connect_url, follow_redirects=False,
                           headers=pin_headers, extensions=extensions)
            nxt = _next_hop(url, r)
            if nxt is None:
                return r
            url = nxt
    raise UnsafeURLError(f"too many redirects fetching {url}")


async def get_async(client: httpx.AsyncClient, url: str, *, timeout: float = 12.0,
                    headers: dict[str, str] | None = None,
                    max_redirects: int = _MAX_REDIRECTS) -> httpx.Response:
    """Async variant; DNS validation + IP pinning runs in a thread to keep the
    loop free. The connection is pinned to the validated IP on every hop."""
    for _ in range(max_redirects + 1):
        connect_url, pin_headers, extensions = await asyncio.to_thread(
            _pin_to_validated_ip, url)
        merged = {**(headers or {}), **pin_headers} if pin_headers else headers
        r = await client.get(connect_url, timeout=timeout, headers=merged,
                             extensions=extensions, follow_redirects=False)
        nxt = _next_hop(url, r)
        if nxt is None:
            return r
        url = nxt
    raise UnsafeURLError(f"too many redirects fetching {url}")
