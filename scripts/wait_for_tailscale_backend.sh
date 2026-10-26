#!/usr/bin/env bash
set -euo pipefail

attempts=120
delay_seconds=2

for ((attempt = 1; attempt <= attempts; attempt++)); do
  if /usr/bin/tailscale status --json 2>/dev/null \
      | /usr/bin/python3 -c '
import json
import sys

try:
    state = json.load(sys.stdin).get("BackendState")
except (json.JSONDecodeError, OSError):
    raise SystemExit(1)
raise SystemExit(0 if state == "Running" else 1)
'; then
    exit 0
  fi
  sleep "${delay_seconds}"
done

echo "Tailscale backend did not reach Running within $((attempts * delay_seconds)) seconds" >&2
exit 1
