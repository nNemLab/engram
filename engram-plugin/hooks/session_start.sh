#!/usr/bin/env bash
# SessionStart: inject the priming block (active goals + recent knowledge).
# Fail-open — empty/error injects nothing.
set -uo pipefail
URL="${ENGRAM_GROUNDING_URL:-http://127.0.0.1:8770}"
input="$(cat)"
cwd="$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)"

resp="$(curl -s --max-time 2 -X POST "$URL/prime" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg c "$cwd" '{cwd:$c}')" 2>/dev/null)"
[ -z "$resp" ] && { echo '{}'; exit 0; }

block="$(printf '%s' "$resp" | jq -r '.block // empty' 2>/dev/null)"
if [ -n "$block" ]; then
  jq -n --arg ctx "$block" \
    '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$ctx}}'
else
  echo '{}'
fi
