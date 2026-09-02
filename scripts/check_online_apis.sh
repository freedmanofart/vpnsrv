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

check "compose" docker compose ps api bot worker
check "local health" curl -fsS http://127.0.0.1:8000/health
check "public landing" bash -c "curl -fsS '$public_base_url/' >/dev/null && echo public_landing_ok"
check "plans" curl -fsS http://127.0.0.1:8000/plans
check "payment methods" curl -fsS http://127.0.0.1:8000/payment-methods

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
