"""Timeout handling for playbook.run (issue #88)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def cfg_root(tmp_path, monkeypatch):
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
                "playbooks": {"jupyter": {"timeout_seconds": 0.25}},
            }
        )
    )
    monkeypatch.setenv("ENGRAM_CONFIG", str(cfg))
    from engram.common.config import load_config

    load_config.cache_clear()
    yield root
    load_config.cache_clear()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript((REPO / "schema" / "001_initial.sql").read_text())
    return c


@pytest.fixture
def run(cfg_root, conn):
    from engram.mcp_server.tools.playbook import register

    return register(conn)["playbook.run"]["handler"]


def test_playbook_run_timeout_returns_structured_error_promptly(cfg_root, run, monkeypatch):
    (cfg_root / "playbooks/scratch/demo.ipynb").write_text("{}")

    def fake_run(cmd, **kwargs):
        assert kwargs["timeout"] == pytest.approx(0.25)
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=kwargs["timeout"],
            output="partial stdout",
            stderr="partial stderr",
        )

    monkeypatch.setattr("engram.mcp_server.tools.playbook.subprocess.run", fake_run)

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
