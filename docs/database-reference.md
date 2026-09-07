# Справочник по базе данных Freedom VPN

Документ фиксирует актуальную структуру БД и быстрые проверки, чтобы при
разборе оплат, подписок, web-кабинета и Telegram не искать поля по коду.

## Где живёт БД

Production использует внешний PostgreSQL в общей инфраструктуре:

```text
host.docker.internal:6432
database: vpn
```

Compose приложения не поднимает отдельный контейнер БД. На сервере
`/home/freedman/vpn-service` контейнеры `api`, `bot`, `worker` подключаются к БД
через `DATABASE_URL`.

Миграции:

```bash
docker compose run --rm api alembic current
docker compose run --rm api alembic upgrade head
```

Перед ручными массовыми правками БД делайте backup. Backup/restore описан в
`docs/maintenance-scripts.md`.

## Основные связи

```text
users
  ├─ subscriptions
  │    └─ vpn_clients
  ├─ payments ── payment_events
  ├─ cabinet_access_tokens
  ├─ cabinet_login_codes
  ├─ access_grants
  ├─ activation_codes
  └─ client_devices

plan_packages
  └─ plans
       ├─ subscriptions
       └─ payments

vpn_nodes
  ├─ vpn_node_configs
  ├─ vpn_clients
  └─ audit_logs
```

Правило доступа простое: оплаченный доступ виден пользователю только когда есть
`payments.status = paid`, у платежа заполнен `subscription_id`, подписка активна,
не истекла, и есть активный `vpn_clients` для этой подписки.

## Таблицы

### `users`

Пользователи Telegram/web-кабинета.

| Поле | Назначение |
| --- | --- |
| `id` | внутренний ID пользователя |
| `telegram_id` | Telegram user id, уникальный |
| `email` | email для web-кабинета, уникальный, может быть пустым |
| `password_hash` | хэш пароля кабинета, пароль в открытом виде не хранится |
| `username`, `first_name`, `last_name` | Telegram-профиль |
| `status` | `active` / заблокированные состояния |
| `created_at`, `updated_at` | даты |

### `plan_packages`

Сущность пакета тарифа: Лайт, Стандарт, Ультра.

| Поле | Назначение |
| --- | --- |
| `id` | ID пакета |
| `code` | машинный код пакета, уникальный |
| `name` | название пакета |
| `description` | описание для сайта/кабинета |
| `max_connections` | лимит подключений пакета, `0` = без ограничений |
| `traffic_limit_gb` | лимит трафика пакета, `0` = без ограничений |
| `sort_order` | порядок отображения |
| `is_active` | показывать/использовать пакет |
| `created_at` | дата создания |

Добавление через админку:

1. Открыть `/admin`.
2. Выбрать раздел `Пакеты тарифов`.
3. Заполнить `Код пакета`, `Название`, `Описание`, лимит подключений, лимит
   трафика, порядок и активность.
4. Нажать `Добавить пакет тарифа`.
5. Перейти в раздел `Тарифы` и у нужных тарифов выбрать новый `package_id`.

`0` в лимитах подключений или трафика означает “без ограничений”.
Telegram-бот и web-кабинет читают пакеты из этой таблицы. Telegram показывает
`name` и `description` как основной текст пакета; лимиты подключений и трафика
используются только как fallback, если `description` пустое.

### `plans`

Конкретные сроки и цены внутри пакетов.

| Поле | Назначение |
| --- | --- |
| `id` | ID тарифа |
| `code` | машинный код тарифа, уникальный |
| `name` | название срока, например `1 мес (-3%)` |
| `duration_days` | срок действия |
| `max_connections` | лимит подключений |
| `traffic_limit_gb` | лимит трафика |
| `package_id` | ссылка на `plan_packages.id` |
| `price`, `currency` | цена и валюта |
| `is_active` | можно использовать в оплатах |
| `is_public` | показывать на сайте/в кабинете |
| `created_at` | дата создания |

### `payment_methods`

Способы оплаты для Telegram и web-кабинета.

| Поле | Назначение |
| --- | --- |
| `id` | ID способа |
| `code` | машинный код, например `platega_sbp_qr` |
| `name` | название для интерфейса |
| `url` | ручная ссылка/реквизиты, для Platega обычно пусто |
| `sort_order` | порядок отображения |
| `is_active` | активен ли способ |
| `image_data`, `image_mime_type`, `image_filename` | QR/картинка для ручных способов |
| `created_at` | дата создания |

Актуальные коды Platega:

```text
platega_sbp_qr
platega_mir_card
platega_crypto
```

