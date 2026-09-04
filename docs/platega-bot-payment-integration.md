# Подключение Platega: бот и web-кабинет

Документ описывает, как подключить платежную систему Platega к Freedom VPN без
хранения секретов в коде. Реальные API-данные вводятся вручную в окружение или
админ-настройки.

Официальная документация:

- API: https://docs.platega.io/
- создание платежа: https://docs.platega.io/reference/create-transaction
- H2H QR: https://docs.platega.io/reference/get-h2h-payment-link
- статус платежа: https://docs.platega.io/reference/get-transaction
- callback: https://docs.platega.io/reference/callback

## Что нужно получить у Platega

1. `Merchant ID`.
2. `Secret/API key`.
3. Подтверждение доступных методов оплаты:
   - СБП (QR);
   - Карта МИР;
   - Криптовалюта.
4. Точные числовые `paymentMethod` для каждого метода.
5. Включение H2H, если нужен прямой QR-код в ответе API.
6. Включение крипто-оплаты в Telegram-боте, если Platega должна отдавать оплату
   без лишней внешней формы.
7. Callback URL для уведомлений об оплате.

Важно: в документации Platega пример СБП использует `paymentMethod: 2` и ответ
возвращает метод `SBPQR`. Для криптовалюты документация отдельно упоминает
`paymentMethod: 13`, но предупреждает, что без включения менеджером пользователь
будет перенаправлен на web-форму Platega. ID для карты МИР нужно подтвердить у
менеджера Platega перед включением.

## Переменные окружения

Секреты не коммитить. Заполнить вручную на сервере:

```env
PLATEGA_ENABLED=false
PLATEGA_BASE_URL=https://app.platega.io
PLATEGA_MERCHANT_ID=
PLATEGA_SECRET=

PLATEGA_RETURN_URL=
PLATEGA_FAILED_URL=
PLATEGA_CALLBACK_URL=https://freedomvpn.taile485ac.ts.net/payments/webhooks/platega

PLATEGA_METHOD_SBP_QR=2
PLATEGA_METHOD_MIR_CARD=
PLATEGA_METHOD_CRYPTO=13
BOT_USERNAME=vpn142323srv_bot
```

`PLATEGA_RETURN_URL` и `PLATEGA_FAILED_URL` можно оставить пустыми. Тогда:

- web-кабинет передаст Platega одноразовый signed return URL
  `/cabinet/payment-return?...&token=...`, чтобы после оплаты восстановить
  cookie кабинета;
- Telegram-покупка передаст return URL на бота
  `https://t.me/<BOT_USERNAME>`, чтобы пользователь не попадал в разлогиненный
  web-кабинет.

После заполнения ключей и проверки тестового платежа можно выставить:

```env
PLATEGA_ENABLED=true
```

## Подготовленные методы оплаты

### 1. СБП (QR)

Код внутри Freedom VPN:

```text
platega_sbp_qr
```

Название для пользователя:

```text
СБП (QR)
```

Настройки:

```text
provider: platega
currency: RUB
paymentMethod: 2
```

Если H2H включен, после создания платежа можно запросить QR:

```http
GET /h2h/{transactionId}
```

Если H2H не включен, пользователю отправляется `redirect` из ответа создания
платежа.

### 2. Карта МИР

Код внутри Freedom VPN:

```text
platega_mir_card
```

Название для пользователя:

```text
Карта МИР
```

Настройки:

```text
provider: platega
currency: RUB
paymentMethod: уточнить у менеджера Platega
```

По умолчанию пользователю отправляется ссылка `redirect` из ответа Platega.

### 3. Криптовалюта

Код внутри Freedom VPN:

```text
platega_crypto
```

Название для пользователя:

```text
Криптовалюта
```

Настройки:

```text
provider: platega
currency: RUB или валюта, согласованная с Platega
paymentMethod: 13
```

До включения крипто-метода менеджером Platega пользователь может уходить на
web-форму Platega. Это нормальное поведение по документации.

## Создание платежа

Базовый URL:

```text
https://app.platega.io
```

Авторизация идет через заголовки:

```http
X-MerchantId: <PLATEGA_MERCHANT_ID>
X-Secret: <PLATEGA_SECRET>
```

