"""playbook.* tools: list templates, run with parameters, summarize a run."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
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

from ...common.config import load_config
from ... import log as event_log


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")


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
        run_id = f"{_now_slug()}_{name.replace('/', '-').replace('.', '-')}_{uuid.uuid4().hex[:6]}"
        run_dir = cfg.paths.playbooks_runs / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "inputs.json").write_text(json.dumps(params, indent=2))

        if runtime == "jupyter":
            template = cfg.paths.playbooks_scratch / name
            if not template.exists() and template.suffix == "":
                template = template.with_suffix(".ipynb")
            output = run_dir / "notebook.ipynb"
            cmd = [_resolve("papermill"), str(template), str(output), "--cwd", str(run_dir)]
            for k, v in params.items():
                cmd += ["-p", k, str(v)]
        elif runtime == "marimo":
            template = cfg.paths.playbooks_curated / name
            if not template.exists() and template.suffix == "":
                template = template.with_suffix(".py")
            cmd = [_resolve("marimo"), "run", str(template), "--headless"]
            for k, v in params.items():
                cmd += [f"--{k}", str(v)]
        else:
            return {"error": f"unknown runtime: {runtime}"}

        if not template.exists():
            return {"error": f"template not found: {template}"}

        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=run_dir,
            env=_subprocess_env(cfg.paths.root),
        )
        (run_dir / "stdout.log").write_text(proc.stdout)
        (run_dir / "stderr.log").write_text(proc.stderr)

        event_log.append(
            conn, "playbook_run",
            {"run_id": run_id, "playbook": name, "runtime": runtime, "params": params,
             "exit_code": proc.returncode, "run_dir": str(run_dir)},
            actor="agent",
        )

        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "exit_code": proc.returncode,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
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
