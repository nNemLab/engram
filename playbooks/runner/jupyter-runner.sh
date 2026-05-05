#!/usr/bin/env bash
# Headless Jupyter runner via Papermill.
# Usage: jupyter-runner.sh <template.ipynb> <run_dir> [-p key value ...]
set -euo pipefail

template="${1:?template}"
run_dir="${2:?run_dir}"
shift 2

mkdir -p "$run_dir"
exec papermill "$template" "$run_dir/notebook.ipynb" --cwd "$run_dir" "$@"