Для них ссылка создаётся через API Platega, поэтому `payment_methods.url` может
быть пустым.

### `payments`

Локальные заказы и статусы оплаты.

| Поле | Назначение |
| --- | --- |
| `id` | внутренний ID платежа |
| `user_id` | кто платит |
| `plan_id` | какой тариф покупается |
| `node_id` | на какую VPN-ноду выдавать/продлевать |
| `subscription_id` | заполнен после успешной выдачи/продления |
| `provider` | `manual_bank`, `platega`, `telegram_stars`, `mock` |
| `provider_payment_id` | transaction/payment ID провайдера |
| `idempotency_key` | защита от дублей; `web:` для кабинета, `platega:` для TG |
| `amount`, `currency` | сумма |
| `status` | внутренний статус платежа |
| `client_type`, `flow`, `fingerprint` | профиль клиента для выдачи ключа |
| `details` | JSON с деталями метода/провайдера |
| `receipt_data`, `receipt_mime_type`, `receipt_filename` | чек для ручных оплат |
| `paid_at`, `failed_at`, `cancelled_at`, `refunded_at` | даты финальных статусов |
| `created_at`, `updated_at` | даты |

Внутренние статусы:

```text
pending
processing
paid
failed
cancelled
expired
refunded
```

Разрешённые переходы:

```text
pending    -> processing, paid, failed, cancelled, expired
processing -> paid, failed, cancelled, expired
paid       -> refunded
failed/cancelled/expired/refunded -> финальные
```

Для Platega callback статусы приводятся так:

| Platega | Внутри БД |
| --- | --- |
| `CONFIRMED` | `paid` |
| `CANCELED` | `cancelled` |
| `CHARGEBACKED` | `refunded` |
| `PENDING` | `pending` |
| `PROCESSING` | `processing` |

`payments.details` для Platega:

| JSON-поле | Назначение |
| --- | --- |
| `method_code` | `platega_sbp_qr`, `platega_mir_card`, `platega_crypto` |
| `source` | где создан заказ: `web_cabinet` или `telegram_bot` |
| `created_source` | исходный источник создания, не должен затираться callback |
| `last_event_source` | последняя служебная операция, обычно `platega_callback` |
| `platega.redirect` | ссылка оплаты, полученная при создании |
| `platega.paymentMethod` | ID метода Platega |
| `platega.status` | последний статус из callback |

### `payment_events`

Журнал webhook/callback событий провайдеров.

| Поле | Назначение |
| --- | --- |
| `id` | ID события |
| `payment_id` | ссылка на `payments.id` |
| `provider` | провайдер |
| `event_id` | ID события; уникален вместе с provider |
| `event_type` | внутренний статус события |
| `status` | обработано/ошибка |
| `payload` | исходный payload события |
| `error` | ошибка обработки, если есть |
| `created_at`, `processed_at` | даты |

Уникальность `provider + event_id` защищает от повторной выдачи доступа при
повторной доставке webhook.

### `subscriptions`

Оплаченный период пользователя.

| Поле | Назначение |
| --- | --- |
| `id` | ID подписки |
| `user_id` | владелец |
| `plan_id` | текущий тариф |
| `status` | `active`, `expired`, `cancelled`, `disabled` |
| `starts_at`, `expires_at` | срок действия |
| `created_at`, `updated_at` | даты |

Ограничение: только одна активная подписка на пользователя
(`uq_subscriptions_one_active_per_user`).

### `vpn_clients`

Версии выданных VPN-ключей.

| Поле | Назначение |
| --- | --- |
| `id` | ID клиента |
| `user_id` | владелец |
| `subscription_id` | подписка |
| `node_id` | VPN-нода |
| `protocol` | сейчас `vless` |
| `client_type` | `amnezia` или `universal` |
| `flow`, `fingerprint` | параметры VLESS/Reality |
| `max_connections` | лимит IP/подключений |
| `traffic_limit_gb` | лимит трафика |
| `client_uuid` | UUID клиента в 3x-ui |
| `status` | `active`, `provisioning`, `revoked`, `expired` |
| `expires_at`, `revoked_at` | срок/отзыв |
| `config_override` | ручная VPN-ссылка, если задана |
| `last_connected_at`, `last_ip` | последняя активность |
| `created_at` | дата создания |

Ограничение: только один активный VPN-клиент на подписку
(`uq_vpn_clients_one_active_per_subscription`).

### `vpn_nodes`

VPN-серверы.

