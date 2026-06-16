"""eos-source CLI hardening (#163): patch-semantics `set`, error handling,
deterministic connection close, empty-string clears, and health --with-errors."""
import sqlite3
from pathlib import Path

import pytest

from engram.cli import eos_source
from engram.cli.eos_source import _clip, _parser, _print_health_table, _run, main
from engram.mcp_server.tools.sources import register

REPO = Path(__file__).resolve().parents[2]


def _apply(conn):
    for fn in ("001_initial.sql", "002_sources_and_revisions.sql"):
        conn.executescript((REPO / "schema" / fn).read_text())


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "db.sqlite")
    c.row_factory = sqlite3.Row
    _apply(c)
    yield c
    c.close()


@pytest.fixture
def tools(conn):
    return register(conn)


def _add(tools, **cfg):
    tools["sources.add"]["handler"]({
        "id": "s1", "name": "S1", "adapter": "github-repo",
        "url": "https://github.com/x/y", "config": cfg,
    })


def _config_of(tools, sid="s1"):
    return tools["sources.get"]["handler"]({"id": sid})["config"]


# ----- #163.1: `set` patch semantics (data-loss bug) -----------------------

def test_set_include_only_preserves_existing_exclude(tools):
    _add(tools, include=["docs/**"], exclude=["**/legacy/**"])
    args = _parser().parse_args(["set", "s1", "--include", "guides/**"])
    assert _run(args, tools) == 0
    cfg = _config_of(tools)
    assert cfg["include"] == ["guides/**"]
    assert cfg["exclude"] == ["**/legacy/**"]  # NOT dropped


def test_set_exclude_only_preserves_existing_include(tools):
    _add(tools, include=["docs/**"], exclude=["**/legacy/**"])
    args = _parser().parse_args(["set", "s1", "--exclude", "**/draft/**"])
    assert _run(args, tools) == 0
    cfg = _config_of(tools)
    assert cfg["include"] == ["docs/**"]  # NOT dropped
    assert cfg["exclude"] == ["**/draft/**"]


# ----- #163.4: empty-string clears via `is not None` -----------------------

def test_set_empty_source_tier_clears(tools):
    _add(tools)
    tools["sources.set"]["handler"]({"id": "s1", "source_tier": "manual"})
    args = _parser().parse_args(["set", "s1", "--source-tier", ""])
    assert _run(args, tools) == 0
    row = tools["sources.get"]["handler"]({"id": "s1"})
    assert row["source_tier"] == ""


# ----- #163.2 + #163.3: error handling + deterministic close ---------------

class _SpyConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_main_closes_connection_on_success(monkeypatch, conn):
    spy = _SpyConn()
    monkeypatch.setattr(eos_source, "get_connection", lambda: spy)
    monkeypatch.setattr(eos_source, "register", lambda c: register(conn))
    rc = main(["list"])
    assert rc == 0
    assert spy.closed is True


def test_main_handler_error_is_concise_and_nonzero(monkeypatch, capsys):
    spy = _SpyConn()

    def _boom(_args):
        raise RuntimeError("kaboom")

    fake_tools = {"sources.list": {"handler": _boom}}
    monkeypatch.setattr(eos_source, "get_connection", lambda: spy)
    monkeypatch.setattr(eos_source, "register", lambda c: fake_tools)
    rc = main(["list"])
    assert rc == 1
    assert spy.closed is True  # closed even on error (finally)
    err = capsys.readouterr().err
    assert "kaboom" in err
    assert "Traceback" not in err  # no raw traceback


# ----- #163.5: health --with-errors + column clamping ----------------------

def test_health_with_errors_filters(monkeypatch, capsys):
    spy = _SpyConn()
    records = [
        {"status": "ok", "id": "good", "last_success_at": None, "error_count": 0,
         "overdue": False, "dup_ratio": 0.0, "content_current": 1, "content_total": 1},
        {"status": "error", "id": "bad", "last_success_at": None, "error_count": 3,
         "overdue": True, "dup_ratio": 0.0, "content_current": 0, "content_total": 1},
    ]
    fake_tools = {"sources.health": {"handler": lambda a: {"sources": records}}}
    monkeypatch.setattr(eos_source, "get_connection", lambda: spy)
    monkeypatch.setattr(eos_source, "register", lambda c: fake_tools)
    rc = main(["health", "--with-errors"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "bad" in out
    assert "good" not in out


def test_clip_truncates_long_values():
    assert _clip("short", 24) == "short"
    long_id = "x" * 40
    clipped = _clip(long_id, 24)
    assert len(clipped) == 24
    assert clipped.endswith("...")


def test_print_health_table_handles_long_id(capsys):
    rec = {
        "status": "ok", "id": "a" * 60, "last_success_at": "2026-01-01T00:00:00Z",
        "error_count": 0, "overdue": False, "dup_ratio": 0.1,
        "content_current": 2, "content_total": 3,
    }
    _print_health_table([rec])
    line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("ok")][0]
    # The id column is clamped so a 60-char id can't run past its 24-col field.
    assert ("a" * 60) not in line
