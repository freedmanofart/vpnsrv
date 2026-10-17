#!/usr/bin/env bash
set -euo pipefail

repo_root="${VPN_REPO_ROOT:-/home/freedman/vpn-service}"
if docker exec vpn-api python -m app.admin_alerts; then
  python3 "${repo_root}/scripts/notify_api_health_state.py" online || true
else
  python3 "${repo_root}/scripts/notify_api_health_state.py" offline || true
  exit 1
fi
