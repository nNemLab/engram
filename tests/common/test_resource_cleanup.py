"""#92: long-lived resources must be released on shutdown.

Covers the three leaks called out in the issue:
  (a) daemon run loops never closed their DB connection;
  (b) poller adapters never aclose()d their httpx.AsyncClient;
  (c) the watcher debouncer kept fired Timer objects in its map forever.
"""
import httpx
import pytest

# ---------------------------------------------------------------------------
# (a) daemon run() loops close the DB connection on exit (finally path)
# ---------------------------------------------------------------------------

class _SpyConn:
    """Stand-in for a sqlite3 connection that records close()."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_reactor_run_closes_connection_on_exit(monkeypatch):
    from engram.reactor import reactor as rmod

    spy = _SpyConn()
    monkeypatch.setattr(rmod, "get_connection", lambda: spy)

    def _boom(_conn):
        raise KeyboardInterrupt

    monkeypatch.setattr(rmod, "_run_loop", _boom)
    with pytest.raises(KeyboardInterrupt):
        rmod.run()
    assert spy.closed is True


def test_projector_run_closes_connection_on_exit(monkeypatch):
    from engram.projector import projector as pmod

    spy = _SpyConn()
    monkeypatch.setattr(pmod, "get_connection", lambda: spy)

    def _boom(_conn):
        raise KeyboardInterrupt

    monkeypatch.setattr(pmod, "_run_loop", _boom)
    with pytest.raises(KeyboardInterrupt):
        pmod.run()
    assert spy.closed is True


@pytest.mark.asyncio
async def test_poller_run_aclose_adapters_and_closes_conn(monkeypatch):
    """(a)+(b): poller shutdown closes the DB connection AND aclose()es adapters."""
    from engram.poller import poller as pmod

    state = {"adapter_closed": False}
    spy = _SpyConn()

    class _FakeAdapter:
        name = "fake-cleanup"

        async def aclose(self) -> None:
            state["adapter_closed"] = True

    monkeypatch.setattr(pmod, "load_config", lambda: None)
    monkeypatch.setattr(pmod, "get_connection", lambda: spy)
    # Replace the registry entirely so we don't touch the real adapter singletons.
    monkeypatch.setattr(pmod, "ADAPTERS", {"fake-cleanup": _FakeAdapter()})

    class _Stop(Exception):
        pass

    async def _stop(_seconds):
        raise _Stop

    monkeypatch.setattr(pmod.asyncio, "sleep", _stop)

    with pytest.raises(_Stop):
        await pmod.run()

    assert state["adapter_closed"] is True
    assert spy.closed is True


# ---------------------------------------------------------------------------
# (b) each adapter's aclose() closes its underlying httpx client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_urls_adapter_aclose_closes_client():
    from engram.poller.adapters.urls import UrlsAdapter

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    adapter = UrlsAdapter(_client=client)
    assert client.is_closed is False
    await adapter.aclose()
    assert client.is_closed is True


@pytest.mark.asyncio
async def test_sitemap_adapter_aclose_closes_client():
    from engram.poller.adapters.sitemap import SitemapAdapter

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    adapter = SitemapAdapter(_client=client)
    await adapter.aclose()
    assert client.is_closed is True


@pytest.mark.asyncio
async def test_mediawiki_adapter_aclose_closes_client():
    from engram.poller.adapters.mediawiki_api import MediaWikiApiAdapter

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    adapter = MediaWikiApiAdapter(_client=client)
    await adapter.aclose()
    assert client.is_closed is True


@pytest.mark.asyncio
async def test_github_adapter_aclose_closes_lazy_client(monkeypatch):
    from engram.poller.adapters.github_repo import GitHubRepoAdapter

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")  # hermetic: skip gh keyring lookup
    adapter = GitHubRepoAdapter(_transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    # No client yet (lazy) -> aclose is a no-op and must not raise.
    await adapter.aclose()
    client = adapter._ensure_client()
    assert client.is_closed is False
    await adapter.aclose()
    assert client.is_closed is True


# ---------------------------------------------------------------------------
# (c) the watcher debouncer drops fired timers and can cancel pending ones
# ---------------------------------------------------------------------------

def test_debouncer_drops_fired_timer():
    import time

    from engram.watcher.watcher import _Debouncer

    fired: list[str] = []
    d = _Debouncer(delay_ms=10, fn=lambda key: fired.append(key))
    d.trigger("note.md", "note.md")
    # Wait for the timer to fire.
    deadline = time.time() + 2.0
    while not fired and time.time() < deadline:
        time.sleep(0.01)

    assert fired == ["note.md"]
    # The fired timer must not linger in the map (#92: no unbounded growth).
    assert d.timers == {}


def test_debouncer_retriggers_dont_leak():
    import time

    from engram.watcher.watcher import _Debouncer

    count = {"n": 0}
    d = _Debouncer(delay_ms=10, fn=lambda: count.__setitem__("n", count["n"] + 1))
    for _ in range(5):
        d.trigger("same")
        time.sleep(0.001)
    # Let the surviving timer fire.
    deadline = time.time() + 2.0
    while count["n"] == 0 and time.time() < deadline:
        time.sleep(0.01)

    assert count["n"] >= 1
    assert d.timers == {}


def test_debouncer_cancel_all_clears_pending():
    from engram.watcher.watcher import _Debouncer

    d = _Debouncer(delay_ms=60_000, fn=lambda: None)  # long delay: stays pending
    d.trigger("a")
    d.trigger("b")
    assert len(d.timers) == 2
    d.cancel_all()
    assert d.timers == {}
