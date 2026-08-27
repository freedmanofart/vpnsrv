#!/usr/bin/env bash
set -euo pipefail

# Backward-compatible name for the complete toolkit copy helper.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec "$SCRIPT_DIR/copy_amneziawg_node_test.sh" "$@"