Запрос:

```http
POST /transaction/process
Content-Type: application/json
```

Шаблон тела:

```json
{
  "paymentMethod": 2,
  "paymentDetails": {
    "amount": 390,
    "currency": "RUB"
  },
  "description": "Freedom VPN: 1 мес (-3%)",
  "return": "https://freedomvpn.taile485ac.ts.net/cabinet?payment=success",
  "failedUrl": "https://freedomvpn.taile485ac.ts.net/cabinet?payment=failed",
  "payload": "payment_id=123",
  "metadata": {
    "userId": "3",
    "userName": "telegram_username",
    "clientIp": "127.0.0.1"
  }
}
```

`id` передавать не нужно: Platega создает свой `transactionId`.

Что сохранить в локальном платеже Freedom VPN:

```text
provider = platega
provider_payment_id = transactionId
status = pending
amount = paymentDetails.amount
currency = paymentDetails.currency
method_code = platega_sbp_qr / platega_mir_card / platega_crypto
details.redirect = redirect
details.expiresIn = expiresIn
details.paymentMethod = paymentMethod
details.payload = payload
```

## Callback от Platega

Callback URL:

```text
https://freedomvpn.taile485ac.ts.net/payments/webhooks/platega
```

Этот адрес нужно указать в личном кабинете Platega в настройках callback URLs.

Требования Platega:

- публичный HTTPS-домен;
- валидный SSL-сертификат;
- нельзя использовать `localhost`, приватный IP или самоподписанный сертификат;
- ответ сервера должен быть быстрее 60 секунд.

Platega присылает те же заголовки:

```http
X-MerchantId: <merchant id>
X-Secret: <secret>
```

И тело со статусом платежа.

Маппинг статусов:

```text
CONFIRMED   -> paid, выдать/продлить VPN
CANCELED    -> failed/cancelled, доступ не выдавать
CHARGEBACKED -> refunded/chargeback, отметить возврат
```

Callback должен быть идемпотентным: повторный callback по уже обработанному
`transactionId` не должен выдавать подписку второй раз.

Если Platega не получила успешный ответ, она делает до 3 повторных попыток с
интервалом 5 минут.

## Проверка статуса вручную

Если callback задержался или был недоступен:

```http
GET /transaction/{transactionId}
```

Ответ содержит статус, сумму, валюту, метод оплаты, QR/ссылку и другие поля.
Эту проверку можно использовать как резервный механизм в worker.

## Проверка платежей скриптом

В репозитории есть безопасный проверочный скрипт:

```text
scripts/check_platega_payment.py
```

Он использует переменные `PLATEGA_*`, создает тестовый платеж в Platega и сразу
запрашивает его статус. Секреты в вывод не печатаются.

Проверить СБП:

```bash
cd /home/freedman/vpn-service
docker cp scripts/check_platega_payment.py vpn-api:/tmp/check_platega_payment.py
docker exec vpn-api python /tmp/check_platega_payment.py --method sbp --amount 10 --currency RUB
```

Проверить карту МИР:

```bash
cd /home/freedman/vpn-service
docker cp scripts/check_platega_payment.py vpn-api:/tmp/check_platega_payment.py
docker exec vpn-api python /tmp/check_platega_payment.py --method mir --amount 10 --currency RUB
```

Если `PLATEGA_METHOD_MIR_CARD` пустой, скрипт пропустит МИР и напишет `SKIP`.
Это значит, что нужно получить у Platega числовой `paymentMethod` для карты МИР.

Проверить криптовалюту:

```bash
cd /home/freedman/vpn-service
docker cp scripts/check_platega_payment.py vpn-api:/tmp/check_platega_payment.py
docker exec vpn-api python /tmp/check_platega_payment.py --method crypto --amount 10 --currency RUB
```

Проверить все подготовленные методы:

```bash
cd /home/freedman/vpn-service
docker cp scripts/check_platega_payment.py vpn-api:/tmp/check_platega_payment.py
docker exec vpn-api python /tmp/check_platega_payment.py --method all --amount 10 --currency RUB
```

Ожидаемый успешный результат:

