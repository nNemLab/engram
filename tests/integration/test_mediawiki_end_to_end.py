"""End-to-end: serve a fake MediaWiki api.php from localhost, run the poller
through the same pipeline (gate → events → projector logic), modify the canned
recentchanges between polls, assert revision chain."""
import http.server
import json
import socketserver
import sqlite3
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in sorted(p.name for p in (REPO / "schema").glob("[0-9][0-9][0-9]_*.sql")):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def httpd(tmp_path):
    """Local httpd that serves api.php with response state controlled by a dict."""
    state = {
        "allpages": {
            "batchcomplete": "",
            "query": {"allpages": [
                {"pageid": 1, "ns": 0, "title": "Engine"},
            ]},
        },
        "parse_body": "<p>v1 engine description</p>",
        "recentchanges": {
            "batchcomplete": "",
            "query": {"recentchanges": []},
        },
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a, **kw): pass

        def do_GET(self):
            if "/api.php" not in self.path:
                self.send_response(404)
                self.end_headers()
                return
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            action = qs.get("action", [""])[0]
            list_ = qs.get("list", [""])[0]
            if action == "query" and list_ == "allpages":
                resp = state["allpages"]
            elif action == "query" and list_ == "recentchanges":
                resp = state["recentchanges"]
            elif action == "parse":
                resp = {"parse": {"title": qs.get("page", [""])[0], "pageid": 1,
                                  "text": {"*": state["parse_body"]}}}
            else:
                self.send_response(400)
                self.end_headers()
                return
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield {"port": port, "state": state}
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
async def test_mediawiki_full_supersede_flow(tmp_path, monkeypatch, httpd):
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply(conn)

    from types import SimpleNamespace

    fake = SimpleNamespace(rag=SimpleNamespace(near_dup_threshold=0.92))
    # Patch where gate() looks the symbol up (engram.dedup.load_config), not
    # where it's defined: dedup binds load_config at import time, so patching the
    # config module is missed once dedup is already imported (e.g. by an earlier
    # test in the suite), leaving the real ~/.engram/config.yml loader in place.
    monkeypatch.setattr("engram.dedup.load_config", lambda: fake)

    # Importing populates ADAPTERS
    import engram.poller.adapters.mediawiki_api  # noqa: F401
    from engram.poller.poller import poll_one

    wiki_url = f"http://127.0.0.1:{httpd['port']}"
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, config, schedule, source_tier) "
        "VALUES ('wiki', 'Test Wiki', 'mediawiki-api', ?, ?, '7d', 'vendor-doc')",
        (wiki_url, json.dumps({"namespaces": [0], "request_interval_ms": 0,
                                "max_pages_first_run": 10})),
    )
    conn.commit()
    src = dict(conn.execute("SELECT * FROM sources WHERE id='wiki'").fetchone())

    # First poll: should ingest 1 page (Engine) at revision 1
    counts1 = await poll_one(conn, src)
    assert counts1["ingested"] == 1, f"got: {counts1}"

    rev1 = conn.execute(
        "SELECT hash, revision, is_current FROM content WHERE source_id='wiki'"
    ).fetchall()
    assert len(rev1) == 1
    assert rev1[0]["revision"] == 1
    assert rev1[0]["is_current"] == 1

    # Modify the served content AND populate recentchanges so the incremental
    # path picks it up
    httpd["state"]["parse_body"] = "<p>v2 engine description with major changes</p>"
    httpd["state"]["recentchanges"] = {
        "batchcomplete": "",
        "query": {"recentchanges": [
            {"type": "edit", "ns": 0, "title": "Engine", "pageid": 1, "revid": 200,
             "timestamp": "2026-05-06T12:00:00Z"},
        ]},
    }

    src = dict(conn.execute("SELECT * FROM sources WHERE id='wiki'").fetchone())
    counts2 = await poll_one(conn, src)
    assert counts2["superseded"] == 1, f"got: {counts2}"

    rev2 = conn.execute(
        "SELECT hash, revision, is_current, superseded_by FROM content "
        "WHERE source_id='wiki' ORDER BY revision"
    ).fetchall()
    assert len(rev2) == 2
    assert rev2[0]["revision"] == 1 and rev2[0]["is_current"] == 0
    assert rev2[0]["superseded_by"] == rev2[1]["hash"]
    assert rev2[1]["revision"] == 2 and rev2[1]["is_current"] == 1
