# Редактирование Telegram-бота

## Назначение и границы

Telegram-бот — тонкий интерфейс над VPN API. Он показывает меню, собирает выбор
устройства, страны и тарифа, создаёт платёж, показывает состояние подписки и
запрашивает перевыпуск ключа. Бот не подключается к PostgreSQL и 3x-ui напрямую.

Правильная цепочка любого изменения:

```text
Telegram callback/message
        ↓
bot/app/main.py
        ↓ Bearer SERVICE_API_TOKEN
VPN API http://api:8000
        ↓ Bearer THREEXUI_API_TOKEN
3x-ui master → нужный inbound/child
```

Не добавляйте прямые вызовы `/panel/api/*` в бот. Логику оплаты, блокировки
строк, компенсационного удаления клиента и аудита должен сохранять VPN API.

## Файлы бота

| Файл | Что редактировать |
|---|---|
| `bot/app/main.py` | aiogram router, клавиатуры, HTTP-вызовы VPN API, QR и запуск polling |
| `bot/app/content.json` | пользовательские тексты, ссылки, платформы и дополнительные URL-кнопки |
| `bot/app/content.py` | загрузка JSON и подстановка `${ENV_NAME}` |
| `bot/app/domain.py` | чистые функции: подписи стран и payload единственного профиля ключа |
| `bot/app/logging_config.py` | формат и уровень логирования |
| `bot/requirements.txt` | зависимости контейнера |
| `tests/test_bot_domain.py` | unit-тесты чистой логики и content |
| `docker-compose.yml` | передача переменных и запуск контейнера `vpn-bot` |

`main.py` пока является единым модулем. При росте кода выносите независимые
сценарии в `handlers/`, клавиатуры в `keyboards/`, а VPN API client в
`services/vpn_api.py`. Перенос делайте механически с сохранением callback data,
иначе старые сообщения Telegram перестанут открываться.

## Переменные окружения

| Переменная | Обязательность | Назначение |
|---|---|---|
| `BOT_TOKEN` | обязательно | токен Telegram Bot API |
| `API_URL` | обязательно в production | URL VPN API, в Compose `http://api:8000` |
| `SERVICE_API_TOKEN` | обязательно | Bearer token внутренних маршрутов VPN API |
| `LOG_LEVEL` | нет | уровень логирования, обычно `INFO` |
| `BOT_PLAN_CODES` | нет | порядок и allowlist тарифов через запятую |
| `TELEGRAM_CHANNEL_URL` | нет | ссылка кнопки канала |
| `SUPPORT_URL` | нет | ссылка поддержки |
| `YOOMONEY_PAYMENT_URL` | нет | общая резервная ссылка оплаты |
| `YOOMONEY_14D_URL` | нет | ссылка тарифа `vpn_14d` |
| `YOOMONEY_30D_URL` | нет | ссылка тарифа `vpn_30d` |
| `YOOMONEY_90D_URL` | нет | ссылка тарифа `vpn_90d` |
| `TRY_PAYMENT_URL` | нет | ссылка тестовой оплаты |
| `BOT_CONTENT_FILE` | нет | альтернативный JSON с контентом |

Секреты задаются только в `.env` на сервере. Не помещайте в `content.json`, Git,
callback data или логи `BOT_TOKEN`, `SERVICE_API_TOKEN`, токены 3x-ui, полный
VLESS URI и закрытый base path панели.

`VPN_PLAN_ID` и `VPN_NODE_ID` ещё передаются контейнеру исторически, но текущий
бот выбирает тариф и ноду через API и не использует эти переменные. Не стройте
на них новые сценарии.

## Контент без изменения Python

Обычный текст и ссылки меняются в `bot/app/content.json`:

- `texts.welcome` — сообщение `/start`;
- `texts.main_menu` — заголовок главного меню;
- `texts.platforms_intro` — экран выбора устройства;
- `texts.instructions` — инструкция подключения;
- `texts.try` и `texts.promo` — тест и промокод;
- `links.*` — ссылки, подставляемые из окружения;
- `platforms[]` — поддерживаемые ОС и приложения;
- `main_url_buttons[]` — дополнительные кнопки главного меню.

