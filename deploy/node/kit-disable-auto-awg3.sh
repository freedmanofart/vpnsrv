#!/usr/bin/env bash
set -euo pipefail

kit_path=${KIT_PATH:-/usr/local/bin/kit}
patched='awg_ids() { api GET inbounds/list | jq -r '\''[.[] | select(.protocol == "amneziawg" and .remark == "AmneziaWG") | .id] | sort | .[]'\''; }'

grep -q '^awg_ids() {' "$kit_path"
if grep -Fqx "$patched" "$kit_path"; then
  exit 0
fi

tmp_path=$(mktemp "${kit_path}.XXXXXX")
trap 'rm -f "$tmp_path"' EXIT
awk -v replacement="$patched" '
  /^awg_ids\(\) \{/ { print replacement; next }
  { print }
' "$kit_path" >"$tmp_path"
chmod 0755 "$tmp_path"
mv "$tmp_path" "$kit_path"
trap - EXIT
grep -Fqx "$patched" "$kit_path"
