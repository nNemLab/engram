"""Timeout handling for playbook.run (issue #88)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]


def _make_run_handler(tmp_path, monkeypatch, conn, *, playbooks_cfg):
    root = tmp_path / "engram"
    for sub in ("playbooks/scratch", "playbooks/curated", "playbooks/runs", "vault"):
        (root / sub).mkdir(parents=True)
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        json.dumps(
            {
                "paths": {
                    "root": str(root),
                    "vault": str(root / "vault"),
                    "playbooks_scratch": str(root / "playbooks/scratch"),
                    "playbooks_curated": str(root / "playbooks/curated"),
                    "playbooks_runs": str(root / "playbooks/runs"),
                    "db": str(root / "db.sqlite"),
                },
                "playbooks": playbooks_cfg,
            }
        )
    )
    monkeypatch.setenv("ENGRAM_CONFIG", str(cfg))
    from engram.common.config import load_config
    from engram.mcp_server.tools.playbook import register

    load_config.cache_clear()
    run = register(conn)["playbook.run"]["handler"]
    return root, run


@pytest.fixture
def cfg_root_run(tmp_path, monkeypatch, conn):
    root, run = _make_run_handler(
        tmp_path,
        monkeypatch,
        conn,
        playbooks_cfg={"jupyter": {"timeout_seconds": 0.25}},
    )
    yield root, run
    from engram.common.config import load_config

    load_config.cache_clear()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript((REPO / "schema" / "001_initial.sql").read_text())
    return c


def test_playbook_run_timeout_returns_structured_error_promptly(cfg_root_run, monkeypatch):
    cfg_root, run = cfg_root_run
    (cfg_root / "playbooks/scratch/demo.ipynb").write_text("{}")

    mock_proc = mock.Mock()
    mock_proc.pid = 99999
    mock_proc.returncode = None  # timeout path: exit_code stays None
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="papermill", timeout=0.25, output=b"partial stdout", stderr=b"partial stderr"),
        (b"partial stdout", b"partial stderr"),  # drain after kill
    ]

    def fake_popen(cmd, **kwargs):
        assert kwargs["start_new_session"] is True
        return mock_proc

    monkeypatch.setattr("engram.mcp_server.tools.playbook.subprocess.Popen", fake_popen)

    started = time.monotonic()
    out = run({"name": "demo", "runtime": "jupyter"})
    elapsed = time.monotonic() - started

    assert elapsed < 1
    assert out["timeout"] is True
    assert out["timeout_seconds"] == pytest.approx(0.25)
    assert out["exit_code"] is None
    assert "timed out" in out["error"]
    assert out["stdout_tail"] == "partial stdout"
    assert out["stderr_tail"] == "partial stderr"


def test_playbook_run_marimo_timeout_returns_structured_error(tmp_path, monkeypatch, conn):
    cfg_root, run = _make_run_handler(
        tmp_path,
        monkeypatch,
        conn,
        playbooks_cfg={"marimo": {"timeout_seconds": 0.1}},
    )
    (cfg_root / "playbooks/curated/demo.py").write_text("print('hi')")

    mock_proc = mock.Mock()
    mock_proc.pid = 99998
    mock_proc.returncode = None
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="marimo", timeout=0.1, output=b"marimo stdout", stderr=b"marimo stderr"),
        (b"marimo stdout", b"marimo stderr"),  # drain after kill
    ]

    def fake_popen(cmd, **kwargs):
        return mock_proc

    monkeypatch.setattr("engram.mcp_server.tools.playbook.subprocess.Popen", fake_popen)

    out = run({"name": "demo", "runtime": "marimo"})

    assert out["timeout"] is True
    assert out["timeout_seconds"] == pytest.approx(0.1)
    assert out["exit_code"] is None
    assert "timed out" in out["error"]
    assert out["stdout_tail"] == "marimo stdout"
    assert out["stderr_tail"] == "marimo stderr"


def test_playbook_run_null_marimo_timeout_falls_back_to_default(tmp_path, monkeypatch, conn):
    cfg_root, run = _make_run_handler(
        tmp_path,
        monkeypatch,
        conn,
        playbooks_cfg={"marimo": None},
    )
    (cfg_root / "playbooks/curated/demo.py").write_text("print('hi')")

    from engram.mcp_server.tools.playbook import DEFAULT_PLAYBOOK_TIMEOUT_SECONDS

    mock_proc = mock.Mock()
    mock_proc.pid = 99997
    mock_proc.returncode = None
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="marimo", timeout=DEFAULT_PLAYBOOK_TIMEOUT_SECONDS, output=b"fallback stdout", stderr=b"fallback stderr"),
        (b"fallback stdout", b"fallback stderr"),  # drain after kill
    ]

    def fake_popen(cmd, **kwargs):
        return mock_proc

    monkeypatch.setattr("engram.mcp_server.tools.playbook.subprocess.Popen", fake_popen)

    out = run({"name": "demo", "runtime": "marimo"})

    assert out["timeout"] is True
    assert out["exit_code"] is None
    assert out["timeout_seconds"] == pytest.approx(300.0)
    assert "timed out" in out["error"]
