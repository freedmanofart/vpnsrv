# Обслуживание

## Состав production

Каталог документации и переходы к инструкциям сгруппированы в
[`docs/README.md`](README.md).

```text
postgres
vpn-api
vpn-bot
vpn-worker
3x-ui master (systemd)
```

Redis и мониторинг отсутствуют. PostgreSQL — общий контейнер `postgres`, данные
VPN находятся только в отдельной БД `vpn`.
Актуальная схема таблиц, связи и безопасные команды диагностики описаны в
[`database-reference.md`](database-reference.md).

## Проверка состояния

```bash
cd /home/freedman/vpn-service
docker compose ps
curl -fsS http://127.0.0.1:8000/health
docker compose logs --since=10m api bot worker
systemctl status x-ui vpn-threexui-proxy.service --no-pager
```

Worker в норме пишет `xray_errors=0`.

## Единый операторский flow

Основной путь обслуживания теперь собран в `/admin`:

1. Откройте `https://freedomvpn.taile485ac.ts.net/admin`.
2. В боковом меню выберите нужный раздел:
   - `Пользователи` — доступ, перевыпуск ключа, ручная смена пароля;
   - `Платежи` — чеки, подтверждение, ошибка, отмена, возврат;
   - `Коды входа` — последние email-коды web-кабинета;
   - `Документация` — основные markdown-файлы проекта;
   - `Health` — дашборд состояния API, PostgreSQL, публичного URL, SMTP и 3x-ui;
   - `Скрипты` — запуск безопасных проверок и команды host-only операций;
   - `Инфраструктура` — сайт, кабинет, admin и master 3x-ui из конфигурации нод.
3. Если проблема связана с письмами, сначала выполните проверку почтовой цепочки.
4. Если проблема шире, запустите online-тесты ключевых API.
5. Перед опасными изменениями или обновлением сделайте backup.

Админка запускает только заранее разрешённые проверки, которые можно выполнить
из API-контейнера: health-dashboard, public probes, SMTP-login и проверку 3x-ui
через API нод. Операции уровня хоста — `docker compose`, `tailscale`,
PostgreSQL backup/restore — остаются host-only: кнопка возвращает точную
команду для SSH-сессии на сервере. Docker socket намеренно не проброшен в
web-админку.

## Health-dashboard

Раздел `/admin` → `Health` показывает состояние:

- API-контейнера;
- PostgreSQL (`SELECT 1`);
- публичного сайта через `PUBLIC_BASE_URL`;
- web-кабинета `/cabinet`;
- админки `/admin`;
- `/plans` и `/payment-methods`;
- SSL-сертификата master/site по `PUBLIC_BASE_URL`;
- SSL-сертификатов VPN-нод: по HTTPS `vpn_node_configs.config.api_address`,
  а если API-адрес внутренний HTTP — по публичному `vpn_nodes.ip_address:443`;
- механизма обновления master-сертификата: `vpn-tailscale-cert.service` и
  `vpn-tailscale-cert.timer`. Host-таймер
  `vpn-tailscale-cert-health.timer` каждые 5 минут записывает результат в
  `/var/backups/vpn-service/tailscale-cert-health.json`, который доступен
  API-контейнеру через существующий bind mount;
- отдельные host-проверки Tailscale: `tailscaled`, Funnel service и watchdog,
  backend и self node, online ingress peers, UDP/DERP,
  Funnel-конфигурация, локальный API, ответы публичных DNS-резолверов, TLS и
  маршруты `/`, `/cabinet`, `/admin`, `/plans`, `/payment-methods` через каждый
  публичный Funnel relay. Для закрытых операторских маршрутов ожидается HTTP
  401. REALITY-ноды проверяются по доступности TCP listener, поскольку их SNI
  не использует обычную проверку PKI-сертификата. Результаты
  `vpn-tailscale-health.timer` обновляет каждые 2 минуты в
  `/var/backups/vpn-service/tailscale-health.json`;
- SMTP-логина для писем web-кабинета;
- каждой активной VPN-ноды через её `api_address` master 3x-ui.

`vpn-admin-health-alerts.timer` каждые 2 минуты вызывает все проверки
операторского Health-dashboard. Telegram-группа из `admin_contacts` получает
сообщение при изменении статуса любой проверки, включая восстановление. Там же
появляются новые записи audit log. Детали audit-записей и IP-адреса в группу не
передаются; успешные пустые циклы worker сворачиваются, чтобы не засорять чат.
При недоступности API хостовый runner отправляет отдельное уведомление и
сообщает о восстановлении.