Строки отправляются с `parse_mode="HTML"`. Разрешены поддерживаемые Telegram
теги, например `<b>` и `<code>`. Любые данные пользователя или API перед
вставкой в HTML пропускайте через `html.escape()`.

Чтобы добавить устройство, создайте объект:

```json
{
  "id": "new_platform",
  "label": "Название кнопки",
  "client": "Название приложения",
  "url": "https://example.org/download",
  "description": "Краткая инструкция"
}
```

`id` должен быть коротким и стабильным: он входит в callback data
`device:<id>` и `purchase_device:<id>`.

## Меню и callback data

Кнопки создаются функциями `main_menu()`, `back_menu()`,
`vpn_ready_keyboard()`, `active_vpn_keyboard()`, `platforms_keyboard()` и
`access_node_keyboard()`.

Текущие callback-шаблоны:

| Callback | Обработчик |
|---|---|
| `main_menu` | возврат в главное меню и очистка FSM |
| `devices`, `device:<platform>` | инструкции по устройству |
| `buy_vpn`, `purchase_device:<platform>` | начало покупки и выбор страны |
| `purchase_country:<node_id>` | выбор тарифа |
| `purchase_plan:<plan_id>:<node_id>` | экран оплаты |
| `payment_qr:<plan_id>:<node_id>` | QR платёжной ссылки |
| `pay_qr:<plan_id>:<node_id>` | создание/проверка платежа через VPN API |
| `vpn_status`, `vpn_key` | личный кабинет и показ ключа |
| `vpn_reissue` | список стран для перевыпуска |
| `rotate_country:<subscription_id>:<node_id>` | новый ключ и отзыв старого |
| `promo_start`, `promo_country:<node_id>` | ввод и применение промокода |
| `try_start` | экран тестового доступа |

Telegram ограничивает размер callback data. Не включайте туда ссылки, JSON,
токены или пользовательский текст. Переданные ID нельзя считать авторизацией:
VPN API обязан проверить владельца и допустимость операции.

## Состояния FSM

Сейчас FSM применяется только для промокода:

```text
promo_start → PromoFlow.waiting_code → promo_country:<node_id> → clear
```

Код хранится в `FSMContext` до выбора страны. При возврате в главное меню
состояние очищается. Для нового многошагового сценария создавайте отдельный
`StatesGroup`; не используйте глобальные словари, файлы или Redis — Redis в
текущем минимальном стеке отсутствует. После перезапуска незавершённый диалог
может быть потерян, поэтому каждый шаг должен безопасно начинаться заново.

## Вызовы VPN API

`api_client()` создаёт `httpx.AsyncClient` с базовым URL и заголовком:

```http
Authorization: Bearer <SERVICE_API_TOKEN>
```

Основные функции-обёртки:

| Функция | Маршрут VPN API |
|---|---|
| `get_or_create_user()` | `GET /users/{telegram_id}`, затем `POST /users` |
| `get_vpn_status()` | `GET /users/{telegram_id}/vpn-status` |
| `get_plans()` | `GET /plans` |
| `get_nodes()` | `GET /vpn/nodes` |
| `get_node_configs()` | `GET /vpn/nodes/{id}/configs` |
| `create_payment()` | `POST /payments` |
| `get_vpn_client_config()` | `GET /vpn/clients/{id}/config` |
| `rotate_vpn_client()` | `POST /subscriptions/{id}/rotate` |
| `create_access_grant()` | `POST /subscriptions/access-grants` |

При добавлении маршрута сначала реализуйте и протестируйте его в VPN API,
затем добавляйте маленькую typed-обёртку в бот. Не размазывайте одинаковые
`client.get/post` по нескольким handlers.

## Как формируется список стран

`available_nodes()` сначала фильтрует логические ноды:

