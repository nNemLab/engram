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

    # Default docker stub: reports NO engram install, so native-mode tests are
    # hermetic regardless of the host's real Docker state. Docker-path tests
    # opt in via _add_docker_stub(env, present=True).
    docker_log = tmp_path / "docker.log"
    _write_docker_stub(stub_bin / "docker", docker_log, present=False)

    environ = dict(os.environ)
    environ["PATH"] = f"{stub_bin}:{environ['PATH']}"
    environ["HOME"] = str(home)
    environ["XDG_CONFIG_HOME"] = str(home / ".config")
    environ["ENGRAM_ROOT"] = str(root)

    return {
        "environ": environ, "root": root, "workdir": workdir, "home": home,
        "marker": marker, "sysctl_log": sysctl_log, "stub_bin": stub_bin,
        "docker_log": docker_log,
    }


def _write_docker_stub(path: Path, log: Path, *, present: bool):
    """Write a `docker` stub.

    When ``present`` is True it simulates an installed engram stack: the
    ``engram-data`` volume exists, ``compose ls`` knows the project, the
    ``run ... tar`` export produces a real tarball containing ``db.sqlite``,
    and ``run ... du`` reports a size. All invocations are appended to ``log``.
    """
    vol_exit = "0" if present else "1"
    ls_body = '[{"Name":"engram","Status":"running(1)"}]' if present else "[]"
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        '\n'
        '# docker volume inspect engram-data\n'
        'if [[ "$1" == "volume" && "$2" == "inspect" && "$3" == "engram-data" ]]; then\n'
        f'    exit {vol_exit}\n'
        'fi\n'
        '\n'
        '# docker compose ls --filter name=engram --format json\n'
        'if [[ "$1" == "compose" && "$2" == "ls" ]]; then\n'
        f'    printf \'%s\\n\' \'{ls_body}\'\n'
        '    exit 0\n'
        'fi\n'
        '\n'
        '# docker run ... (export tar, or du size probe)\n'
        'if [[ "$1" == "run" ]]; then\n'
        '    # Find a -czf <path> pair: emit a real tarball with db.sqlite.\n'
        '    prev=""\n'
        '    for a in "$@"; do\n'
        '        if [[ "$prev" == "-czf" ]]; then\n'
        '            host="${a/#\\/out\\//}"   # /out/NAME -> NAME (cwd is /out)\n'
        '            tmpd="$(mktemp -d)"\n'
        '            printf "SQLite format 3\\000" > "$tmpd/db.sqlite"\n'
        '            tar -czf "$host" -C "$tmpd" db.sqlite\n'
        '            rm -rf "$tmpd"\n'
        '            exit 0\n'
        '        fi\n'
        '        prev="$a"\n'
        '    done\n'
        '    # du size probe (sh -c "du -h /data/db.sqlite ...").\n'
        '    case "$*" in *du*db.sqlite*) echo "12K"; exit 0 ;; esac\n'
        '    exit 0\n'
        'fi\n'
        '\n'
        'exit 0\n'
    )
    path.chmod(0o755)


def _add_docker_stub(env, *, present=True):
    """Switch the env's docker stub to simulate an install (or not)."""
    _write_docker_stub(env["stub_bin"] / "docker", env["docker_log"], present=present)
    return env["docker_log"]


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


# --- Docker-mode tests -------------------------------------------------------
# These switch the env's docker stub to "installed" and assert the script
# detects the Docker stack, exports/verifies the db from the volume, and tears
# down the compose project. No native root is created, so detection picks Docker.


def test_docker_only_install_detected_and_torn_down(env):
    log = _add_docker_stub(env, present=True)
    # No native root; only the Docker stack is present.
    # stdin: skip export, DELETE, keep vault, keep image.
    r = _run(env, "n\nDELETE\nn\nn\n")
    assert r.returncode == 0, r.stderr
    assert "Docker uninstall" in r.stdout
    text = log.read_text()
    # Detection probed the volume, teardown ran `compose ... down -v`.
    assert "volume inspect engram-data" in text
    assert "compose" in text and "down" in text and "-v" in text
    # MCP registration removed (claude stub records to marker).
    assert env["marker"].exists(), "claude mcp remove engram must be called"
    assert "remove engram" in env["marker"].read_text()


