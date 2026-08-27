# VPN Service

MVP сервиса выдачи VPN-доступа через Telegram. Управляющий API хранит пользователей и подписки в PostgreSQL, динамически добавляет VLESS-клиентов в Xray через gRPC и отзывает их после окончания срока действия.

> Статус на 25 августа 2026 года: реализованы Telegram purchase-flow, web admin, авторизация API, payment state machine, `grpc.aio` Xray, отдельный lifecycle worker, reconciliation, outbound node-agent, device activation/profile API, PostgreSQL audit и локальные Grafana/Loki/Alloy. Подключена первая VPN-нода DigitalOcean; реальный платёжный провайдер ещё не подключён.

## Архитектура

```text
Telegram user ---> aiogram bot ---> FastAPI ---> PostgreSQL
                                      ^   |
                                      |   +-- lifecycle worker + advisory lock
                                      |
future VPN node-agent ----------------+-- desired state/status over HTTPS
         |
         +-- local Xray gRPC --> public VLESS Reality :443

Docker logs ---> Grafana Alloy ---> Loki ---> Grafana (SSH tunnel)

Redis входит в Compose, но прикладной код его пока не использует.
```

Состав проекта:

| Компонент | Назначение |
|---|---|
| `bot` | Telegram-интерфейс на aiogram 3 |
| `api` | FastAPI, бизнес-логика подписок и управление Xray |
| `postgres` | Пользователи, тарифы, подписки, платежи, VPN-ноды и клиенты |
| `redis` | Резерв под кэш, блокировки или фоновые задания; пока не используется |
| `xray` | VLESS Reality и локальный gRPC API управления пользователями |
| `worker` | Истечение подписок и reconciliation; один активный цикл через PostgreSQL advisory lock |
| `node-agent` | Исходящее получение desired state и локальное применение его к Xray |
| `alloy` / `loki` / `grafana` | Сбор, хранение и просмотр подробных логов без alerting |
| `alembic` | Миграции PostgreSQL |

## Основной поток

1. `/start` регистрирует Telegram-пользователя через API.
2. Бот получает список активных тарифов.
3. Пользователь выбирает ОС, получает официальную ссылку AmneziaVPN, затем выбирает страну и VLESS-профиль.
4. При подтверждении бот идемпотентно создаёт `Payment`.
5. Подтверждённый платёж атомарно создаёт подписку и `VPNClient`; при ошибке БД добавленный Xray-пользователь компенсирующе удаляется.
6. API добавляет UUID и выбранный `flow` в Xray через асинхронный `grpc.aio` `HandlerService.AlterInbound`.
7. Пользователю выдаётся URI `vless://...` и инструкция для выбранного клиента.
8. Отдельный worker раз в 60 секунд под PostgreSQL advisory lock проверяет сроки и сверяет desired state PostgreSQL с Xray.
9. Истёкший клиент удаляется из Xray и переводится в `revoked`; отсутствующие после рестарта Xray активные пользователи восстанавливаются.

`XRAY_MANAGEMENT_MODE=direct` используется только домашним стендом. В production задаётся `agent`: worker меняет desired state в БД, а каждая нода сама забирает его исходящим HTTPS-запросом. Tailscale в production-схему не входит.

Поддерживаемые варианты клиента:

- AmneziaVPN (импорт стандартного VLESS URI, без WireGuard/AmneziaWG);
- универсальный VLESS для v2rayNG, v2rayN и совместимых клиентов.

Профили:

- Reality: без `flow`, максимальная совместимость;
- Reality + XTLS Vision: `flow=xtls-rprx-vision` сохраняется в БД, передаётся в аккаунт Xray и добавляется в URI.

Главное меню бота содержит оплату, выбор приложения для устройства, личный кабинет, промокод, тест за 50 ₽, инструкции, поддержку и канал. Покупка проходит в порядке: ОС → страна → Reality/Vision → тариф → ЮMoney/QR → ключ. Тексты, ссылки приложений и дополнительные URL-кнопки редактируются в `bot/app/content.json`; подробная инструкция находится в [`docs/telegram-bot-configuration.md`](docs/telegram-bot-configuration.md). Промокоды задаются без изменения кода, например `PROMO_CODES=WELCOME7:7,SUMMER30:30`.

Для активной подписки раздел «Мой VPN» дополнительно позволяет:

- повторно получить действующий ключ и QR-код;
- перевыпустить UUID без продления срока подписки;
- сменить страну/ноду;
- сменить клиентский формат и профиль Reality/Vision.

При ротации API сначала добавляет новый клиент в целевой Xray и только затем отзывает старый. Если отзыв старого ключа завершается ошибкой, API компенсирующе удаляет новый ключ и откатывает транзакцию БД.

Страны не привязаны к фиксированным ID: бот показывает активные ноды, у которых `region` равен `us`, `nl` или `de` (также поддерживаются английские и русские названия).

Продление создаёт новый UUID, добавляет его в Xray и отзывает предыдущий клиент. При ошибке удаления старого клиента предусмотрена компенсирующая попытка удалить новый.

## API

После локального запуска OpenAPI доступен по адресу `http://localhost:8000/docs`.

Основные маршруты:

