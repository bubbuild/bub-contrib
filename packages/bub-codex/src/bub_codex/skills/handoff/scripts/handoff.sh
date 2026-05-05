#!/usr/bin/env bash
set -euo pipefail

NAME="handoff"
SUMMARY=""
NEXT_STEPS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --summary) SUMMARY="$2"; shift 2 ;;
    --next-steps) NEXT_STEPS="$2"; shift 2 ;;
    *) shift ;;
  esac
done

python3 -c "
import json, sys
signal = {'name': sys.argv[1], 'summary': sys.argv[2], 'next_steps': sys.argv[3]}
signal = {k: v for k, v in signal.items() if v}
with open('.bub-codex-handoff.json', 'w') as f:
    json.dump(signal, f, indent=2)
" "$NAME" "$SUMMARY" "$NEXT_STEPS"

echo "Handoff signal written. Stop working now."
