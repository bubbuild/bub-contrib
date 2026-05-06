#!/usr/bin/env bash
set -euo pipefail

NAME="handoff"
SUMMARY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --summary) SUMMARY="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# Require session info
if [[ -z "${BUB_SESSION_ID:-}" ]]; then
  echo "ERROR: BUB_SESSION_ID not set. Cannot perform handoff." >&2
  exit 1
fi

BRIDGE_URL="${BUB_BRIDGE_URL:-http://127.0.0.1:9800}"

# Build the ,tape.handoff command
COMMAND=",tape.handoff name=${NAME} summary='${SUMMARY}'"

# Post to http-bridge
curl -s -X POST "${BRIDGE_URL}/message" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c "
import json, sys
print(json.dumps({
    'session_id': sys.argv[1],
    'content': sys.argv[2],
    'source': 'codex'
}))
" "$BUB_SESSION_ID" "$COMMAND")"

echo "Handoff signal sent via http-bridge. Stop working now."
