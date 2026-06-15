"""Timeout hardening for playbook.run (issue #102).

Two concerns:
  (a) On timeout, the child's process group is fully killed (no orphaned grandchildren).
  (b) The early-out (timeout_seconds <= 0) return has the same key set as the normal return.
"""
from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript((REPO / "schema" / "001_initial.sql").read_text())
    return c


@pytest.fixture
def cfg_root_run(tmp_path, monkeypatch, conn):
    """Fixture with a 0.25s jupyter timeout, ready for subprocess mocking."""
    root, run = _make_run_handler(
        tmp_path,
        monkeypatch,
        conn,
        playbooks_cfg={"jupyter": {"timeout_seconds": 0.25}},
    )
    yield root, run
    from engram.common.config import load_config
    load_config.cache_clear()


# ---------------------------------------------------------------------------
# (a) Process-group cleanup on timeout
# ---------------------------------------------------------------------------


def test_killpg_called_on_timeout(tmp_path, monkeypatch, conn):
    """On timeout, os.killpg is invoked with the child's pgid and SIGKILL."""
    cfg_root, run = _make_run_handler(
        tmp_path,
        monkeypatch,
        conn,
        playbooks_cfg={"jupyter": {"timeout_seconds": 0.25}},
    )
    (cfg_root / "playbooks/scratch/demo.ipynb").write_text("{}")

    mock_proc = mock.Mock()
    mock_proc.pid = 99999
    mock_proc.returncode = None  # timeout path
    # side_effect as list: first call raises, second call (drain) returns
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="papermill", timeout=0.25, output=b"", stderr=b""),
        (b"", b""),
    ]

    def fake_popen(cmd, **kwargs):
        return mock_proc

    monkeypatch.setattr("engram.mcp_server.tools.playbook.subprocess.Popen", fake_popen)

    with mock.patch("engram.mcp_server.tools.playbook.os.killpg") as mock_killpg:
        # Mock getpgid to return the same PID as the fake process
        monkeypatch.setattr("engram.mcp_server.tools.playbook.os.getpgid",
                            lambda pid: pid)
        out = run({"name": "demo", "runtime": "jupyter"})

        mock_killpg.assert_called_once_with(99999, signal.SIGKILL)
        assert out["timeout"] is True
        assert out["exit_code"] is None


def test_killpg_silenced_when_process_gone(tmp_path, monkeypatch, conn):
    """If killpg raises ProcessLookupError the handler still succeeds."""
    cfg_root, run = _make_run_handler(
        tmp_path,
        monkeypatch,
        conn,
        playbooks_cfg={"jupyter": {"timeout_seconds": 0.25}},
    )
    (cfg_root / "playbooks/scratch/demo.ipynb").write_text("{}")

    mock_proc = mock.Mock()
    mock_proc.pid = 99999
    mock_proc.returncode = None  # timeout path
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="papermill", timeout=0.25, output=b"", stderr=b""),
        (b"", b""),
    ]

    def fake_popen(cmd, **kwargs):
        return mock_proc

    monkeypatch.setattr("engram.mcp_server.tools.playbook.subprocess.Popen", fake_popen)

    with mock.patch("engram.mcp_server.tools.playbook.os.killpg") as mock_killpg:
        mock_killpg.side_effect = ProcessLookupError()
        monkeypatch.setattr("engram.mcp_server.tools.playbook.os.getpgid",
                            lambda pid: pid)
        out = run({"name": "demo", "runtime": "jupyter"})

        mock_killpg.assert_called_once()
        assert out["timeout"] is True


def test_start_new_session_flag_passed_to_subprocess(tmp_path, monkeypatch, conn):
    """subprocess.Popen is called with start_new_session=True."""
    cfg_root, run = _make_run_handler(
        tmp_path,
        monkeypatch,
        conn,
        playbooks_cfg={"jupyter": {"timeout_seconds": 10}},
    )
    (cfg_root / "playbooks/scratch/demo.ipynb").write_text("{}")

    session_flag = []

    def fake_popen(cmd, **kwargs):
        session_flag.append(kwargs.get("start_new_session", False))
        m = mock.Mock()
        m.pid = 88888
        m.communicate.return_value = (b"", b"")
        m.returncode = 0
        return m

    monkeypatch.setattr("engram.mcp_server.tools.playbook.subprocess.Popen", fake_popen)
    run({"name": "demo", "runtime": "jupyter"})

    assert session_flag[0] is True


