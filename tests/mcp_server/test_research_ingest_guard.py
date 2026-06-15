"""research.ingest_url must reject non-public targets with a tool error (issue #31),
and must reject non-HTML content-types (issue #77).
"""
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]


@dataclass
class _Outcome:
    new: int = 1


@pytest.fixture(autouse=True)
def _engram_config(tmp_path, monkeypatch):
    """Point ENGRAM_CONFIG at a throwaway config so handlers that call
    load_config() (dedup.gate computes embeddings via it) work on a clean
    machine / CI runner with no ~/.engram/config.yml."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "paths:\n"
        f"  root: {tmp_path}\n  vault: {tmp_path}/vault\n"
        f"  playbooks_scratch: {tmp_path}/ps\n  playbooks_curated: {tmp_path}/pc\n"
        f"  playbooks_runs: {tmp_path}/pr\n  db: {tmp_path}/db.sqlite\n"
    )
    monkeypatch.setenv("ENGRAM_CONFIG", str(cfg))
    from engram.common.config import load_config
    load_config.cache_clear()
    yield
    load_config.cache_clear()


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql", "003_grounding.sql"):
        c.executescript((REPO / "schema" / fn).read_text())
    return c


@pytest.fixture
def ingest():
    from engram.mcp_server.tools.research import register
    conn = _conn()
    return register(conn)["research.ingest_url"]["handler"]


# ── SSRF guards (issue #31) ──────────────────────────────────────────────────

def test_ingest_url_rejects_loopback(ingest):
    out = ingest({"url": "http://127.0.0.1:1234/v1/models"})
    assert "error" in out


def test_ingest_url_rejects_lan_address(ingest):
    out = ingest({"url": "http://192.168.50.7:8000/"})
    assert "error" in out


def test_ingest_url_rejects_file_scheme(ingest):
    out = ingest({"url": "file:///etc/passwd"})
    assert "error" in out


# ── Content-type / PDF guards (issue #77) ────────────────────────────────────


def _make_response(content_type: str, body: str | bytes = "<html><body>Hello</body></html>"):
    """Create a mock httpx.Response with the given content-type and body."""
    resp = mock.MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-type": content_type}
    resp.content = body.encode() if isinstance(body, str) else body
    resp.text = body
    resp.is_redirect = False
    return resp


def _gate_mock(outcome: str = "new", h: str = "abc123"):
    """Return a mock for dedup.gate that mimics its return shape."""
    from engram.dedup import GateResult

    result = GateResult(outcome=outcome, hash=h)
    return lambda *a, **k: result


def test_ingest_url_rejects_pdf_content_type(ingest):
    with mock.patch(
        "engram.research.safe_fetch.get",
        return_value=_make_response(
            "application/pdf",
            b"%PDF-1.7\n1 0 obj<<...>>endobj\n",
        ),
    ):
        out = ingest({"url": "https://arxiv.org/pdf/2603.08747v1"})
    assert "error" in out
    assert "pdf" in out["error"].lower()


def test_ingest_url_rejects_pdf_magic_bytes_even_with_text_ct(ingest):
    # A server sends a PDF but with a misleading content-type header
    with mock.patch(
        "engram.research.safe_fetch.get",
        return_value=_make_response(
            "text/html",  # misleading CT
            b"%PDF-1.7\n1 0 obj<<...>>endobj\n",
        ),
    ):
        out = ingest({"url": "https://example.com/tricky"})
    assert "error" in out
    assert "pdf" in out["error"].lower()


def test_ingest_url_rejects_octet_stream(ingest):
    with mock.patch(
        "engram.research.safe_fetch.get",
        return_value=_make_response(
            "application/octet-stream",
            b"some binary data",
        ),
    ):
        out = ingest({"url": "https://example.com/binary.bin"})
    assert "error" in out


def test_ingest_url_rejects_json_content_type(ingest):
    with mock.patch(
        "engram.research.safe_fetch.get",
        return_value=_make_response(
            "application/json",
            '{"key": "value"}',
        ),
    ):
        out = ingest({"url": "https://api.example.com/data.json"})
    assert "error" in out


def test_ingest_url_accepts_html_and_passes_through_trafilatura(ingest):
    html_body = "<html><body><p>Hello world</p></body></html>"
    with mock.patch(
        "engram.research.safe_fetch.get",
        return_value=_make_response("text/html; charset=utf-8", html_body),
    ), mock.patch("engram.mcp_server.tools.research.dedup.gate", _gate_mock()):
        out = ingest({"url": "https://example.com/page"})
    assert "error" not in out
    assert out["outcome"] == "new"
    assert out["hash"] == "abc123"
    assert out["extracted_chars"] > 0
