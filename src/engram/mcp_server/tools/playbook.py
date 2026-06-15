"""playbook.* tools: list templates, run with parameters, summarize a run."""
from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _resolve(name: str) -> str:
    """Resolve a console script next to the running interpreter, then PATH."""
    venv_bin = Path(sys.executable).parent / name
    if venv_bin.exists():
        return str(venv_bin)
    found = shutil.which(name)
    if found:
        return found
    return name  # let subprocess fail loudly with the bare name


def _subprocess_env(root: Path) -> dict[str, str]:
    """os.environ + ~/.engram/.env (latter wins). Lets playbooks see secrets
    without requiring them in the launcher's shell env."""
    merged = dict(os.environ)
    env_path = root / ".env"
    if env_path.exists():
        try:
            from dotenv import dotenv_values
            for k, v in dotenv_values(env_path).items():
                if v is not None:
                    merged[k] = v
        except ImportError:
            pass
    return merged

# Imported below the module helpers above; keep here to avoid an import cycle.
from ... import log as event_log  # noqa: E402
from ...common.config import load_config  # noqa: E402


def _now_slug() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")


DEFAULT_PLAYBOOK_TIMEOUT_SECONDS = 300.0


def register(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    cfg = load_config()

    def list_(args: dict[str, Any]) -> dict[str, Any]:
        scratch = sorted(p.name for p in cfg.paths.playbooks_scratch.glob("*.ipynb")) \
            if cfg.paths.playbooks_scratch.exists() else []
        curated = sorted(p.name for p in cfg.paths.playbooks_curated.glob("*.py")) \
            if cfg.paths.playbooks_curated.exists() else []
        return {"scratch": scratch, "curated": curated}

    def run(args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"]
        params = args.get("params", {})
        runtime = args.get("runtime", cfg.playbooks.default_runtime)

        if runtime == "jupyter":
            base, suffix = cfg.paths.playbooks_scratch, ".ipynb"
        elif runtime == "marimo":
            base, suffix = cfg.paths.playbooks_curated, ".py"
        else:
            return {"error": f"unknown runtime: {runtime}"}

        template = base / name
        if not template.exists() and template.suffix == "":
            template = template.with_suffix(suffix)
        # Containment: the agent-supplied name must stay inside the template dir —
        # reject ../, absolute paths, and symlinks pointing outside.
        if not template.resolve().is_relative_to(base.resolve()):
            return {"error": f"playbook name escapes {base}: {name}"}
        if not template.exists():
            return {"error": f"template not found: {template}"}

        run_id = f"{_now_slug()}_{name.replace('/', '-').replace('.', '-')}_{uuid.uuid4().hex[:6]}"
        run_dir = cfg.paths.playbooks_runs / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "inputs.json").write_text(json.dumps(params, indent=2))

        runtime_cfg = (cfg.playbooks.jupyter if runtime == "jupyter" else cfg.playbooks.marimo) or {}
        raw_timeout = args.get("timeout_seconds")
        if raw_timeout is None:
            raw_timeout = runtime_cfg.get("timeout_seconds")
        if raw_timeout is None:
            timeout_seconds = DEFAULT_PLAYBOOK_TIMEOUT_SECONDS
        else:
            try:
                timeout_seconds = float(raw_timeout)
            except (TypeError, ValueError):
                timeout_seconds = DEFAULT_PLAYBOOK_TIMEOUT_SECONDS
        if timeout_seconds <= 0:
            return {
                "run_id": run_id,
                "run_dir": str(run_dir),
                "exit_code": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "timeout": False,
                "timeout_seconds": timeout_seconds,
                "error": f"timeout_seconds must be > 0 (got {timeout_seconds})",
            }

        if runtime == "jupyter":
            output = run_dir / "notebook.ipynb"
            cmd = [_resolve("papermill"), str(template), str(output), "--cwd", str(run_dir)]
            for k, v in params.items():
                cmd += ["-p", k, str(v)]
        else:
            cmd = [_resolve("marimo"), "run", str(template), "--headless"]
            for k, v in params.items():
                cmd += [f"--{k}", str(v)]

        timeout_hit = False
        error = None
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=run_dir,
            env=_subprocess_env(cfg.paths.root),
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timeout_hit = True
            error = f"playbook timed out after {timeout_seconds}s"
            # Kill the entire process group so grandchildren (kernels, etc.)
            # don't survive the timeout.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass  # process already exited or not in our session
            # Drain remaining output and reap the process.
            stdout_b, stderr_b = proc.communicate()
            stdout_b = stdout_b or b""
            stderr_b = stderr_b or b""

        stdout = stdout_b.decode() if isinstance(stdout_b, bytes) else stdout_b
        stderr = stderr_b.decode() if isinstance(stderr_b, bytes) else stderr_b
        exit_code = proc.returncode

        (run_dir / "stdout.log").write_text(stdout)
        (run_dir / "stderr.log").write_text(stderr)

        event_log.append(
            conn, "playbook_run",
            {
                "run_id": run_id,
                "playbook": name,
                "runtime": runtime,
                "params": params,
                "exit_code": exit_code,
                "timeout": timeout_hit,
                "timeout_seconds": timeout_seconds,
                "run_dir": str(run_dir),
            },
            actor="agent",
        )

        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "exit_code": exit_code,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
            "timeout": timeout_hit,
            "timeout_seconds": timeout_seconds,
            **({"error": error} if error else {}),
        }

    def summarize(args: dict[str, Any]) -> dict[str, Any]:
        # Caller (the agent) provides the summary text — it's the one that just read
        # the output notebook. We push it through the dedup gate as a kb entry.
        from ... import dedup
        run_dir = Path(args["run_dir"])
        result = dedup.gate(
            conn,
            body=args["summary"],
            title=args.get("title", run_dir.name),
            source_url=f"file://{run_dir}",
            source_tier="agent-derived",
            confidence=float(args.get("confidence", 0.7)),
            kind="playbook-summary",
            actor="agent",
        )
        return {"outcome": result.outcome, "hash": result.hash}

    return {
        "playbook.list": {
            "description": "List available playbook templates (scratch=Jupyter, curated=Marimo).",
            "input_schema": {"type": "object", "properties": {}},
            "handler": list_,
        },
        "playbook.run": {
            "description": "Execute a playbook headlessly with parameters. Outputs land in playbooks/runs/<run_id>/.",
            "input_schema": {
                "type": "object", "required": ["name"],
                "properties": {
                    "name":    {"type": "string"},
                    "runtime": {"type": "string", "enum": ["jupyter", "marimo"]},
                    "params":  {"type": "object"},
                    "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                },
            },
            "handler": run,
        },
        "playbook.summarize": {
            "description": "Persist a playbook-run summary into the KB (gated). Vault gets the summary; full notebook stays in run_dir.",
            "input_schema": {
                "type": "object", "required": ["run_dir", "summary"],
                "properties": {
                    "run_dir":    {"type": "string"},
                    "summary":    {"type": "string"},
                    "title":      {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
            "handler": summarize,
        },
    }
