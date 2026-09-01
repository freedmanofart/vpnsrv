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
`<panel-base>/api-docs`. Для API и worker нужен отдельный Bearer token,
созданный в `Settings → Security → API Token`. Если установленная сборка
поддерживает scopes, выдайте минимально достаточный `node-sync`. Если scopes в
ней нет, считайте API token полноадминистративным и дополнительно ограничьте
доступ к панели сетью и TLS.

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

После запуска публичный лендинг доступен по адресу `http://localhost:8000/`,
резервный кабинет — на `/cabinet`, административная панель — на `/admin`, а
OpenAPI — на `/docs`. В текущей реализации `/docs`, `/redoc` и
`/openapi.json` публичны; закрывайте их на reverse proxy либо отключайте в
FastAPI, если схема API не должна публиковаться. Вход в административную панель
выполняется через HTTP Basic; учётные данные задают переменные
`ADMIN_USERNAME` и `ADMIN_PASSWORD`.
Настройка сайта, SMTP и токенизированного входа описана в
[docs/web-cabinet.md](docs/web-cabinet.md).
Полный отчёт по последнему обновлению интерфейса, парольного входа, Telegram,
миграций и деплоя: [docs/latest-changes-2026-09-01.md](docs/latest-changes-2026-09-01.md).

`ADMIN_USERNAME` и `ADMIN_PASSWORD` в `.env`. Подробности — в
[`docs/access-and-credentials.md`](docs/access-and-credentials.md).

| Метод | Маршрут | Назначение |
|---|---|---|
| `GET` | `/health` | Состояние API |
| `GET` | `/db-health` | Проверка PostgreSQL |
| `POST` | `/users` | Регистрация Telegram-пользователя ботом; нужен service Bearer или admin Basic |
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
| `POST` | `/payments/manual` | Создание ручного платежа |
| `GET` | `/payment-methods` | Доступные способы оплаты |
| `POST` | `/v1/client/activation-codes` | Код активации клиентского устройства |
| `GET` | `/` | Публичный лендинг |
| `GET` | `/cabinet` | Резервный кабинет по защищённой cookie |
| `POST` | `/web/password/login` | Вход в кабинет по email и паролю |
| `POST` | `/web/password` | Установка пароля с действующей cookie |
| `GET` | `/admin` | Web admin с HTTP Basic |
| `POST` | `/admin/users/{id}/rotate` | Перевыпуск ключа из админки |

Служебные маршруты требуют service Bearer token или HTTP Basic администратора.
Публичными остаются лендинг, health, документация FastAPI, регистрация и вход в
web-кабинет, активация устройства по одноразовому коду, а также подписанный
webhook. Telegram-регистрация `/users` не публична. Маршруты
`/v1/client/profile` и `/refresh` используют отдельный токен активированного
устройства.

Повторный подтверждённый платёж не создаёт вторую активную подписку: он
продлевает существующую, меняет тариф и выбранную страну согласно заказу,
перевыпускает ключ и связывается с той же подпиской. Поэтому одна подписка может
иметь несколько исторических платежей.

## Недавние изменения

- добавлены публичный лендинг и резервный web-кабинет с magic-link по email;
- Telegram-кнопки сайта и кабинета разделены через `WEB_SITE_URL` и
  `WEB_CABINET_URL`;
- из бота можно доверенно привязать email к существующему Telegram-пользователю;
- web-кабинет поддерживает покупку и продление, выбор страны, загрузку чека и
  просмотр состояния платежей;
- повторный оплаченный заказ автоматически продлевает активную подписку и
  заменяет её VPN-клиента;
- live-трафик читается из разрешённого `clientStats` ответа 3x-ui;
- добавлены QR-изображения способов оплаты и восстановление старых Telegram
  `file_id` чеков скриптом `bot/app/backfill_receipt.py`;
- в каталоге `3x-ui/` зафиксирована справочная документация панели 3.7,
  соответствующая интеграции проекта.

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
- Если установленная сборка 3x-ui поддерживает scopes, используйте
  `node-sync`; иначе обращайтесь с API token как с полным административным
  секретом.
- Не отключайте TLS verification вне закрытого тестового стенда.
- Не публикуйте закрытый base path панели.
- Полный VLESS URI является секретом.
- Перед перезапуском API и worker применяйте миграции Alembic.

Операционные инструкции: [`docs/maintenance-scripts.md`](docs/maintenance-scripts.md),
доступы: [`docs/access-and-credentials.md`](docs/access-and-credentials.md),
бизнес-логика: [`docs/vpn-api-lifecycle.md`](docs/vpn-api-lifecycle.md),
план устранения неисправностей:
[`docs/remediation-plan.md`](docs/remediation-plan.md).
Документация разработчика бота:
[`docs/editing-telegram-bot.md`](docs/editing-telegram-bot.md).
