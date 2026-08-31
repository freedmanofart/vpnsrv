# Настройка 3x-ui master и child-нод

## Целевая схема

- 3x-ui 3.7 на основном сервере работает как master.
- 3x-ui на `159.223.22.59` работает как child node.
- VPN API обращается только к master через Bearer token со scope `node-sync`.
- Master самостоятельно применяет изменения к локальному Xray или отправляет
  их нужной child-панели.

Старые Xray gRPC, контейнер `vpn-xray`, собственный node-agent и маршруты
`/agent/v1/*` в ветке `newnode` удалены.

## Доступ к панелям

Master запущен на основном сервере и публикуется через настроенный reverse
proxy. Его фактический web base path является закрытым параметром и в Git не
фиксируется.

Child-панель на тестовом VPS слушает только loopback-порт `60628`. Для ручной
первичной настройки с операторского компьютера:

```bash
ssh -N -L 2223:127.0.0.1:60628 root@159.223.22.59
```

После открытия туннеля панель доступна по `http://127.0.0.1:2223`. Туннель
предназначен для браузера оператора и не является постоянным каналом master →
child.

Для постоянного соединения master → child можно выбрать один из вариантов:

1. **Tailscale (рекомендуется):** панель остаётся на `127.0.0.1`, а порт
   публикуется только внутри tailnet посредством Tailscale Serve.
2. **Открытый порт:** панель слушает публичный интерфейс, а `60628/tcp`
   открывается в firewalld. Этот вариант требует HTTPS, действующего API token и
   дополнительных ограничений доступа.

Не смешивать варианты без необходимости. Перед изменением сети обязательно
оставить открытой отдельную SSH-сессию: через неё можно выполнить откат, если
новое правило окажется ошибочным.

## Подготовка child

1. Открыть child-панель через SSH-туннель.
2. Убедиться, что версия панели совместима с master (сейчас 3.7.0).
3. В `Settings → Security → API Token` создать токен со scope `node-sync`.
4. Сохранить открытое значение токена сразу: повторно панель его не показывает.
5. Настроить сертификат и сетевой адрес, по которому master сможет обращаться к
   child. Для production предпочтителен HTTPS с проверяемым сертификатом или
   режим `pin`/`mtls` 3x-ui.
6. Не публиковать административную панель без firewall и аутентификации.

## Вариант 1: подключение через Tailscale

### 1. Проверка Tailscale

На master и child выполнить:

```bash
sudo systemctl is-active tailscaled
sudo tailscale status
sudo tailscale ip -4
```

Записать Tailscale IPv4 обоих серверов. В текущей конфигурации:

- master: `100.102.21.123`;
- child: `100.89.228.2`.

Адреса Tailscale стабильнее публичного адреса провайдера, но при удалении и
повторной регистрации устройства в tailnet могут измениться.

Проверить связность с master:

```bash
tailscale ping 100.89.228.2
```

Успешный ping подтверждает присутствие устройств в одном tailnet, но не
гарантирует, что ACL разрешает TCP-порт панели.

### 2. Сохранение текущего состояния

На child:

```bash
sudo install -d -m 700 /var/backups/x-ui
sudo cp -a /etc/x-ui/x-ui.db \
  "/var/backups/x-ui/x-ui.db.before-network-$(date -u +%Y%m%dT%H%M%SZ)"
sudo firewall-cmd --list-all-zones \
  | sudo tee "/var/backups/x-ui/firewalld-before-network-$(date -u +%Y%m%dT%H%M%SZ).txt" \
  >/dev/null
sudo tailscale serve status
```

Если x-ui использует PostgreSQL, отдельно создать штатную резервную копию этой
БД: копия `/etc/x-ui/x-ui.db` в таком случае сохраняет только локальный файл и
не заменяет backup PostgreSQL.

### 3. Публикация loopback-порта в tailnet

Оставить панель на loopback и включить TCP proxy Tailscale:

```bash
sudo /usr/local/x-ui/x-ui setting -listenIP 127.0.0.1
sudo systemctl restart x-ui
sudo tailscale serve --bg --tcp=60628 tcp://127.0.0.1:60628
```

Проверить оба listener:

```bash
sudo systemctl is-active x-ui
sudo tailscale serve status
sudo ss -lntp | grep 60628
```

Ожидаются:

- процесс `x-ui` на `127.0.0.1:60628`;
- процесс `tailscaled` на Tailscale IP child и порту `60628`.

### 4. Настройка firewalld

Не добавлять `60628` как общедоступный порт. Разрешить его только от
Tailscale-IP master:

```bash
sudo firewall-cmd --permanent --zone=public \
  --add-rich-rule='rule family="ipv4" source address="100.102.21.123/32" port port="60628" protocol="tcp" accept'
sudo firewall-cmd --reload
sudo firewall-cmd --zone=public --list-rich-rules
```

Не следует без необходимости помещать весь `tailscale0` в зону `trusted`: это
разрешит участникам tailnet обращаться и к другим сервисам child, слушающим
Tailscale-интерфейс.

