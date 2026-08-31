# Доступы, служебные страницы и переменные

Фактические пароли, токены, закрытые base path и Reality private keys в Git не
хранятся.

## Где выполняются команды

- **Основной сервер** — Docker Compose control plane и 3x-ui master.
- **Child VPS** — удалённая установка 3x-ui и управляемый ею Xray.
- **Операторский компьютер** — SSH-туннели и браузер.

## Служебные страницы control plane

| Страница | Адрес через туннель | Доступ |
|---|---|---|
| VPN Admin | `http://localhost:8000/admin` | HTTP Basic |
| Swagger | `http://localhost:8000/docs` | HTTP Basic или service token |
| API health | `http://localhost:8000/health` | без авторизации |
| DB health | `http://localhost:8000/db-health` | авторизация |
| Grafana | `http://localhost:3000` | учётная запись Grafana |

```bash
ssh -N -L 8000:127.0.0.1:8000 codex@192.168.10.60
ssh -N -L 3000:127.0.0.1:3000 codex@192.168.10.60
```

Child 3x-ui на тестовом VPS:

```bash
ssh -N -L 2223:127.0.0.1:60628 root@159.223.22.59
```

После этого панель доступна на `http://127.0.0.1:2223`.

## Основные переменные

| Группа | Переменные | Назначение |
|---|---|---|
| Админка | `ADMIN_USERNAME`, `ADMIN_PASSWORD` | VPN Admin и Swagger |
| Внутренний API | `SERVICE_API_TOKEN`, `API_URL` | bot и внутренние вызовы |
| 3x-ui | `THREEXUI_API_TOKEN`, `THREEXUI_VERIFY_TLS` | доступ API/worker к master |
| Telegram | `BOT_TOKEN`, `TELEGRAM_CHANNEL_URL`, `SUPPORT_URL` | бот и ссылки |
| Оплата | `PAYMENT_PROVIDER`, `PAYMENT_WEBHOOK_SECRET`, `YOOMONEY_*` | платежи |
| PostgreSQL | `DATABASE_URL`, `POSTGRES_*` | прикладная БД |
| Grafana | `GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD` | вход в Grafana |

Переменные `XRAY_API_ADDRESS`, `XRAY_INBOUND_TAG`, `XRAY_MANAGEMENT_MODE`,
`NODE_AGENT_TOKEN`, `NODE_AGENT_NODE_ID` и `CONTROL_PLANE_URL` удалены.

## API-токены 3x-ui

Используются два разных назначения:

1. На child создаётся `node-sync` token, который хранится только в настройках
   Nodes master-панели.
2. На master создаётся отдельный `node-sync` token для VPN API. Он хранится
   только в `.env` как `THREEXUI_API_TOKEN`.

Не используйте один токен одновременно для обеих связей. Admin scope приложению
не требуется.

## Просмотр и изменение

```bash
cd /home/freedman/vpn-service
python3 scripts/configctl.py get ADMIN_USERNAME
python3 scripts/configctl.py get THREEXUI_API_TOKEN
python3 scripts/configctl.py get THREEXUI_API_TOKEN --show-secret
```

Обычный `get` маскирует секреты. `--show-secret` используйте только в защищённой
SSH-сессии. Проверка прав:

```bash
stat -c '%a %U:%G %n' .env
```

Ожидаются права `0600`.

После изменения токена master:

```bash
python3 scripts/configctl.py set THREEXUI_API_TOKEN '<new-token>'
python3 scripts/configctl.py apply --services api worker
```

Сначала выполните Health в VPN Admin, затем отключите прежний токен в 3x-ui.

Подробная настройка топологии: [`3x-ui-master.md`](3x-ui-master.md).