| Метод | Маршрут | Назначение |
|---|---|---|
| `GET` | `/health` | Состояние API |
| `GET` | `/db-health` | Проверка PostgreSQL |
| `POST` | `/users` | Создать пользователя |
| `GET` | `/users/{telegram_id}` | Получить пользователя |
| `GET` | `/users/{telegram_id}/vpn-status` | Подписка и активный клиент |
| `GET`, `POST` | `/plans` | Активные тарифы и создание тарифа |
| `POST` | `/subscriptions` | Создать подписку и клиента в Xray |
| `POST` | `/subscriptions/{id}/renew` | Продлить подписку с новым UUID |
| `POST` | `/subscriptions/{id}/rotate` | Перевыпустить/перенести ключ без продления |
| `GET`, `POST` | `/vpn/nodes` | VPN-ноды |
| `PATCH` | `/vpn/nodes/{id}` | Изменить регион, состояние и ёмкость ноды |
| `GET` | `/vpn/nodes/{id}/health` | Проверить gRPC Xray и число пользователей |
| `POST` | `/vpn/nodes/{id}/reconcile` | Восстановить отсутствующих и удалить осиротевших динамических Xray-пользователей |
| `GET`, `POST` | `/vpn/nodes/{id}/configs` | Конфигурация протокола ноды |
| `POST` | `/vpn/clients` | Отдельное создание клиента |
| `GET`, `DELETE` | `/vpn/clients/{id}` | Получение или отзыв клиента |
| `GET` | `/vpn/clients/{id}/config` | Готовый VLESS URI |
| `POST` | `/payments` | Идемпотентно создать платёж |
| `GET` | `/payments/{id}` | Получить состояние платежа |
| `POST` | `/payments/webhooks/{provider}` | Принять подписанное идемпотентное событие провайдера |
| `POST` | `/agent/v1/credentials/{node_id}/rotate` | Выпустить scoped token ноды; значение показывается один раз |
| `GET` | `/agent/v1/state` | Desired state только для ноды из Bearer token |
| `POST` | `/agent/v1/status` | Статус, latency и результат reconciliation от node-agent |
| `POST` | `/v1/client/activation-codes` | Создать одноразовый код привязки устройства |
| `POST` | `/v1/client/activate` | Обменять код на хешируемый device token |
| `GET` | `/v1/client/profile` | Получить доступные конфигурации активной подписки |
| `POST` | `/v1/client/refresh` | Ротировать device token и немедленно отозвать старый |
| `POST`, `DELETE` | `/admin/debug-sessions` | Открыть или закрыть ограниченный sensitive-debug |

Все прикладные маршруты, кроме `/health`, `/`, документации и платёжного webhook, требуют одну из двух схем:

- `Authorization: Bearer $SERVICE_API_TOKEN` — Telegram-бот и внутренние сервисы;
- HTTP Basic с `ADMIN_USERNAME` / `ADMIN_PASSWORD` — web admin и ручная работа через Swagger.

`/db-health` также защищён. Swagger сохранён, но вызовы из него требуют авторизации. Webhook проверяет HMAC-SHA256 сырого тела с `PAYMENT_WEBHOOK_SECRET` в заголовке `X-Payment-Signature`; уникальный ID события передаётся в `X-Payment-Event-Id`.

Состояния платежа: `pending → processing → paid|failed|cancelled|expired`; из `paid` разрешён только `refunded`. Refund отзывает активные Xray-клиенты и переводит подписку в `cancelled`. Повтор одного `idempotency_key` или `(provider, event_id)` возвращает уже созданный результат и не выдаёт второй VPN-клиент.

`POST /subscriptions` обратно совместим со старым телом из `user_id` и `plan_id`, но также принимает:

```json
{
  "user_id": 1,
  "plan_id": 1,
  "node_id": 2,
  "client_type": "amnezia",
  "flow": "xtls-rprx-vision",
  "fingerprint": "chrome"
}
```

## Веб-админка

Адрес: `http://localhost:8000/admin`. Доступ защищён HTTP Basic переменными `ADMIN_USERNAME` и `ADMIN_PASSWORD`.

На домашнем сервере порт опубликован только на loopback. С управляющего Mac открыть туннель и оставить процесс запущенным:

```bash
ssh -N -L 8000:127.0.0.1:8000 \
  -i /Users/freedman/.ssh/vpnsrv_codex \
  codex@192.168.10.60
```

После этого admin доступен на `http://localhost:8000/admin`, Swagger — на `http://localhost:8000/docs`. Host firewall для этого изменения не включался и не изменялся.

Grafana также доступна только через SSH tunnel:

```bash
ssh -N -L 3000:127.0.0.1:3000 \
  -i /Users/freedman/.ssh/vpnsrv_codex \
  codex@192.168.10.60
```

Открыть `http://localhost:3000`; логин берётся из `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`. Alerting отключён. Dashboard `VPN / VPN logs` показывает ошибки, lifecycle/reconciliation и общий поток API/bot/worker/node-agent.

Первый вариант интерфейса позволяет:

- просматривать пользователей, тарифы, ноды, подписки и VPN-клиентов;
- создавать тарифы;
- создавать ноды для США, Нидерландов и Германии;
- добавлять VLESS Reality-конфигурацию ноды;
- продлевать подписки;
- отзывать активных VPN-клиентов.
- редактировать тарифы и включать/выключать их;
- менять регион, ёмкость и состояние VPN-ноды;
- проверять доступность Xray и число пользователей.
- просматривать платежи, устройства, node health/latency и audit log;
- отзывать device token и VPN-клиентов;
- запускать reconciliation;
- открывать и закрывать ограниченные sensitive-debug сессии.
- для каждого пользователя вручную назначать или менять тариф, VPN-ноду, срок и состояние доступа;
- просматривать и редактировать выданную VPN-ссылку; пустое поле возвращает автоматическую генерацию URI;
- видеть последнее подключение и исходный IP, зафиксированные Xray на выбранной ноде;
- одной командой «Сбросить план и ссылку» завершать подписку, отзывать клиент и очищать ручную ссылку.

