#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: verify_backup.sh /path/to/vpn-db-TIMESTAMP.dump" >&2
  exit 2
fi

dump_path=$1
[[ -s "$dump_path" ]] || { echo "backup is missing or empty" >&2; exit 1; }

postgres_user=$(docker exec vpn-postgres printenv POSTGRES_USER)
temporary_db="vpn_restore_verify_$(date -u +%Y%m%d%H%M%S)"
[[ "$temporary_db" =~ ^vpn_restore_verify_[0-9]{14}$ ]] || exit 1

cleanup() {
  docker exec vpn-postgres psql -U "$postgres_user" -d postgres \
    -c "DROP DATABASE IF EXISTS $temporary_db WITH (FORCE);" >/dev/null
}
trap cleanup EXIT

docker exec vpn-postgres createdb -U "$postgres_user" "$temporary_db"
docker exec -i vpn-postgres pg_restore \
  -U "$postgres_user" \
  -d "$temporary_db" \
  --no-owner \
  --no-privileges <"$dump_path"

counts=$(docker exec vpn-postgres psql -U "$postgres_user" -d "$temporary_db" -Atc \
  "SELECT (SELECT count(*) FROM users),(SELECT count(*) FROM subscriptions),(SELECT count(*) FROM vpn_clients);")
printf 'restore_ok database=%s counts=%s\n' "$temporary_db" "$counts"
