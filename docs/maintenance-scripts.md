# Обслуживающие скрипты

В каталоге `scripts/` находятся операции, которые запускаются оператором,
таймером systemd или как сквозная проверка развёрнутого стенда. Этот документ
описывает не только команды, но и их предусловия, побочные эффекты, ожидаемый
результат и правила работы с секретами.

## Общие правила

- Запускайте команды из корня репозитория, если не указано обратное.
- Не включайте shell tracing (`set -x`): параметры содержат пароли и токены.
- Файлы `.env`, дампы, архивы конфигурации, node-agent token и Reality private
  key считаются секретами. Не прикладывайте их к issue и не отправляйте в чат.
- E2E-скрипты изменяют данные. Используйте отдельного тестового пользователя и
  только тестовый платёжный провайдер.
- Сначала выполните `python3 scripts/configctl.py validate`, затем проверьте
  состояние контейнеров командой `docker compose ps`.

## `configctl.py`: управление `.env`

Скрипт не требует сторонних Python-пакетов, сохраняет порядок строк и
комментарии, записывает файл атомарно и устанавливает права `0600`. Секретные
значения в `list` и `get` маскируются по умолчанию.

```bash
python3 scripts/configctl.py validate
python3 scripts/configctl.py list
python3 scripts/configctl.py get XRAY_MANAGEMENT_MODE
python3 scripts/configctl.py set LOG_LEVEL INFO
python3 scripts/configctl.py generate SERVICE_API_TOKEN
python3 scripts/configctl.py apply --services api bot worker
```

`generate` доступен только для переменных, для которых задана безопасная длина.
После ротации токена одновременно обновите всех потребителей. Флаги
`--show-secret` и `--show-secrets` предназначены только для контролируемой
диагностики: их вывод попадёт в историю терминала или журнал CI.

`apply` валидирует конфигурацию и пересоздаёт перечисленные Compose-сервисы.
По умолчанию это `api`, `bot` и `worker`; Grafana и node-agent нужно указывать
явно. Альтернативный файл выбирается до подкоманды:

```bash
python3 scripts/configctl.py --env-file /secure/path/staging.env validate
```

## `backup.sh`: резервное копирование

Скрипт создаёт два артефакта:

1. `vpn-db-<UTC>.dump` — PostgreSQL custom-format dump;
2. `vpn-config-<UTC>.tar.gz` — `.env`, Compose, Xray и observability config.

По умолчанию исходный проект находится в `/home/freedman/vpn-service`, а
результат — в `/var/backups/vpn-service`. Пути можно переопределить:

```bash
sudo VPN_PROJECT_DIR="$PWD" VPN_BACKUP_DIR=/mnt/secure/vpn \
  scripts/backup.sh
```

Нужны запущенный контейнер `vpn-postgres`, доступ к Docker и читаемые файлы
конфигурации. После создания дамп проверяется через `pg_restore --list`.
Артефакты старше 14 дней удаляются только по ожидаемым шаблонам имён.
Успешный stdout содержит пути `database=...` и `config=...`.

Архив на том же диске не защищает от потери узла. После локальной проверки
копируйте оба файла в зашифрованное внешнее хранилище и контролируйте успешность
копирования отдельно. Скрипт не шифрует и не загружает архивы самостоятельно.

## `verify_backup.sh`: проверка восстановления

Проверка создаёт временную БД в существующем контейнере, полностью разворачивает
дамп, выводит количества пользователей, подписок и клиентов, затем удаляет БД:

```bash
sudo scripts/verify_backup.sh \
  /var/backups/vpn-service/vpn-db-20260825T120000Z.dump
```

Команда не трогает рабочую БД. Тем не менее ей требуются свободное место размером
не меньше восстановленной базы и право создавать/удалять базы. Успех
`pg_restore --list` в `backup.sh` проверяет структуру архива, а этот скрипт —
реальное восстановление; для регулярного disaster-recovery нужны обе проверки.

Для production-восстановления остановите пишущие сервисы, создайте чистую БД,
восстановите дамп совместимой версией `pg_restore`, верните конфигурационный
архив с правами `0600`, выполните миграции и только затем запустите API/worker.
Сначала репетируйте процедуру на изолированном узле.

## `deploy_vpn_node.sh`: развёртывание VPN-ноды

Скрипт идемпотентно регистрирует ноду в control plane, сохраняет или создаёт
Reality material, выпускает scoped token, загружает Quadlet units, собирает
node-agent и проверяет Xray. Он также устанавливает SSH hardening snippets;
поэтому перед первым запуском должен существовать отдельный проверенный SSH-сеанс.

Обязательные параметры:

```bash
export NODE_SSH=root@203.0.113.10
export NODE_NAME=do-fra1-01
export NODE_PROVIDER=digitalocean
export NODE_REGION=de                 # только us, nl или de
export NODE_IP=203.0.113.10
export CONTROL_PLANE_URL=https://control.example.com
export ADMIN_API_URL=http://127.0.0.1:8000
export ADMIN_USERNAME=admin
export ADMIN_PASSWORD='...'
scripts/deploy_vpn_node.sh
```

На управляющей машине нужны `ssh`, `scp`, `curl` и `python3`; на Fedora-ноде —
`podman`, `systemd`, `sshd` и `openssl`. API должен быть доступен оператору, а
`CONTROL_PLANE_URL` — самой ноде. `NODE_HOSTNAME`, `NODE_CAPACITY`, `REALITY_SNI`
и digest `XRAY_IMAGE` имеют значения по умолчанию.

Повторный запуск сохраняет private key и node token. Если публичный Reality key
или short ID не совпадает с записью control plane, выполнение останавливается:
автоматическая перезапись сделала бы выданные URI недействительными. После успеха
проверьте node health в админке и журналы:

```bash
ssh "$NODE_SSH" 'systemctl status vpn-xray vpn-node-agent'
ssh "$NODE_SSH" 'journalctl -u vpn-xray -u vpn-node-agent --since=-10m'
```

## Сквозные проверки

Эти команды предназначены для уже запущенного тестового окружения и печатают
только безопасные идентификаторы/состояния.

## Безопасное включение firewall на публичной ноде

`harden_vpn_node_firewall.sh` создаёт для внешнего интерфейса отдельную зону
firewalld с политикой `DROP` и оставляет публичным Xray TCP/443. По умолчанию SSH
доступен с любого адреса, но скрипт сначала проверяет эффективную конфигурацию
sshd: разрешены ключи, а парольная и keyboard-interactive аутентификация
отключены. Таким образом, смена динамического адреса оператора не блокирует вход,
а войти без зарегистрированного ключа нельзя. Скрипт нельзя применять через
SSH без страховки: **до** смены зоны он запускает автоматический rollback через
systemd. Подтвердить результат можно только из новой SSH-сессии; подтверждение
из исходной сессии намеренно отклоняется.

Сначала скопируйте скрипт. Эта команда ничего не меняет на ноде:

```bash
NODE_SSH=root@203.0.113.10 ./scripts/copy_vpn_node_firewall.sh
```

Подключитесь к ноде по ключу и оставьте терминал открытым. Для динамического
адреса не задавайте `SSH_ALLOW_CIDRS`:

```bash
/root/harden_vpn_node_firewall.sh --apply
```

Если нужен standalone Xray на 8443, перечислите оба порта:

```bash
XRAY_TCP_PORTS=443,8443 \
  /root/harden_vpn_node_firewall.sh --apply
```

Не закрывая первый терминал, откройте второй, заново войдите по SSH и только в
нём подтвердите правила:

```bash
/root/harden_vpn_node_firewall.sh --confirm
```

Без подтверждения прежняя зона автоматически восстановится через 5 минут.
Немедленный откат из первой сессии: `harden_vpn_node_firewall.sh --rollback`.
Проверка состояния: `harden_vpn_node_firewall.sh --status`. Cloud Firewall
провайдера настраивается отдельно: он также должен пропускать SSH-порт и нужные
порты VPN-ноды.

Если позднее появится постоянная административная сеть, SSH можно ограничить ею
явно: `SSH_ACCESS_MODE=cidr SSH_ALLOW_CIDRS=198.51.100.0/24`. Firewall не умеет
определять SSH-ключ до установления TCP-соединения, поэтому интернет-сканеры всё
ещё могут появляться в sshd journal, но пройти аутентификацию без ключа не смогут.

Повторный `--apply` нельзя запускать, пока предыдущая операция ожидает
подтверждения: сначала войдите во второй SSH-сеанс и выполните `--confirm` либо
выполните `--rollback`. Обновлённый rollback восстанавливает каждый сохранённый
порт отдельным аргументом firewalld и понимает state-файлы первой версии, где
порты могли храниться одной строкой (`22/tcp 443/tcp`). Ошибка
`INVALID_PORT: bad port` в этой ситуации не означает, что SSH уже закрыт; она
означает, что старая версия не смогла разобрать собственный snapshot.

### Не проверяйте VLESS с помощью `ping`

VLESS передаёт TCP и UDP, но не является IP-туннелем для ICMP. Поэтому после
включения TUN в AmneziaVPN обычный `ping 1.1.1.1` или `ping example.com` может
перестать отвечать даже при полностью рабочем доступе к интернету. Этот результат
нельзя считать признаком неисправности ноды или outbound Xray.