Административная страница использует существующие защищённые REST-методы и агрегирующий read-only endpoint `GET /admin/overview`. Перед запуском обязательно заменить пароль `change_me`.

Ручное управление доступом выполняют `PUT /admin/users/{user_id}/access` и `DELETE /admin/users/{user_id}/access`. Отключение сохраняет данные подписки и историю последнего подключения, но удаляет клиента из desired state Xray. Сброс переводит подписки в истёкшие, отзывает клиенты и очищает ручной URI; время и IP последнего подключения сохраняются для диагностики. Node-agent читает локальный Xray access log и передаёт только последнюю активность каждого клиента. Это не список текущих сессий: «активен» в таблице означает действующий выданный доступ.

Полный VPN URI является секретом. Он показывается только после HTTP Basic-аутентификации администратора и не выводится целиком в общей таблице.

Sensitive-debug включается только из админки с причиной и сроком. Полный снимок Telegram token, паролей, Authorization headers, Reality private key и активных клиентских URI создаётся явной командой:

```bash
sudo /home/freedman/vpn-service/scripts/capture_sensitive_debug.py DEBUG_SESSION_ID \
  --project /home/freedman/vpn-service
```

Снимок попадает в PostgreSQL audit и Loki как `event_type=sensitive_debug_snapshot`; команда выводит только количества, не значения. После диагностики сессию нужно закрыть в админке. Это намеренно небезопасный режим: любой оператор с доступом к Grafana/audit увидит секреты, поэтому затронутые значения при необходимости ротируются.

## Модель данных

- `users` — Telegram-пользователи;
- `plans` — тарифы, длительность и цена;
- `payments` — состояние, идемпотентность, провайдер, выбранный профиль и связь с подпиской;
- `payment_events` — уникальные события webhook и исходный payload;
- `subscriptions` — периоды доступа;
- `vpn_nodes` — сведения о VPN-серверах;
- `vpn_node_configs` — JSON-конфигурация VLESS/Reality;
- `vpn_clients` — UUID, нода, срок и состояние подключения.
- `audit_logs` и `debug_sessions` — действия оператора, запросы и диагностические сессии;
- `node_agent_credentials` — хешированные scoped tokens нод;
- `client_devices` и `activation_codes` — привязанные установки будущего клиента.
- `access_grants` — одноразовые trial/promo выдачи; служебные планы имеют `is_public=false` и не показываются в продаже.

Цепочка миграций: `45a8774c25bc` → `9d1db660812a` → `b6c1e7a4d920` → `c3f8a91e74bd` → `d8e26f19a4c1` → `e4c729a6b132` → `f7a2c21e9b44` → `a13f6c92d8e1`.

## Конфигурация и локальный запуск

```bash
cp .env.example .env
# Заполнить BOT_TOKEN, ADMIN_PASSWORD, SERVICE_API_TOKEN,
# PAYMENT_WEBHOOK_SECRET, ссылки Telegram и параметры Xray.

python3 scripts/configctl.py validate
python3 scripts/configctl.py list
python3 scripts/configctl.py set PROMO_CODES 'WELCOME7:7,SUMMER30:30'
python3 scripts/configctl.py generate GRAFANA_ADMIN_PASSWORD

docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose up -d
```

`configctl` атомарно сохраняет `.env` с правами `0600`, маскирует секреты в обычном выводе и умеет валидировать, менять, генерировать и применять конфигурацию. После изменения рабочих параметров: `python3 scripts/configctl.py apply`; для Grafana или node-agent эти сервисы нужно явно передать через `--services`. Обязательные переменные находятся в `.env.example`. Секреты нельзя коммитить в Git. `PAYMENT_PROVIDER=mock` и `PAYMENT_AUTO_CONFIRM=true` предназначены только для тестового полного цикла; перед подключением реальной оплаты автоподтверждение нужно выключить.

Подробный справочник по обслуживающим скриптам — с предусловиями, побочными
эффектами, примерами, восстановлением и диагностикой — находится в
[`docs/maintenance-scripts.md`](docs/maintenance-scripts.md).
Отдельный справочник переменных, credentials, ролей доступа и служебных страниц:
[`docs/access-and-credentials.md`](docs/access-and-credentials.md).
В том же справочнике описаны container-only AmneziaWG, повторное копирование
runner с backend-сервера, проверка node-agent (`scripts/check_node_agent.sh`) и
настройка Reality SNI/fingerprint для production-нод.
Пошаговая production-процедура получения новой версии, применения миграций,
проверки и восстановления после грязного рабочего дерева описана в
[`docs/server-update.md`](docs/server-update.md).

Операционные режимы Xray:

- `XRAY_MANAGEMENT_MODE=direct` — домашний стенд, worker обращается к локальному Xray;
- `XRAY_MANAGEMENT_MODE=agent` — production control plane, БД хранит desired state, ноды применяют его сами;
- node token ротируется `POST /agent/v1/credentials/{node_id}/rotate`, хранится только как hash, открытое значение возвращается один раз.

