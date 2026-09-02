#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_email="${1:-}"

cd "$project_dir"

env_value() {
  local key="$1"
  local fallback="$2"
  local value
  value="$(awk -F= -v key="$key" '$1 == key {value=$0; sub("^[^=]*=", "", value); print value}' .env 2>/dev/null | tail -n 1)"
  printf '%s' "${value:-$fallback}"
}

echo "== Compose services =="
docker compose ps api bot worker

echo
echo "== API health =="
curl -fsS http://127.0.0.1:8000/health
echo

echo
echo "== Mail environment inside api =="
docker compose exec -T api python - <<'PY'
import os
for key in (
    "PUBLIC_BASE_URL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_FROM",
    "SMTP_STARTTLS",
    "SMTP_USE_SSL",
):
    value = os.getenv(key, "")
    print(f"{key}={value}")
PY

echo
echo "== Ensure login-code admin column exists =="
postgres_container="$(env_value POSTGRES_CONTAINER postgres)"
postgres_user="$(docker exec "$postgres_container" printenv POSTGRES_USER)"
postgres_db="$(env_value VPN_DATABASE_NAME vpn)"
docker exec "$postgres_container" psql -U "$postgres_user" -d "$postgres_db" -c \
  "ALTER TABLE cabinet_login_codes ADD COLUMN IF NOT EXISTS plain_code VARCHAR(6);"

echo
echo "== SMTP login check =="
docker compose exec -T api python - <<'PY'
import os
import smtplib

host = os.getenv("SMTP_HOST", "")
port = int(os.getenv("SMTP_PORT") or 0)
username = os.getenv("SMTP_USERNAME", "")
password = os.getenv("SMTP_PASSWORD", "")
use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
starttls = os.getenv("SMTP_STARTTLS", "false").lower() == "true"

if not host:
    raise SystemExit("SMTP_HOST is empty")
smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
with smtp_class(host, port, timeout=15) as client:
    if starttls and not use_ssl:
        client.starttls()
    if username:
        code, _ = client.login(username, password)
        print(f"smtp_login={code}")
    else:
        print("smtp_login=skipped_no_username")
PY

if [[ -n "$test_email" ]]; then
  echo
  echo "== Send test cabinet code to ${test_email} =="
  docker compose exec -T api python - "$test_email" <<'PY'
import asyncio
import sys

from app.core.config import settings
from app.services.email import send_cabinet_code

async def main() -> None:
    await send_cabinet_code(sys.argv[1], "000000", settings.cabinet_email_code_ttl_minutes)
    print("test_email_sent=ok")

asyncio.run(main())
PY
fi

echo
echo "== Fast recreate api/bot =="
docker compose up -d --force-recreate api bot

echo
echo "== Recent mail/cabinet logs =="
docker compose logs --tail=120 api bot | grep -i -E "smtp|email|mail|cabinet|telegram-cabinet-link|web/register|error|failed|exception|503" || true

echo
echo "mail_chain_check=ok"
