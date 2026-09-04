# Уведомления Freedom VPN

Документ описывает уведомления, которые сейчас есть в проекте, каналы отправки и условия срабатывания.

## Настройки

Общие email-настройки задаются переменными окружения API:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `SMTP_STARTTLS`
- `SMTP_USE_SSL`

Админские уведомления дополнительно используют:

- `ADMIN_NOTIFICATION_EMAIL`
- `BOT_TOKEN`
- `BOT_ADMIN_CHAT_ID`

`BOT_ADMIN_CHAT_ID` должен быть numeric ID личного чата администратора или
группы поддержки, куда добавлен бот. Публичный канал `TELEGRAM_CHANNEL_URL` не
используется для покупок и чеков. Чтобы узнать ID, отправьте боту команду
`/chatid` в нужном личном чате или группе и сохраните число в
`VPN Admin -> Настройки -> Telegram admin chat ID`.

Фоновые уведомления по подпискам отправляет worker, поэтому должны быть включены:

- `BACKGROUND_JOBS_ENABLED=true`
- `SUBSCRIPTION_EXPIRATION_REMINDER_DAYS=3`

`SUBSCRIPTION_EXPIRATION_REMINDER_DAYS` задаёт, за сколько дней до окончания активной услуги отправлять письмо клиенту. По умолчанию используется 3 дня.

## Клиентские email-уведомления

### Код входа в web-кабинет

Когда отправляется:

- пользователь запрашивает вход в web-кабинет по email;
- Telegram-пользователь привязывает email через бота и получает код входа.

Тема письма:

- `Вход в кабинет Freedom VPN`

Содержит:

- одноразовый шестизначный код;
- срок действия кода;
- ссылку входа в web-кабинет;
- ссылку на продление: `/cabinet?checkout=1#payment`;
- призыв продлить доступ, если подписка скоро закончится или уже закончилась;
- предупреждение никому не передавать код.

Код действует `CABINET_EMAIL_CODE_TTL_MINUTES`, по умолчанию 10 минут.

Журнал:

- audit action `email.cabinet_code.send`

### Услуга скоро закончится

Когда отправляется:

- подписка активна;
- до окончания осталось не больше `SUBSCRIPTION_EXPIRATION_REMINDER_DAYS`;
- у пользователя указан email;
- по этой подписке ещё не было успешного уведомления `email.subscription_expiring.send`.

Тема письма:

- `Freedom VPN: услуга скоро закончится`

Содержит:

- название тарифа;
- дату окончания;
- сколько дней осталось;
- ссылку входа в web-кабинет;
- ссылку на продление: `/cabinet?checkout=1#payment`;
- призыв зайти в web-кабинет и продлить доступ.

Журнал:

- audit action `email.subscription_expiring.send`
- если у пользователя нет email: `email.subscription_expiring.skip` с причиной `user_email_missing`

### Услуга закончилась

Когда отправляется:

- подписка уже в статусе `expired`;
- у пользователя указан email;
- по этой подписке ещё не было успешного уведомления `email.subscription_expired.send`.

Тема письма:

- `Freedom VPN: услуга закончилась`

Содержит:

- название тарифа;
- дату окончания;
- ссылку входа в web-кабинет;
- ссылку на продление: `/cabinet?checkout=1#payment`;
- призыв зайти в web-кабинет и продлить доступ.

Журнал:

- audit action `email.subscription_expired.send`
- если у пользователя нет email: `email.subscription_expired.skip` с причиной `user_email_missing`

## Админские уведомления

### Новая покупка

Когда отправляется:

- создан новый ручной платёж через web-кабинет, Telegram или другой платежный
  поток, который вызывает `notify_payment_created`;
- для Platega pending/processing платежей это уведомление не отправляется:
  Platega уведомляет только после фактической оплаты.

Каналы:

- Telegram админу, если настроены `BOT_TOKEN` и `BOT_ADMIN_CHAT_ID`;
- email админу, если настроены `ADMIN_NOTIFICATION_EMAIL`, `SMTP_HOST` и `SMTP_FROM`.

Email-тема:

- `Новая покупка Freedom VPN #{payment_id}`

Содержит:

- номер платежа;
- статус;
- сумму;
- тариф;
- способ оплаты;
- источник;
- ноду;
- Telegram ID, username и email пользователя.
- inline-кнопки `Подтвердить`, `Ошибка`, `Отменить`, если платёж ожидает проверки.

Кнопка `Подтвердить` вызывает ботом служебный endpoint `POST /payments/{id}/status`
с Bearer token и создаёт/продлевает VPN-доступ без входа в админку.

### Подтверждённая оплата Platega

Когда отправляется:

- Platega прислала callback `CONFIRMED`;
- backend успешно перевёл платеж в `paid` и создал/продлил подписку.

Каналы:

- Telegram админу;
- email админу;
- Telegram клиенту, если у пользователя есть `telegram_id`;
- email клиенту, если у пользователя есть email и настроен SMTP.

Содержит:

- номер платежа;
- сумму;
- тариф;
- способ оплаты;
- ссылки на web-кабинет и продление;
- статус/дату действия подписки, если она уже привязана к платежу.

Журнал:

- audit action `payment_paid_notification`;
- логи `platega_webhook_received`, `platega_webhook_paid_notified`,
  `payment_paid_notification_sent`.

### Загружен чек

Когда отправляется:

- пользователь загрузил чек по ручному платежу в web-кабинете.

Каналы:

- Telegram админу с фото или документом;
- email админу с вложением чека.

Email-тема:

- `Чек по платежу Freedom VPN #{payment_id}`

Содержит:

- карточку платежа;
- вложение с чеком.
- inline-кнопки `Подтвердить`, `Ошибка`, `Отменить`, если платёж ожидает проверки.

## Защита от повторов

Email-уведомления по окончанию подписок защищены от повторной отправки через `audit_logs`.

Если письмо отправлено успешно, worker больше не отправляет такой же тип письма для этой подписки. Если у пользователя нет email, worker пишет `skipped` и тоже не повторяет этот пропуск каждый цикл. Если отправка завершилась ошибкой, в audit пишется failure, и worker попробует снова в следующем цикле.

## Где смотреть историю

Историю можно смотреть в админке:

- VPN Admin -> Audit
- VPN Admin -> Email logs

Основные action-коды:

- `email.cabinet_code.send`
- `email.subscription_expiring.send`
- `email.subscription_expiring.skip`
- `email.subscription_expired.send`
- `email.subscription_expired.skip`
- `payment_paid_notification`
- `lifecycle.cycle`
