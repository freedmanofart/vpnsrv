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

## Подготовка child

1. Открыть child-панель через SSH-туннель.
2. Убедиться, что версия панели совместима с master (сейчас 3.7.0).
3. В `Settings → Security → API Token` создать токен со scope `node-sync`.
4. Сохранить открытое значение токена сразу: повторно панель его не показывает.
5. Настроить сертификат и сетевой адрес, по которому master сможет обращаться к
   child. Для production предпочтителен HTTPS с проверяемым сертификатом или
   режим `pin`/`mtls` 3x-ui.
6. Не публиковать административную панель без firewall и аутентификации.

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
