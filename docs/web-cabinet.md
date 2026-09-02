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
3. Генерируется шестизначный код. Для проверки входа используется
   `code_hash`: HMAC-SHA-256 с серверным секретом и привязкой к `user_id`.
   Дополнительно в `plain_code` сохраняется сам короткоживущий код, чтобы
   администратор мог увидеть последние отправленные коды во время поддержки.
   Также сохраняются срок действия и число неудачных попыток.
4. Код отправляется на email и вводится в том же окне сайта. Действует только
   последний выданный код, по умолчанию 10 минут и не более пяти попыток.
5. `POST /web/code/login` одноразово погашает код и выпускает случайный session
   token. В браузер он попадает только как cookie с `HttpOnly`,
   `SameSite=Strict`, а при HTTPS — также `Secure`.
6. Пароль остаётся необязательным альтернативным способом входа и хранится как
   PBKDF2-SHA256-хеш с индивидуальной солью.
7. Кнопка «Выйти» удаляет cookie. Срок cookie-сессии задаёт
   `CABINET_TOKEN_TTL_DAYS`, срок email-кода —
   `CABINET_EMAIL_CODE_TTL_MINUTES`.

Код нельзя пересылать или сообщать поддержке. Страницы кабинета отправляют
`Cache-Control: no-store`, `Referrer-Policy: no-referrer`, CSP и запрет
встраивания во frame. Старый `/cabinet/access/{token}` временно сохранён только
для совместимости с уже отправленными ссылками; новые письма URL-токенов не
содержат.

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
PUBLIC_BASE_URL=https://freedomvpn.taile485ac.ts.net
CABINET_TOKEN_TTL_DAYS=365
CABINET_EMAIL_CODE_TTL_MINUTES=10
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
sudo VPN_MAIL_HOSTNAME=freedomvpn.taile485ac.ts.net scripts/setup_postfix_relay.sh
python3 scripts/configctl.py set SMTP_HOST host.docker.internal
python3 scripts/configctl.py set SMTP_PORT 25
python3 scripts/configctl.py set SMTP_FROM \
  'Freedom VPN <no-reply@freedomvpn.taile485ac.ts.net>'
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
PUBLIC_BASE_URL=https://freedomvpn.taile485ac.ts.net
WEB_CABINET_URL=https://freedomvpn.taile485ac.ts.net/cabinet
```

При Serve адрес доступен только устройствам того же tailnet. При Funnel он
доступен публично; сертификат выпускается и обновляется самим Tailscale без
certbot и без файлов сертификата в приложении.
Tailscale-IP мастера — `100.102.21.123`, но TLS-сертификат выпущен на его
MagicDNS-имя `freedomvpn.taile485ac.ts.net`. Поэтому в Telegram используется имя:
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
существующей записи пользователя, а код — создан для её `user_id` через
доверенный интерфейс (бот или администратор). Нельзя связывать аккаунты только
по введённому Telegram ID: это позволило бы похитить чужую подписку.

В боте доверенная привязка реализована кнопкой `✉️ Email` под приветствием.
Бот уже знает `telegram_id` отправителя и обращается с внутренним Bearer token к
`POST /web/telegram-cabinet-link`. API сохраняет email у существующего
пользователя и отправляет код, поэтому в web-кабинете видна та же подписка,
платежи и VPN-клиент, что в Telegram.

## Покупка и продление без Telegram

В разделе «Приобрести или продлить» пользователь выбирает:

1. активный публичный тариф из `plans`;
2. активный способ оплаты из `payment_methods`.

После выбора тарифа кабинет сразу показывает краткое резюме заказа:
название тарифа, срок, сумму и способ оплаты. Кнопка «Оплата» открывает
отдельное окно подтверждения, где эти данные повторяются крупным блоком перед
переходом к оплате или созданием ручного платежа.

Нода назначается автоматически; пользователю не показывается выбор страны.

API создаёт обычный платёж `manual_bank` с отметкой
`details.source=web_cabinet`. Если у способа оплаты загружен QR, кабинет
показывает его из PostgreSQL; иначе показывает URL или инструкции из поля
`payment_methods.url`. Пользователь загружает PNG, JPEG, WebP или PDF чека до
8 МБ. Чек сохраняется в `payments.receipt_data`, платёж переходит в
`processing` и появляется в административной панели вместе с Telegram-чеками.
При создании покупки API отправляет администратору карточку покупки в Telegram
и на email `ADMIN_NOTIFICATION_EMAIL`. При загрузке чека API отправляет вторую
карточку с приложенным чеком в тот же Telegram-чат и на ту же почту.

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

Для совместимости API маскирует токены старых ссылок в собственном журнале как
`/cabinet/access/[redacted]`. Новые email-коды передаются в JSON-теле POST и не
включаются в URL или структурированный журнал. HTTPS обязателен: без него
cookie не получает флаг `Secure`.

## Проверка почты и быстрый перезапуск цепочки

Для диагностики входа по email используйте серверный скрипт:

```bash
scripts/check_mail_chain.sh
```

Он проверяет:

- состояние `api`, `bot`, `worker`;
- `/health` API;
- SMTP-переменные внутри контейнера `api`;
- авторизацию на SMTP без отправки письма;
- наличие колонки `cabinet_login_codes.plain_code`, которая нужна админке для
  просмотра новых отправленных кодов;
- быстро пересоздаёт `api` и `bot`;
- показывает хвост логов по `smtp`, `email`, `mail`, `cabinet`, `503`,
  `error`, `failed`, `exception`.

Чтобы отправить контрольное письмо с кодом `000000`, явно передайте адрес:

```bash
scripts/check_mail_chain.sh user@example.com
```

Не используйте чужой адрес для теста без согласия владельца. Если SMTP идёт
через Mail.ru, `SMTP_FROM` должен совпадать с авторизованным ящиком, например:

```dotenv
SMTP_USERNAME=freedomvpn@list.ru
SMTP_FROM=Freedom VPN <freedomvpn@list.ru>
```

Если `SMTP_FROM` указывает на домен Tailscale или другой неподтверждённый
домен, внешний SMTP relay может принять логин, но отказать в отправке письма.

Для общей online-проверки ключевых API используйте:

```bash
scripts/check_online_apis.sh
scripts/check_online_apis.sh TELEGRAM_ID
```

Скрипт проверяет `docker compose ps`, локальный `/health`, публичный лендинг,
`/plans`, `/payment-methods`, `/admin/overview`, `tailscale funnel status`.
Если передать Telegram ID, дополнительно проверяются `/users/{telegram_id}` и
`/users/{telegram_id}/status` с внутренним service token.

В `/admin` раздел `Скрипты` содержит кнопки-команды для всех ключевых операций:
почта, быстрый restart `api`/`bot`, Tailscale certificate/Funnel, backup,
проверка backup и online-тесты API.

## Коды входа и пароли в админке

В `/admin` есть вкладка `login_codes`. Она показывает последние коды входа в
web-кабинет:

- `code` — отправленный 6-значный код для новых записей;
- `legacy_hash_only` — старый код, созданный до добавления поля `plain_code`;
- `status` — `active`, `used` или `expired`;
- `attempts`, `created_at`, `expires_at`, `used_at`.

Коды дают доступ к аккаунту до истечения срока, поэтому открывайте эту вкладку
только администраторам.

Пароли не отображаются и не хранятся в открытом виде. В таблице `users` видно
только состояние `password`: `set` или `not_set`. Чтобы сменить пароль вручную,
нажмите кнопку `Пароль` у пользователя, введите новый пароль минимум из 8
символов и подтвердите действие. Старый пароль сразу перестанет работать.

## Проверка

```bash
curl -fsS http://127.0.0.1:8000/ | grep 'Freedom VPN'
curl -i http://127.0.0.1:8000/cabinet
curl -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" http://127.0.0.1:8000/admin
```
