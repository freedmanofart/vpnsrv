#!/usr/bin/env bash
# Восстанавливает production-БД vpn из custom-format dump.
# Опасная операция: текущая БД переименовывается в аварийную копию, затем
# создаётся новая БД vpn и в неё загружается dump.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: RESTORE_CONFIRM=I_UNDERSTAND scripts/restore_postgres.sh /var/backups/vpn-service/vpn-db-TIMESTAMP.dump" >&2
  exit 2
fi

if [[ "${RESTORE_CONFIRM:-}" != "I_UNDERSTAND" ]]; then
  echo "Refusing to restore without RESTORE_CONFIRM=I_UNDERSTAND" >&2
  exit 2
fi

dump_path=$1
[[ -s "$dump_path" ]] || { echo "backup is missing or empty: $dump_path" >&2; exit 1; }

postgres_container=${POSTGRES_CONTAINER:-postgres}
postgres_user=$(docker exec "$postgres_container" printenv POSTGRES_USER)
database_name=${VPN_DATABASE_NAME:-vpn}
timestamp=$(date -u +%Y%m%d%H%M%S)
previous_db="${database_name}_before_restore_${timestamp}"

[[ "$database_name" =~ ^[a-zA-Z0-9_]+$ ]] || { echo "invalid database name" >&2; exit 1; }
[[ "$previous_db" =~ ^[a-zA-Z0-9_]+$ ]] || { echo "invalid backup database name" >&2; exit 1; }

echo "Stopping services that write to PostgreSQL..."
docker compose stop api bot worker >/dev/null

echo "Renaming $database_name to $previous_db..."
docker exec "$postgres_container" psql -U "$postgres_user" -d postgres \
  -c "ALTER DATABASE $database_name WITH ALLOW_CONNECTIONS false;" >/dev/null
docker exec "$postgres_container" psql -U "$postgres_user" -d postgres \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$database_name';" >/dev/null
docker exec "$postgres_container" psql -U "$postgres_user" -d postgres \
  -c "ALTER DATABASE $database_name RENAME TO $previous_db;" >/dev/null

echo "Creating clean $database_name..."
docker exec "$postgres_container" createdb -U "$postgres_user" "$database_name"

echo "Restoring dump..."
docker exec -i "$postgres_container" pg_restore \
  -U "$postgres_user" \
  -d "$database_name" \
  --no-owner \
  --no-privileges <"$dump_path"

echo "Starting services..."
docker compose up -d api bot worker >/dev/null

echo "restore_ok database=$database_name previous_database=$previous_db dump=$dump_path"
