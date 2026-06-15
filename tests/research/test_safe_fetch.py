"""SSRF guard: scheme pinning, non-public IP rejection, per-hop redirect
re-validation (issue #31), and DNS-rebinding/TOCTOU connection pinning (#95)."""
import socket

import httpx
import pytest

from engram.research import safe_fetch
from engram.research.safe_fetch import UnsafeURLError, assert_public_url

# Public IP literal (example.com's documentation range) — only ever hit a
# MockTransport, never the network.
PUBLIC = "93.184.216.34"
PUBLIC2 = "93.184.216.35"


# --- assert_public_url -------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://127.0.0.1:8080/admin",
    "http://10.0.0.5/",
    "http://192.168.50.7:8000/v1",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/",
    "http://[::1]/",
    "http://[fd00::1]/",
])
def test_rejects_non_public_ip_literals(url):
    with pytest.raises(UnsafeURLError):
        assert_public_url(url)


@pytest.mark.parametrize("url", [
    "ftp://93.184.216.34/file",
    "file:///etc/passwd",
    "gopher://93.184.216.34/",
])
def test_rejects_non_http_schemes(url):
    with pytest.raises(UnsafeURLError):
        assert_public_url(url)


def test_rejects_url_without_host():
    with pytest.raises(UnsafeURLError):
        assert_public_url("http:///path")


def test_accepts_public_ip_literal():
    assert_public_url(f"http://{PUBLIC}/page")


def test_hostname_resolving_to_private_ip_rejected(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 80))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError):
        assert_public_url("http://evil.example/")


def test_hostname_resolving_to_public_ip_accepted(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 80))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert_public_url("https://good.example/")


def test_hostname_with_any_private_answer_rejected(monkeypatch):
    """One private A record among public ones is enough to reject."""
    def fake_getaddrinfo(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
        ]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError):
        assert_public_url("http://rebind.example/")


def test_unresolvable_hostname_rejected(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **kw):
        raise socket.gaierror("NXDOMAIN")
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeURLError):
        assert_public_url("http://nxdomain.example/")


# --- sync get: redirect hops re-validated ------------------------------------

def test_sync_get_blocks_redirect_to_loopback():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1:9999/secret"})
    with pytest.raises(UnsafeURLError):
        safe_fetch.get(f"http://{PUBLIC}/", transport=httpx.MockTransport(handler))


def test_sync_get_follows_public_redirect():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": f"http://{PUBLIC}/end"})
        return httpx.Response(200, text="ok")
    r = safe_fetch.get(f"http://{PUBLIC}/start", transport=httpx.MockTransport(handler))
    assert r.status_code == 200
    assert r.text == "ok"


def test_sync_get_caps_redirect_count():
    def handler(request):
        return httpx.Response(302, headers={"location": f"http://{PUBLIC}/loop"})
    with pytest.raises(UnsafeURLError):
        safe_fetch.get(f"http://{PUBLIC}/loop",
                       transport=httpx.MockTransport(handler), max_redirects=3)


def test_sync_get_validates_before_any_request():
    def handler(request):
        raise AssertionError("request must not be sent for a private URL")
    with pytest.raises(UnsafeURLError):
        safe_fetch.get("http://192.168.0.1/", transport=httpx.MockTransport(handler))


# --- async get: same guarantees ----------------------------------------------

async def test_async_get_blocks_redirect_to_lan():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://192.168.50.7:8000/v1/models"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeURLError):
            await safe_fetch.get_async(client, f"http://{PUBLIC}/")


async def test_async_get_follows_public_redirect():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": f"http://{PUBLIC}/end"})
        return httpx.Response(200, text="ok")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await safe_fetch.get_async(client, f"http://{PUBLIC}/start")
    assert r.status_code == 200
    assert r.text == "ok"


async def test_async_get_rejects_private_url_before_request():
    def handler(request):
        raise AssertionError("request must not be sent for a private URL")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeURLError):
            await safe_fetch.get_async(client, "http://[::1]:6379/")


# --- DNS-rebinding / TOCTOU: the validated IP is pinned (issue #95) -----------

def _flipping_resolver(first_ip, later_ip, calls):
    """getaddrinfo stand-in modelling a rebinding server: the first lookup
    answers `first_ip`, every later lookup answers `later_ip`."""
    def fake_getaddrinfo(host, port, *a, **kw):
        ip = first_ip if not calls else later_ip
        calls.append(ip)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80))]
    return fake_getaddrinfo