Git-коммиты отправляются post-commit hook в `.githooks`. Чтобы включить его в
другом clone:

```bash
git config core.hooksPath .githooks
```

Коммит содержит ветку, SHA, автора и subject. Сообщение Codex можно отправить в
эту же группу через `scripts/notify_admin_event.py assistant`, передав текст в
stdin. Автоматической подписки на сообщения интерфейса Codex у backend-сервиса
нет, поэтому сообщения отправляются этим явным вызовом.

На master новые systemd units устанавливаются так:

```bash
install -m 0644 deploy/systemd/vpn-tailscale-health.service /etc/systemd/system/
install -m 0644 deploy/systemd/vpn-tailscale-health.timer /etc/systemd/system/
install -m 0644 deploy/systemd/vpn-admin-health-alerts.service /etc/systemd/system/
install -m 0644 deploy/systemd/vpn-admin-health-alerts.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vpn-tailscale-health.timer vpn-admin-health-alerts.timer
```

Если 3x-ui открыт только через SSH proxy, адрес в разделе `Инфраструктура`
помечается как внутренний/proxy SSH адрес. Для ноды важен именно `api_address`
из `vpn_node_configs`: health проверяет этот VPN API и обновляет
`health_status`, `latency_ms`, `last_seen_at` у ноды.

`401` на `/admin` или защищённых API считается рабочим ответом: endpoint
доступен снаружи, а авторизация корректно закрывает данные.

## Быстрая проверка почты и переподнятие цепочки

В `/admin` → `Скрипты` кнопка `Проверить почту и быстро переподнять api/bot`
выполняет доступную из web-админки часть:

- проверяет ответ API;
- проверяет PostgreSQL;
- гарантирует наличие `cabinet_login_codes.plain_code`;
- проверяет SMTP-login;
- показывает последние 10 кодов входа и их статус.

Для фактического recreate `api`/`bot` кнопка возвращает готовую SSH-команду:
контейнер не должен сам перезапускать Docker-хост.

```bash
cd /home/freedman/vpn-service
scripts/check_mail_chain.sh
```

Скрипт делает:

- `docker compose ps api bot worker`;
- `curl http://127.0.0.1:8000/health`;
- вывод SMTP-настроек внутри `api` без пароля;
- проверку SMTP-login без отправки письма;
- добавление `cabinet_login_codes.plain_code`, если колонка ещё не создана;
- быстрый recreate `api` и `bot`;
- вывод последних mail/cabinet/error логов.

Для реального тестового письма явно передайте адрес:

```bash
scripts/check_mail_chain.sh user@example.com
```

Контрольное письмо содержит код `000000`. Не отправляйте его на чужие адреса
без согласия владельца.

## Online-тесты ключевых API

```bash
cd /home/freedman/vpn-service
scripts/check_online_apis.sh
```

Проверяются:

- `docker compose ps api bot worker`;
- локальный `/health`;
- публичный лендинг через `PUBLIC_BASE_URL`;
- `/plans`;
- `/payment-methods`;
- `/admin/overview` с `ADMIN_USERNAME`/`ADMIN_PASSWORD`;
- `tailscale funnel status`.

Для проверки пользовательских endpoints передайте Telegram ID:

```bash
scripts/check_online_apis.sh 106123347
```

Тогда дополнительно проверяются:

- `/users/{telegram_id}`;
- `/users/{telegram_id}/status`.

## Проверка реального трафика VPN-клиента

Скрипт `scripts/check_vpn_traffic.py` читает клиента из БД `vpn`, находит его
ноду и берёт live-счётчики из `clientStats` 3x-ui. Запускайте его внутри
`api`-контейнера, чтобы использовались production `DATABASE_URL` и
`THREEXUI_API_TOKEN`.

Проверить конкретного клиента:

```bash
cd /home/freedman/vpn-service
docker compose exec -T api python scripts/check_vpn_traffic.py vpn-80
```

Можно указывать ID без префикса и несколько клиентов сразу:

```bash
docker compose exec -T api python scripts/check_vpn_traffic.py 80 85
```

Показать последние выданные VPN-клиенты:

```bash
docker compose exec -T api python scripts/check_vpn_traffic.py --limit 20
```