### 5. ACL Tailscale

В ACL tailnet разрешить master обращаться к child на TCP/60628. Для политики с
`grants` правило выглядит так:

```json
{
  "src": ["100.102.21.123"],
  "dst": ["100.89.228.2"],
  "ip": ["tcp:60628"]
}
```

Для старого синтаксиса `acls`:

```json
{
  "action": "accept",
  "src": ["100.102.21.123"],
  "dst": ["100.89.228.2:60628"]
}
```

Предпочтительно использовать Tailscale-теги, например `tag:vpnsrv-master` и
`tag:vpnsrv-node`, а не IP-адреса. После изменения ACL проверить с master:

```bash
curl -v --max-time 10 \
  http://100.89.228.2:60628/panel/api/server/status
```

Коды `401`, `403` или `404` означают, что TCP-соединение уже работает и дальше
нужно проверять token/base path. Timeout означает проблему ACL, маршрута или
firewall. `EOF`/`Empty reply from server` при обращении к публичному IP обычно
означает, что панель слушает только loopback или Tailscale.

В Nodes master указать:

```text
scheme: http
address: 100.89.228.2
port: 60628
basePath: /panel/
```

Значение `basePath` должно совпадать с настройкой child. `/panel/` приведён как
пример; при другом закрытом пути необходимо указать фактическое значение.

## Вариант 2: открытый публичный порт

Этот вариант допустим, если Tailscale использовать невозможно. Панель окажется
доступна по публичному адресу, поэтому до открытия порта необходимо настроить
HTTPS, API token `node-sync`, сложный base path и актуальные обновления 3x-ui.

### 1. Включение публичного listener

На child:

```bash
sudo /usr/local/x-ui/x-ui setting -listenIP 0.0.0.0
sudo systemctl restart x-ui
sudo systemctl is-active x-ui
sudo ss -lntp | grep 60628
```

Ожидается listener `*:60628`. Если ранее использовался Tailscale Serve,
отключить только его правило для этого порта:

```bash
sudo tailscale serve --tcp=60628 off
```

### 2. Открытие firewalld

Полностью открытый вариант:

```bash
sudo firewall-cmd --permanent --zone=public --add-port=60628/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --zone=public --query-port=60628/tcp
```

Ответ должен быть `yes`. Если публичный IP master известен и стабилен, безопаснее
не добавлять общий порт, а использовать rich rule только для `/32`:

```bash
sudo firewall-cmd --permanent --zone=public \
  --add-rich-rule='rule family="ipv4" source address="MASTER_PUBLIC_IP/32" port port="60628" protocol="tcp" accept'
sudo firewall-cmd --reload
```

Также проверить cloud firewall/VPC firewall у провайдера: локального разрешения
firewalld недостаточно, если входящий порт заблокирован на уровне облака.

В Nodes master указать публичный адрес child, HTTPS и фактический base path:

```text
scheme: https
address: 159.223.22.59
port: 60628
basePath: /фактический-путь/
tlsVerifyMode: verify
```

Не использовать `tlsVerifyMode=skip` постоянно. При сертификате на доменное имя
в поле `address` следует указывать этот домен, а не IP.

## Откат сетевой настройки и firewalld

Для новой ноды рекомендуется применять правила скриптом с автоматическим
таймером отката. Подробная процедура приведена в
[`new-node-firewall.md`](new-node-firewall.md).

Ниже описан точечный откат к стандартному безопасному варианту: панель доступна
только на `127.0.0.1`, Tailscale Serve выключен, публичный `60628/tcp` закрыт,
временные rich rules удалены. SSH и штатные сервисы firewalld не затрагиваются.

### 1. Не закрывать текущую SSH-сессию

Открыть вторую SSH-сессию и убедиться, что вход работает. Все команды отката
выполнять из первой сессии. Не использовать `firewall-cmd --complete-reload`
удалённо без консоли провайдера: он может оборвать активные соединения.

### 2. Вернуть x-ui на loopback

```bash
sudo /usr/local/x-ui/x-ui setting -listenIP 127.0.0.1
sudo systemctl restart x-ui
sudo systemctl is-active x-ui
sudo ss -lntp | grep 60628
```

Должен остаться listener `127.0.0.1:60628` от процесса `x-ui`.

### 3. Выключить Tailscale Serve

```bash
sudo tailscale serve --tcp=60628 off
sudo tailscale serve status
```

Если на сервере нет других правил Tailscale Serve и требуется удалить всю его
конфигурацию:

```bash
sudo tailscale serve reset
```

`reset` нельзя применять, если Tailscale Serve публикует другие нужные сервисы.

### 4. Закрыть публичный порт

Удалить общее разрешение, если оно добавлялось:

```bash
sudo firewall-cmd --permanent --zone=public --remove-port=60628/tcp
```

Удалить rich rule Tailscale master:

```bash
sudo firewall-cmd --permanent --zone=public \
  --remove-rich-rule='rule family="ipv4" source address="100.102.21.123/32" port port="60628" protocol="tcp" accept'
```

