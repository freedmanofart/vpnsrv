#!/usr/bin/env bash
set -euo pipefail

# Read-only diagnostic for a deployed node-agent. Run on the VPN node.
AGENT_URL=${AGENT_URL:-http://127.0.0.1:10086}
AGENT_HEALTH_PATH=${AGENT_HEALTH_PATH:-/health}
URL="${AGENT_URL%/}${AGENT_HEALTH_PATH}"
command -v curl >/dev/null || { echo 'curl is required' >&2; exit 2; }
printf 'Node-agent endpoint: %s\n' "$URL"
curl --fail --show-error --max-time 10 "$URL"
printf '\n'
command -v podman >/dev/null && podman ps --filter name=vpn-node-agent --format 'container={{.Names}} status={{.Status}}' || true
