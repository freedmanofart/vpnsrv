# Доступы и переменные

Секреты хранятся только в `/home/freedman/vpn-service/.env` с правами `0600`.
Не помещайте в Git токены, пароли, закрытые base path, полный VLESS URI и
Reality private key.

## VPN Admin

API опубликован только на loopback основного сервера:

| Адрес | Назначение | Доступ |
|---|---|---|
| `http://127.0.0.1:8000/` | redirect в VPN Admin | HTTP Basic |
| `http://127.0.0.1:8000/admin` | админка | HTTP Basic |
| `http://127.0.0.1:8000/docs` | OpenAPI | HTTP Basic/service token |
| `http://127.0.0.1:8000/health` | API health | без авторизации |
| `http://127.0.0.1:8000/db-health` | PostgreSQL health | авторизация |

Для доступа с операторской машины откройте SSH-туннель:

```bash
ssh -N -L 8000:127.0.0.1:8000 codex@<master-host>
```

Затем откройте `http://localhost:8000/admin`.

```dotenv
ADMIN_USERNAME=<admin-login>
ADMIN_PASSWORD=<long-random-password>
```

## PostgreSQL

Используется один внешний контейнер `postgres` (`postgres:16-alpine`) и
отдельная БД `vpn`. Compose приложения PostgreSQL не запускает.

```dotenv
POSTGRES_CONTAINER=postgres
VPN_DATABASE_NAME=vpn
DATABASE_URL=postgresql+asyncpg://<user>:<password>@host.docker.internal:6432/vpn
```

Не меняйте БД `mydb`: она принадлежит другому приложению в том же экземпляре.

## Остальные переменные

| Группа | Переменные |
|---|---|
| 3x-ui | `THREEXUI_API_TOKEN`, `THREEXUI_VERIFY_TLS` |
| Telegram | `BOT_TOKEN`, `TELEGRAM_CHANNEL_URL`, `SUPPORT_URL`, `BOT_PLAN_CODES` |
| API | `API_URL`, `SERVICE_API_TOKEN` |
| Платежи | `PAYMENT_PROVIDER`, `PAYMENT_WEBHOOK_SECRET`, `YOOMONEY_*` |
| Worker | `LIFECYCLE_INTERVAL_SECONDS`, `LIFECYCLE_ADVISORY_LOCK_KEY` |
| Admin | `ADMIN_USERNAME`, `ADMIN_PASSWORD` |

Redis, Grafana, Loki, Alloy, собственный Xray и node-agent удалены. Их
переменные больше не используются.

## Безопасное изменение

```bash
cd /home/freedman/vpn-service
python3 scripts/configctl.py validate
python3 scripts/configctl.py get ADMIN_USERNAME
python3 scripts/configctl.py get ADMIN_PASSWORD
python3 scripts/configctl.py get ADMIN_PASSWORD --show-secret
python3 scripts/configctl.py set ADMIN_PASSWORD '<new-password>'
python3 scripts/configctl.py apply --services api
```

Обычный `get` маскирует секреты. `--show-secret` используйте только в
защищённой SSH-сессии.

Проверка:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" http://127.0.0.1:8000/admin
curl -fsS -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" \
  http://127.0.0.1:8000/db-health
```