def test_docker_export_creates_verified_tarball(env):
    log = _add_docker_stub(env, present=True)
    # stdin: accept export (Y default), DELETE, keep vault, keep image.
    r = _run(env, "y\nDELETE\nn\nn\n")
    assert r.returncode == 0, r.stderr
    tarballs = list(env["workdir"].glob("engram-export-*.tar.gz"))
    assert len(tarballs) == 1, f"expected one export, got {tarballs}"
    with tarfile.open(tarballs[0]) as t:
        assert "db.sqlite" in t.getnames()
    assert "Exported" in r.stdout
    # The export went through `docker run ... tar`, not just compose down.
    assert "tar" in log.read_text()


def test_docker_wrong_confirmation_aborts(env):
    _add_docker_stub(env, present=True)
    r = _run(env, "n\nnope\n")  # skip export, fail the typed gate
    assert r.returncode == 1
    assert "Aborted" in r.stdout
    # No teardown should have happened (no down -v logged).
    assert "down" not in env["docker_log"].read_text()


def test_docker_offers_vault_and_image_removal(env):
    log = _add_docker_stub(env, present=True)
    # Create a host vault dir the script will offer to delete.
    vault = env["workdir"] / "vault"
    (vault).mkdir()
    (vault / "note.md").write_text("# kept")
    environ = dict(env["environ"])
    environ["ENGRAM_VAULT"] = str(vault)
    # skip export, DELETE, delete vault (y), remove image (y).
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        input="n\nDELETE\ny\ny\n", capture_output=True, text=True,
        cwd=env["workdir"], env=environ,
    )
    assert r.returncode == 0, r.stderr
    assert not vault.exists(), "host vault must be removed when accepted"
    assert "image rm engram:local" in log.read_text()


def test_both_installs_prompt_docker(env):
    """When both native and Docker are present, choosing 'docker' tears down Docker."""
    _make_install(env["root"])
    log = _add_docker_stub(env, present=True)
    # which=docker, skip export, DELETE, keep vault, keep image.
    r = _run(env, "docker\nn\nDELETE\nn\nn\n")
    assert r.returncode == 0, r.stderr
    assert "Both a native and a Docker install were detected" in r.stdout
    assert "Docker uninstall" in r.stdout
    # Native root must survive (we chose docker only).
    assert env["root"].exists(), "native root must be untouched when only docker chosen"
    assert "down" in log.read_text()


def test_both_installs_prompt_native(env):
    """Choosing 'native' tears down native and leaves the Docker stack alone."""
    _make_install(env["root"])
    log = _add_docker_stub(env, present=True)
    # which=native, skip export, DELETE.
    r = _run(env, "native\nn\nDELETE\n")
    assert r.returncode == 0, r.stderr
    assert "Both a native and a Docker install were detected" in r.stdout
    assert not env["root"].exists(), "native root must be removed"
    # No docker teardown ran.
    assert "down" not in log.read_text()


def test_both_installs_invalid_choice_aborts(env):
    _make_install(env["root"])
    _add_docker_stub(env, present=True)
    r = _run(env, "huh\n")
    assert r.returncode == 1
    assert "Aborted" in r.stdout
    assert env["root"].exists(), "nothing removed on invalid choice"


def test_docker_flag_forces_docker_when_only_native_present(env):
    """--docker with no Docker install present exits clean (does-not-appear)."""
    _make_install(env["root"])
    _add_docker_stub(env, present=False)
    r = subprocess.run(
        ["bash", str(SCRIPT), "--docker"],
        input="", capture_output=True, text=True,
        cwd=env["workdir"], env=env["environ"],
    )
    assert r.returncode == 0
    assert "does not appear to be installed" in r.stdout
    assert env["root"].exists(), "native root untouched under --docker"


def test_native_flag_skips_docker_detection(env):
    """--native ignores a present Docker stack and only removes native."""
    _make_install(env["root"])
    log = _add_docker_stub(env, present=True)
    r = subprocess.run(
        ["bash", str(SCRIPT), "--native"],
        input="n\nDELETE\n", capture_output=True, text=True,
        cwd=env["workdir"], env=env["environ"],
    )
    assert r.returncode == 0, r.stderr
    assert "Both a native and a Docker install were detected" not in r.stdout, \
        "no both-prompt under --native"
    assert not env["root"].exists()
    assert "down" not in log.read_text(), "docker stack untouched under --native"
