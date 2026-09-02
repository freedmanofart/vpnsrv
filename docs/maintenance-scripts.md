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

## Единый операторский flow

Основной путь обслуживания теперь собран в `/admin`:

1. Откройте `https://freedomvpn.taile485ac.ts.net/admin`.
2. В боковом меню выберите нужный раздел:
   - `Пользователи` — доступ, перевыпуск ключа, ручная смена пароля;
   - `Платежи` — чеки, подтверждение, ошибка, отмена, возврат;
   - `Коды входа` — последние email-коды web-кабинета;
   - `Документация` — основные markdown-файлы проекта;
   - `Health` — дашборд состояния API, PostgreSQL, публичного URL, SMTP и 3x-ui;
   - `Скрипты` — запуск безопасных проверок и команды host-only операций;
   - `Инфраструктура` — сайт, кабинет, admin и master 3x-ui из конфигурации нод.
3. Если проблема связана с письмами, сначала выполните проверку почтовой цепочки.
4. Если проблема шире, запустите online-тесты ключевых API.
5. Перед опасными изменениями или обновлением сделайте backup.

Админка запускает только заранее разрешённые проверки, которые можно выполнить
из API-контейнера: health-dashboard, public probes, SMTP-login и проверку 3x-ui
через API нод. Операции уровня хоста — `docker compose`, `tailscale`,
PostgreSQL backup/restore — остаются host-only: кнопка возвращает точную
команду для SSH-сессии на сервере. Docker socket намеренно не проброшен в
web-админку.

## Health-dashboard

Раздел `/admin` → `Health` показывает состояние:

- API-контейнера;
- PostgreSQL (`SELECT 1`);
- публичного сайта через `PUBLIC_BASE_URL`;
- `/plans` и `/payment-methods`;
- SMTP-логина для писем web-кабинета;
- каждой активной VPN-ноды через её `api_address` master 3x-ui.

Если 3x-ui открыт только через SSH proxy, адрес в разделе `Инфраструктура`
помечается как внутренний/proxy SSH адрес. Для ноды важен именно `api_address`
из `vpn_node_configs`: health проверяет этот VPN API и обновляет
`health_status`, `latency_ms`, `last_seen_at` у ноды.

## Быстрая проверка почты и переподнятие цепочки

```bash
cd /home/freedman/vpn-service
scripts/check_mail_chain.sh
```

Скрипт делает:

- `docker compose ps api bot worker`;
- `curl http://127.0.0.1:8000/health`;
- вывод SMTP-настроек внутри `api` без пароля;
- проверку SMTP-login без отправки письма;
- добавление `cabinet_login_codes.plain_code`, если колонка ещё не создана;
- быстрый recreate `api` и `bot`;
- вывод последних mail/cabinet/error логов.

Для реального тестового письма явно передайте адрес:

```bash
scripts/check_mail_chain.sh user@example.com
```

Контрольное письмо содержит код `000000`. Не отправляйте его на чужие адреса
без согласия владельца.

## Online-тесты ключевых API

```bash
cd /home/freedman/vpn-service
scripts/check_online_apis.sh
```

Проверяются:

- `docker compose ps api bot worker`;
- локальный `/health`;
- публичный лендинг через `PUBLIC_BASE_URL`;
- `/plans`;
- `/payment-methods`;
- `/admin/overview` с `ADMIN_USERNAME`/`ADMIN_PASSWORD`;
- `tailscale funnel status`.

Для проверки пользовательских endpoints передайте Telegram ID:

```bash
scripts/check_online_apis.sh 106123347
```

Тогда дополнительно проверяются:

- `/users/{telegram_id}`;
- `/users/{telegram_id}/status`.

## Логи

Быстрые команды:

```bash
docker compose logs --tail=200 api bot worker
docker compose logs --since=20m api | grep -i -E 'smtp|email|mail|cabinet|503|error|failed|exception' || true
journalctl -u vpn-tailscale-cert.service -n 80 --no-pager
tailscale funnel status
```

При успешной отправке email-кода API пишет событие
`cabinet_login_code_sent`. При ошибке отправки — `cabinet_login_code_email_failed`.
Пароли в логи не пишутся.

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

`backup.sh` создаёт два вида backup за один запуск:

- custom-format dump БД `vpn` из контейнера `postgres`;
- защищённый архив `.env` и `docker-compose.yml`.

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

Контроль наличия свежих backup-файлов:

```bash
ls -lh /var/backups/vpn-service | tail -20
```

Перед обновлением, ручными правками БД, сменой нод или массовой правкой
пользователей сначала выполните `scripts/backup.sh`, затем
`scripts/verify_backup.sh` для созданного dump.

## Восстановление PostgreSQL

Восстановление выполняется только с SSH-хоста, потому что оно останавливает
сервисы и меняет рабочую БД. Скрипт имеет предохранитель и без явного
подтверждения ничего не делает.

1. Найдите нужный dump:

```bash
ls -lh /var/backups/vpn-service/vpn-db-*.dump
```

2. Проверьте его во временной БД:

```bash
cd /home/freedman/vpn-service
sudo scripts/verify_backup.sh /var/backups/vpn-service/vpn-db-<timestamp>.dump
```

3. Запустите восстановление:

```bash
cd /home/freedman/vpn-service
sudo RESTORE_CONFIRM=I_UNDERSTAND \
  scripts/restore_postgres.sh /var/backups/vpn-service/vpn-db-<timestamp>.dump
```

Скрипт:

- останавливает `api`, `bot`, `worker`;
- запрещает новые подключения к текущей БД `vpn`;
- завершает активные сессии;
- переименовывает старую БД в `vpn_before_restore_<UTC timestamp>`;
- создаёт чистую БД `vpn`;
- восстанавливает dump через `pg_restore`;
- запускает `api`, `bot`, `worker`.

Если после восстановления нужно откатиться, старая БД не удаляется сразу:
она остаётся под именем `vpn_before_restore_<UTC timestamp>`.

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