- `status == active`;
- health не `offline`;
- `active_connections < capacity`;
- регион преобразуется функцией `country_label()`;
- существует VLESS-конфигурация с HTTP(S) `api_address` и числовым inbound ID.

Проверки конфигураций выполняются параллельно через `asyncio.gather`. Новую
страну нельзя хардкодить в боте: она появляется после регистрации ноды, а флаг
строится из ISO-кода в `region`, например `SE|Швеция`.

## Единственный профиль ключа

Бот не предлагает пользователю варианты протокола, flow или fingerprint.
Payload создаётся функциями `subscription_payload()` и `rotation_payload()`:

```json
{
  "client_type": "universal",
  "flow": "",
  "fingerprint": "firefox"
}
```

Окончательные `encryption`, Reality и XHTTP-параметры берутся VPN API из
конфигурации выбранной ноды. Не собирайте `vless://` в боте. Единый генератор
на стороне API гарантирует одинаковую ссылку в Telegram, web admin и личном
кабинете.

`send_key_message()` получает готовую URI, строит PNG QR в памяти и отправляет
фото с HTML-escaped ссылкой. Не сохраняйте QR и ключ на диск.

## Покупка

Пользователь проходит цепочку:

```text
устройство → страна → тариф → ссылка/QR оплаты → Проверить оплату
```

`pay_qr_handler()`:

1. получает пользователя;
2. повторно проверяет наличие тарифа;
3. блокирует покупку при активной подписке;
4. вызывает `POST /payments` с `node_id` выбранной страны;
5. использует `telegram:<callback.id>` как idempotency key;
6. при `pending` сообщает, что ожидается webhook;
7. при `paid` получает созданные подписку и клиента;
8. запрашивает готовую URI и отправляет ссылку с QR.

Кнопка «Проверить оплату» в текущей реализации фактически создаёт платёж. Она
не запрашивает статус ранее созданной операции по постоянному payment ID.
Повторный клик создаёт новый idempotency key, потому что меняется callback ID.
При подключении реального провайдера рекомендуется хранить payment ID в FSM или
БД и сделать отдельный маршрут проверки, иначе пользователь может создать
несколько `pending` платежей.

`PAYMENT_AUTO_CONFIRM=true` допустим только для теста. В production выдача
происходит после подписанного webhook, как описано в
[`vpn-api-lifecycle.md`](vpn-api-lifecycle.md).

## Личный кабинет и перевыпуск

`vpn_status_handler()` показывает только активную подписку и её текущего
клиента. `vpn_key_handler()` повторно запрашивает конфигурацию; ключ не хранится
в Telegram FSM.

При перевыпуске пользователь снова выбирает страну. VPN API сначала создаёт
нового клиента в целевом inbound, затем отзывает старого и только после этого
фиксирует новый активный клиент. Бот не должен заранее показывать успех или
самостоятельно удалять прежний ключ.

## Промокод и тестовый доступ

`create_access_grant()` передаёт Telegram ID, тип `promo` или `trial`, страну и
необязательный код. Срок и допустимость определяет API. Handler должен различать
ожидаемые `404/409` и временные ошибки связи, но не раскрывать внутренний ответ
или stack trace пользователю.

Текущий экран `try_start` направляет пользователя на оплату и поддержку, а не
создаёт trial автоматически. Если автоматизировать trial, вызывайте
`create_access_grant(kind="trial")` только после серверной проверки лимита.

## Ошибки и логирование

Для HTTP-операций отдельно обрабатывайте:

- `httpx.HTTPStatusError` — API доступен, но отклонил операцию;
- `httpx.HTTPError` — timeout, DNS или отсутствие соединения;
- прочие исключения — программная ошибка.

Пользователю показывается короткое безопасное сообщение, полная техническая
ошибка уходит в container log через `logging.exception`. Запрещено логировать
Bearer headers, webhook secret и `config` из ответа ключа.

