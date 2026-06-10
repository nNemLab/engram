"""Auto-summarize a playbook run directory into a markdown summary.

Reads run_dir/{notebook.ipynb,inputs.json,stdout.log,stderr.log} and writes
run_dir/summary.md. The agent then calls playbook.summarize MCP tool with
the summary text to push it through the dedup gate.

This script does NOT call an LLM; it produces a structural summary the agent
can read, optionally rewrite, and ingest. Keep dependencies thin.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def summarize(run_dir: Path) -> str:
    inputs = run_dir / "inputs.json"
    stdout = run_dir / "stdout.log"
    stderr = run_dir / "stderr.log"
    nb = run_dir / "notebook.ipynb"

    parts = [f"# Playbook run: {run_dir.name}", ""]

    if inputs.exists():
        parts += ["## Inputs", "", "```json", inputs.read_text().strip(), "```", ""]

    if nb.exists():
        try:
            data = json.loads(nb.read_text())
            cell_count = len(data.get("cells", []))
            parts += ["## Notebook", "", f"- Cells: {cell_count}", f"- Path: `{nb}`", ""]
        except Exception:
            pass

    if stderr.exists() and stderr.stat().st_size > 0:
        tail = stderr.read_text().splitlines()[-30:]
        parts += ["## stderr (tail)", "", "```", *tail, "```", ""]

    if stdout.exists() and stdout.stat().st_size > 0:
        tail = stdout.read_text().splitlines()[-30:]
        parts += ["## stdout (tail)", "", "```", *tail, "```", ""]

    parts += ["## Findings", "", "_(agent: fill in before ingesting)_", ""]
    return "\n".join(parts)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: summarize.py <run_dir>", file=sys.stderr)
        sys.exit(2)
    run_dir = Path(sys.argv[1]).resolve()
    out = run_dir / "summary.md"
    out.write_text(summarize(run_dir))
    print(out)


if __name__ == "__main__":
    main()
