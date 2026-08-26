#!/usr/bin/env bash
set -euo pipefail

# Запускается на сервере/компьютере с checkout репозитория и копирует автономный
# тест на VPN-ноду. Сам run_standalone_xray_node.sh запускается уже на ноде.

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SOURCE="$PROJECT_ROOT/scripts/run_standalone_xray_node.sh"
NODE_SSH=${NODE_SSH:?Set NODE_SSH, for example root@203.0.113.10}
REMOTE_PATH=${REMOTE_PATH:-/root/run_standalone_xray_node.sh}

command -v ssh >/dev/null
command -v scp >/dev/null
test -f "$SOURCE"

SSH_ARGS=(
  -o BatchMode=yes
  -o ConnectTimeout=15
  -o ConnectionAttempts=3
)

# REMOTE_PATH предназначен только для абсолютного простого Unix-пути. Запрет
# пробелов и shell-метасимволов не позволяет превратить переменную в SSH-команду.
if [[ ! $REMOTE_PATH =~ ^/[a-zA-Z0-9._/-]+$ ]]; then
  echo "REMOTE_PATH must be an absolute path without spaces" >&2
  exit 2
fi
REMOTE_DIR=${REMOTE_PATH%/*}
[[ -n $REMOTE_DIR ]] || REMOTE_DIR=/

echo "Copying standalone Xray test to $NODE_SSH:$REMOTE_PATH"
ssh "${SSH_ARGS[@]}" "$NODE_SSH" "install -d -m 700 '$REMOTE_DIR'"
scp "${SSH_ARGS[@]}" "$SOURCE" "$NODE_SSH:$REMOTE_PATH"
ssh "${SSH_ARGS[@]}" "$NODE_SSH" "chmod 700 '$REMOTE_PATH' && test -x '$REMOTE_PATH'"

cat <<EOF
Copied successfully. Now connect to the VPN node:
  ssh $NODE_SSH

Then run on the node (use an allowed alternate port while production uses 443):
  PUBLIC_HOST=<NODE_PUBLIC_IP> XRAY_PORT=8443 $REMOTE_PATH

The copy script does not start Xray or change the node firewall.
EOF
