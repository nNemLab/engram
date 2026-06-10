#!/usr/bin/env bash
# Stop: record the content hashes the agent grounded its answer in (the visible
# `grounded in … [<hash12>]` markers in its final message), as a backstop usage
# signal. Reads the transcript (Claude Code Stop hooks provide transcript_path,
# not the message text). Fail-open; never injects — always prints {}.
set -uo pipefail
URL="${ENGRAM_GROUNDING_URL:-http://127.0.0.1:8770}"
input="$(cat)"
transcript="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)"
{ [ -z "$transcript" ] || [ ! -f "$transcript" ]; } && { echo '{}'; exit 0; }

# Last assistant message's text blocks, joined.
msg="$(jq -rs '
  map(select(.type=="assistant")) | last
  | if . == null then "" else ([(.message.content // [])[] | select(.type=="text") | .text] | join("\n")) end
' "$transcript" 2>/dev/null)"
[ -z "$msg" ] && { echo '{}'; exit 0; }

hashes="$(printf '%s' "$msg" | grep -oE '\[[0-9a-f]{12}\]' | tr -d '[]' | sort -u)"
[ -z "$hashes" ] && { echo '{}'; exit 0; }

arr="$(printf '%s\n' "$hashes" | jq -R . | jq -s .)"
turn_id="$(printf '%s' "$msg" | sha256sum | cut -c1-16)"
curl -s --max-time 2 -X POST "$URL/cite" -H 'Content-Type: application/json' \
  -d "$(jq -n --argjson h "$arr" --arg t "$turn_id" '{hashes:$h,turn_id:$t}')" \
  >/dev/null 2>&1 || true
echo '{}'