Если добавлялось правило для публичного IP master, удалить его с **точно тем же
адресом**, который использовался при создании:

```bash
sudo firewall-cmd --permanent --zone=public \
  --remove-rich-rule='rule family="ipv4" source address="MASTER_PUBLIC_IP/32" port port="60628" protocol="tcp" accept'
```

Если `tailscale0` ранее помещался в `trusted`, вернуть интерфейс в стандартную
зону:

```bash
sudo firewall-cmd --permanent --zone=trusted --remove-interface=tailscale0
```

Применить изменения обычным reload:

```bash
sudo firewall-cmd --reload
```

Команда удаления может вывести `NOT_ENABLED`, если конкретное правило уже
отсутствует. Это не требует добавлять его обратно.

### 5. Итоговая проверка стандартного состояния

```bash
sudo firewall-cmd --get-active-zones
sudo firewall-cmd --zone=public --list-all
sudo firewall-cmd --zone=public --list-rich-rules
sudo firewall-cmd --zone=public --query-port=60628/tcp
sudo ss -lntp | grep 60628
sudo tailscale serve status
```

Ожидаемый результат:

- `60628/tcp` отсутствует в `ports` зоны `public`;
- rich rules для `60628` отсутствуют;
- `tailscale0` не находится в `trusted`, если это не является общей политикой
  данного сервера;
- x-ui слушает только `127.0.0.1:60628`;
- Tailscale Serve не публикует `60628`;
- SSH остаётся в списке разрешённых сервисов firewalld.

С другого компьютера публичная проверка должна завершаться отказом или timeout:

```bash
curl -v --max-time 5 http://159.223.22.59:60628/
```

Локальная панель при этом должна оставаться доступной через SSH-туннель:

```bash
ssh -N -L 2223:127.0.0.1:60628 root@159.223.22.59
```

### 6. Аварийное восстановление firewalld

Если после изменения правил пропал SSH-доступ, использовать web/serial console
провайдера. Сначала проверить конфигурацию:

```bash
sudo firewall-cmd --check-config
sudo firewall-cmd --get-active-zones
sudo firewall-cmd --list-all-zones
```

Чтобы вернуть только доступ по SSH в публичной зоне:

```bash
sudo firewall-cmd --permanent --zone=public --add-service=ssh
sudo firewall-cmd --reload
```

Не удалять каталог `/etc/firewalld` и не выполнять массовый сброс правил без
консоли и резервной копии: на других серверах могут быть обязательные правила
для Docker, мониторинга и VPN.

## Регистрация child на master

В разделе Nodes панели master создать ноду со следующими параметрами:

- `name`: стабильное техническое имя, например `do-fra1-de-01`;
- `scheme`, `address`, `port`, `basePath`: API-адрес child;
- `apiToken`: токен `node-sync`, созданный на child;
- `tlsVerifyMode`: `verify`, `pin` или `mtls`; `skip` допустим только временно;
- `enable`: включено;
- `inboundSyncMode`: `all` либо `selected`.

Запустить Test/Probe. Нода должна перейти в `online`, а master должен показать
версию панели и состояние Xray. После этого создать или импортировать VLESS
Reality inbound и назначить его child-ноде.

## Токен VPN API на master

На master создать отдельный API token:

- имя: `vpnsrv-control-plane`;
- scope: `node-sync`;
- срок: согласно политике ротации проекта.

Открытое значение записать только в `.env` control plane:

```bash
python3 scripts/configctl.py set THREEXUI_API_TOKEN '<token>'
python3 scripts/configctl.py set THREEXUI_VERIFY_TLS true
python3 scripts/configctl.py apply --services api worker
```

Файл `.env` должен иметь права `0600`. Токен нельзя помещать в конфигурацию
логической ноды, PostgreSQL, README, логи или скриншоты.

## Привязка логической ноды VPN Admin

В VPN Admin создать локацию, затем выбрать «Привязать inbound из 3x-ui master».
Заполняются:

- Node ID в VPN Admin;
- URL master вместе с web base path;
- числовой ID inbound из 3x-ui;
- публичный адрес и порт child-ноды;
- Reality SNI, fingerprint, public key и short ID.

API token берётся только из окружения и в форму не вводится.

## Проверка

1. В VPN Admin выполнить Health для логической ноды.
2. Ожидаемый результат: `online` и число клиентов inbound.
3. Создать тестовую подписку.
4. Убедиться, что клиент `vpn-<id>` появился в нужном inbound master.
5. Проверить импорт VLESS URI и выходной IP.
6. Отозвать клиента и убедиться, что он исчез из 3x-ui.
7. Выполнить Reconcile; ожидается `errors=0`.

## Ротация токенов

При ротации токена VPN API сначала создать новый token на master, затем обновить
`.env` и пересоздать `api`/`worker`. После успешного Health старый token можно
отключить.

При ротации токена child сначала создать новый token на child и заменить его в
настройках Nodes master. После успешного Probe отключить старый token.