# ---------------------------------------------------------------------------
# (b) Early-out return has the same key set as the normal return
# ---------------------------------------------------------------------------

def test_normal_success_keys(cfg_root_run, monkeypatch):
    """The normal (success) return carries a specific key set."""
    cfg_root, run = cfg_root_run
    (cfg_root / "playbooks/scratch/demo.ipynb").write_text("{}")

    mock_proc = mock.Mock()
    mock_proc.pid = 88888
    mock_proc.communicate.return_value = (b"ok", b"")
    mock_proc.returncode = 0

    monkeypatch.setattr(
        "engram.mcp_server.tools.playbook.subprocess.Popen",
        lambda *a, **kw: mock_proc,
    )

    out = run({"name": "demo", "runtime": "jupyter"})

    keys = {
        "run_id", "run_dir", "exit_code", "stdout_tail",
        "stderr_tail", "timeout", "timeout_seconds",
    }
    for k in keys:
        assert k in out, f"missing key {k} in normal success return"
    assert "error" not in out


def test_timeout_keys(cfg_root_run, monkeypatch):
    """The timeout return carries the same base key set (plus 'error')."""
    cfg_root, run = cfg_root_run
    (cfg_root / "playbooks/scratch/demo.ipynb").write_text("{}")

    mock_proc = mock.Mock()
    mock_proc.pid = 77777
    mock_proc.returncode = None  # timeout path
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="papermill", timeout=0.25, output=b"", stderr=b""),
        (b"", b""),  # drain after kill
    ]

    monkeypatch.setattr(
        "engram.mcp_server.tools.playbook.subprocess.Popen",
        lambda *a, **kw: mock_proc,
    )

    out = run({"name": "demo", "runtime": "jupyter"})

    base_keys = {
        "run_id", "run_dir", "exit_code", "stdout_tail",
        "stderr_tail", "timeout", "timeout_seconds",
    }
    for k in base_keys:
        assert k in out, f"missing key {k} in timeout return"
    assert "error" in out


def test_early_out_keys_same_as_normal(cfg_root_run, monkeypatch):
    """timeout_seconds <= 0 returns the same structural keys as success + error."""
    cfg_root, run = cfg_root_run
    # Create the template so we reach the timeout check (it's after template check)
    (cfg_root / "playbooks/scratch/demo.ipynb").write_text("{}")

    # First get the baseline key set from a successful call
    mock_proc = mock.Mock()
    mock_proc.pid = 66666
    mock_proc.communicate.return_value = (b"ok", b"")
    mock_proc.returncode = 0

    monkeypatch.setattr(
        "engram.mcp_server.tools.playbook.subprocess.Popen",
        lambda *a, **kw: mock_proc,
    )
    normal_out = run({"name": "demo", "runtime": "jupyter"})
    normal_keys = set(normal_out.keys())

    # Now trigger the early-out with an invalid timeout
    bad_out = run({"name": "demo", "runtime": "jupyter", "timeout_seconds": -1})

    early_keys = set(bad_out.keys())
    # Early-out should carry all normal keys plus 'error'
    assert normal_keys == early_keys - {"error"}


def test_early_out_values(cfg_root_run):
    """Early-out values are sensible: exit_code=None, timeout=False, empty tails."""
    cfg_root, run = cfg_root_run
    # Create the template so we reach the timeout check
    (cfg_root / "playbooks/scratch/demo.ipynb").write_text("{}")

    out = run({"name": "demo", "runtime": "jupyter", "timeout_seconds": -1})

    assert out["exit_code"] is None
    assert out["timeout"] is False
    assert out["stdout_tail"] == ""
    assert out["stderr_tail"] == ""
    assert "run_id" in out
    assert "run_dir" in out
    assert "timed out" not in out.get("error", "")
