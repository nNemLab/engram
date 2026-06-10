#!/usr/bin/env bash
# UserPromptSubmit: inject calibrated grounding for the user's prompt.
# Fail-open — any error / NONE verdict injects nothing (never blocks the turn).
set -uo pipefail
URL="${ENGRAM_GROUNDING_URL:-http://127.0.0.1:8770}"
input="$(cat)"
prompt="$(printf '%s' "$input" | jq -r '.prompt // empty' 2>/dev/null)"
[ -z "$prompt" ] && { echo '{}'; exit 0; }

resp="$(curl -s --max-time 2 -X POST "$URL/grounding" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg q "$prompt" '{query:$q}')" 2>/dev/null)"
[ -z "$resp" ] && { echo '{}'; exit 0; }

verdict="$(printf '%s' "$resp" | jq -r '.verdict // "NONE"' 2>/dev/null)"
block="$(printf '%s' "$resp" | jq -r '.block // empty' 2>/dev/null)"
if { [ "$verdict" = "STRONG" ] || [ "$verdict" = "WEAK" ]; } && [ -n "$block" ]; then
  jq -n --arg ctx "$block" \
    '{hookSpecificOutput:{hookEventName:"UserPromptSubmit",additionalContext:$ctx}}'
else
  echo '{}'
fi