JSON-вывод для копирования в диагностику:

```bash
docker compose exec -T api python scripts/check_vpn_traffic.py vpn-80 --json
```

В выводе:

- `db_client_status` и `db_subscription_status` — состояние в нашей БД;
- `panel_enabled` — включён ли клиент в 3x-ui;
- `client_used`, `client_left`, `client_limit` — реальные счётчики клиента;
- `last_online` — последнее подключение по данным 3x-ui;
- `inbound_used` — общий трафик inbound на ноде.

Если `inbound_used` растёт, а у клиента `client_used=0` и `last_online=—`,
значит конкретный ключ не подключался или 3x-ui не атрибутирует трафик этому
email. Если клиент есть в БД, но `panel: not found`, проверьте синхронизацию
ноды, SSL/API 3x-ui и worker.

## RU-нода: профили и раздельная маршрутизация

Действующая RU child — `176.108.250.142`, сервис `x-ui` на хосте. Прежняя
`151.248.113.240` выключена и не используется для новых ссылок. На master
RU child зарегистрирована как node 8, а в БД сервиса логическая RU-нода имеет
ID 4. Пользовательские ссылки указывают на буквальный IP; TLS SNI и REALITY
SNI задаются отдельно.

| Профиль | Порт | ID в master 3x-ui | Тег на RU child |
| --- | --- | --- | --- |
| Россия (Reality) | TCP 443 | 28 | `in-443-tcp` |
| Россия (Reality-2) | TCP 23691 | 29 | `in-23691-tcp` |
| Россия (Hysteria2) | UDP 443 | 30 | `in-443-udp` |
| Россия (xHTTP) | TCP 8443 | 31 | `in-8443-tcp` |
| Россия (VLESS WebSocket) | TCP 2081 | 32 | `in-2081-tcp` |
| Россия (Trojan gRPC) | TCP 2083 | 33 | `in-2083-tcp` |
| Россия (VMess WebSocket) | TCP 2087 | 34 | `in-2087-tcp` |
| Россия (TUIC) | UDP 8444 | 35 | `in-8444-udp` |
| Россия (WireGuard) | UDP 51820 | 36 | `in-51820-udp` |

В `VPNNodeConfig` RU `alternate_profile_links=true`, `share_address` и `host`
равны действующему IP, `kit_inbound_ids` содержат 29–36. У SE конфигурации
`alternate_node_ids=[4]`. Одна личность `vpn-<client_id>` прикрепляется к
SE и RU входящим через master. KIT-подписка и формат мобильного профиля могут
показывать разное подмножество протоколов. Старые RU inbound 10 и 25–27
сохранены в master для отката, но активные клиенты от них отсоединены.

Маршрутизация хранится в `xrayTemplateConfig` самого RU child. Порядок правил
важен: Xray использует первое совпадение.

1. Служебный API, запрет private IP и BitTorrent.
2. `ext:geosite_RU.dat:ru-blocked` — в `cascade-sweden`.
3. `ext:geosite_RU.dat:ru-available-only-inside` и `geosite:category-ru` — в `direct`.
4. `geoip:ru` — в `direct`.
5. Остальной трафик всех девяти RU-тегов — в `cascade-sweden` на
   `89.127.212.239:2443`.

`domainStrategy=AsIs`: доменные правила проверяют переданное или распознанное
имя, GeoIP — адрес назначения. На входящих включён sniffing HTTP/TLS/QUIC;
текущее `routeOnly=false`. Для `yandex.ru` и `77.88.8.8` встроенный routeTest
17 октября вернул `direct`, для `youtube.com` и `8.8.8.8` —
`cascade-sweden` на всех девяти тегах. Это подтверждает выбор правила, но
не измеряет путь реального пользовательского соединения.

### Загрузка и обновление списков

Xray читает бинарные `.dat` из `/usr/local/x-ui/bin` на RU-хосте. HTTP-адрес
списка нельзя вставить непосредственно в `routing`.
`geosite:category-ru` использует `geosite.dat`, `geoip:ru` — `geoip.dat`;
префикс `ext:geosite_RU.dat:` явно выбирает дополнительный файл.
`geoip_RU.dat` содержит списки блокировок, а не замену страновой категории RU.

