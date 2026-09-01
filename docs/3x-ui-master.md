# 3x-ui master и child-ноды

## Текущая схема

- основной сервер запускает 3x-ui как master;
- удалённые 3x-ui подключаются к master как child nodes;
- VPN API обращается только к REST API master;
- приложение выдаёт один тип ключа: VLESS Reality xHTTP без flow;
- Xray, node-agent и Xray gRPC в этом репозитории отсутствуют.

В VPN Admin сейчас используется одна тестовая локация — `node-sw`, регион
`SE|Швеция`. Исторические логические ноды отключены: они остаются в PostgreSQL
для связности старых подписок и аудита, но не возвращаются через `/vpn/nodes` и
не показываются в Telegram.

## Токены

На каждом child создаётся отдельный токен со scope `node-sync`. Он сохраняется
в записи Nodes на master. Для VPN API на master создаётся другой токен со scope
`node-sync` и записывается в `.env`:

```dotenv
THREEXUI_API_TOKEN=<token-master-node-sync>
THREEXUI_VERIFY_TLS=true
```

Операции `/panel/api/nodes/*` требуют административного токена master. Такой
токен используется только интерактивным скриптом регистрации ноды и не
передаётся контейнерам приложения.

## Адреса панели и inbound

Не смешивайте два разных соединения:

- `scheme/address/port/basePath` — административный API child;
- `host/port` inbound — публичная точка подключения VPN-клиента.

Панель может работать на HTTPS/443, а VLESS inbound — на другом порту. Web base
path панели никогда не включается в VLESS URI. Порт subscription server 3x-ui
также не является API-портом и не является портом VLESS inbound.

Для production используйте HTTPS и `tlsVerifyMode=verify`, `pin` или `mtls`.
Режим `skip` допустим только для краткой диагностики. Закрытый base path,
токены и Reality private key не фиксируются в Git.

## Связь master с child

Master подключается к child только по внешнему HTTPS-адресу. Сертификат должен
быть действителен, его цепочка должна быть доверена master, а DNS-имя —
совпадать с сертификатом. Приватные адреса нод и оверлейные VPN-сети в текущей
схеме не используются. Для рабочей ноды задавайте `allowPrivateAddress=false` и
`tlsVerifyMode=system`.

Проверка выполняется из master:

```bash
curl -vk --max-time 10 https://<child-host>:<panel-port>/<base-path>/panel/api/server/status
```

Коды `401`/`403` подтверждают сетевую доступность. `404` обычно означает
неверный base path, `EOF` — ошибку listener/reverse proxy, timeout — отсутствие
внешнего маршрута или недоступный порт.

## Inbound и логическая нода

После успешного Probe создайте или импортируйте VLESS Reality xHTTP inbound и
назначьте его child. Для VPN Admin требуются:

- числовой inbound ID;
- публичные host и port;
- Reality SNI, public key, short ID и spiderX;
- VLESS ML-KEM encryption из inbound;
- xHTTP path, mode, host и диапазон padding.

Регистрация выполняется скриптом из
[`add-3x-ui-node.md`](add-3x-ui-node.md).

## Локальный API master

Если master слушает только loopback, API-контейнер использует локальный proxy
`vpn-threexui-proxy.service`. Он связывает адрес Docker bridge с loopback-портом
3x-ui и не публикует панель в интернет. Фактический gateway проверяется так:

```bash
docker inspect vpn-api --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
sudo systemctl status vpn-threexui-proxy.service --no-pager
```

Unit proxy содержит `PartOf=x-ui.service`: обычный `systemctl restart x-ui`
должен автоматически перезапустить и proxy. Если 3x-ui online, но создание
клиента возвращает `All connection attempts failed`, проверьте listener на
Docker bridge и восстановите proxy:

```bash
sudo systemctl start vpn-threexui-proxy.service
sudo systemctl is-active vpn-threexui-proxy.service
ss -lntp | grep 41026
```

После ручной замены unit-файла выполните `systemctl daemon-reload`. Изменение
порта 3x-ui требует синхронно обновить обе стороны `ExecStart` и `api_address`
логической ноды.

После изменения токена или адреса:

```bash
python3 scripts/configctl.py validate
python3 scripts/configctl.py apply --services api worker
curl -fsS http://127.0.0.1:8000/health
```