На клиентском устройстве проверяйте реальный поддерживаемый трафик:

```bash
curl --fail --show-error --max-time 15 https://api.ipify.org
curl --fail --show-error --max-time 15 https://example.com/ -o /dev/null
```

Первый ответ должен содержать публичный адрес VPN-ноды, а второй завершиться с
кодом `0`. Если обе команды не проходят, сравните время попытки с access log ноды:

```bash
journalctl -u vpn-xray.service --since '-5 minutes' --no-pager
tail -n 100 /var/log/vpn-xray/access.log
```

Запись `accepted` подтверждает, что клиентский трафик дошёл до VLESS inbound.
Отсутствие записи при установленной в приложении сессии указывает на клиентскую
маршрутизацию/TUN, а ошибка outbound в journal — на серверную проблему. Такой
тест разделяет установление Reality-сессии и фактический HTTPS egress; heartbeat
node-agent проверяет только управление Xray и не заменяет эту проверку.

### Полностью автономная проверка ноды

Чтобы исключить API, БД, node-agent и генерацию ключа ботом, скопируйте на ноду
`scripts/run_standalone_xray_node.sh` и запустите его от root. Скрипт создаёт
новые Reality-ключи, один статический UUID и максимально простой профиль
VLESS Reality TCP без Vision/flow. Он сначала проверяет конфигурацию встроенным
парсером Xray и только затем запускает отдельный Podman-контейнер. Конфигурация
перед проверкой получает владельца UID `65532`, от которого работает закреплённый
образ Xray; это необходимо помимо SELinux relabel `:Z` на Fedora.
После запуска server-контейнера скрипт поднимает второй Xray как эталонный клиент,
подключает его к Reality inbound через loopback SOCKS и сам выполняет HTTPS-запрос.
Сообщение `Built-in Xray client egress succeeded` доказывает, что UUID, Reality
keys, VLESS и outbound работают без AmneziaVPN. После этого повторяющиеся
`failed to read client hello` от внешнего IP означают, что внешний клиент дошёл до
порта, но не отправил ожидаемый Reality TLS ClientHello: проверяйте импорт URI и
поддержку Reality в выбранной версии клиента, а не интернет на ноде.

При этом `access.log` останется пустым: туда попадают принятые VLESS-запросы, а не
соединения, отклонённые ещё до VLESS-аутентификации. Предупреждение `Listening on
non-443 ports` для диагностического `8443` также не является ошибкой запуска.
Сводку по server log, access log и встроенной egress-проверке можно получить так:

```bash
/root/run_standalone_xray_node.sh --diagnose
```

Команда должна присутствовать в строке ровно один раз. Строка вида
`--diagnose/root/run_standalone_xray_node.sh --diagnose` — это две команды,
склеенные без перевода строки; она не запускает диагностику. Счётчик принятых
внешних запросов отделён от loopback-запроса встроенного эталонного клиента.

Если одновременно присутствуют `accepted ... [vless-reality >> direct]` от
внешнего IP и `failed to read client hello`/`handshake did not complete`, это не
противоречие: первые строки доказывают рабочие Reality, VLESS и outbound, а вторые
относятся к отдельным TCP-пробам или незавершённым попыткам клиента. При наличии
принятых TCP **и** UDP-запросов с адреса клиента дальнейшую потерю системного
интернета следует искать в клиентских TUN, DNS, firewall и policy routing.

`run_standalone_xray_node.sh` запускается **на VPN-ноде**. Из checkout проекта на
управляющем сервере или рабочем компьютере его можно безопасно скопировать так:

```bash
NODE_SSH=root@203.0.113.10 ./scripts/copy_standalone_xray_node.sh
ssh root@203.0.113.10
PUBLIC_HOST=203.0.113.10 XRAY_PORT=8443 /root/run_standalone_xray_node.sh
```

### Независимая проверка AmneziaWG

Чтобы полностью исключить Xray/Reality и сравнить другой transport, добавлен
`run_standalone_amneziawg_node.sh`. Он не интегрирован с backend и требует заранее
установленные официальные `awg` и `awg-quick`. Скрипт создаёт отдельный интерфейс
`awg-test`, одну пару ключей клиента, PSK, runtime-правила firewalld и готовый
конфиг для импорта в AmneziaVPN:

```bash
scp scripts/run_standalone_amneziawg_node.sh root@203.0.113.10:/root/
ssh root@203.0.113.10
PUBLIC_HOST=203.0.113.10 AWG_PORT=51820 \
  /root/run_standalone_amneziawg_node.sh
```

