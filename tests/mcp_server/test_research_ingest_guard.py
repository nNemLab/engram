"""research.ingest_url must reject non-public targets with a tool error (issue #31)."""
import sqlite3

import pytest


@pytest.fixture
def ingest():
    from engram.mcp_server.tools.research import register
    conn = sqlite3.connect(":memory:")
    return register(conn)["research.ingest_url"]["handler"]


def test_ingest_url_rejects_loopback(ingest):
    out = ingest({"url": "http://127.0.0.1:1234/v1/models"})
    assert "error" in out


def test_ingest_url_rejects_lan_address(ingest):
    out = ingest({"url": "http://192.168.50.7:8000/"})
    assert "error" in out


def test_ingest_url_rejects_file_scheme(ingest):
    out = ingest({"url": "file:///etc/passwd"})
    assert "error" in out
