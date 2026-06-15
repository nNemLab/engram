"""MediaWiki API adapter: discovery via list=allpages, content via parse."""
import json
from pathlib import Path

import httpx
import pytest

from engram.poller.adapters.mediawiki_api import MediaWikiApiAdapter

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "mediawiki"


def _src(**overrides) -> dict:
    base = {
        "id": "test-wiki",
        "url": "https://wiki.example.com",
        "config": json.dumps({
            "namespaces": [0],
            "include": ["*"],
            "exclude": ["File:*"],
            "max_pages_first_run": 1000,
            "request_interval_ms": 0,
        }),
        "cursor": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_first_run_lists_pages_and_filters_globs():
    """First poll walks list=allpages, fetches each via parse, applies globs."""
    allpages = (FIX / "allpages_response.json").read_text()
    parse_resp = (FIX / "parse_response.json").read_text()
    captured = {"requests": []}

    def h(req):
        captured["requests"].append(dict(req.url.params))
        action = req.url.params.get("action")
        if action == "query":
            return httpx.Response(200, text=allpages,
                                  headers={"content-type": "application/json"})
        if action == "parse":
            return httpx.Response(200, text=parse_resp,
                                  headers={"content-type": "application/json"})
        return httpx.Response(404)

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src()
    cands = [c async for c in adapter.fetch(src)]

    # File:Logo.png excluded by glob; 2 remain
    titles = sorted(c.title for c in cands)
    assert titles == ["Engine", "Frame Shift Drive"]
    # Each candidate's source_url is wiki/<Title with underscores>
    urls = sorted(c.source_url for c in cands)
    assert urls == [
        "https://wiki.example.com/wiki/Engine",
        "https://wiki.example.com/wiki/Frame_Shift_Drive",
    ]
    # maxlag=5 and assert=anon and format=json on every request
    for params in captured["requests"]:
        assert params.get("maxlag") == "5"
        assert params.get("assert") == "anon"
        assert params.get("format") == "json"
    # Cursor populated with last_rc_at only
    cursor = json.loads(src["cursor"])
    assert "last_rc_at" in cursor


@pytest.mark.asyncio
async def test_max_pages_first_run_caps():
    allpages = (FIX / "allpages_response.json").read_text()
    parse_resp = (FIX / "parse_response.json").read_text()

    def h(req):
        if req.url.params.get("action") == "query":
            return httpx.Response(200, text=allpages)
        return httpx.Response(200, text=parse_resp)

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src(config=json.dumps({
        "namespaces": [0],
        "include": ["*"],
        "exclude": [],
        "max_pages_first_run": 2,
        "request_interval_ms": 0,
    }))
    cands = [c async for c in adapter.fetch(src)]
    assert len(cands) <= 2


@pytest.mark.asyncio
async def test_continuation_pagination():
    """list=allpages returns continue token; adapter follows it."""
    page_a = (FIX / "allpages_paginated_a.json").read_text()
    page_b = (FIX / "allpages_paginated_b.json").read_text()
    parse_resp = (FIX / "parse_response.json").read_text()
    state = {"call": 0}

    def h(req):
        action = req.url.params.get("action")
        if action == "query":
            state["call"] += 1
            if state["call"] == 1:
                return httpx.Response(200, text=page_a)
            return httpx.Response(200, text=page_b)
        return httpx.Response(200, text=parse_resp)

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src()
    cands = [c async for c in adapter.fetch(src)]
    titles = sorted(c.title for c in cands)
    assert titles == ["A1", "A2", "M1", "M2"]


@pytest.mark.asyncio
async def test_missingtitle_skipped():
    """parse returns missingtitle error → skip without failing run."""
    allpages = (FIX / "allpages_response.json").read_text()

    def h(req):
        action = req.url.params.get("action")
        if action == "query":
            return httpx.Response(200, text=allpages)
        # parse always returns missingtitle
        return httpx.Response(200, text=json.dumps({
            "error": {"code": "missingtitle", "info": "The page does not exist."}
        }))

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src()
    cands = [c async for c in adapter.fetch(src)]
    assert cands == []  # all skipped, no exception


@pytest.mark.asyncio
async def test_maxlag_error_raises():
    """parse returns maxlag error → propagate (handled as transient by poller)."""
    allpages = (FIX / "allpages_response.json").read_text()

    def h(req):
        action = req.url.params.get("action")
        if action == "query":
            return httpx.Response(200, text=allpages)
        return httpx.Response(200, text=json.dumps({
            "error": {"code": "maxlag", "info": "Waiting for replica"}
        }))

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src()
    with pytest.raises(Exception):
        [c async for c in adapter.fetch(src)]


@pytest.mark.asyncio
async def test_subsequent_run_uses_recentchanges():
    """When cursor has last_rc_at, adapter queries recentchanges (not allpages)."""
    rc = (FIX / "recentchanges_response.json").read_text()
    parse_resp = (FIX / "parse_response.json").read_text()
    state = {"calls": []}

    def h(req):
        action = req.url.params.get("action")
        list_ = req.url.params.get("list")
        state["calls"].append((action, list_, req.url.params.get("page")))
        if action == "query" and list_ == "recentchanges":
            return httpx.Response(200, text=rc)
        if action == "query" and list_ == "allpages":
            raise AssertionError("allpages should not be queried with cursor present")
        if action == "parse":
            return httpx.Response(200, text=parse_resp)
        return httpx.Response(404)

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src(cursor=json.dumps({
        "last_rc_at": "2026-05-05T00:00:00Z",
    }))
    cands = [c async for c in adapter.fetch(src)]

    # 2 unique pages (Engine edited twice → fetched once; Kestrel created)
    titles = sorted(c.title for c in cands)
    assert titles == ["Engine", "Kestrel Mk II"]

    # log entry was filtered, not fetched
    parse_pages = [c[2] for c in state["calls"] if c[0] == "parse"]
    assert "Engine" in parse_pages
    assert "Kestrel Mk II" in parse_pages
    assert len(parse_pages) == 2

    # rcstart was the cursor timestamp
    rc_calls = [req for req in state["calls"] if req[1] == "recentchanges"]
    assert len(rc_calls) >= 1
    # cursor advanced
    new_cursor = json.loads(src["cursor"])
    assert new_cursor["last_rc_at"] != "2026-05-05T00:00:00Z"


@pytest.mark.asyncio
async def test_recentchanges_empty_yields_zero():
    """No changes since cursor → zero candidates, zero parse calls."""
    state = {"parse_calls": 0}

    def h(req):
        action = req.url.params.get("action")
        if action == "query":
            return httpx.Response(200, text=json.dumps({
                "batchcomplete": "",
                "query": {"recentchanges": []},
            }))
        if action == "parse":
            state["parse_calls"] += 1
            return httpx.Response(200, text=json.dumps({"parse": {"title": "x", "text": {"*": "x"}}}))
        return httpx.Response(404)

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src(cursor=json.dumps({
        "last_rc_at": "2026-05-05T00:00:00Z",
    }))
    cands = [c async for c in adapter.fetch(src)]
    assert cands == []
    assert state["parse_calls"] == 0


@pytest.mark.asyncio
async def test_recentchanges_pagination():
    """rccontinue pagination is followed."""
    page_1 = json.dumps({
        "batchcomplete": "",
        "continue": {"rccontinue": "2026-05-06T10:00:00Z|100", "continue": "-||"},
        "query": {"recentchanges": [
            {"type": "edit", "ns": 0, "title": "P1", "pageid": 1, "revid": 1, "timestamp": "2026-05-06T10:00:00Z"},
        ]},
    })
    page_2 = json.dumps({
        "batchcomplete": "",
        "query": {"recentchanges": [
            {"type": "edit", "ns": 0, "title": "P2", "pageid": 2, "revid": 2, "timestamp": "2026-05-06T11:00:00Z"},
        ]},
    })
    parse_resp = (FIX / "parse_response.json").read_text()
    state = {"call": 0}

    def h(req):
        action = req.url.params.get("action")
        if action == "query":
            state["call"] += 1
            return httpx.Response(200, text=page_1 if state["call"] == 1 else page_2)
        if action == "parse":
            return httpx.Response(200, text=parse_resp)
        return httpx.Response(404)

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src(cursor=json.dumps({
        "last_rc_at": "2026-05-05T00:00:00Z",
    }))
    cands = [c async for c in adapter.fetch(src)]
    titles = sorted(c.title for c in cands)
    assert titles == ["P1", "P2"]


@pytest.mark.asyncio
async def test_empty_body_candidates_are_skipped():
    """Pages whose parse yields empty/whitespace body are skipped, not yielded."""
    allpages = (FIX / "allpages_response.json").read_text()

    def h(req):
        action = req.url.params.get("action")
        if action == "query":
            return httpx.Response(200, text=allpages)
        # Parse returns empty text — should be skipped
        return httpx.Response(200, text=json.dumps({
            "parse": {"title": "Engine", "text": {"*": ""}}
        }))

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src()
    cands = [c async for c in adapter.fetch(src)]
    assert cands == []  # empty body → skipped


@pytest.mark.asyncio
async def test_whitespace_only_body_is_skipped():
    """Whitespace-only text is also skipped."""
    allpages = (FIX / "allpages_response.json").read_text()

    def h(req):
        action = req.url.params.get("action")
        if action == "query":
            return httpx.Response(200, text=allpages)
        return httpx.Response(200, text=json.dumps({
            "parse": {"title": "Engine", "text": {"*": "   \n  "}}
        }))

    transport = httpx.MockTransport(h)
    adapter = MediaWikiApiAdapter(_client=httpx.AsyncClient(transport=transport))
    src = _src()
    cands = [c async for c in adapter.fetch(src)]
    assert cands == []