Разрешите `51820/udp` также в cloud firewall. Затем скопируйте напечатанный
`/etc/vpn-standalone-awg/client.conf` на клиент и импортируйте как AmneziaWG.
Текущий handshake и счётчики байтов доступны через `--status`; отсутствие
handshake при видимых UDP-пакетах в `tcpdump` указывает на несовместимость
параметров/клиента, а отсутствие самих пакетов — на firewall или блокировку UDP:

```bash
/root/run_standalone_amneziawg_node.sh --status
tcpdump -ni any udp port 51820
```

После теста удалите runtime-интерфейс и правила командой `--remove`. Никакая
криптографическая библиотека или VPN-протокол не может гарантировать работу с
вероятностью 100% во всех ОС и сетях; эта проверка нужна именно для независимого
сравнения UDP AmneziaWG с TCP Reality.

Путь назначения можно заменить через `REMOTE_PATH`. Скрипт копирования ничего не
запускает на ноде и не меняет firewall; он только создаёт каталог, копирует файл и
устанавливает права `0700`.

Production `443/tcp` скрипт самостоятельно не останавливает. Для проверки на 443
сначала остановите production Xray в отдельном SSH-сеансе либо задайте открытый в
firewall альтернативный порт:

```bash
PUBLIC_HOST=203.0.113.10 XRAY_PORT=8443 ./scripts/run_standalone_xray_node.sh
```

Импортируйте напечатанный URI как **новый** профиль клиента. Если автономный URI
также не открывает `https://api.ipify.org` и в `access.log` нет запросов, причина
находится до Xray outbound: firewall/маршрут до ноды либо TUN клиента. Если есть
`accepted`, смотрите `podman logs -f vpn-xray-standalone`. После проверки удалите
контейнер командой `./scripts/run_standalone_xray_node.sh --remove`.

### Покупка и Xray — `e2e_bot_xray.py`

Запускается внутри bot-контейнера, где импортируется приложение. Нужны активный
тариф, активная нода `us`/`nl`/`de`, доступный Xray и mock auto-confirm.

```bash
docker compose exec -T -e E2E_TELEGRAM_ID=900000001 \
  -e E2E_EXPECT_NEW=true bot python - < scripts/e2e_bot_xray.py
```

Образ бота содержит приложение, но не каталог `scripts`, поэтому команда выше
передаёт проверку в Python через stdin; `-T` отключает псевдотерминал.

Для второго запуска с тем же Telegram ID задайте `E2E_EXPECT_NEW=false`: число
пользователей Xray не должно измениться благодаря idempotency key.

### Device token — `e2e_device_profile.py`

Проверяет activation code, профиль, sensitive-debug session, немедленный отзыв
старого токена после refresh и административный revoke устройства. Нужны
`E2E_TELEGRAM_ID`, `SERVICE_API_TOKEN`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` и при
необходимости `E2E_API_URL`. Пользователь должен иметь активный VPN-доступ.

### Webhook — `e2e_payment_webhook.py`

Нужен ID отдельного тестового платежа, который допускает переход в `refunded`:

```bash
E2E_PAYMENT_ID=123 E2E_API_URL=http://127.0.0.1:8000 \
SERVICE_API_TOKEN='...' PAYMENT_WEBHOOK_SECRET='...' \
python3 scripts/e2e_payment_webhook.py
```

Скрипт отправляет корректный refund дважды с одним event ID и затем событие с
неверной подписью и новым ID. Ожидаются сохранённый `refunded`, идемпотентный
повтор и HTTP 401 для подделки.

## Sensitive debug

`capture_sensitive_debug.py` намеренно собирает действующие секреты. Используйте
его только после открытия ограниченной debug session в админке:

```bash
sudo scripts/capture_sensitive_debug.py SESSION_ID --project "$PWD"
```

Утилита не пишет секреты на диск и выводит только количества, но отправляет их в
audit/Loki через API. Закройте session сразу после диагностики, ограничьте доступ
к журналам и ротируйте раскрытые значения согласно инцидент-процедуре. Ошибка
посередине не закрывает session автоматически — это обязанность оператора.

## Диагностика ошибок

- `configuration is invalid` — выполните `configctl validate` и исправьте все
  строки `ERROR` до `apply`.
- `vpn-postgres` не найден — проверьте `docker compose ps postgres` и имя
  Compose-проекта.
- deploy остановлен на Reality mismatch — не обходите проверку; сравните запись
  control plane, `/etc/vpn-node/xray-config.json` и ранее выданные URI.
- E2E получает 401 — проверьте тип токена, URL окружения и синхронизацию `.env`
  после ротации.
- E2E не видит активный тариф/ноду — создайте тестовые данные через admin/API и
  дождитесь успешного node-agent status перед повтором.
