"""bin/eos-uninstall — driven via subprocess against a throwaway ENGRAM_ROOT.

`systemctl` and `claude` are stubbed onto PATH so the script never touches the
host's real engram install, MCP registration, or systemd units.
"""
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "bin" / "eos-uninstall"


def _make_install(root: Path, *, db_bytes: bytes = b"SQLite format 3\x00curated") -> Path:
    """Materialize a fake engram runtime root and return the db path."""
    (root / "vault").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "playbooks" / "runs").mkdir(parents=True)
    db = root / "db.sqlite"
    db.write_bytes(db_bytes)
    (root / "config.yml").write_text(f"paths:\n  db: {db}\n")
    (root / "vault" / "note.md").write_text("# kept note")
    return db


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An isolated environment: stub bin on PATH, temp HOME/XDG, temp root."""
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    marker = tmp_path / "claude-remove-called"

    # claude stub: `mcp list` says no engram by default; `mcp remove` records.
    (stub_bin / "claude").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "mcp" && "$2" == "list" ]]; then exit 0; fi\n'
        f'if [[ "$1" == "mcp" && "$2" == "remove" ]]; then echo "$@" > "{marker}"; exit 0; fi\n'
        "exit 0\n"
    )
    # systemctl stub: always succeeds, records invocations.
    sysctl_log = tmp_path / "systemctl.log"
    (stub_bin / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{sysctl_log}"\n'
        "exit 0\n"
    )
    for f in ("claude", "systemctl"):
        (stub_bin / f).chmod(0o755)

    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    root = tmp_path / "engram"
    workdir = tmp_path / "cwd"
    workdir.mkdir()

    environ = dict(os.environ)
    environ["PATH"] = f"{stub_bin}:{environ['PATH']}"
    environ["HOME"] = str(home)
    environ["XDG_CONFIG_HOME"] = str(home / ".config")
    environ["ENGRAM_ROOT"] = str(root)

    return {
        "environ": environ, "root": root, "workdir": workdir, "home": home,
        "marker": marker, "sysctl_log": sysctl_log, "stub_bin": stub_bin,
    }


def _run(env, stdin: str):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin, capture_output=True, text=True,
        cwd=env["workdir"], env=env["environ"],
    )


def test_export_creates_valid_tarball_then_removes_root(env):
    db = _make_install(env["root"])
    r = _run(env, "y\nDELETE\n")
    assert r.returncode == 0, r.stderr

    tarballs = list(env["workdir"].glob("engram-export-*.tar.gz"))
    assert len(tarballs) == 1, f"expected one export, got {tarballs}"
    with tarfile.open(tarballs[0]) as t:
        names = t.getnames()
    assert "db.sqlite" in names
    assert not env["root"].exists(), "root must be gone after DELETE"
    assert not db.exists()
    assert "Exported" in r.stdout


def test_wrong_confirmation_aborts_and_keeps_everything(env):
    _make_install(env["root"])
    r = _run(env, "n\nnope\n")  # decline export, fail the gate
    assert r.returncode == 1
    assert "Aborted" in r.stdout
    assert env["root"].exists(), "root must survive a failed confirmation"
    assert (env["root"] / "db.sqlite").exists()
    assert list(env["workdir"].glob("*.tar.gz")) == []


def test_delete_without_export_removes_root(env):
    _make_install(env["root"])
    r = _run(env, "n\nDELETE\n")  # skip export, confirm deletion
    assert r.returncode == 0, r.stderr
    assert not env["root"].exists()
    assert list(env["workdir"].glob("*.tar.gz")) == []
    assert "No export was taken" in r.stdout


def test_reports_database_size(env):
    _make_install(env["root"], db_bytes=b"x" * 4096)
    r = _run(env, "n\nnope\n")
    assert "Curated database:" in r.stdout
    assert "db.sqlite" in r.stdout


def test_missing_install_exits_clean(env):
    # No root created, stubs report no units / no MCP registration.
    r = _run(env, "")
    assert r.returncode == 0
    assert "does not appear to be installed" in r.stdout
    assert list(env["workdir"].glob("*.tar.gz")) == []


def test_systemd_units_and_mcp_torn_down(env, tmp_path):
    _make_install(env["root"])
    # Fake installed units.
    unit_dir = env["home"] / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for u in ("engram-projector.service", "engram-daily-digest.timer"):
        (unit_dir / u).write_text("[Unit]\n")
    # Make the claude stub report an engram registration so teardown runs.
    (env["stub_bin"] / "claude").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "mcp" && "$2" == "list" ]]; then echo "engram: /x/engram-mcp"; exit 0; fi\n'
        f'if [[ "$1" == "mcp" && "$2" == "remove" ]]; then echo "$@" > "{env["marker"]}"; exit 0; fi\n'
        "exit 0\n"
    )
    (env["stub_bin"] / "claude").chmod(0o755)

    r = _run(env, "n\nDELETE\n")
    assert r.returncode == 0, r.stderr
    assert not list(unit_dir.glob("engram-*")), "unit files must be removed"
    assert env["sysctl_log"].exists() and "disable" in env["sysctl_log"].read_text()
    assert env["marker"].exists(), "claude mcp remove engram must be called"
    assert "engram" in env["marker"].read_text()
