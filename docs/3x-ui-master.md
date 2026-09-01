# 3x-ui master и child-ноды

## Текущая схема

- основной сервер запускает 3x-ui как master;
- удалённые 3x-ui подключаются к master как child nodes;
- VPN API обращается только к REST API master;
- приложение выдаёт один тип ключа: VLESS Reality xHTTP без flow;
- Xray, node-agent и Xray gRPC в этом репозитории отсутствуют.

В VPN Admin сейчас используются две логические локации: Германия и Швеция.
Отключённые исторические ноды не возвращаются через `/vpn/nodes` и не
показываются в Telegram.

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

Поддерживаются два варианта:

1. публичный HTTPS-адрес child с проверяемым сертификатом;
2. приватный адрес Tailscale, разрешённый политикой tailnet.

При Tailscale нельзя включать `allowPrivateAddress`, пока адрес не проверен и
действительно принадлежит нужной ноде. При публичном варианте ограничьте панель
cloud firewall/firewalld, если это возможно без привязки к изменяемому IP.

Проверка выполняется из master:

```bash
curl -vk --max-time 10 https://<child-host>:<panel-port>/<base-path>/panel/api/server/status
```

Коды `401`/`403` подтверждают сетевую доступность. `404` обычно означает
неверный base path, `EOF` — ошибку listener/reverse proxy, timeout — маршрут,
ACL или firewall.

## Inbound и логическая нода

После успешного Probe создайте или импортируйте VLESS Reality xHTTP inbound и
назначьте его child. Для VPN Admin требуются:

- числовой inbound ID;
- публичные host и port;
- Reality SNI, public key и short ID;
- xHTTP path, mode и при необходимости host.

Регистрация выполняется скриптом из
[`add-3x-ui-node.md`](add-3x-ui-node.md). Настройка firewall с аварийным
откатом описана в [`new-node-firewall.md`](new-node-firewall.md).

## Локальный API master

Если master слушает только loopback, API-контейнер использует локальный proxy
`vpn-threexui-proxy.service`. Он связывает адрес Docker bridge с loopback-портом
3x-ui и не публикует панель в интернет. Фактический gateway проверяется так:

```bash
docker inspect vpn-api --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
sudo systemctl status vpn-threexui-proxy.service --no-pager
```

После изменения токена или адреса:

```bash
python3 scripts/configctl.py validate
python3 scripts/configctl.py apply --services api worker
curl -fsS http://127.0.0.1:8000/health
```
