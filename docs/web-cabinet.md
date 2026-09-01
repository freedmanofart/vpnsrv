# Лендинг и веб-кабинет Freedom VPN

## Назначение

Публичная страница `/` показывает только активные публичные тарифы из таблицы
`plans`. Поэтому цены, сроки, трафик и число одновременных подключений совпадают
с административной панелью и Telegram-ботом. Административная панель осталась
по адресу `/admin` и защищена HTTP Basic Authentication.

Белый сайтный вариант логотипа хранится в
`api/app/static/freedom-vpn-logo-web.webp` и используется в шапке, preview-карте
и кабинете. Тёмный `bot/app/static/freedom-vpn-logo.png` оставлен только для
Telegram и отправляется как фотография в приветственном сообщении `/start`.

Веб-кабинет `/cabinet` — резервный способ управления подпиской, когда Telegram
недоступен. Он использует те же записи `users`, `subscriptions`, `vpn_clients`,
`vpn_nodes` и `vpn_node_configs`, что API и бот. В кабинете отображаются:

- состояние и название подписки;
- число оставшихся дней;
- лимит трафика;
- число разрешённых одновременных подключений;
- действующий VLESS-ключ и локация;
- ссылки на приложения для Windows, macOS, Android и iOS;
- создание платежа без Telegram с выбором тарифа, страны и способа оплаты;
- QR/платёжные инструкции, загрузка чека и состояние последних платежей.

## Регистрация и вход

1. Пользователь выбирает тариф на лендинге и вводит email.
2. API нормализует email и создаёт веб-пользователя, если его ещё нет.
3. Генерируется криптографически случайный токен. В PostgreSQL хранится только
   SHA-256-хеш токена, срок действия, время создания и последнего использования.
4. На email отправляется ссылка вида
   `https://vpn.example.com/cabinet/access/<token>`.
5. При переходе API проверяет токен, записывает его в cookie с флагами
   `HttpOnly` и `SameSite=Strict`, затем убирает токен из адресной строки
   перенаправлением. Для HTTPS также включается `Secure`.
6. При первом подтверждённом входе пользователь может установить пароль. Он
   хранится как PBKDF2-SHA256-хеш с индивидуальной солью; после этого экран
   входа поддерживает и письмо, и пароль. Восстановление пароля начинается с
   новой ссылки на email.
7. Кнопка «Выйти» удаляет cookie. Срок ссылки задаёт
   `CABINET_TOKEN_TTL_DAYS`.

Ссылка равнозначна паролю: её нельзя пересылать или публиковать. Страницы
кабинета отправляют `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
CSP и запрет встраивания во frame. Для отзыва доступа установите
`revoked_at` нужной записи `cabinet_access_tokens`; интерфейс отзыва можно
добавить в административную панель позднее.

### Временная регистрация без email

Для отладки можно включить:

```dotenv
CABINET_ALLOW_TEMPORARY_REGISTRATION=true
```

На лендинге и странице входа появится кнопка «Зарегистрироваться без email».
Она создаёт отдельного временного пользователя, выпускает токен и сразу
сохраняет его в защищённой cookie браузера. Такой аккаунт не получает доступ к
чужой Telegram-подписке. Если cookie будет удалена, восстановить доступ без
привязанного email невозможно.

Режим не предназначен для постоянной публичной эксплуатации: анонимная
регистрация может создавать неограниченное число пустых пользователей. После
настройки SMTP установите `CABINET_ALLOW_TEMPORARY_REGISTRATION=false`.

## Настройка SMTP и внешнего relay

Добавьте в серверный `.env`:

```dotenv
PUBLIC_BASE_URL=https://vpn.example.com
CABINET_TOKEN_TTL_DAYS=365
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=vpn@example.com
SMTP_PASSWORD=<пароль-приложения-или-smtp-пароль>
SMTP_FROM=Freedom VPN <vpn@example.com>
SMTP_STARTTLS=true
SMTP_USE_SSL=false
```

Для SMTP через TLS сразу после подключения обычно используют порт `465`,
`SMTP_USE_SSL=true`, `SMTP_STARTTLS=false`. Для STARTTLS обычно используют порт
`587`, `SMTP_USE_SSL=false`, `SMTP_STARTTLS=true`.

Если `SMTP_HOST` или `SMTP_FROM` не заданы, API возвращает `503` и не сохраняет
новый токен. Это предотвращает создание доступа, который пользователь не
сможет получить. Пароль SMTP хранится только в `.env`, не в Git.

### Вариант 1: API подключается к relay напрямую

Это самый короткий путь. Укажите выданные почтовым провайдером hostname,
username и app password в `SMTP_*`, используйте подтверждённый адрес или домен в
`SMTP_FROM`, затем пересоздайте только API. Не используйте обычный пароль от
почтового ящика, если провайдер поддерживает отдельные app passwords.

### Вариант 2: API → локальный Postfix → authenticated smarthost

Этот вариант удобен, когда несколько локальных сервисов должны отправлять через
один relay. Скрипт `scripts/setup_postfix_relay.sh` настраивает только локальный
приём от Docker и сам по себе **не настраивает внешний relayhost**. Без
smarthost Postfix пытается доставлять почту напрямую; письма от `.ts.net` или от
домена без SPF/DKIM/DMARC часто отклоняются либо попадают в спам.

Сначала установите локальный Postfix:

```bash
sudo VPN_MAIL_HOSTNAME=fedora.taile485ac.ts.net scripts/setup_postfix_relay.sh
python3 scripts/configctl.py set SMTP_HOST host.docker.internal
python3 scripts/configctl.py set SMTP_PORT 25
python3 scripts/configctl.py set SMTP_FROM \
  'Freedom VPN <no-reply@fedora.taile485ac.ts.net>'
