#!/usr/bin/env bash
set -euo pipefail

# Run as root on the 3x-ui master. Inbound 9 is the Sweden transit listener.
mode=${1:-apply}
path=${2:-}
case "$mode" in apply|rollback) ;; *) echo 'usage: migrate_cascade_xhttp_master.sh apply|rollback [path]' >&2; exit 2 ;; esac
[[ $mode == rollback || $path =~ ^/[A-Za-z0-9_-]{1,64}$ ]] || { echo 'XHTTP path must be a simple absolute path' >&2; exit 2; }
source /etc/x-ui/install-result.env
api="http://127.0.0.1:41026/${XUI_WEB_BASE_PATH}/panel/api"
dir=/var/backups/cascade-xhttp-20261017
install -d -m 700 "$dir"
auth="Authorization: Bearer $XUI_API_TOKEN"

if [[ $mode == rollback ]]; then
  source_row="$dir/se-inbound-9.json"
  [[ -s $source_row ]] || { echo 'saved inbound backup not found' >&2; exit 1; }
else
  source_row=$(mktemp)
  curl -fsS -H "$auth" "$api/inbounds/list" |
    jq -ec '.obj[] | select(.id == 9 and .nodeId == 6)' > "$source_row"
  if [[ ! -e "$dir/se-inbound-9.json" ]]; then
    install -m 600 "$source_row" "$dir/se-inbound-9.json"
  fi
fi

row=$(cat "$source_row")
if [[ $mode == apply ]]; then
  row=$(jq -c --arg path "$path" '
    .port = 8443 |
    .streamSettings |= (if type == "string" then fromjson else . end) |
    .streamSettings.network = "xhttp" |
    .streamSettings.xhttpSettings = {path: $path, mode: "auto"} |
    del(.streamSettings.tcpSettings)' <<< "$row")
fi
body=$(jq -c '{id,remark,enable,listen,port,protocol,expiryTime,total,
  settings:(.settings | if type == "string" then . else tojson end),
  streamSettings:(.streamSettings | if type == "string" then . else tojson end),
  sniffing:(.sniffing | if type == "string" then . else tojson end)}' <<< "$row")
response=$(curl -fsS --max-time 25 -X POST -H "$auth" -H 'Content-Type: application/json' \
  --data-binary "$body" "$api/inbounds/update/9")
jq -e '.success == true' <<< "$response" >/dev/null || {
  jq -r '.msg // "inbound update failed"' <<< "$response" >&2
  exit 1
}
[[ $mode == apply ]] && echo 'Sweden transit inbound now uses XHTTP on TCP/8443' || echo 'Sweden transit inbound restored'
