# Frontend-структура сайта и web-кабинета Freedom VPN

Документ для фронтендера: где находится текущий интерфейс, какие страницы есть,
какие CSS-классы и API используются, и что важно не сломать при переносе в
отдельный frontend.

## Где лежит frontend

Сейчас сайт, web-кабинет и простая админка отдаются сервером FastAPI из одного
файла:

```text
api/app/api/routes/web.py    — лендинг, вход, кабинет, оплата, загрузка чека
api/app/api/routes/admin.py  — административная панель
api/app/static/              — статические файлы сайта
bot/app/static/              — статические файлы Telegram-бота
```

Основной сайтный логотип:

```text
api/app/static/freedom-vpn-logo-web.webp
```

Тёмный логотип для Telegram:

```text
bot/app/static/freedom-vpn-logo.png
```

## Страницы

### `/`

Публичный лендинг.

Секции:

- верхняя навигация;
- hero-блок;
- карточка-превью личного кабинета;
- блок тарифов;
- блок преимуществ;
- footer;
- modal выбора подписки.

Ключевые CSS-классы:

```text
.site-top
.f-nav
.f-brand
.f-hero
.f-kicker
.f-hero-actions
.f-preview
.f-preview-brand
.plans
.plan
.tier-groups
.tier-group
.duration-buttons
.duration
.modal
.plan-modal
```

Данные тарифов берутся из таблицы `plans` через backend. На лендинге показываются
только активные публичные месячные тарифы, а в modal выбора подписки тарифы
группируются по уровням:

- `Лайт`;
- `Стандарт`;
- `Ультра`.

### `/cabinet`

Web-кабинет пользователя.

Если пользователь не авторизован, эта же страница возвращает экран входа:

- email;
- переключатель `Код из письма` / `Пароль`;
- запрос кода;
- вход по коду;
- вход по паролю.

Если пользователь авторизован, показываются:

- статус подписки;
- оставшиеся дни;
- трафик;
- число подключений;
- VPN-ключ;
- ссылки на приложения;
- блок смены пароля;
- покупка или продление;
- последние платежи.

Ключевые CSS-классы:

```text
.login-page
.login-card
.login-tabs
.login-mode
.cabinet
.cabinet-grid
.panel
.stats
.stat
.key
.cabinet-apps
.cabinet-purchase
```

### `/cabinet/password`

Страница первичного создания пароля после входа по magic-link/token. Сейчас
пароль также можно менять прямо в кабинете.

### `/admin`

Административная панель.

Сейчас это server-rendered HTML внутри `api/app/api/routes/admin.py`.
Навигация боковая: верхние дублирующиеся metric-карточки убраны, все действия
идут через один sidebar-flow. Русские названия разделов:

- `Тарифы`;
- `VPN-ноды`;
- `Пользователи`;
- `Подписки`;
- `VPN-клиенты`;
- `Платежи`;
- `Способы оплаты`;
- `Устройства`;
- `Коды входа`;
- `Документация`;
- `Скрипты`;
- `Инфраструктура`;
- `Debug`;
- `Audit log`.

Ключевые CSS-классы админки:

```text
.layout
.sidebar
.sidebar-actions
.content
.table
.script-list
.script-item
.resource-list
.resource-item
.doc-list
.doc-item
```

Раздел `Документация` получает список из `state.docs`, раздел `Скрипты` —
`state.scripts`, раздел `Инфраструктура` — `state.resources`. Эти данные
формируются в `GET /admin/overview`.

Важно: админка показывает команды запуска скриптов, но не выполняет их сама.
Если нужен настоящий “запуск по кнопке”, нужен отдельный backend endpoint с
allow-list команд, журналированием, таймаутами и правами доступа. Текущий
вариант осознанно безопасный: оператор копирует команду в SSH.

## Frontend API

### Авторизация web-кабинета

```http
POST /web/register
```

Запрос кода на email.

```json
{
  "email": "user@example.com",
  "plan_id": 1
}
```

`plan_id` опционален.

```http
POST /web/code/login
```

Вход по одноразовому коду.

```json
{
  "email": "user@example.com",
  "code": "123456"
}
```

При успехе backend ставит cookie `freedom_cabinet`.

```http
POST /web/password/login
```

Вход по email и паролю.

```json
{
  "email": "user@example.com",
  "password": "password123"
}
```

```http
POST /web/password
```

Создание или смена пароля в кабинете.

```json
{
  "password": "password123"
}
```

Требует cookie `freedom_cabinet`.

```http
POST /cabinet/logout
```

Выход из кабинета.

### Платежи web-кабинета

Перед созданием платежа web-кабинет показывает отдельное окно «Оплата».
В нём пользователь видит выбранный тариф, срок, итоговую сумму и способ
оплаты. Только после подтверждения кабинет вызывает backend для ручной оплаты
или переводит пользователя на внешний URL способа оплаты.

```http
POST /web/payments/manual
```

Создать ручной платёж.

```json
{
  "plan_id": 1,
  "method_code": "sber_qr"
}
```

`node_id` можно не передавать: backend назначает сервер автоматически.

Ответ содержит:

```json
{
  "payment_id": 1,
  "status": "pending",
  "amount": "490.00",
  "currency": "RUB",
  "instructions": "...",
  "qr_url": "/web/payment-methods/1/image"
}
```

```http
GET /web/payment-methods/{method_id}/image
```

QR-картинка способа оплаты.

```http
POST /web/payments/{payment_id}/receipt
```

Загрузка чека.

```json
{
  "filename": "receipt.png",
  "mime_type": "image/png",
  "data_base64": "..."
}
```

Поддерживаются PNG, JPEG, WebP и PDF до 8 МБ.

### Публичные справочники

```http
GET /plans
GET /payment-methods
```

Используются ботом, кабинетом и админкой.

## Правила адаптива

В текущем CSS важные breakpoint’ы:

```text
900px — кабинет и stats складываются в одну колонку
850px — hero лендинга становится одной колонкой
760px — старые общие сетки переходят в одну колонку
520px — уплотнение hero/modal на телефонах
```

Для Telegram WebView особенно важно:

- не блокировать вертикальную прокрутку modal;
- использовать `100dvh`, `overflow-y:auto`, `-webkit-overflow-scrolling:touch`;
- не делать фиксированные широкие блоки без `min-width:0`.

## Что нельзя ломать

- Cookie кабинета называется `freedom_cabinet`; frontend не читает её напрямую,
  она `HttpOnly`.
- Код входа одноразовый и привязан к `user_id`.
- Пароли не показываются и не сохраняются в открытом виде.
- QR для `sber_qr` и `tbank_qr` должен быть загружен в админке, иначе создание
  платежа вернёт ошибку.
- Web-кабинет не должен показывать выбор страны: нода назначается автоматически.
- Тарифы должны быть сгруппированы по верхнему уровню `Лайт / Стандарт / Ультра`.

## Куда смотреть при изменениях

Перед frontend-правками полезно открыть:

```text
docs/web-cabinet.md
docs/latest-changes-2026-09-01.md
docs/editing-telegram-bot.md
docs/maintenance-scripts.md
```

Проверки после изменений:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/vpnsrv-pycache python3 -m py_compile api/app/api/routes/web.py api/app/api/routes/admin.py
bash -n scripts/check_mail_chain.sh
bash -n scripts/check_online_apis.sh
scripts/check_online_apis.sh
```
