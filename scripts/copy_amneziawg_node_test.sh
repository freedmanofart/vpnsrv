#!/usr/bin/env bash
set -euo pipefail

# Copy the complete standalone AmneziaWG toolkit to a VPN node. Nothing is
# installed or started by this helper.
PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUNNER_SOURCE="$PROJECT_ROOT/scripts/run_standalone_amneziawg_node.sh"
INSTALLER_SOURCE="$PROJECT_ROOT/scripts/install_amneziawg_node_dependencies.sh"
NODE_SSH=${NODE_SSH:?Set NODE_SSH, for example root@203.0.113.10}
RUNNER_REMOTE_PATH=${RUNNER_REMOTE_PATH:-/root/run_standalone_amneziawg_node.sh}
INSTALLER_REMOTE_PATH=${INSTALLER_REMOTE_PATH:-/root/install_amneziawg_node_dependencies.sh}

command -v ssh >/dev/null
command -v scp >/dev/null
test -f "$RUNNER_SOURCE"
test -f "$INSTALLER_SOURCE"

validate_path() {
  [[ $1 =~ ^/[a-zA-Z0-9._/-]+$ ]] || {
    echo "Remote paths must be absolute paths without spaces" >&2
    exit 2
  }
}
validate_path "$RUNNER_REMOTE_PATH"
validate_path "$INSTALLER_REMOTE_PATH"

SSH_ARGS=(-o BatchMode=yes -o ConnectTimeout=15 -o ConnectionAttempts=3)
for remote_path in "$RUNNER_REMOTE_PATH" "$INSTALLER_REMOTE_PATH"; do
  remote_dir=${remote_path%/*}
  [[ -n $remote_dir ]] || remote_dir=/
  ssh "${SSH_ARGS[@]}" "$NODE_SSH" "install -d -m 0700 '$remote_dir'"
done

scp "${SSH_ARGS[@]}" "$RUNNER_SOURCE" "$NODE_SSH:$RUNNER_REMOTE_PATH"
scp "${SSH_ARGS[@]}" "$INSTALLER_SOURCE" "$NODE_SSH:$INSTALLER_REMOTE_PATH"
ssh "${SSH_ARGS[@]}" "$NODE_SSH" \
  "chmod 0700 '$RUNNER_REMOTE_PATH' '$INSTALLER_REMOTE_PATH' && \
   test -x '$RUNNER_REMOTE_PATH' && test -x '$INSTALLER_REMOTE_PATH'"

cat <<EOF
Copied the standalone AmneziaWG toolkit to $NODE_SSH.
  runner:    $RUNNER_REMOTE_PATH
  installer: $INSTALLER_REMOTE_PATH
Nothing was installed or started. On the node, run explicitly:
  INSTALL_AWG=1 $INSTALLER_REMOTE_PATH
  $RUNNER_REMOTE_PATH --check
EOF
