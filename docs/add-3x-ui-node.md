# Добавление новой 3x-ui ноды

`scripts/register_3xui_node.py` идемпотентно:

1. создаёт или обновляет child в Nodes master;
2. выполняет Probe;
3. создаёт или обновляет логическую ноду VPN Admin;
4. привязывает её к VLESS Reality xHTTP inbound;
5. определяет страну по публичному IP через HTTPS `ipwho.is`;
6. выполняет health check.

Скрипт не меняет SSH, Tailscale ACL и firewall.

## Перед запуском

- child установлен и доступен master;
- на child создан токен `node-sync`;
- на master создан временно используемый административный API token;
- VLESS Reality xHTTP inbound создан и известен его числовой ID;
- известны публичные параметры Reality и xHTTP;
- `SERVICE_API_TOKEN` VPN API доступен оператору.

Порт панели и порт inbound могут отличаться. `THREEXUI_CHILD_PORT` относится к
панели, `VPN_PUBLIC_PORT` — к пользовательскому VPN-трафику.

## Файл параметров

Создайте вне репозитория файл с правами `600`:

```dotenv
THREEXUI_MASTER_URL=https://master.example/<private-base-path>
THREEXUI_MASTER_VERIFY_TLS=true
THREEXUI_ADMIN_TOKEN=<master-admin-token>

THREEXUI_CHILD_NAME=node-se-01
THREEXUI_CHILD_SCHEME=https
THREEXUI_CHILD_ADDRESS=node.example.com
THREEXUI_CHILD_PORT=443
THREEXUI_CHILD_BASE_PATH=/<private-child-path>/
THREEXUI_CHILD_API_TOKEN=<child-node-sync-token>
THREEXUI_CHILD_TLS_VERIFY_MODE=system
THREEXUI_CHILD_ALLOW_PRIVATE=false
THREEXUI_CHILD_INBOUND_SYNC_MODE=all

VPN_API_URL=http://127.0.0.1:8000
SERVICE_API_TOKEN=<service-token>
VPN_NODE_PROVIDER=3x-ui
VPN_NODE_CAPACITY=100
VPN_NODE_IP=203.0.113.10
VPN_PUBLIC_HOST=node.example.com
VPN_PUBLIC_PORT=2453

THREEXUI_INBOUND_ID=8
VPN_TRANSPORT=xhttp
VPN_REALITY_SNI=example.org
VPN_REALITY_PUBLIC_KEY=<public-key>
VPN_REALITY_SHORT_ID=<short-id>
VPN_XHTTP_PATH=/
VPN_XHTTP_MODE=auto
VPN_XHTTP_HOST=
```

`VPN_NODE_IP` должен быть публичным IP: по нему определяется страна. Для
закрытого тестового адреса разрешён явный формат
`VPN_NODE_REGION=SE|Швеция`.

## Запуск

```bash
set -a
. /root/node.env
set +a
python3 scripts/register_3xui_node.py --dry-run
python3 scripts/register_3xui_node.py
```

`--panel-only` регистрирует только child в master. Повторный обычный запуск
обновляет записи по имени и конфигурацию VLESS.

## Проверка

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS -H "Authorization: Bearer $SERVICE_API_TOKEN" \
  http://127.0.0.1:8000/vpn/nodes
```

Новая нода должна иметь `status=active`, `health_status=online` и регион вида
`SE|Швеция`. Telegram покажет флаг и название страны автоматически.

После этого выпустите тестовый ключ через VPN Admin или Telegram. URI должен
содержать публичный порт inbound, а не порт панели. Вручную созданный в 3x-ui
UUID не управляется приложением и не перевыпускается из личного кабинета.

Диагностика:

- `404` — неверный base path;
- `EOF` — ошибка listener/reverse proxy;
- timeout — маршрут, ACL или firewall;
- панель online, но VPN не работает — неверен порт inbound, SNI, public key,
  short ID, xHTTP path/mode либо версия клиента.

Firewall настраивается отдельно по
[`new-node-firewall.md`](new-node-firewall.md).
