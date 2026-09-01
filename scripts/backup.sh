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

# Храним 14 полных дней. Глубина и шаблоны имён ограничены, чтобы неверно
# настроенный каталог не привёл к удалению посторонних файлов.
find "$backup_directory" -maxdepth 1 -type f \
  \( -name 'vpn-db-*.dump' -o -name 'vpn-config-*.tar.gz' \) \
  -mtime +14 -delete

# Машиночитаемый вывод попадает в журналы и может использоваться задачами загрузки.
printf 'database=%s\nconfig=%s\n' "$dump_path" "$config_path"
