#!/usr/bin/env bash
# Создаёт согласованный дамп PostgreSQL и отдельный архив файлов, необходимых
# для восстановления control plane. Процедура восстановления, сроки хранения
# и модель безопасности описаны в docs/maintenance-scripts.md.
set -euo pipefail

# Оба пути можно переопределить через systemd или при разовом ручном запуске.
# Метка UTC сохраняет правильную сортировку архивов, созданных на разных узлах.
project_directory=${VPN_PROJECT_DIR:-/home/freedman/vpn-service}
backup_directory=${VPN_BACKUP_DIR:-/var/backups/vpn-service}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)

# Копии содержат строки БД, учётные данные и закрытый ключ Reality, поэтому
# каталог и созданные файлы не должны быть доступны другим пользователям.
install -d -m 700 "$backup_directory"

# VPN использует общесистемный контейнер PostgreSQL, но отдельную БД.
postgres_container=${POSTGRES_CONTAINER:-postgres}
postgres_user=$(docker exec "$postgres_container" printenv POSTGRES_USER)
postgres_db=${VPN_DATABASE_NAME:-vpn}
dump_path="$backup_directory/vpn-db-$timestamp.dump"
config_path="$backup_directory/vpn-config-$timestamp.tar.gz"

# Custom format поддерживает проверку через pg_restore и выборочное восстановление.
# Перенаправление выполняется на хосте и кладёт артефакт в каталог копий.
docker exec "$postgres_container" pg_dump \
  -U "$postgres_user" \
  -d "$postgres_db" \
  -Fc >"$dump_path"

# Конфигурация хранится отдельно от дампа БД. Архив включает .env с токеном
# 3x-ui master, поэтому требует такой же защиты, как пароль.
tar -C "$project_directory" -czf "$config_path" \
  .env \
  docker-compose.yml

# Выявляем обрезанный или повреждённый дамп до удаления старой точки восстановления.
# Сначала используем pg_restore хоста, а при его отсутствии — бинарник контейнера.
chmod 600 "$dump_path" "$config_path"
pg_restore --list "$dump_path" >/dev/null 2>&1 || \
  docker exec -i "$postgres_container" pg_restore --list <"$dump_path" >/dev/null

# Keep three complete restore points, pairing each database dump with its config.
mapfile -t backup_timestamps < <(
  for existing_dump in "$backup_directory"/vpn-db-*.dump; do
    [[ -f "$existing_dump" ]] || continue
    existing_timestamp=${existing_dump##*vpn-db-}
    existing_timestamp=${existing_timestamp%.dump}
    [[ -f "$backup_directory/vpn-config-$existing_timestamp.tar.gz" ]] &&
      printf '%s\n' "$existing_timestamp"
  done | sort -r
)
retained_timestamps=("${backup_timestamps[@]:0:3}")
for backup_file in "$backup_directory"/vpn-db-*.dump \
  "$backup_directory"/vpn-config-*.tar.gz; do
  [[ -f "$backup_file" ]] || continue
  case "$backup_file" in
    *vpn-db-*.dump)
      file_timestamp=${backup_file##*vpn-db-}
      file_timestamp=${file_timestamp%.dump}
      ;;
    *vpn-config-*.tar.gz)
      file_timestamp=${backup_file##*vpn-config-}
      file_timestamp=${file_timestamp%.tar.gz}
      ;;
  esac
  [[ " ${retained_timestamps[*]} " == *" $file_timestamp "* ]] ||
    rm -f -- "$backup_file"
done

# Машиночитаемый вывод попадает в журналы и может использоваться задачами загрузки.
printf 'database=%s\nconfig=%s\n' "$dump_path" "$config_path"