Uvicorn запускается без `--reload`. Для production ещё нужно закрепить версию образа Xray и добавить healthcheck для API, бота и Xray.

Локальные тесты:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r api/requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

## Домашний тестовый сервер

Первичный аудит выполнен 24 августа 2026 года; перечисленные ниже прикладные и сетевые изменения затем применены на стенде.

### Система

- Fedora Linux 44 Server, kernel `7.1.9-200.fc44.x86_64`;
- мини-ПК AZW MINI S;
- 7.5 GiB RAM, около 1.2 GiB использовано во время проверки;
- swap 7.5 GiB, не использовался;
- корневой XFS-раздел 15 GiB: занято 5.6 GiB (37%);
- load average около `1.08`;
- Docker Engine `29.7.2`, Docker Compose `5.4.0`;
- SELinux отключён;
- firewalld не активен;
- статический hostname не задан (`localhost`);
- имеются обновления, в частности для `containerd`.

Проект развёрнут в `/home/freedman/vpn-service`, но каталог и файлы принадлежат `root:root`. `.env` имеет корректные права `0600`; `docker-compose.yml` и `xray/config.json` — `0644`.

### Сеть

- Ethernet: `192.168.10.60/24`;
- Wi-Fi: `192.168.10.19/24`;
- два default route через один шлюз; Ethernet имеет больший приоритет;
- Tailscale: `100.102.21.123`;
- Xray gRPC: `172.18.0.1:10085`;
- PostgreSQL: только `127.0.0.1:5432`;
- Redis: только `127.0.0.1:6379`;
- FastAPI: только `127.0.0.1:8000`;
- SSH: `0.0.0.0:22` и `[::]:22`;
- Cockpit удалён, `9090/tcp` не слушается.

Tailscale Funnel настроен следующим образом:

```text
fedora.taile485ac.ts.net:443
        -> TCP/TLS Funnel
        -> 127.0.0.1:8443
        -> Xray VLESS Reality
```

Таким образом, локальный bind Xray на `127.0.0.1:8443` является намеренным: внешний VPN-трафик доставляет Tailscale Funnel. Публичный адрес клиента должен соответствовать Funnel endpoint и порту `443`, а не локальному `8443`.

### Состояние сервисов

Во время аудита работали все контейнеры:

- `vpn-postgres` — healthy;
- `vpn-redis` — healthy;
- `vpn-api` — работает, `/health`, `/db-health` и `/docs` отвечают успешно;
- `vpn-bot` — работает;
- `vpn-xray` — работает в host network.

Миграция БД находится на `c3f8a91e74bd (head)`. Xray версии `26.3.27`; встроенная проверка конфигурации завершилась с `Configuration OK`. В API-журнале фоновая проверка истечения и reconciliation выполняются каждую минуту без ошибок.

Xray предупреждает, что Reality inbound фактически слушает нестандартный локальный порт `8443`; с учётом Funnel внешний порт остаётся стандартным `443`.

### Развёртывание нового Telegram-flow и админки

24 августа 2026 года изменения развёрнуты на домашнем стенде:

- применена миграция `9d1db660812a (head)`;
- пересобраны и перезапущены только `vpn-api` и `vpn-bot`;
- PostgreSQL, Redis и Xray не перезапускались;
- `/health` и `/db-health` отвечают успешно;
- `/admin` возвращает `401` без учётных данных и `200` с HTTP Basic;
- `/admin/overview` успешно читает существующие данные;
- существующий VPN-клиент продолжает получать корректный VLESS Reality URI;
- бот успешно запустил Telegram polling;
- ошибок API, бота и expiration loop после развёртывания не обнаружено.

Резервная копия заменённых серверных файлов находится в `/root/vpn-service-backup-20260824-1355`.

Для end-to-end проверки домашней ноде `test-node-01` временно назначен `region=nl`, поэтому в Telegram она отображается как Нидерланды. Это только тестовая метка интерфейса: физическое расположение домашнего сервера не изменилось. После подключения настоящей нидерландской ноды тестовую запись нужно вернуть в отдельный тестовый регион или отключить.

24 августа 2026 года выполнен полный автоматизированный цикл на временном пользователе:

1. Создан пользователь и подписка на выбранной ноде.
2. Создан AmneziaVPN-клиент с `flow=xtls-rprx-vision`.
3. Клиент подтверждён непосредственно в Xray через `GetInboundUsers`.
4. Получен и разобран VLESS URI.
5. Проверены `security=reality`, `encryption=none`, `flow=xtls-rprx-vision`, `fp=chrome`, Funnel hostname и порт 443.
6. Клиент отозван через API и подтверждено его удаление из Xray.
7. Временные пользователь, подписка и клиент удалены из БД.

Пользователь владельца `telegram_id=106123347` оставлен в состоянии `subscription=expired`, `vpn_client=null` для ручной проверки полного Telegram-сценария.

### Результат management-спринта

24 августа 2026 года коммит `7e4c633` развёрнут на домашнем стенде. Добавлены повторная выдача ключа, QR-код, ротация/смена страны без продления, редактирование тарифов и нод, а также Xray healthcheck.

Проверка на временном пользователе подтвердила:

