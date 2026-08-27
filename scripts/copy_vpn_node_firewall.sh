#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
NODE_SSH=${NODE_SSH:?Set NODE_SSH, for example root@203.0.113.10}
REMOTE_PATH=${REMOTE_PATH:-/root/harden_vpn_node_firewall.sh}
[[ $REMOTE_PATH =~ ^/[A-Za-z0-9._/-]+$ ]] || { echo "Unsafe REMOTE_PATH" >&2; exit 2; }
command -v ssh >/dev/null
command -v scp >/dev/null
scp -o BatchMode=yes "$PROJECT_ROOT/scripts/harden_vpn_node_firewall.sh" "$NODE_SSH:$REMOTE_PATH"
ssh -o BatchMode=yes "$NODE_SSH" "chmod 0700 '$REMOTE_PATH'"
echo "Copied to $NODE_SSH:$REMOTE_PATH; the firewall was NOT applied."
