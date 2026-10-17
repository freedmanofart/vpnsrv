#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
telegram_id="${1:-}"

cd "$project_dir"

env_value() {
  local key="$1"
  local fallback="$2"
  local value
  value="$(awk -F= -v key="$key" '$1 == key {value=$0; sub("^[^=]*=", "", value); print value}' .env 2>/dev/null | tail -n 1)"
  printf '%s' "${value:-$fallback}"
}

public_base_url="$(env_value PUBLIC_BASE_URL https://freedomvpn.taile485ac.ts.net)"
admin_username="$(env_value ADMIN_USERNAME admin)"
admin_password="$(env_value ADMIN_PASSWORD "")"
service_api_token="$(env_value SERVICE_API_TOKEN "")"

check() {
  local name="$1"
  shift
  printf '== %s ==\n' "$name"
  "$@"
  printf '\n'
}

check_collection() {
  local name="$1"
  local url="$2"
  local body
  local count
  body="$(curl -fsS -H "Authorization: Bearer $service_api_token" "$url")"
  count="$(python3 -c 'import json,sys; value=json.load(sys.stdin); print(len(value) if isinstance(value,list) else -1)' <<<"$body")"
  [[ "$count" -ge 0 ]] || return 1
  printf '%s_ok entries=%s\n' "$name" "$count"
}

check_public_landing() {
  local scheme host ips ip code total=0
  read -r scheme host < <(
    python3 -c 'from urllib.parse import urlsplit; import sys; p=urlsplit(sys.argv[1]); print(p.scheme, p.hostname or "")' \
      "$public_base_url"
  )

  if [[ "$scheme" != "https" || "$host" != *.ts.net ]]; then
    curl -fsS "$public_base_url/" >/dev/null
    echo public_landing_ok
    return
  fi

  ips="$(dig +short +time=2 +tries=1 @1.1.1.1 A "$host" 2>/dev/null | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/')"
  if [[ -z "$ips" ]]; then
    ips="$(dig +short +time=2 +tries=1 @8.8.8.8 A "$host" 2>/dev/null | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/')"
  fi
  [[ -n "$ips" ]] || return 1

  while IFS= read -r ip; do
    [[ -n "$ip" ]] || continue
    code="$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
      --connect-timeout 4 --max-time 12 \
      --resolve "$host:443:$ip" "$public_base_url/" 2>/dev/null || true)"
    [[ "$code" == 200 ]] || return 1
    total=$((total + 1))
  done <<< "$(printf '%s\n' "$ips" | sort -u)"
  (( total > 0 )) || return 1
  printf 'public_landing_ok relays=%s\n' "$total"
}

check "compose" docker compose ps api bot worker
check "local health" curl -fsS http://127.0.0.1:8000/health
check "public landing" check_public_landing
check "plans" check_collection plans http://127.0.0.1:8000/plans
check "payment methods" check_collection payment_methods http://127.0.0.1:8000/payment-methods

if [[ -n "$admin_password" ]]; then
  check "admin overview" bash -c "curl -fsS -u '$admin_username:$admin_password' http://127.0.0.1:8000/admin/overview >/dev/null && echo admin_overview_ok"
else
  echo "== admin overview =="
  echo "skip: ADMIN_PASSWORD is empty"
  echo
fi

if [[ -n "$telegram_id" && -n "$service_api_token" ]]; then
  check "telegram user" curl -fsS -H "Authorization: Bearer $service_api_token" "http://127.0.0.1:8000/users/$telegram_id"
  check "telegram user status" curl -fsS -H "Authorization: Bearer $service_api_token" "http://127.0.0.1:8000/users/$telegram_id/status"
else
  echo "== telegram user/status =="
  echo "skip: pass TELEGRAM_ID as first argument to check user endpoints"
  echo
fi

check "tailscale funnel" tailscale funnel status

echo "online_api_check=ok"