- нода отвечает `status=online`, возвращается число пользователей Xray;
- стандартный Reality-клиент успешно создаётся;
- ротация создаёт новый AmneziaVPN + Vision UUID;
- дата окончания подписки при ротации не меняется;
- старый UUID исчезает из Xray, новый появляется;
- новый URI содержит `flow=xtls-rprx-vision`;
- PNG QR-код формируется внутри рабочего контейнера бота;
- отзыв удаляет новый UUID из Xray;
- временные данные полностью очищены;
- после развёртывания в логах API и бота нет ошибок.

Резервная копия файлов перед спринтом: `/root/vpn-service-backup-sprint-20260824-1427`.

### Результат reliability/payment-спринта

24 августа 2026 года на домашнем стенде:

- применены миграции `b6c1e7a4d920` и `c3f8a91e74bd (head)`; перед ними подтверждено отсутствие конфликтующих активных записей;
- API и bot пересобраны, Uvicorn запущен без `--reload`;
- публикация `8000/tcp` перевязана на `127.0.0.1`; запрос к `192.168.10.60:8000` с LAN получает connection refused;
- `/plans` без авторизации возвращает `401`, с service Bearer — `200`; `/admin` с Basic и `/docs` через localhost — `200`;
- сгенерированы отдельные `SERVICE_API_TOKEN` и `PAYMENT_WEBHOOK_SECRET`, `.env` сохранён с правами `0600`;
- `VPNNodeConfig.config.api_address` явно заполнен значением домашнего management API `172.18.0.1:10085`;
- автоматизированный bot-side цикл создал mock-платёж, подписку и AmneziaVPN-клиента с XTLS Vision; число Xray-пользователей выросло на один;
- повтор с тем же idempotency key вернул тот же payment/subscription/client и не изменил число Xray-пользователей;
- сформированный URI проверен структурно: `vless`, Reality, порт `443`, `flow=xtls-rprx-vision`, `fp=chrome`;
- подписанный refund webhook дважды вернул `200`, но создал одно событие; неверная подпись вернула `401`;
- после рестарта Xray число пользователей временно снизилось до одного статического, ручной reconciliation восстановил все три ожидаемых активных DB-клиента без ошибок;
- временный E2E-клиент отозван, а пользователь, платёж, событие и подписка удалены; остальные клиенты не затронуты;
- локальный набор прикладных тестов: `13 passed`.

Полный импорт URI в графический AmneziaVPN остаётся ручной проверкой на клиентском устройстве: серверный E2E подтверждает весь цикл до выдачи корректного URI и наличия UUID в Xray, но не управляет приложением AmneziaVPN.

Резервные копии перед развёртыванием:

- файлы: `/root/vpn-service-backup-reliability-20260824-172919`;
- PostgreSQL custom dump: `/root/vpn-db-before-reliability-20260824.dump`.

### Результат operations/bot-спринта

24 августа 2026 года на домашнем стенде:

- применены миграции `d8e26f19a4c1` и `e4c729a6b132`;
- lifecycle вынесен в `vpn-worker`; одновременный one-shot подтвердил `lifecycle_lock_busy`, после освобождения lock цикл выполнился;
- outbound `vpn-node-agent` получил scoped token, загрузил desired state, сверил 3 Xray-пользователя и отправил `online`/latency;
- API, bot и worker пишут JSON; Alloy собирает Docker logs, Loki хранит 14 дней, Grafana доступна только на `127.0.0.1:3000`, alerting отключён;
- admin показывает payments, devices, node health/latency, audit и sensitive-debug; revoke устройства и ротация device token проверены;
- device activation/profile E2E вернул VLESS manifest, старый token после refresh получил `401`, новый — `200`;
- sensitive snapshot подтвердил запись Telegram token, 8 секретных переменных, Authorization headers, одного Reality private key и двух активных клиентских ключей в audit/Loki; значения в консоль не выводились, сессия закрыта;
- Telegram → mock QR payment → Xray → AmneziaVPN URI с Reality/Vision прошёл; replay не создал второй Xray user;
- refund webhook переводит состояния в `refunded / cancelled / revoked`, неверная подпись получает `401`;
- trial E2E дал 3 дня и один Xray user, повтор вернул `409`; `WELCOME7` добавил 7 дней, повтор вернул `409`; служебный тариф скрыт из списка продажи;
- локально проходят 20 прикладных тестов;
- временные E2E-пользователи и ключи удалены.

Автоматический backup установлен как `vpn-backup.timer`: PostgreSQL custom dump и конфигурационный архив создаются ежедневно, сохраняются 14 дней. Реальное восстановление последнего dump проверено во временную БД: `restore_ok`, после проверки временная БД удалена. Ручные команды:

```bash
sudo /home/freedman/vpn-service/scripts/backup.sh
sudo /home/freedman/vpn-service/scripts/verify_backup.sh /var/backups/vpn-service/vpn-db-TIMESTAMP.dump
```

Перед operations-развёртыванием создана копия `/var/backups/vpn-service/pre-operations-20260824-175608`; перед bot/trial миграцией — `vpn-db-20260824T161454Z.dump` и соответствующий config archive.

### Системные предупреждения

В журнале загрузки присутствуют ошибки ACPI BIOS и отключение IRQ 16. Во время аудита явного влияния на VPN-сервисы не обнаружено, но стабильность сети и оборудования нужно наблюдать. Также Fedora сообщает об отключённом SELinux.

SSH переведён на ключи: `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password`. После reload повторно проверены вход `codex` ключом и `sudo -n`. Firewall не включался.

### Граница сети и мониторинга