python3 scripts/configctl.py set SMTP_STARTTLS false
python3 scripts/configctl.py set SMTP_USE_SSL false
```

Postfix слушает только loopback и Docker bridge, поэтому не является публичным
open relay. Затем настройте у Postfix внешний authenticated smarthost. Пример
для relay с STARTTLS на порту 587:

```bash
sudo dnf install -y cyrus-sasl-plain
sudo postconf -e 'relayhost = [smtp.provider.example]:587'
sudo postconf -e 'smtp_sasl_auth_enable = yes'
sudo postconf -e 'smtp_sasl_password_maps = hash:/etc/postfix/sasl_passwd'
sudo postconf -e 'smtp_sasl_security_options = noanonymous'
sudo postconf -e 'smtp_tls_security_level = encrypt'
sudo postconf -e 'smtp_tls_CApath = /etc/pki/tls/certs'
sudo install -m 600 /dev/null /etc/postfix/sasl_passwd
sudoedit /etc/postfix/sasl_passwd
```

В `/etc/postfix/sasl_passwd` добавьте одну строку, не фиксируя её в Git:

```text
[smtp.provider.example]:587 relay-user@example.com:app-password
```

Примените и проверьте конфигурацию:

```bash
sudo postmap /etc/postfix/sasl_passwd
sudo chmod 600 /etc/postfix/sasl_passwd /etc/postfix/sasl_passwd.db
sudo postfix check
sudo systemctl restart postfix
sudo postconf relayhost smtp_sasl_auth_enable smtp_tls_security_level
sudo postqueue -p
```

После этого API по-прежнему обращается к локальному Postfix без SMTP AUTH:

```dotenv
SMTP_HOST=host.docker.internal
SMTP_PORT=25
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_STARTTLS=false
SMTP_USE_SSL=false
SMTP_FROM=Freedom VPN <verified-sender@example.com>
```

Доступ от контейнера разрешайте только фактической Docker-сети. Проверьте её
gateway через `docker inspect vpn-api`; фиксированные `172.17.0.1` и
`172.18.0.1` из установочного скрипта подходят не для каждой Compose-сети.
Успешный ответ relay (`250`) означает принятие письма relay-сервером, но ещё не
гарантирует inbox. Для доставки нужны подтверждённый sender и корректные SPF,
DKIM и DMARC домена.

После изменения переменных пересоздайте API:

```bash
docker compose up -d --build api
docker compose exec api alembic upgrade head
curl -fsS http://127.0.0.1:8000/health
```

Проверка полного пути:

```bash
curl -fsS -X POST http://127.0.0.1:8000/web/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"controlled-test-address@example.com"}'
sudo journalctl -u postfix --since '10 minutes ago' --no-pager
sudo postqueue -p
```

Если API вернул `200`, но письма нет, найдите в журнале конечный статус relay.
`status=sent` подтверждает передачу следующему серверу; `status=deferred`,
`SASL authentication failed`, `Relay access denied` и `Sender address rejected`
указывают соответственно на очередь, неверные credentials, запрет relay или
неподтверждённого отправителя.

## Временный доступ через Tailscale-IP

API продолжает слушать безопасный `127.0.0.1:8000`. Для доступа только из
tailnet используется Tailscale Serve, а для публикации того же адреса в
интернет — Tailscale Funnel:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8000
tailscale funnel --bg --yes 8000
```

