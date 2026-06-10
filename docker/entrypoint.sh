#!/usr/bin/env bash
# First-run init, then run the MCP HTTP server + daemons under one process.
# Minimal supervisor: start each daemon, propagate SIGTERM, exit if any dies.
set -euo pipefail

DATA=/data

# Privilege step-down. The container starts as root so it can fix ownership of the
# named data volume AND the host-bind-mounted vault (Docker creates the bind source
# as root, so the unprivileged user otherwise can't scaffold/write it). Then we drop
# to the runtime user (PUID/PGID, default 1000) for everything else.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
if [[ "$(id -u)" == "0" ]]; then
    chown -R "$PUID:$PGID" "$DATA" 2>/dev/null || true
    exec gosu "$PUID:$PGID" "$0" "$@"
fi

export HOME="$DATA"
export ENGRAM_CONFIG="$DATA/config.yml"

# --- First-run init (idempotent) ---
[[ -f "$ENGRAM_CONFIG" ]] || cp /opt/engram/config.docker.yml "$ENGRAM_CONFIG"
mkdir -p "$DATA/playbooks/scratch" "$DATA/playbooks/curated" "$DATA/playbooks/runs"
if [[ ! -d "$DATA/vault" || -z "$(ls -A "$DATA/vault" 2>/dev/null)" ]]; then
    mkdir -p "$DATA/vault"
    cp -r /opt/engram/vault-template/. "$DATA/vault/"
    find "$DATA/vault" -name '.gitkeep' -delete 2>/dev/null || true
fi
# Apply schema / create DB (engram applies schema on first connect).
python -c "from engram.common.db import get_connection; get_connection().close()"
# Seed starter playbooks if the scratch dir is empty. The seeder resolves its
# output dir via load_config(), which honors the ENGRAM_CONFIG exported above,
# so notebooks land in /data/playbooks/scratch (not ~/.engram).
[[ -n "$(ls -A "$DATA/playbooks/scratch" 2>/dev/null)" ]] || \
    python /opt/engram/seed_starter_playbooks.py 2>/dev/null || true

# --- Supervisor ---
pids=()
term() { kill "${pids[@]}" 2>/dev/null || true; }
trap term SIGTERM SIGINT

ENGRAM_MCP_TRANSPORT=http ENGRAM_MCP_HOST=0.0.0.0 ENGRAM_MCP_PORT=8765 engram-mcp & pids+=($!)
engram-reactor   & pids+=($!)
engram-projector & pids+=($!)
engram-watcher   & pids+=($!)
engram-poller    & pids+=($!)
engram-rag serve --host 0.0.0.0 --port 8770 & pids+=($!)

# Digest scheduler: run the daily-digest playbook every ENGRAM_DIGEST_INTERVAL seconds.
( interval="${ENGRAM_DIGEST_INTERVAL:-86400}"
  while true; do
      sleep "$interval"
      run_dir="$DATA/playbooks/runs/auto-digest-$(date -u +%Y-%m-%d)"
      mkdir -p "$run_dir"
      papermill "$DATA/playbooks/scratch/daily-digest.ipynb" "$run_dir/notebook.ipynb" \
          --cwd "$run_dir" -p window_hours 24 2>/dev/null || true
  done ) & pids+=($!)

# Exit (and stop the container) if any supervised process exits.
wait -n
term
exit 1