Повторная проверка с LAN 24 августа 2026 года подтвердила следующую поверхность домашнего сервера:

- `22/tcp` — SSH доступен из локальной сети;
- `8000/tcp` — слушает только `127.0.0.1`, с LAN недоступен;
- `9090/tcp` — закрыт после удаления Cockpit;
- `5432/tcp`, `6379/tcp`, `8443/tcp` и `10085/tcp` с LAN недоступны;
- `3000/tcp`, `3100/tcp` и `12345/tcp` (Grafana/Loki/Alloy) слушают только `127.0.0.1`;
- Docker API `2375/tcp` и `2376/tcp` закрыт;
- через tailnet из проверенных портов доступен только `443/tcp`, используемый Funnel/Xray; SSH, API и Cockpit фильтруются.

Целевое состояние домашнего сервера:

1. Использовать Funnel/Xray `443/tcp` только для текущего временного теста; Tailscale не входит в production-архитектуру.
2. Docker-публикация API перевязана с `0.0.0.0:8000` на `127.0.0.1:8000`.
3. На домашнем стенде предоставлять web admin через SSH port forwarding; production admin публиковать через обычный HTTPS с прикладной авторизацией.
4. На текущем тестовом этапе оставить Swagger, ReDoc и OpenAPI доступными; перед production закрыть их административной авторизацией либо отключить.
5. Cockpit удалён, `9090/tcp` закрыт.
6. Оставить PostgreSQL, Redis и Xray API только на loopback или внутренней Docker-сети.
7. После настройки SSH-ключей запретить `root` login и password authentication.
8. Host firewall не включать и не изменять; ограничение локальных служебных портов выполнять привязкой Docker-портов к loopback.

Cockpit ранее удалён. Дополнительно удалены только два явно неиспользуемых сетевых пакета `passim` и `avahi` с `--noautoremove`; порты `27500`, `5353` и `5355` закрыты, LLMNR/mDNS отключены. По решению владельца дальнейшая работа с пакетами и зависимостями пропущена. `firewalld`, NetworkManager, Tailscale, Docker, chrony, OpenSSH и системный журнал не менялись и не удалялись.

Для текущего этапа вместо полноценного мониторинга используется локальная централизованная система логов:

- Grafana, Loki и Alloy сейчас размещены на домашнем backend и доступны только через loopback/SSH tunnel;
- Alloy собирает Docker logs API, bot, worker, node-agent, Xray, PostgreSQL и остальных контейнеров;
- после появления Hetzner/DigitalOcean Alloy на каждой ноде будет отправлять logs в production Loki исходящим HTTPS;
- Prometheus, node exporter, cAdvisor, отдельное хранилище метрик и alerting на этом этапе не разворачиваются;
- API и бот пишут структурированные JSON-логи с `request_id`, сервисом, нодой, регионом, операцией, результатом и длительностью;
- выдача, отзыв, ротация и reconciliation ключей фиксируются отдельными событиями;
- node-agent пишет health, Xray user count, reconciliation и latency; точные подключения/трафик появятся после включения Xray stats на реальных нодах;
- обычный поток логов маскирует токены, пароли, Authorization headers, Reality private key, полный VPN URI и содержимое клиентского ключа;
- для воспроизведения сложных ошибок предусмотрен отдельный `sensitive-debug` режим, в котором эти значения записываются полностью;
- sensitive-debug включается на ограниченную диагностическую сессию с указанием оператора, причины и срока окончания; текущая Loki retention — 14 дней, а PostgreSQL audit входит в обычный backup;
- секретные значения записываются только в тело события, но никогда не используются как Loki labels;
- после диагностической сессии Telegram-токен, пароли, Reality private key и затронутые клиентские ключи считаются раскрытыми и подлежат ротации;
- детали клиента (`last_seen`, traffic, online IP) хранятся в Redis/PostgreSQL и показываются в защищённой админке;
- срок хранения подробных логов ограничивается 7–14 днями, а audit log в PostgreSQL хранится дольше.

24 августа 2026 года Cockpit удалён с домашнего сервера:

- удалены 9 пакетов `cockpit*` с `--no-autoremove`;
- связанные автоматически осиротевшие зависимости не удалялись;
- `cockpit.socket` отсутствует и неактивен;
- `9090/tcp` закрыт и возвращает `connection refused`;
- API `/health` после удаления отвечает `200`;
- контейнеры API, бота, Xray, PostgreSQL и Redis продолжают работать;
- `firewalld` по решению владельца не включается, не изменяется и исключён из текущего плана.
- Swagger `/docs` по решению владельца остаётся доступным на текущем тестовом этапе.

Для технического доступа создан пользователь `codex` с SSH key authentication и passwordless sudo. Приватный ключ хранится только на управляющем Mac в `/Users/freedman/.ssh/vpnsrv_codex`; публичный ключ установлен в `/home/codex/.ssh/authorized_keys`. Проверены вход без пароля и выполнение `sudo -n` от `root`.

## Известные проблемы

### Критические для бизнес-потока

1. **Реальный платёжный провайдер не подключён.** State machine и HMAC webhook реализованы, но тестовый стенд использует `mock` с автоматическим подтверждением.
2. **Control plane пока домашний.** Первая реальная DigitalOcean-нода во FRA1 разворачивается как Германия. До появления публичного HTTPS control plane node-agent использует временный обратный SSH-туннель; Tailscale в production-схеме не используется.
3. **Telegram-flow ещё не проверен с тремя реальными нодами.** Автоматизированный цикл на домашнем Xray не заменяет импорт ключа в графический AmneziaVPN на целевых ОС.
4. **Ссылки канала и поддержки не заполнены.** До задания `TELEGRAM_CHANNEL_URL` / `SUPPORT_URL` бот показывает информирующее окно вместо перехода.