`show_screen()` редактирует существующее текстовое сообщение. После QR/photo
Telegram не позволяет заменить фото через `edit_text`, поэтому функция снимает
клавиатуру с фото и отправляет новый текстовый экран. Сохраняйте эту обработку
при рефакторинге навигации.

## Фактический API 3x-ui

На рабочем endpoint проверено:

- страница `/panel/api-docs` доступна, но без UI-сессии открывает страницу входа;
- `/panel/api/openapi.json` требует cookie или Bearer token;
- рабочий token приложения со scope `node-sync` получает `403` на OpenAPI — это
  ожидаемое ограничение прав;
- бот не должен получать более привилегированный токен ради документации.

Предоставленная OpenAPI-документация описывает cookie-сессию и Bearer token.
Для Bearer-запросов CSRF не требуется. Из клиентских методов 3x-ui для VPN API
наиболее важны:

| Метод 3x-ui | Использование проектом |
|---|---|
| `GET /panel/api/inbounds/list` | проверка inbound и список клиентов |
| `GET /panel/api/inbounds/get/{id}` | возможное точечное чтение полного inbound |
| `POST /panel/api/clients/add` | создать клиента и прикрепить к inbound |
| `GET /panel/api/clients/get/{email}` | точечная диагностика клиента |
| `POST /panel/api/clients/update/{email}` | полная замена клиента, не PATCH |
| `POST /panel/api/clients/del/{email}` | удалить из всех inbound; `keepTraffic=1` сохраняет счётчики |
| `POST /panel/api/clients/{email}/attach` | прикрепить существующего клиента |
| `POST /panel/api/clients/{email}/detach` | отсоединить без удаления записи |
| `GET /panel/api/clients/links/{email}` | готовые ссылки клиента по всем inbound |
| `GET /panel/api/clients/subLinks/{subId}` | готовые ссылки по subscription ID |
| `POST /panel/api/inbounds/{id}/delAllClients` | опасное массовое удаление клиентов inbound |

API 3x-ui также имеет bulk create/delete/enable/disable/attach/detach, traffic,
IP/HWID и online endpoints. Их не следует вызывать из Telegram handlers.
Сначала добавьте операцию в `api/app/services/threexui.py`, затем бизнес-правило
и аудит в VPN API, и только затем кнопку бота.

`clients/update/{email}` заменяет полный объект клиента. Нельзя отправлять один
изменённый атрибут как PATCH: пропущенные поля могут быть потеряны. Массовые
`delAllClients`, `bulkDel`, `delDepleted` и `delOrphans` необратимы и требуют
резервной копии и отдельной операторской процедуры.

## Локальная проверка

Создайте `.env` и проверьте конфигурацию:

```bash
python3 scripts/configctl.py validate
docker compose build bot
docker compose up -d bot
docker compose logs --tail=100 bot
```

Для быстрого unit-теста:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r api/requirements-dev.txt
.venv/bin/python -m unittest tests.test_bot_domain -v
```

Полный набор:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Перед тестом в Telegram убедитесь, что `/health` отвечает, нужные страны online,
а тестовый тариф не приводит к реальному списанию. Не используйте production
платёжную ссылку для автоматических тестов.

## Чек-лист изменения

1. Определите, является изменение контентом, UI или бизнес-логикой.
2. Тексты/ссылки меняйте в `content.json`; сетевые правила — только в VPN API.
3. Сохраните старые callback data либо обработайте их совместимость.
4. Добавьте тест чистой функции в `tests/test_bot_domain.py`.
5. Запустите unit-тесты и `docker compose build bot`.
6. Проверьте `/start`, возврат назад, покупку, личный кабинет, QR и перевыпуск.
7. Убедитесь, что логи не содержат ключей и токенов.
8. Выполните commit и push в `newnode`.
9. На сервере сделайте `git pull --ff-only`, пересоберите только `bot` и
   проверьте его логи.

Если менялись контракты API или формат ключа, пересоберите также `api` и
`worker`, а перед миграциями БД создайте backup.
