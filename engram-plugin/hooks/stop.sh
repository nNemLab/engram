#!/usr/bin/env bash
# Stop: record the content hashes the agent grounded its answer in (the visible
# `grounded in … [<hash12>]` markers), as a backstop usage signal. Fail-open.
# Never injects context — always prints {}.
set -uo pipefail
URL="${ENGRAM_GROUNDING_URL:-http://127.0.0.1:8770}"
input="$(cat)"
msg="$(printf '%s' "$input" | jq -r '.assistant_message // empty' 2>/dev/null)"
[ -z "$msg" ] && { echo '{}'; exit 0; }

# 12-hex-char content-hash tokens in brackets, e.g. [a1b2c3d4e5f6]
hashes="$(printf '%s' "$msg" | grep -oE '\[[0-9a-f]{12}\]' | tr -d '[]' | sort -u)"
[ -z "$hashes" ] && { echo '{}'; exit 0; }

arr="$(printf '%s\n' "$hashes" | jq -R . | jq -s .)"
turn_id="$(printf '%s' "$msg" | sha256sum | cut -c1-16)"
curl -s --max-time 2 -X POST "$URL/cite" -H 'Content-Type: application/json' \
  -d "$(jq -n --argjson h "$arr" --arg t "$turn_id" '{hashes:$h,turn_id:$t}')" \
  >/dev/null 2>&1 || true
echo '{}'