```text
CREATE_HTTP_STATUS=200
TRANSACTION_ID=<id платежа>
PAYMENT_METHOD=SBPQR / Crypto / ...
STATUS=PENDING
REDIRECT=https://pay.platega.io...
STATUS_HTTP_STATUS=200
STATUS_CHECK=PENDING
```

`PENDING` для теста создания — нормальный статус: платеж создан, но еще не
оплачен.

В админке этот скрипт добавлен в раздел `Скрипты` как:

```text
Проверить Platega: СБП, МИР, крипта
```

Текущая админка для host-only команд показывает точную команду запуска на
сервере. Если в ответе админки пришел статус `host_required`, нужно скопировать
команду из поля `command` и выполнить ее по SSH на сервере.

## UX в Telegram-боте

Рекомендуемый путь:

1. Пользователь нажимает `💳 Приобрести подписку`.
2. Выбирает устройство, страну, тариф и срок.
3. Выбирает метод:
   - `СБП (QR)`;
   - `Карта МИР`;
   - `Криптовалюта`.
4. Бот создает платеж в Platega.
5. Бот отправляет кнопку оплаты:
   - для СБП — QR/ссылку;
   - для МИР — ссылку на оплату картой;
   - для крипты — ссылку/форму Platega.
6. После callback `CONFIRMED` API автоматически выдаёт или продлевает VPN.
   Ручной апрув для Platega запрещён.

Текст ожидания:

```text
Оплатите заказ по ссылке ниже. После подтверждения платежа бот автоматически
выдаст или продлит VPN.
```

После оплаты:

```text
✅ Оплата Freedom VPN подтверждена.
```

## UX в web-кабинете

В web-кабинете методы должны отображаться как обычные способы оплаты:

- `СБП (QR)`;
- `Карта МИР`;
- `Криптовалюта`.

После выбора метода кабинет создаёт платеж в Platega и открывает полученный
`redirect`. Для web-кабинета backend передаёт signed return URL
`/cabinet/payment-return`, поэтому после возврата пользователь остаётся в своём
кабинете. Финальное подтверждение и выдача доступа всё равно идут только через
callback `CONFIRMED`.

## Админка

В админке нужно иметь возможность видеть:

- локальный ID платежа;
- `provider = platega`;
- `provider_payment_id / transactionId`;
- метод оплаты;
- статус;
- сумму;
- пользователя;
- дату создания;
- дату подтверждения;
- сырые данные callback/status для диагностики.

Если платеж завис в `pending`, администратор проверяет `transactionId` через
статус Platega. Вручную переводить Platega-платёж в `paid` нельзя: админка и
служебный endpoint возвращают ошибку, чтобы не обойти callback.

## Чеклист включения

1. Получить у Platega `Merchant ID` и `Secret`.
2. Уточнить `paymentMethod` для СБП, МИР и криптовалюты.
3. Попросить включить H2H QR, если QR нужен сразу в боте.
4. Попросить включить крипто-метод для Telegram, если нужен без промежуточной
   web-формы.
5. Внести переменные окружения на сервере.
6. Указать callback URL в Platega.
7. Перезапустить `api`, `bot`, `worker`.
8. Сделать тестовый платеж на минимальную сумму.
9. Проверить callback, выдачу VPN, email/Telegram-уведомления и запись в админке.

## Что уже реализовано в коде

1. Конфиг Platega через переменные окружения `PLATEGA_*`.
2. Клиент Platega для `POST /transaction/process`.
3. Webhook endpoint `POST /payments/webhooks/platega` с проверкой
   `X-MerchantId` и `X-Secret`.
4. Идемпотентная обработка статусов `CONFIRMED`, `CANCELED`, `CHARGEBACKED`.
5. Три метода оплаты в справочнике способов оплаты: СБП QR, МИР, криптовалюта.
6. Отображение методов в Telegram-боте и web-кабинете.
7. Автоматическое продление/выдача VPN после `CONFIRMED`.
8. Уведомления админу и клиенту после подтверждённой оплаты.
9. Логи `platega_webhook_received`, `platega_webhook_paid_notified`,
   `payment_paid_notification_sent`.
