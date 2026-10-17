#!/usr/bin/env bash
set -euo pipefail

# Run on the master as root. Existing IDs, UUIDs, client lists, and ports stay
# unchanged; only listener binding and TLS termination move behind Nginx.
mode=${1:-}
id=${2:-}
case "$mode" in update|restore) ;; *) echo 'usage: ru_kit_443_master.sh update|restore ID' >&2; exit 2 ;; esac
case "$id" in 28|29|31|32|33|34) ;; *) echo "unsupported inbound ID: $id" >&2; exit 2 ;; esac
source /etc/x-ui/install-result.env
api="http://127.0.0.1:41026/${XUI_WEB_BASE_PATH}/panel/api"
backup_dir=/var/backups/ru-hosts-443-20261017
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

get_row() {
  curl -fsS --max-time 15 -H "Authorization: Bearer $XUI_API_TOKEN" "$api/inbounds/list" |
    jq -ec --argjson id "$id" '.obj[] | select(.id == $id)'
}
body() {
  jq -c '{id,remark,enable,listen,port,protocol,expiryTime,total,
    settings:(.settings | if type=="string" then . else tojson end),
    streamSettings:(.streamSettings | if type=="string" then . else tojson end),
    sniffing:(.sniffing | if type=="string" then . else tojson end)}'
}
update_row() {
  local response
  response=$(body | curl -fsS --max-time 20 -X POST -H "Authorization: Bearer $XUI_API_TOKEN" \
    -H 'Content-Type: application/json' --data-binary @- "$api/inbounds/update/$id")
  jq -e '.success == true' <<< "$response" >/dev/null || { jq -r '.msg // "API update failed"' <<< "$response" >&2; return 1; }
}

if [[ $mode == restore ]]; then
  [[ -s $backup_dir/inbound-$id.json ]] || exit 1
  update_row < "$backup_dir/inbound-$id.json"
  echo "restored inbound $id"
  exit
fi

old=$(get_row)
if [[ ! -f $backup_dir/inbound-$id.json ]]; then
  umask 077
  printf '%s\n' "$old" > "$backup_dir/inbound-$id.json"
fi
new=$(jq -c --argjson id "$id" '
  .listen = "127.0.0.1" |
  .streamSettings |= (if type=="string" then fromjson else . end) |
  if $id == 28 or $id == 29 then
    .streamSettings.tcpSettings.acceptProxyProtocol = true
  elif $id == 31 then
    .streamSettings.sockopt.acceptProxyProtocol = true
  else
    .streamSettings.security = "none" |
    del(.streamSettings.tlsSettings)
  end' <<< "$old")
update_row <<< "$new"
current=$(get_row)
[[ $(jq -r '.listen' <<< "$current") == 127.0.0.1 ]] || { echo "listen did not update for $id" >&2; exit 1; }
echo "updated inbound $id"