def test_sync_get_pins_validated_ip_to_connection(monkeypatch):
    """A hostname is resolved once; the connection is pinned to that validated
    IP. Even though a second lookup would rebind to loopback, the request must
    target the validated public IP (not the hostname) and resolve only once."""
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo",
                        _flipping_resolver(PUBLIC, "127.0.0.1", calls))
    seen = {}

    def handler(request):
        seen["host"] = request.url.host
        seen["host_header"] = request.headers.get("host")
        return httpx.Response(200, text="ok")

    r = safe_fetch.get("http://rebind.example/page",
                       transport=httpx.MockTransport(handler))
    assert r.status_code == 200
    assert calls == [PUBLIC]               # resolved exactly once — no re-resolve
    assert seen["host"] == PUBLIC          # connection pinned to the validated IP
    assert seen["host_header"] == "rebind.example"  # original Host preserved


async def test_async_get_pins_validated_ip_to_connection(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo",
                        _flipping_resolver(PUBLIC, "127.0.0.1", calls))
    seen = {}

    def handler(request):
        seen["host"] = request.url.host
        seen["host_header"] = request.headers.get("host")
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await safe_fetch.get_async(client, "http://rebind.example/page")
    assert r.status_code == 200
    assert calls == [PUBLIC]
    assert seen["host"] == PUBLIC
    assert seen["host_header"] == "rebind.example"


def test_sync_get_refuses_host_resolving_to_private(monkeypatch):
    """A hostname whose (single) resolution is private is refused, no send."""
    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 80))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request):
        raise AssertionError("must not send to a host that resolves to loopback")

    with pytest.raises(UnsafeURLError):
        safe_fetch.get("http://rebind.example/", transport=httpx.MockTransport(handler))


def test_sync_get_blocks_redirect_to_private_hostname(monkeypatch):
    """A redirect to a *hostname* that resolves to a private IP is rejected,
    proving every hop is re-resolved and re-validated."""
    def fake_getaddrinfo(host, port, *a, **kw):
        ip = PUBLIC if host == "good.example" else "10.0.0.5"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request):
        if request.headers.get("host") == "good.example":
            return httpx.Response(302, headers={"location": "http://evil.example/x"})
        raise AssertionError("must not follow redirect to a private hostname")

    with pytest.raises(UnsafeURLError):
        safe_fetch.get("http://good.example/", transport=httpx.MockTransport(handler))


async def test_async_get_blocks_redirect_to_private_hostname(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **kw):
        ip = PUBLIC if host == "good.example" else "10.0.0.5"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request):
        if request.headers.get("host") == "good.example":
            return httpx.Response(302, headers={"location": "http://evil.example/x"})
        raise AssertionError("must not follow redirect to a private hostname")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeURLError):
            await safe_fetch.get_async(client, "http://good.example/")


async def test_async_get_tries_next_validated_ip_on_connect_failure(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port or 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC2, port or 80)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    seen_hosts: list[str] = []

    def handler(request):
        seen_hosts.append(request.url.host)
        if request.url.host == PUBLIC:
            raise httpx.ConnectError("first IP failed", request=request)
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await safe_fetch.get_async(client, "http://retry.example/")

    assert r.status_code == 200
    assert seen_hosts == [PUBLIC, PUBLIC2]


def test_sync_get_tries_next_validated_ip_on_connect_failure(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port or 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC2, port or 80)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    seen_hosts: list[str] = []

    def handler(request):
        seen_hosts.append(request.url.host)
        if request.url.host == PUBLIC:
            raise httpx.ConnectError("first IP failed", request=request)
        return httpx.Response(200, text="ok")

    r = safe_fetch.get("http://retry.example/", transport=httpx.MockTransport(handler))

    assert r.status_code == 200
    assert seen_hosts == [PUBLIC, PUBLIC2]


def test_sync_get_follows_public_hostname_redirect(monkeypatch):
    """Hostname → hostname redirect, both public, still works end to end with
    each hop pinned to its validated IP."""
    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port or 80))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request):
        if request.headers.get("host") == "start.example":
            return httpx.Response(301, headers={"location": "http://end.example/done"})
        assert request.headers.get("host") == "end.example"
        assert request.url.host == PUBLIC
        return httpx.Response(200, text="ok")

    r = safe_fetch.get("http://start.example/", transport=httpx.MockTransport(handler))
    assert r.status_code == 200
    assert r.text == "ok"