Источник дополнительных российских списков:
[runetfreedom/russia-v2ray-rules-dat](https://github.com/runetfreedom/russia-v2ray-rules-dat).
Ветка `release` публикует актуальный `geosite.dat`; в 3x-ui он устанавливается
под именем `geosite_RU.dat`. Файл уже присутствует на RU child, категории
проверены API панели. Новый отдельный cron для скачивания не добавлялся.

При обновлении сначала сохраните действующие `.dat` и конфигурацию, затем
обновите нужный Geo-файл средствами 3x-ui. Проверьте категории через
`POST /panel/api/xray/geodata/validate`, конфигурацию через
`xray run -test -format json -config ...` и перезагрузите ядро для перечитывания
файлов. После обновления используйте `POST /panel/api/xray/routeTest`
(form-поля `domain` или `ip`, `inboundTag`, `port`, `network`):
`yandex.ru`, `gosuslugi.ru`, `77.88.8.8` должны выбрать `direct`,
`youtube.com`, `8.8.8.8` — `cascade-sweden` для каждого RU-тега.
Проверка маршрута не заменяет тест реального трафика.

TLS-профили RU используют сертификат для `176.108.250.142.nip.io` в
`/root/cert/kit-nipio/`; после его продления проверяйте пути сертификата в
inbound 3x-ui и результат внешней TLS-проверки. Не запускайте hook
`sync-ru-xui-cert.sh` со старой `imba.shop` ноды на новом сервере.

## Обновление SSL-сертификатов master и 3x-ui нод

В `/admin` → `Скрипты` есть:

- `Проверить Tailscale cert service/timer` — показывает актуальный host-снимок
  состояния `vpn-tailscale-cert.service`, `vpn-tailscale-cert.timer`, срока
  сертификата, следующего запуска и последнего срабатывания;
- `Обновить SSL-сертификат master/site` — возвращает host-only команду
  `scripts/renew_master_cert.sh`;
- `Обновить SSL-сертификат ноды #...` — отдельная кнопка для каждой активной
  VPN-ноды из БД. Команда копирует node-скрипт на выбранную ноду, включает
  timer и запускает renew.

Для master используется Tailscale certificate:

```bash
cd /home/freedman/vpn-service
install -m 0755 scripts/check_tailscale_cert_units.sh \
  /home/freedman/vpn-service/scripts/check_tailscale_cert_units.sh
install -m 0644 deploy/systemd/vpn-tailscale-cert-health.service \
  deploy/systemd/vpn-tailscale-cert-health.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vpn-tailscale-cert-health.timer
systemctl start vpn-tailscale-cert-health.service
systemctl status vpn-tailscale-cert.service vpn-tailscale-cert.timer --no-pager
systemctl status vpn-tailscale-cert-health.service vpn-tailscale-cert-health.timer --no-pager
systemctl list-timers vpn-tailscale-cert.timer --all --no-pager
journalctl -u vpn-tailscale-cert.service -n 40 --no-pager
cat /var/backups/vpn-service/tailscale-cert-health.json
sudo scripts/renew_master_cert.sh
```

API считает снимок устаревшим через 15 минут. Если health-таймер не работает,
в админке будет `degraded`; если renew-таймер выключен, последний renew завершён
ошибкой или сертификат отсутствует/истёк — `offline`. Срок менее 7 дней даёт
`degraded`.

Для child-ноды с short-lived Let's Encrypt certificate на IP используется
node-скрипт `deploy/node/renew_3xui_ip_cert.sh`, который устанавливается на
саму ноду в `/usr/local/sbin/renew_3xui_ip_cert.sh`. Timer
`vpn-3xui-ip-cert-renew.timer` запускается каждые 5 дней, чтобы сертификат,
живущий примерно 6 дней, не успевал истечь.

Подробная установка, проверка и команды логов описаны в
[`3x-ui-master.md`](3x-ui-master.md#ssl-на-ip-child-ноды).

## Логи

Быстрые команды:

```bash
docker compose logs --tail=200 api bot worker
docker compose logs --since=20m api | grep -i -E 'smtp|email|mail|cabinet|503|error|failed|exception' || true
journalctl -u vpn-tailscale-cert.service -n 80 --no-pager
tailscale funnel status
```

При успешной отправке email-кода API пишет событие
`cabinet_login_code_sent`. При ошибке отправки — `cabinet_login_code_email_failed`.
Пароли в логи не пишутся.

## Быстрое переподнятие Tailscale

Если сайт открывается внутри tailnet, но не открывается снаружи, чаще всего
нужно проверить связку `tailscaled` → Funnel → публичный HTTPS-адрес. Для этого
добавлен host-only скрипт:

```bash
cd /home/freedman/vpn-service
sudo scripts/recover_tailscale.sh
```

Скрипт использует домен `freedomvpn.taile485ac.ts.net` по умолчанию и делает:

- перезапуск `tailscaled.service`;
- вывод короткого `tailscale status`;
- сброс старой публикации `tailscale funnel reset`;
- запуск `vpn-tailscale-cert.service` или прямое обновление сертификата через
  `scripts/renew_tailscale_cert.sh`;
- перезапуск `vpn-tailscale-funnel.service` или прямой запуск Funnel на
  `http://127.0.0.1:8000`;
- вывод `tailscale funnel status`;
- проверку локального `/health`;
- проверку публичного `https://freedomvpn.taile485ac.ts.net/`.

Переопределяемые переменные:

```bash
TAILSCALE_CERT_DOMAIN=freedomvpn.taile485ac.ts.net
PUBLIC_BASE_URL=https://freedomvpn.taile485ac.ts.net
TAILSCALE_FUNNEL_TARGET=http://127.0.0.1:8000
TAILSCALE_CERT_SERVICE=vpn-tailscale-cert.service
TAILSCALE_FUNNEL_SERVICE=vpn-tailscale-funnel.service
```

В `/admin` → `Скрипты` кнопка `Быстро переподнять Tailscale, Funnel и сертификат`
возвращает готовую SSH-команду. Она не выполняется внутри API-контейнера, потому
что контейнер не должен управлять host-level `systemd`, `tailscaled` и Funnel.

## Конфигурация

```bash
python3 scripts/configctl.py validate
python3 scripts/configctl.py list
python3 scripts/configctl.py get THREEXUI_API_TOKEN
python3 scripts/configctl.py set THREEXUI_VERIFY_TLS true
python3 scripts/configctl.py apply --services api worker
```

`.env` должен иметь права `0600`. Не используйте `set -x`.

### Ротация секретов

`configctl rotate` — единая операция для переменных, которыми владеет сам
проект. Она генерирует новые значения в памяти, записывает `.env` атомарно,
проверяет итоговую конфигурацию и пересоздаёт все сервисы, которым нужны новые
значения. В вывод секреты не попадают. Если `docker compose up` не удался,
скрипт восстанавливает прежний `.env` и пытается вернуть прежнюю конфигурацию
тех же сервисов.

Сначала всегда можно посмотреть план без изменений:

```bash
python3 scripts/configctl.py rotate --all-internal --dry-run
```

Обычная внутренняя ротация меняет `SERVICE_API_TOKEN` и `ADMIN_PASSWORD` и
пересоздаёт `api`, `bot` и `worker` одной командой:

```bash
python3 scripts/configctl.py rotate --all-internal
```

После неё операторам нужно заново аутентифицироваться в Basic Auth, а внешние
скрипты и интеграции с `SERVICE_API_TOKEN` должны получить новое значение из
защищённого хранилища. Не выводите это значение через `get --show-secret` в
терминал с записью истории.

Можно выбрать конкретный внутренний секрет:

```bash
python3 scripts/configctl.py rotate ADMIN_PASSWORD
```

`PAYMENT_WEBHOOK_SECRET` передаётся стороннему платёжному провайдеру, поэтому
он намеренно исключён из локальной генерации: сервер не может сам сообщить
провайдеру новое значение. Сначала перевыпустите или смените секрет в панели
провайдера, затем внесите выданное значение и пересоздайте нужные сервисы:

```bash
python3 scripts/configctl.py set PAYMENT_WEBHOOK_SECRET '<provider-issued-secret>'
python3 scripts/configctl.py apply --services api worker
```

Токены `BOT_TOKEN`,
`THREEXUI_API_TOKEN`, SMTP-пароль и `DATABASE_URL` не генерируются локально:
их надо перевыпустить у Telegram, 3x-ui, почтового провайдера или PostgreSQL,
внести через `configctl set` и выполнить `configctl apply` для затронутых
сервисов. В частности, произвольный `THREEXUI_API_TOKEN` не будет принят
мастер-узлом 3x-ui.

## Backup

В `/admin` → `Скрипты` есть активные кнопки:

- `Создать PostgreSQL backup сейчас` — запускает `pg_dump -Fc` из
  API-контейнера и кладёт dump в `/var/backups/vpn-service`;
- `Проверить последний PostgreSQL backup` — запускает `pg_restore --list`;
- `Показать последние PostgreSQL backup-файлы` — выводит список dump-файлов.

Для этого API-контейнер имеет `postgresql-client` и volume
`/var/backups/vpn-service:/var/backups/vpn-service`.

`backup.sh` создаёт два вида backup за один запуск:

- custom-format dump БД `vpn` из контейнера `postgres`;
- защищённый архив `.env` и `docker-compose.yml`.

Скрипт хранит три последние полные точки восстановления: каждый dump БД и
архив конфигурации с одинаковой временной меткой.

```bash
sudo VPN_PROJECT_DIR="$PWD" VPN_BACKUP_DIR=/var/backups/vpn-service \
  scripts/backup.sh
```

Проверка dump во временной БД того же PostgreSQL:

```bash
sudo scripts/verify_backup.sh \
  /var/backups/vpn-service/vpn-db-<timestamp>.dump
```

Backup не включает БД 3x-ui. Её резервируйте отдельно.

Контроль наличия свежих backup-файлов:

```bash
ls -lh /var/backups/vpn-service | tail -20
```

Перед обновлением, ручными правками БД, сменой нод или массовой правкой
пользователей сначала выполните `scripts/backup.sh`, затем
`scripts/verify_backup.sh` для созданного dump.

## Восстановление PostgreSQL

Восстановление выполняется только с SSH-хоста, потому что оно останавливает
сервисы и меняет рабочую БД. Скрипт имеет предохранитель и без явного
подтверждения ничего не делает.

1. Найдите нужный dump:

```bash
ls -lh /var/backups/vpn-service/vpn-db-*.dump
```

2. Проверьте его во временной БД:

```bash
cd /home/freedman/vpn-service
sudo scripts/verify_backup.sh /var/backups/vpn-service/vpn-db-<timestamp>.dump
```

3. Запустите восстановление:

```bash
cd /home/freedman/vpn-service
sudo RESTORE_CONFIRM=I_UNDERSTAND \
  scripts/restore_postgres.sh /var/backups/vpn-service/vpn-db-<timestamp>.dump
```

Скрипт:

- останавливает `api`, `bot`, `worker`;
- запрещает новые подключения к текущей БД `vpn`;
- завершает активные сессии;
- переименовывает старую БД в `vpn_before_restore_<UTC timestamp>`;
- создаёт чистую БД `vpn`;
- восстанавливает dump через `pg_restore`;
- запускает `api`, `bot`, `worker`.

Если после восстановления нужно откатиться, старая БД не удаляется сразу:
она остаётся под именем `vpn_before_restore_<UTC timestamp>`.

## Обновление

```bash
cd /home/freedman/vpn-service
git pull --ff-only origin НАЗВАНИЕ ВЕТКИ
python3 scripts/configctl.py validate
docker compose build api bot worker
docker compose run --rm api alembic upgrade head
docker compose up -d --remove-orphans
curl -fsS http://127.0.0.1:8000/health
```

Перед удалением контейнера или БД всегда создавайте backup. Не удаляйте `mydb`
из общего PostgreSQL: VPN использует только БД `vpn`.

## Тесты

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Unit-тесты не обращаются к production 3x-ui. Скрипты `e2e_*` создают реальные
данные и запускаются только в отдельном тестовом окружении.

## Восстановление старого Telegram-чека

Если в старой записи платежа сохранился только Telegram `file_id`, одноразовый
скрипт скачивает файл через Bot API и загружает бинарный чек в VPN API:

```bash
docker compose exec bot python -m app.backfill_receipt \
  PAYMENT_ID USER_ID TELEGRAM_FILE_ID TELEGRAM_FILE_UNIQUE_ID photo
```

Последний аргумент — `photo` для изображения или `document` для PDF. Скрипту
нужны штатные `BOT_TOKEN`, `API_URL` и `SERVICE_API_TOKEN` контейнера. Перед
запуском сверьте `PAYMENT_ID` и `USER_ID`: API отклоняет чек, если платёж
принадлежит другому пользователю. Повторный запуск перезаписывает сохранённый
binary-чек этой записи платежа.