### Технический долг

- Redis пока не используется;
- автоматический запуск миграций и seed отсутствуют;
- используется `xray-core:latest`;
- прямые legacy-ветки частично продолжают использовать `print`; основной API/bot/worker/agent уже пишет JSON;
- `VPN_PLAN_ID`, `VPN_NODE_ID`, `XRAY_INBOUND_TAG` и часть Redis-конфигурации не задействованы последовательно;
- в проекте есть, вероятно, устаревший `api/app/router/vpn.py`, не подключённый к FastAPI.

## План масштабирования

Tailscale используется только временным домашним тестом и не является частью production. Целевая схема:

```text
Telegram bot / VPN client / web admin
                  |
                  v
       HTTPS production control plane
       API + PostgreSQL + worker
              (Hetzner)
                  ^
                  | outbound HTTPS desired-state/status
          +-------+--------+
          |                |
 Hetzner node-agent   DigitalOcean node-agent
          |                |
  local Xray gRPC     local Xray gRPC
  public VPN :443     public VPN :443
```

Публичные клиенты не должны зависеть от домашнего сервера за NAT. Домашний backend остаётся development/staging-стендом либо выполняет необязательные фоновые задачи через исходящее HTTPS-соединение. Production API, subscription endpoint и Telegram webhook/polling размещаются на доступном облачном control plane.

На каждой VPN-ноде публично открывается только VLESS/Reality endpoint `443/tcp`. Xray gRPC слушает только loopback. Локальный node-agent исходящими HTTPS-запросами получает desired state, применяет пользователей через Xray API и отправляет статус и логи. Входящий управляющий Xray-порт между серверами не требуется.

Для каждой ноды нужны собственные Reality private/public key, short ID, публичный hostname, inbound tag, страна, город, capacity и service credential node-agent. Три реальные production-страны — США, Нидерланды и Германия — требуют минимум три физически размещённые ноды; один Hetzner и один DigitalOcean дают только две реальные локации.

В `VPNNodeConfig.config` управляющий адрес задаётся полем `api_address`, например:

```json
{
  "api_address": "127.0.0.1:10085",
  "inbound_tag": "vless-reality",
  "host": "nl.example.com",
  "port": 443,
  "type": "tcp",
  "security": "reality",
  "sni": "www.cloudflare.com",
  "fp": "chrome",
  "pbk": "PUBLIC_REALITY_KEY",
  "sid": "SHORT_ID"
}
```

В production `api_address` используется только локальным node-agent. Control plane не соединяется с Xray gRPC напрямую. Текущий прямой `XrayClient` сохраняется для домашнего теста; production переключается на `XRAY_MANAGEMENT_MODE=agent`.

### Быстрое добавление VPN-ноды

Повторяемый bootstrap находится в `scripts/deploy_vpn_node.sh`. Он не ставит пакеты в хостовую ОС: целевой Fedora-инстанс должен уже иметь Podman, systemd, OpenSSH и OpenSSL. Скрипт:

Параметры Reality для backend-ноды задаются до запуска bootstrap: `REALITY_SNI`
определяет доменное имя маскировки, а `REALITY_FINGERPRINT` — TLS fingerprint
клиента (`chrome`, `firefox`, `safari` или `randomized`). Они сохраняются в
конфигурации ноды control plane и применяются к новым выдаваемым профилям:

```bash
REALITY_SNI=www.microsoft.com REALITY_FINGERPRINT=firefox \
  ./scripts/deploy_vpn_node.sh
```

Изменение этих переменных не переписывает уже выданные URI; для них выполните
ротацию клиента.

- создаёт отдельную пару Reality и short ID для ноды;
- регистрирует ноду и VLESS-конфигурацию через API;
- выпускает scoped token node-agent;
- передаёт Xray-конфигурацию и собирает минимальный agent image;
- устанавливает Quadlet units с автозапуском;
- оставляет `10085/tcp` только на loopback, а `443/tcp` публикует для VPN;
- безопасно повторяется: существующие Reality-ключи и node token не ротируются.

Пример запуска для следующего инстанса:

```bash
export NODE_SSH=root@203.0.113.10
export NODE_NAME=provider-region-01
export NODE_PROVIDER=hetzner
export NODE_REGION=nl                 # us, nl или de
export NODE_IP=203.0.113.10
export NODE_HOSTNAME=nl1.example.com  # до DNS можно использовать IP
export NODE_CAPACITY=100
export CONTROL_PLANE_URL=https://api.example.com
export ADMIN_API_URL=http://127.0.0.1:8000
export ADMIN_USERNAME=admin
read -rs ADMIN_PASSWORD; export ADMIN_PASSWORD
./scripts/deploy_vpn_node.sh
unset ADMIN_PASSWORD
```

Для временного домашнего control plane `CONTROL_PLANE_URL` может указывать на loopback reverse SSH-туннеля ноды, например `http://127.0.0.1:18000`. Это переходная схема без Tailscale: после появления публичного HTTPS URL достаточно обновить `CONTROL_PLANE_URL` и повторно запустить bootstrap.

