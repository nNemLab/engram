"""End-to-end: serve a fixture sitemap from a local httpd, run the poller,
modify content, run again, assert the supersede flow worked top to bottom."""
import http.server
import json
import socketserver
import sqlite3
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures"


def _apply(conn):
    for fn in sorted(p.name for p in (REPO / "schema").glob("[0-9][0-9][0-9]_*.sql")):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def httpd(tmp_path):
    """Serve docs_v1 then swap to docs_v2 between calls."""
    serve_root = tmp_path / "site"
    (serve_root / "engine" / "install" / "linux").mkdir(parents=True)
    (serve_root / "engine" / "install" / "linux" / "index.html").write_bytes(
        (FIX / "docs_v1" / "index.html").read_bytes()
    )

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(serve_root), **kw)
        def log_message(self, *a, **kw): pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]

    # Now write the sitemap with the actual port (after we know it)
    (serve_root / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f'<url><loc>http://127.0.0.1:{port}/engine/install/linux/</loc></url>'
        '</urlset>'
    )

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield {"port": port, "root": serve_root}
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
async def test_full_supersede_flow(tmp_path, monkeypatch, httpd):
    """1) ingest revision 1, 2) modify file, 3) ingest revision 2, 4) verify chain."""
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

    # Importing the sitemap adapter triggers register() into ADAPTERS
    import engram.poller.adapters.sitemap  # noqa: F401
    from engram.poller.poller import poll_one

    sitemap_url = f"http://127.0.0.1:{httpd['port']}/sitemap.xml"
    conn.execute(
        "INSERT INTO sources (id, name, adapter, url, config, schedule, source_tier) "
        "VALUES ('test', 't', 'sitemap', ?, ?, '7d', 'vendor-doc')",
        (sitemap_url, json.dumps({})),
    )
    conn.commit()
    src = dict(conn.execute("SELECT * FROM sources WHERE id='test'").fetchone())

    # First poll: should ingest one new entry
    counts1 = await poll_one(conn, src)
    assert counts1["ingested"] == 1, f"got: {counts1}"

    rev1 = conn.execute(
        "SELECT hash, revision, is_current FROM content "
        "WHERE source_id='test' ORDER BY revision DESC"
    ).fetchall()
    assert len(rev1) == 1
    assert rev1[0]["revision"] == 1
    assert rev1[0]["is_current"] == 1

    # Modify the served file
    target = httpd["root"] / "engine" / "install" / "linux" / "index.html"
    target.write_bytes((FIX / "docs_v2" / "index.html").read_bytes())

    # Force re-poll (clear cursor so etag doesn't trip us up)
    conn.execute("UPDATE sources SET cursor=NULL WHERE id='test'")
    conn.commit()
    src = dict(conn.execute("SELECT * FROM sources WHERE id='test'").fetchone())
    counts2 = await poll_one(conn, src)
    assert counts2["superseded"] == 1, f"got: {counts2}"

    rev2 = conn.execute(
        "SELECT hash, revision, is_current, superseded_by FROM content "
        "WHERE source_id='test' ORDER BY revision"
    ).fetchall()
    assert len(rev2) == 2
    assert rev2[0]["revision"] == 1 and rev2[0]["is_current"] == 0
    assert rev2[0]["superseded_by"] == rev2[1]["hash"]
    assert rev2[1]["revision"] == 2 and rev2[1]["is_current"] == 1
