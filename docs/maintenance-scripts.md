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

### Ротация секретов

`configctl rotate` — единая операция для переменных, которыми владеет сам
проект. Она генерирует новые значения в памяти, записывает `.env` атомарно,
проверяет итоговую конфигурацию и пересоздаёт все сервисы, которым нужны новые
значения. В вывод секреты не попадают. Если `docker compose up` не удался,
скрипт восстанавливает прежний `.env` и пытается вернуть прежнюю конфигурацию
тех же сервисов.

Сначала всегда можно посмотреть план без изменений:

```bash
python3 scripts/configctl.py rotate --all-internal --dry-run
```

Обычная внутренняя ротация меняет `SERVICE_API_TOKEN` и `ADMIN_PASSWORD` и
пересоздаёт `api`, `bot` и `worker` одной командой:

```bash
python3 scripts/configctl.py rotate --all-internal
```

После неё операторам нужно заново аутентифицироваться в Basic Auth, а внешние
скрипты и интеграции с `SERVICE_API_TOKEN` должны получить новое значение из
защищённого хранилища. Не выводите это значение через `get --show-secret` в
терминал с записью истории.

Можно выбрать конкретный внутренний секрет:

```bash
python3 scripts/configctl.py rotate ADMIN_PASSWORD
```

`PAYMENT_WEBHOOK_SECRET` передаётся стороннему платёжному провайдеру, поэтому
он намеренно исключён из локальной генерации: сервер не может сам сообщить
провайдеру новое значение. Сначала перевыпустите или смените секрет в панели
провайдера, затем внесите выданное значение и пересоздайте нужные сервисы:

```bash
python3 scripts/configctl.py set PAYMENT_WEBHOOK_SECRET '<provider-issued-secret>'
python3 scripts/configctl.py apply --services api worker
```

Токены `BOT_TOKEN`,
`THREEXUI_API_TOKEN`, SMTP-пароль и `DATABASE_URL` не генерируются локально:
их надо перевыпустить у Telegram, 3x-ui, почтового провайдера или PostgreSQL,
внести через `configctl set` и выполнить `configctl apply` для затронутых
сервисов. В частности, произвольный `THREEXUI_API_TOKEN` не будет принят
мастер-узлом 3x-ui.

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
