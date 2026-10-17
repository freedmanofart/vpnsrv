#!/usr/bin/env bash
set -euo pipefail

# Run on the master as root after the RU Nginx listener is active.
source /etc/x-ui/install-result.env
api="http://127.0.0.1:41026/${XUI_WEB_BASE_PATH}/panel/api"
auth="Authorization: Bearer $XUI_API_TOKEN"
list=$(curl -fsS -H "$auth" "$api/hosts/list" | jq -ec '.obj')

for spec in '28:12:REALITY' '29:12:REALITY-2' '31:14:XHTTP' \
            '32:15:VLESS-WS' '33:16:Trojan-gRPC' '34:17:VMess-WS'; do
  IFS=: read -r id template name <<< "$spec"
  if jq -e --argjson id "$id" --arg host '176.108.250.142:443' \
    'any(.[]; (.inboundIds | index($id)) != null and (.hosts | index($host)) != null)' <<< "$list" >/dev/null; then
    echo "host for inbound $id already exists"
    continue
  fi
  payload=$(jq -c --argjson template "$template" --argjson id "$id" --arg name "$name" '
    .[] | select(.inboundIds == [$template]) |
    del(.groupId, .sortOrder) |
    .inboundIds = [$id] |
    .hosts = ["176.108.250.142:443"] |
    .port = 443 |
    .remark = ("RU " + $name) |
    .sni = (if .security == "tls" then "176.108.250.142.nip.io" else "" end)' <<< "$list")
  [[ -n $payload ]] || { echo "template for $id not found" >&2; exit 1; }
  response=$(curl -fsS -X POST -H "$auth" -H 'Content-Type: application/json' \
    --data-binary "$payload" "$api/hosts/add")
  jq -e '.success == true' <<< "$response" >/dev/null || { jq -r '.msg // "host add failed"' <<< "$response" >&2; exit 1; }
  echo "added RU host for inbound $id"
  list=$(curl -fsS -H "$auth" "$api/hosts/list" | jq -ec '.obj')
done