### Первая DigitalOcean-нода

25 августа 2026 года развёрнута нода `do-fra1-de-01` (`159.223.22.59`, DigitalOcean FRA1, регион `de`, node id `2`):

- Xray `26.3.27` закреплён digest образа и работает через Podman Quadlet;
- наружу доступны SSH и VLESS/Reality `443/tcp`; firewall не включался;
- Xray gRPC `127.0.0.1:10085` и временный control-plane bridge `127.0.0.1:18000` снаружи недоступны;
- `vpn-xray` и `vpn-node-agent` запускаются systemd и имеют memory limits;
- backend переключён на `XRAY_MANAGEMENT_MODE=agent`;
- node-agent отображается `online` и отправляет reconciliation/latency в audit log;
- E2E проверка создала клиента, синхронизировала его в Xray, получила VLESS Reality URI, вывела трафик через `159.223.22.59`, затем отозвала клиента и подтвердила удаление из Xray.

Временная связь с домашним control plane обслуживается `vpn-control-tunnel.service` на домашнем сервере. SSH-ключ ограничен только reverse-forward на loopback `18000`; пользователь `vpn-tunnel` на VPN-ноде не имеет sudo. После переноса API на публичный HTTPS этот tunnel нужно удалить.

Готовность перед добавлением второй ноды:

1. [x] Node-agent и scoped desired-state API.
2. [x] Локальное применение Xray-конфигурации агентом.
3. [x] Health/status/latency нод.
4. [x] Исключение `offline` и перегруженных нод из bot/profile.
5. [x] Reconciliation с БД как source of truth.
6. [x] Отдельный worker и PostgreSQL advisory lock.
7. [ ] Обычный публичный HTTPS и при необходимости mTLS на production control plane.
8. [x] Docker/Xray/backend logs в Loki; точные traffic/active-connection метрики добавятся на реальных нодах.
9. [x] Ежедневный backup и проверяемое восстановление PostgreSQL.

## Multi-server клиент и автовыбор

Этот блок относится к последнему этапу разработки. До стабилизации backend, Telegram-бота, админки и production-нод система продолжает выдавать отдельные VLESS/AmneziaVPN-конфигурации. Финальная целевая единица выдачи — не одиночный VLESS URI, а subscription profile с несколькими VPN-нодами. Telegram-бот выдаёт одну subscription-ссылку и QR; после добавления новой ноды клиент получает её при следующем обновлении профиля без ручного перевыпуска ссылки.

Новые сущности control plane:

- `client_devices` — установка приложения и её состояние;
- `subscription_tokens` — отзываемый, хранимый в виде hash токен загрузки профиля;
- `vpn_client_credentials` — credential клиента для каждой разрешённой ноды;
- `client_profile_versions` — версия manifest, ETag и время обновления;
- расширенный `vpn_nodes` — country, city, priority, capacity, maintenance и public endpoint.

API клиента:

- `POST /v1/client/activate` — привязка устройства по коду из Telegram;
- `GET /v1/client/profile` — подписанный JSON manifest всех доступных нод;
- `GET /v1/subscriptions/{token}/sing-box.json` — remote sing-box profile;
- `POST /v1/client/refresh` — обновление короткоживущего access token;
- `POST /v1/client/diagnostics` — добровольная отправка клиентского debug bundle.

Клиент строится вокруг готового sing-box core, а не собственного VPN-движка. Для каждой ноды manifest содержит отдельный VLESS Reality outbound. Группа `urltest` проверяет их через реальный proxy-запрос, выбирает лучший и использует hysteresis/tolerance, чтобы не переключаться из-за небольших колебаний.

Режимы выбора:

- `Auto` — лучший доступный сервер всех стран;
- `Auto: страна` — лучший сервер выбранной страны;
- `Manual` — закреплённая нода;
- автоматический failover при недоступности текущей ноды.

Client-side выбор учитывает реальную задержку и успешность подключения. Backend отвечает за допуск нод: не включает в manifest `offline`, `maintenance` и перегруженные серверы. Активное соединение не переключается только из-за небольшой разницы latency; немедленный failover выполняется при ошибке туннеля.

Первый клиентский MVP: Android, затем iOS/macOS и desktop. До выпуска брендированного приложения тот же remote profile можно проверять в совместимом sing-box-клиенте. При форке или встраивании sing-box необходимо учитывать GPL-3.0-or-later и требования к распространению производного клиента.

## Приоритет работ

1. [x] Единая `.env` и `configctl`.
2. [x] PostgreSQL audit и Grafana/Loki/Alloy без alerting.
3. [x] Web admin: клиенты, платежи, reconciliation, устройства и sensitive-debug.
4. [x] Node-agent, desired-state, отдельный worker и advisory lock.
5. [x] Scoped device activation/profile API — foundation этапа 9.
6. [x] Bot UI: QR payment choice, инструкция, trial и promo; ссылки канала/поддержки требуют значений.
7. [ ] Выбрать и подключить реального платёжного провайдера, выключить mock auto-confirm.
8. [ ] Развернуть production control plane и реальные ноды США/Нидерланды/Германия; проверить HTTPS agent traffic.
9. [ ] Выполнить ручной импорт Telegram → Xray → AmneziaVPN на целевых ОС и реальные latency/connection проверки.
10. [ ] Последним этапом: per-node credentials, sing-box remote profile, `urltest`, авто-выбор, failover и клиентская диагностика.
