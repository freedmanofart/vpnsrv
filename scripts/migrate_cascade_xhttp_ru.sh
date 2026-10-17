#!/usr/bin/env bash
set -euo pipefail

# Run as root on the RU child. Backups are captured before this migration.
mode=${1:-stage}
case "$mode" in stage|apply|rollback) ;; *) echo 'usage: migrate_cascade_xhttp_ru.sh stage|apply|rollback [path]' >&2; exit 2 ;; esac
path=${2:-}
if [[ $mode == stage ]]; then
  [[ $path =~ ^/[A-Za-z0-9_-]{1,64}$ ]] || { echo 'XHTTP path must be a simple absolute path' >&2; exit 2; }
elif [[ -n $path && ! $path =~ ^/[A-Za-z0-9_-]{1,64}$ ]]; then
  echo 'XHTTP path must be a simple absolute path' >&2
  exit 2
fi
source /etc/x-ui/install-result.env
dir=/root/cascade-xhttp-20261017
base="https://127.0.0.1:$XUI_PANEL_PORT/$XUI_WEB_BASE_PATH/panel/api"

if [[ $mode == stage ]]; then
  jq --arg path "$path" '
    .outbounds |= map(
      if .tag == "cascade-sweden" then
        .settings.vnext[0].port = 8443 |
        .streamSettings.network = "xhttp" |
        .streamSettings.xhttpSettings = {path: $path, mode: "auto"} |
        del(.streamSettings.tcpSettings)
      else . end
    )' "$dir/xray-template.json" > "$dir/xray-template.new.json"
  chmod 600 "$dir/xray-template.new.json"
  jq -e 'any(.outbounds[]; .tag == "cascade-sweden" and .settings.vnext[0].port == 8443 and .streamSettings.network == "xhttp")' \
    "$dir/xray-template.new.json" >/dev/null
  /usr/local/x-ui/bin/xray-linux-amd64 run -test -format json -config "$dir/xray-template.new.json"
  echo 'RU Xray template staged and validated'
  exit 0
fi

if [[ $mode == rollback ]]; then
  template="$dir/xray-template.json"
else
  template="$dir/xray-template.new.json"
  [[ -s $template ]] || { echo 'staged template not found; run stage first' >&2; exit 1; }
fi
response=$(curl -kfsS --max-time 30 -X POST \
  -H "Authorization: Bearer $XUI_API_TOKEN" \
  --data-urlencode "xraySetting@$template" "$base/xray/update")
jq -e '.success == true' <<< "$response" >/dev/null || {
  jq -r '.msg // "Xray template update failed"' <<< "$response" >&2
  exit 1
}
echo "RU cascade Xray template $mode complete"