| Поле | Назначение |
| --- | --- |
| `id` | ID ноды |
| `name` | имя, уникальное |
| `provider` | провайдер/тип |
| `region` | регион для интерфейса |
| `hostname`, `ip_address` | адреса |
| `status` | `active` и другие состояния |
| `capacity` | ёмкость |
| `health_status`, `last_seen_at`, `latency_ms`, `active_connections` | мониторинг |
| `created_at`, `updated_at` | даты |

### `vpn_node_configs`

Конфиги нод для генерации VLESS и управления 3x-ui.

| Поле | Назначение |
| --- | --- |
| `id` | ID конфига |
| `node_id` | ссылка на `vpn_nodes.id` |
| `protocol` | например `vless` |
| `config` | JSON: `api_address`, `inbound_tag`, host/port/sni/public key и т.д. |
| `created_at`, `updated_at` | даты |

### `cabinet_access_tokens`

Сессии/магические ссылки web-кабинета.

| Поле | Назначение |
| --- | --- |
| `id` | ID токена |
| `user_id` | владелец |
| `token_hash` | хэш токена, сам токен не хранится |
| `expires_at` | срок действия |
| `revoked_at` | отозван |
| `last_used_at` | последний вход |
| `created_at` | дата создания |

### `cabinet_login_codes`

Одноразовые email-коды входа.

| Поле | Назначение |
| --- | --- |
| `id` | ID кода |
| `user_id` | владелец |
| `code_hash` | хэш кода |
| `plain_code` | временное поле для админ-диагностики |
| `expires_at` | срок действия |
| `attempts` | попытки ввода |
| `used_at` | использован |
| `created_at` | дата создания |

### `access_grants`

Пробный/выданный вручную доступ.

| Поле | Назначение |
| --- | --- |
| `id` | ID выдачи |
| `user_id` | владелец |
| `kind` | тип выдачи |
| `code` | код сценария |
| `duration_days` | длительность |
| `subscription_id` | связанная подписка после выдачи |
| `created_at` | дата |

Уникальность: `user_id + kind + code`.

### `activation_codes`

Коды активации устройства/клиента.

| Поле | Назначение |
| --- | --- |
| `id` | ID |
| `user_id` | владелец |
| `code_hash`, `code_prefix` | код без хранения полного значения |
| `expires_at`, `used_at` | срок и использование |
| `device_id` | привязанное устройство |
| `created_at` | дата |

### `client_devices`

Устройства пользователя.

| Поле | Назначение |
| --- | --- |
| `id` | ID устройства |
| `user_id` | владелец |
| `name`, `platform` | название и платформа |
| `token_hash`, `token_prefix` | токен устройства |
| `status` | `active`, `revoked` и т.п. |
| `expires_at`, `last_seen_at`, `created_at`, `revoked_at` | даты |

### `audit_logs`

Аудит действий админки, worker, уведомлений и служебных операций.

| Поле | Назначение |
| --- | --- |
| `id` | ID записи |
| `request_id` | request id из логов |
| `actor_type`, `actor_id` | кто сделал действие |
| `action` | имя действия |
| `resource_type`, `resource_id` | объект |
| `result` | результат |
| `node_id`, `ip_address` | контекст |
| `details` | JSON-детали |
| `sensitive` | содержит ли чувствительные данные |
| `created_at` | дата |

### `debug_sessions`

Окна debug-доступа.

| Поле | Назначение |
| --- | --- |
| `id` | ID |
| `created_by` | кто открыл |
| `reason` | причина |
| `status` | `active` / закрыто |
| `expires_at`, `created_at`, `closed_at` | даты |

### `admin_settings`

Настройки админки.

| Поле | Назначение |
| --- | --- |
| `key` | ключ настройки |
| `value` | значение |
| `updated_at` | дата обновления |

Используется для админских контактов и прочих runtime-настроек.

## Быстрая диагностика платежа Platega

Безопасный вывод последних Platega-платежей без email/telegram/секретов:

```bash
cd /home/freedman/vpn-service
docker compose exec -T api python - <<'PY'
import asyncio
from sqlalchemy import select, desc
from app.db.session import AsyncSessionLocal
from app.db.models.payment import Payment

async def main():
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Payment)
                .where(Payment.provider == "platega")
                .order_by(desc(Payment.id))
                .limit(20)
            )
        ).scalars().all()
        for p in rows:
            details = p.details or {}
            idem = p.idempotency_key or ""
            print({
                "id": p.id,
                "user_id": p.user_id,
                "status": p.status,
                "subscription_id": p.subscription_id,
                "plan_id": p.plan_id,
                "node_id": p.node_id,
                "source": details.get("source"),
                "created_source": details.get("created_source"),
                "last_event_source": details.get("last_event_source"),
                "method_code": details.get("method_code"),
                "idempotency_prefix": idem.split(":", 1)[0] if ":" in idem else idem[:16],
                "created_at": str(p.created_at),
                "updated_at": str(p.updated_at),
            })

asyncio.run(main())
PY
```

