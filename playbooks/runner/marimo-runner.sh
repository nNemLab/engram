#!/usr/bin/env bash
# Headless Marimo runner.
# Usage: marimo-runner.sh <template.py> <run_dir> [--key value ...]
set -euo pipefail

template="${1:?template}"
run_dir="${2:?run_dir}"
shift 2

mkdir -p "$run_dir"
cd "$run_dir"
exec marimo run "$template" --headless "$@"
