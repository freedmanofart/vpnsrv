# Настройка Telegram-бота

Интерфейс бота разделён на две части:

- рабочие callback-сценарии находятся в `bot/app/main.py`;
- тексты, ссылки на приложения и дополнительные URL-кнопки находятся в `bot/app/content.json`.

Так оператор может менять описания и обычные ссылки без изменения Python-кода. После любого изменения нужно проверить JSON и перезапустить только контейнер бота.

## Текущий пользовательский сценарий

Основная цепочка покупки:

1. «Оплатить».
2. Android, iOS, macOS, Windows, Linux или Android TV.
3. Официальная ссылка на AmneziaVPN.
4. Страна подключения.
5. `VLESS Reality` либо `VLESS + XTLS Vision`.
6. Один из трёх тарифов:
   - 2 недели — 200 ₽;
   - 1 месяц — 300 ₽;
   - 3 месяца — 600 ₽.
7. Ссылка ЮMoney или QR-код оплаты.
8. «Проверить оплату».
9. QR-код VPN-ключа и тот же URI текстом для копирования в AmneziaVPN.

Главное меню также содержит личный кабинет, промокод, инструкции, тестовый доступ за 50 ₽, поддержку и канал.

## Изменение текстов

На сервере файл расположен здесь:

```text
/home/freedman/vpn-service/bot/app/content.json
```

Секция `texts` содержит приветствие, главное меню, инструкцию, описание тестового доступа и сообщения для незаполненных ссылок. Перенос строки внутри JSON записывается как `\n`; HTML-разметка Telegram поддерживает, например, `<b>жирный текст</b>`.

Перед перезапуском проверить синтаксис:

```bash
cd /home/freedman/vpn-service
python3 -m json.tool bot/app/content.json >/dev/null
sudo docker compose restart bot
sudo docker compose logs --since=2m bot
```

Если JSON некорректен, бот не запустится; поэтому сначала выполняется проверка.

## Ссылки поддержки, канала и оплаты

В `content.json` используются ссылки вида `${SUPPORT_URL}`. Значение берётся из единого `.env`, поэтому рабочие URL не требуется записывать в Git.

Доступные переменные:

| Переменная | Назначение |
|---|---|
| `SUPPORT_URL` | поддержка |
| `TELEGRAM_CHANNEL_URL` | канал |
| `YOOMONEY_PAYMENT_URL` | общая запасная ссылка ЮMoney |
| `YOOMONEY_14D_URL` | оплата 2 недель |
| `YOOMONEY_30D_URL` | оплата 1 месяца |
| `YOOMONEY_90D_URL` | оплата 3 месяцев |
| `TRY_PAYMENT_URL` | тестовый доступ за 50 ₽ |
| `BOT_PLAN_CODES` | коды и порядок трёх тарифов |

Пример изменения через общий конфигуратор:

```bash
cd /home/freedman/vpn-service
python3 scripts/configctl.py set SUPPORT_URL 'https://t.me/example_support'
python3 scripts/configctl.py set YOOMONEY_14D_URL 'https://yoomoney.ru/to/...'
python3 scripts/configctl.py set YOOMONEY_30D_URL 'https://yoomoney.ru/to/...'
python3 scripts/configctl.py set YOOMONEY_90D_URL 'https://yoomoney.ru/to/...'
sudo docker compose up -d --force-recreate bot
```

QR оплаты строится из той же ссылки, поэтому URL и QR всегда совпадают.

## Редактирование устройств и клиентских ссылок

Массив `platforms` в `content.json` описывает кнопки Android, iOS, macOS, Windows, Linux и Android TV. Поля:

- `id` — короткий стабильный идентификатор без пробелов;
- `label` — название кнопки;
- `client` — имя рекомендуемого приложения;
- `url` — ссылка на загрузку или инструкцию;
- `description` — описание, которое показывается перед покупкой.

Сейчас используются официальная страница загрузок AmneziaVPN и официальная инструкция Android TV. Для добавления ещё одной ОС достаточно добавить объект в `platforms`; обработчики покупки менять не требуется.

## Добавление кнопок

### Обычная URL-кнопка главного меню

Добавить объект в `main_url_buttons`:

```json
"main_url_buttons": [
  {
    "text": "📰 Новости сервиса",
    "url": "https://t.me/example"
  }
]
```

Такие кнопки безопасно добавляются без Python-кода: они только открывают URL.

### Кнопка с новым действием

Кнопке, которая должна менять данные или открывать новый экран, нужен `callback_data` и обработчик `@router.callback_query(...)` в `bot/app/main.py`. После добавления обязательно:

1. отвечать на callback через `await callback.answer()` даже при ошибке;
2. использовать `show_screen(...)`, чтобы кнопка работала и под текстом, и под сообщением с QR-картинкой;
3. не помещать секреты или полный VPN URI в `callback_data`;
4. держать `callback_data` короче 64 байт;
5. добавить тест для преобразования параметров в `tests/test_bot_domain.py`.

## Тарифы

Миграция `a13f6c92d8e1` создаёт или обновляет тарифы с кодами `vpn_14d`, `vpn_30d`, `vpn_90d`. Бот показывает только коды из `BOT_PLAN_CODES` и сохраняет заданный там порядок. Старые тарифы не удаляются, чтобы не ломать существующие подписки.

## Reality, Vision, TCP, xHTTP и gRPC

В текущем production-потоке доступны два реально поддержанных профиля:

- VLESS Reality без `flow`;
- VLESS Reality с `flow=xtls-rprx-vision`.

TCP, xHTTP и gRPC являются транспортами Xray, а Vision — режимом VLESS flow. Нельзя просто добавить их названия в Telegram: для каждого транспорта сначала нужны рабочий inbound Xray, соответствующие поля `VPNNodeConfig`, генерация URI и E2E-проверка на каждой ноде. До этого бот намеренно не предлагает xHTTP/gRPC, чтобы не выдавать нерабочие ключи.

## Диагностика зависших кнопок

Сначала проверить последние события:

```bash
cd /home/freedman/vpn-service
sudo docker compose logs --since=10m bot api
```

Кнопки под QR-картинками должны открывать новый текстовый экран через `show_screen`. Перевыпуск на production-нодах не подключается к удалённому gRPC напрямую: API меняет desired state в PostgreSQL, а node-agent применяет его локально.
