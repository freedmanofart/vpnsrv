# VPN Service

Telegram-сервис продажи и выдачи VPN-доступа. Управляющий API делегирует
физическое управление нодами и Xray встроенному REST API панели
`freedmanofart/3x-ui` версии 3.7.

## Архитектура

```text
Telegram -> aiogram bot -> FastAPI -> PostgreSQL
                              |
                              +-> REST API 3x-ui master
                                      |-> локальный Xray
                                      +-> child-панели -> удалённый Xray

```

Основной сервер запускает 3x-ui в роли master. Удалённые VPS регистрируются в
ней как child nodes. Этот репозиторий больше не запускает собственный Xray, не
обращается к Xray gRPC и не разворачивает отдельный node-agent.

| Компонент | Назначение |
|---|---|
| `api` | FastAPI, подписки, платежи, клиенты и web admin |
| `bot` | Telegram-интерфейс на aiogram 3 |
| внешний PostgreSQL | Единый контейнер `postgres`, отдельная БД `vpn` для данных сервиса |
| `worker` | Истечение доступа и reconciliation с 3x-ui master |
| `3x-ui master` | Inbound-конфигурации, физические ноды, Xray и трафик |

## Интеграция с 3x-ui

OpenAPI панели доступен по адресу
`<panel-base>/panel/api/openapi.json`, интерактивная документация — по
`<panel-base>/api-docs`. Для API и worker нужен Bearer token со scope
`node-sync`, созданный в `Settings → Security → API Token`.

```dotenv
THREEXUI_API_TOKEN=одноразово-показанный-токен-master
THREEXUI_VERIFY_TLS=true
```

Каждая логическая VPN-нода в нашей админке связывается с одним inbound панели:

- `api_address` — URL master вместе с закрытым web base path;
- `inbound_tag` — числовой `inboundId` в 3x-ui;
- `host`, `port`, `sni`, `fp`, `pbk`, `sid` — публичные параметры Reality для
  формирования VLESS URI.

Inbound может работать на master или быть назначен child-ноде. Эту топологию
хранит 3x-ui; приложение её не дублирует.

Текущий минимальный стенд публикует в VPN API только одну тестовую локацию:
`node-sw` (`SE|Швеция`). Старые логические ноды хранятся как отключённая
история и не предлагаются пользователю.

Бот выдаёт только один вариант ключа: универсальный VLESS Reality xHTTP без
выбора клиента и flow. При добавлении логической ноды страна автоматически
определяется по публичному IP и используется в меню Telegram.

Жизненный цикл клиента использует `/panel/api/clients/add`,
`/clients/del/{email}` и `/inbounds/list`. Служебный email имеет формат
`vpn-<client_id>`. Компенсационные операции сохранены: если транзакция БД
завершается ошибкой, только что созданный клиент удаляется из 3x-ui.

Подробная настройка master и child описана в
[`docs/3x-ui-master.md`](docs/3x-ui-master.md).
Добавление новой ноды скриптом описано в
[`docs/add-3x-ui-node.md`](docs/add-3x-ui-node.md).
Полный жизненный цикл тарифа, оплаты, выдачи, продления и отзыва описан в
[`docs/vpn-api-lifecycle.md`](docs/vpn-api-lifecycle.md).

Возможности и ограничения интеграции web admin с API 3x-ui:
[`docs/admin-3xui-api.md`](docs/admin-3xui-api.md).
Устройство, изменение и безопасный деплой Telegram-бота описаны в
[`docs/editing-telegram-bot.md`](docs/editing-telegram-bot.md).

## API

После запуска админка доступна по адресу `http://localhost:8000` (корень
перенаправляет на `/admin`), OpenAPI — `http://localhost:8000/docs`.
Вход выполняется через HTTP Basic; учётные данные задают переменные
Публичный лендинг доступен на `/`, резервный кабинет — на `/cabinet`, а
административная панель — на `/admin`. Настройка сайта, SMTP и токенизированного
входа описана в [docs/web-cabinet.md](docs/web-cabinet.md).

`ADMIN_USERNAME` и `ADMIN_PASSWORD` в `.env`. Подробности — в
[`docs/access-and-credentials.md`](docs/access-and-credentials.md).

| Метод | Маршрут | Назначение |
|---|---|---|
| `GET` | `/health` | Состояние API |
| `GET` | `/db-health` | Проверка PostgreSQL |
| `POST` | `/users` | Регистрация Telegram-пользователя |
| `GET`, `POST` | `/plans` | Тарифы |
| `POST` | `/subscriptions` | Создание подписки и клиента 3x-ui |
| `POST` | `/subscriptions/{id}/renew` | Продление с новым UUID |
| `POST` | `/subscriptions/{id}/rotate` | Перевыпуск или перенос клиента |
| `GET`, `POST` | `/vpn/nodes` | Логические VPN-локации |
| `GET` | `/vpn/nodes/{id}/health` | Проверка master и inbound |
| `POST` | `/vpn/nodes/{id}/reconcile` | Сверка БД с inbound панели |
| `GET`, `POST` | `/vpn/nodes/{id}/configs` | Привязка ноды к inbound |
| `POST` | `/payments` | Идемпотентное создание платежа |
| `POST` | `/payments/webhooks/{provider}` | Подписанное событие провайдера |
| `GET` | `/`, `/admin` | Web admin |
| `POST` | `/admin/users/{id}/rotate` | Перевыпуск ключа из админки |

Все прикладные маршруты, кроме `/`, `/health`, документации и платежного
webhook, требуют service Bearer token или HTTP Basic администратора.

## Локальный запуск

```bash
cp .env.example .env
python3 scripts/configctl.py validate

docker compose run --rm api alembic upgrade head
docker compose up -d
```

Compose больше не содержит Xray. Для реальной выдачи ключей заранее нужны
доступный 3x-ui master, API token и хотя бы один VLESS Reality inbound.

## Тесты

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r api/requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

HTTP-вызовы 3x-ui в тестах подменяются. Lifecycle-тесты используют совместимую
in-memory реализацию и не изменяют развёрнутую панель.

## Безопасность

- Не коммитьте `.env` и API token 3x-ui.
- Используйте scope `node-sync`, а не `admin`.
- Не отключайте TLS verification вне закрытого тестового стенда.
- Не публикуйте закрытый base path панели.
- Полный VLESS URI является секретом.
- Перед перезапуском API и worker применяйте миграции Alembic.

Операционные инструкции: [`docs/maintenance-scripts.md`](docs/maintenance-scripts.md),
доступы: [`docs/access-and-credentials.md`](docs/access-and-credentials.md),
бизнес-логика: [`docs/vpn-api-lifecycle.md`](docs/vpn-api-lifecycle.md).
Документация разработчика бота:
[`docs/editing-telegram-bot.md`](docs/editing-telegram-bot.md).
