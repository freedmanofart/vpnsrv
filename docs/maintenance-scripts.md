# Обслуживание

## Состав production

```text
postgres
vpn-api
vpn-bot
vpn-worker
3x-ui master (systemd)
```

Redis и мониторинг отсутствуют. PostgreSQL — общий контейнер `postgres`, данные
VPN находятся только в отдельной БД `vpn`.

## Проверка состояния

```bash
cd /home/freedman/vpn-service
docker compose ps
curl -fsS http://127.0.0.1:8000/health
docker compose logs --since=10m api bot worker
systemctl status x-ui vpn-threexui-proxy.service --no-pager
```

Worker в норме пишет `xray_errors=0`.

## Конфигурация

```bash
python3 scripts/configctl.py validate
python3 scripts/configctl.py list
python3 scripts/configctl.py get THREEXUI_API_TOKEN
python3 scripts/configctl.py set THREEXUI_VERIFY_TLS true
python3 scripts/configctl.py apply --services api worker
```

`.env` должен иметь права `0600`. Не используйте `set -x`.

## Backup

`backup.sh` создаёт custom-format dump БД `vpn` из контейнера `postgres` и
защищённый архив `.env`/Compose:

```bash
sudo VPN_PROJECT_DIR="$PWD" VPN_BACKUP_DIR=/var/backups/vpn-service \
  scripts/backup.sh
```

Проверка dump во временной БД того же PostgreSQL:

```bash
sudo scripts/verify_backup.sh \
  /var/backups/vpn-service/vpn-db-<timestamp>.dump
```

Backup не включает БД 3x-ui. Её резервируйте отдельно.

## Обновление

```bash
cd /home/freedman/vpn-service
git pull --ff-only origin newnode
python3 scripts/configctl.py validate
docker compose build api bot worker
docker compose run --rm api alembic upgrade head
docker compose up -d --remove-orphans
curl -fsS http://127.0.0.1:8000/health
```

Перед удалением контейнера или БД всегда создавайте backup. Не удаляйте `mydb`
из общего PostgreSQL: VPN использует только БД `vpn`.

## Тесты

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Unit-тесты не обращаются к production 3x-ui. Скрипты `e2e_*` создают реальные
данные и запускаются только в отдельном тестовом окружении.

## Восстановление старого Telegram-чека

Если в старой записи платежа сохранился только Telegram `file_id`, одноразовый
скрипт скачивает файл через Bot API и загружает бинарный чек в VPN API:

```bash
docker compose exec bot python -m app.backfill_receipt \
  PAYMENT_ID USER_ID TELEGRAM_FILE_ID TELEGRAM_FILE_UNIQUE_ID photo
```

Последний аргумент — `photo` для изображения или `document` для PDF. Скрипту
нужны штатные `BOT_TOKEN`, `API_URL` и `SERVICE_API_TOKEN` контейнера. Перед
запуском сверьте `PAYMENT_ID` и `USER_ID`: API отклоняет чек, если платёж
принадлежит другому пользователю. Повторный запуск перезаписывает сохранённый
binary-чек этой записи платежа.