Проверка событий по конкретному платежу:

```bash
PAYMENT_ID=95
docker compose exec -T -e PAYMENT_ID="$PAYMENT_ID" api python - <<'PY'
import asyncio, os
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.db.models.payment import PaymentEvent

payment_id = int(os.environ["PAYMENT_ID"])

async def main():
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(PaymentEvent)
                .where(PaymentEvent.payment_id == payment_id)
                .order_by(PaymentEvent.id)
            )
        ).scalars().all()
        for e in rows:
            payload = e.payload or {}
            platega = ((payload.get("details") or {}).get("platega") or {})
            print({
                "event_id": e.id,
                "event_type": e.event_type,
                "processed_at": str(e.processed_at),
                "platega_status": platega.get("status"),
                "platega_method": platega.get("paymentMethod"),
            })

asyncio.run(main())
PY
```

Интерпретация:

```text
payment.status = paid + subscription_id есть   -> доступ выдан/продлён
payment.status = paid + subscription_id пустой -> ошибка provisioning, смотреть api logs
payment.status = cancelled                     -> Platega прислала CANCELED
payment.status = pending/processing            -> ждём callback
```

Логи API:

```bash
docker compose logs --since=2h api | grep -i -E 'platega|payment_subscription_provisioned|payment_paid|provision|error|exception'
```

Ключевой лог успешной выдачи после оплаты:

```text
payment_subscription_provisioned
```

## Проверка активного доступа пользователя

По `user_id`:

```bash
USER_ID=11
docker compose exec -T -e USER_ID="$USER_ID" api python - <<'PY'
import asyncio, os
from datetime import datetime, timezone
from sqlalchemy import select, desc
from app.db.session import AsyncSessionLocal
from app.db.models.subscription import Subscription
from app.db.models.vpn_client import VPNClient
from app.db.models.plan import Plan

user_id = int(os.environ["USER_ID"])

async def main():
    async with AsyncSessionLocal() as db:
        sub = (
            await db.execute(
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .order_by(desc(Subscription.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if not sub:
            print({"active_for_cabinet": False, "reason": "no_subscription"})
            return
        expires = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
        plan = await db.get(Plan, sub.plan_id)
        client = (
            await db.execute(
                select(VPNClient)
                .where(VPNClient.subscription_id == sub.id)
                .order_by(desc(VPNClient.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        print({
            "active_for_cabinet": sub.status == "active" and expires > datetime.now(timezone.utc),
            "subscription_id": sub.id,
            "subscription_status": sub.status,
            "expires_at": str(sub.expires_at),
            "plan_id": sub.plan_id,
            "plan_name": plan.name if plan else None,
            "client_id": client.id if client else None,
            "client_status": client.status if client else None,
            "client_expires_at": str(client.expires_at) if client else None,
        })

asyncio.run(main())
PY
```

## Что менялось в БД последними задачами

- Добавлен web-кабинет:
  - `cabinet_access_tokens`;
  - `cabinet_login_codes`;
  - `users.email`;
  - `users.password_hash`.
- Добавлены платежные методы и чеки:
  - `payment_methods`;
  - `payments.receipt_data`;
  - `payments.receipt_mime_type`;
  - `payments.receipt_filename`.
- Добавлен lifecycle платежей:
  - `payment_events`;
  - timestamp-поля `paid_at`, `failed_at`, `cancelled_at`, `refunded_at`;
  - связь `payments.subscription_id`.
- Убрана старая обратная связь `subscriptions.payment_id`; связь теперь идёт
  от платежа к подписке через `payments.subscription_id`.
- Добавлены лимиты тарифа и клиента:
  - `plans.max_connections`;
  - `plans.traffic_limit_gb`;
  - `vpn_clients.max_connections`;
  - `vpn_clients.traffic_limit_gb`.
- Добавлены пакеты тарифов:
  - `plan_packages`;
  - `plans.package_id`.
- Добавлена операционная база:
  - `audit_logs`;
  - `debug_sessions`;
  - `client_devices`;
  - `activation_codes`;
  - `access_grants`;
  - `admin_settings`.

Полный список миграций лежит в `alembic/versions`.
