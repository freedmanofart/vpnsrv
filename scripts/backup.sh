#!/usr/bin/env bash
set -euo pipefail

project_directory=${VPN_PROJECT_DIR:-/home/freedman/vpn-service}
backup_directory=${VPN_BACKUP_DIR:-/var/backups/vpn-service}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)

install -d -m 700 "$backup_directory"

postgres_user=$(docker exec vpn-postgres printenv POSTGRES_USER)
postgres_db=$(docker exec vpn-postgres printenv POSTGRES_DB)
dump_path="$backup_directory/vpn-db-$timestamp.dump"
config_path="$backup_directory/vpn-config-$timestamp.tar.gz"

docker exec vpn-postgres pg_dump \
  -U "$postgres_user" \
  -d "$postgres_db" \
  -Fc >"$dump_path"

tar -C "$project_directory" -czf "$config_path" \
  .env \
  docker-compose.yml \
  xray/config.json \
  observability

chmod 600 "$dump_path" "$config_path"
pg_restore --list "$dump_path" >/dev/null 2>&1 || \
  docker exec -i vpn-postgres pg_restore --list <"$dump_path" >/dev/null

find "$backup_directory" -maxdepth 1 -type f \
  \( -name 'vpn-db-*.dump' -o -name 'vpn-config-*.tar.gz' \) \
  -mtime +14 -delete

printf 'database=%s\nconfig=%s\n' "$dump_path" "$config_path"