Переменные для текущего мастера:

```dotenv
PUBLIC_BASE_URL=https://fedora.taile485ac.ts.net
WEB_CABINET_URL=https://fedora.taile485ac.ts.net/cabinet
```

При Serve адрес доступен только устройствам того же tailnet. При Funnel он
доступен публично; сертификат выпускается и обновляется самим Tailscale без
certbot и без файлов сертификата в приложении.
Tailscale-IP мастера — `100.102.21.123`, но TLS-сертификат выпущен на его
MagicDNS-имя `fedora.taile485ac.ts.net`. Поэтому в Telegram используется имя:
оно проходит проверку HTTPS и позволяет открыть кабинет как Web App. Обращение
к `https://100.102.21.123` приведёт к ошибке соответствия сертификата.

Проверка и отключение только этого listener:

```bash
tailscale serve status
tailscale serve --https=443 off
tailscale funnel status
tailscale funnel --https=443 off
```

Если сертификат Tailscale нужен отдельному nginx или другому файловому
listener, установите `scripts/renew_tailscale_cert.sh` и systemd units из
`deploy/systemd/vpn-tailscale-cert.*`. Скрипт атомарно обновляет
`/etc/ssl/tailscale/cert.pem` и `key.pem`; Funnel этот скрипт не использует.

## Связь с Telegram

Веб-регистрация создаёт самостоятельный аккаунт по email. Чтобы существующая
Telegram-подписка появилась в веб-кабинете, email должен быть добавлен именно к
существующей записи пользователя, а ссылка — создана для её `user_id` через
доверенный интерфейс (бот или администратор). Нельзя связывать аккаунты только
по введённому Telegram ID: это позволило бы похитить чужую подписку.

В боте доверенная привязка реализована кнопкой `✉️ Email` под приветствием.
Бот уже знает `telegram_id` отправителя и обращается с внутренним Bearer token к
`POST /web/telegram-cabinet-link`. API сохраняет email у существующего
пользователя и отправляет ссылку, поэтому в web-кабинете видна та же подписка,
платежи и VPN-клиент, что в Telegram.

## Покупка и продление без Telegram

В разделе «Приобрести или продлить» пользователь выбирает:

1. активный публичный тариф из `plans`;
2. активный способ оплаты из `payment_methods`.

Нода назначается автоматически; пользователю не показывается выбор страны.

API создаёт обычный платёж `manual_bank` с отметкой
`details.source=web_cabinet`. Если у способа оплаты загружен QR, кабинет
показывает его из PostgreSQL; иначе показывает URL или инструкции из поля
`payment_methods.url`. Пользователь загружает PNG, JPEG, WebP или PDF чека до
8 МБ. Чек сохраняется в `payments.receipt_data`, платёж переходит в
`processing` и появляется в административной панели вместе с Telegram-чеками.

После проверки оператор нажимает «Подтвердить» в `/admin`:

- если активной подписки нет, создаются подписка и клиент 3x-ui;
- если подписка активна, оплаченный срок прибавляется к текущему сроку,
  тариф и выбранная нода применяются к новому клиенту, предыдущий клиент
  отзывается;
- если подписка истекла, новый период считается от момента подтверждения.

Такой порядок не выдаёт VPN-доступ до фактической проверки перевода. Повторный
webhook/запрос подтверждения защищён уникальным событием и машиной состояний
платежа.

## Логи и обратный прокси

API маскирует токен в собственном журнале как `/cabinet/access/[redacted]`.
На публичном reverse proxy также отключите логирование полного URI для маршрута
`/cabinet/access/`, иначе резервная ссылка может попасть в access log. HTTPS
обязателен: без него cookie не получает флаг `Secure`.

## Проверка

```bash
curl -fsS http://127.0.0.1:8000/ | grep 'Freedom VPN'
curl -i http://127.0.0.1:8000/cabinet
curl -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" http://127.0.0.1:8000/admin
```
