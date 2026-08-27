# Доступы, служебные страницы и переменные

Документ описывает, где находятся настройки и как безопасно проверить доступы.
Фактические секреты (пароли, токены, Telegram BOT_TOKEN и private keys) здесь не
хранятся и в Git не коммитятся.

## Где выполняются команды

* **Backend/control plane** — сервер с Git checkout, `.env`, Docker Compose,
  PostgreSQL, API, bot, worker и web admin.
* **VPN-нода** — удалённый сервер с Xray/node-agent или standalone-тестом.
  Полный Git-репозиторий на ноде не нужен.
* **Операторский компьютер** — SSH-туннели, импорт клиентских конфигураций и
  просмотр служебных страниц.

## Служебные страницы

После запуска Compose и SSH-туннеля:

| Страница | Адрес | Доступ |
|---|---|---|
| Web admin | `http://localhost:8000/admin` | HTTP Basic: `ADMIN_USERNAME` / `ADMIN_PASSWORD` |
| Swagger | `http://localhost:8000/docs` | HTTP Basic или Bearer service token |
| API health | `http://localhost:8000/health` | без авторизации |
| DB health | `http://localhost:8000/db-health` | авторизация |
| Grafana | `http://localhost:3000` | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` |

Для доступа с Mac используйте туннели:

```bash
ssh -N -L 8000:127.0.0.1:8000 codex@192.168.10.60
ssh -N -L 3000:127.0.0.1:3000 codex@192.168.10.60
```

## Основные переменные

| Группа | Переменные | Назначение |
|---|---|---|
| Админка | `ADMIN_USERNAME`, `ADMIN_PASSWORD` | HTTP Basic web admin/Swagger |
| API | `SERVICE_API_TOKEN`, `API_URL` | внутренние запросы bot/worker |
| Telegram | `BOT_TOKEN`, `TELEGRAM_CHANNEL_URL`, `SUPPORT_URL` | бот и ссылки |
| Оплата | `YOOMONEY_PAYMENT_URL`, `YOOMONEY_*_URL`, `PAYMENT_WEBHOOK_SECRET` | ЮMoney, QR и webhook |
| Xray | `XRAY_MANAGEMENT_MODE`, `XRAY_API_ADDRESS`, `XRAY_INBOUND_TAG` | режим управления |
| Ноды | `CONTROL_PLANE_URL`, `NODE_AGENT_NODE_ID`, `NODE_AGENT_TOKEN` | node-agent |
| Reality | `REALITY_SNI`, `REALITY_FINGERPRINT` | SNI/fingerprint новых нод |
| Grafana | `GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD` | вход в Grafana |

Полный список значений и безопасные defaults находится в `.env.example`.

## Как смотреть значения

На backend-сервере от root:

```bash
cd /home/freedman/vpn-service
python3 scripts/configctl.py get ADMIN_USERNAME
python3 scripts/configctl.py get ADMIN_PASSWORD --show-secret
```

Обычный `get` маскирует секреты. Флаг `--show-secret` используйте только в
защищённой SSH-сессии; пароль не копируйте в issue, чат или логи. Проверить
наличие без раскрытия:

```bash
sed -n -e 's/^ADMIN_USERNAME=.*/ADMIN_USERNAME=<set>/p' \
       -e 's/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=<set>/p' .env
stat -c '%a %U:%G %n' .env       # ожидается 600 root:root
```

Для просмотра другой переменной замените имя в команде `configctl.py get`.
После временного `export` выполните `unset VARIABLE`.

## Изменение и применение

```bash
python3 scripts/configctl.py set ADMIN_USERNAME admin
python3 scripts/configctl.py generate ADMIN_PASSWORD
python3 scripts/configctl.py apply --services api bot worker
```

После изменения оплаты пересоздайте `bot`; после изменения Grafana передайте
`--services grafana`. Не меняйте `.env` через `cat >` и не включайте `set -x`.

## Проверка node-agent

На VPN-ноде (read-only):

```bash
/root/check_node_agent.sh
```

Скрипт проверяет health endpoint и контейнер `vpn-node-agent`, но не печатает
токен и не изменяет сетевые правила.
