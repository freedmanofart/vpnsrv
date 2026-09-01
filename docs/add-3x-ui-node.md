# Добавление 3x-ui child-ноды

Скрипт `scripts/register_3xui_node.py` регистрирует child в master, создаёт или
обновляет логическую ноду VPN Admin, привязывает её к inbound и проверяет
здоровье. Он **не меняет SSH, пользователей, Tailscale, ACL и firewall**.

## Панель и VPN — разные порты

- порт панели (`443`, `60628` и т. п.) нужен master для управления child;
- порт inbound (`2453` в текущем примере node-sw) принимает VLESS-клиентов;
- web base path относится только к панели и не входит в VLESS URI;
- UUID, вручную созданный в 3x-ui, не принадлежит приложению. Управляемый ключ
  создавайте через Telegram-бота или VPN Admin.

Если панель доступна на `https://host:443/secret/`, а inbound слушает `2453`,
укажите `THREEXUI_CHILD_PORT=443` и `VPN_PUBLIC_PORT=2453`.

## Требования

1. Child установлен, а его API доступен master.
2. На child создан токен со scope `node-sync`.
3. На master создан отдельный административный токен: `/nodes/*` не входит в
   scope `node-sync`.
4. Известны ID inbound, публичный порт, SNI, Reality public key, short ID и
   параметры транспорта.
5. VPN Admin доступен на `127.0.0.1:8000`, известен `SERVICE_API_TOKEN`.

Сохраните параметры в защищённый файл (`chmod 600 /root/node.env`), не вводите
токены прямо в командной строке:

```dotenv
THREEXUI_MASTER_URL=https://master.example/закрытый-путь
THREEXUI_MASTER_VERIFY_TLS=true
THREEXUI_ADMIN_TOKEN=<admin-token-master>
THREEXUI_CHILD_API_TOKEN=<node-sync-token-child>
THREEXUI_CHILD_NAME=node-sw
THREEXUI_CHILD_SCHEME=https
THREEXUI_CHILD_ADDRESS=child.example
THREEXUI_CHILD_PORT=443
THREEXUI_CHILD_BASE_PATH=/закрытый-путь-child/
THREEXUI_CHILD_TLS_VERIFY_MODE=system
THREEXUI_CHILD_ALLOW_PRIVATE=false
THREEXUI_CHILD_INBOUND_SYNC_MODE=all

VPN_API_URL=http://127.0.0.1:8000
SERVICE_API_TOKEN=<service-token>
VPN_NODE_PROVIDER=3x-ui
VPN_NODE_CAPACITY=100
VPN_NODE_IP=203.0.113.10
THREEXUI_INBOUND_ID=8
VPN_PUBLIC_HOST=child.example
VPN_PUBLIC_PORT=2453
VPN_TRANSPORT=xhttp
VPN_REALITY_SNI=example.org
VPN_REALITY_PUBLIC_KEY=<public-key>
VPN_REALITY_SHORT_ID=<short-id>
VPN_XHTTP_PATH=/
VPN_XHTTP_MODE=auto
VPN_XHTTP_HOST=
```

Сначала выполните безопасную проверку, затем регистрацию:

```bash
set -a
. /root/node.env
set +a
python3 scripts/register_3xui_node.py --dry-run
python3 scripts/register_3xui_node.py
```

Повторный запуск обновляет записи по имени и VLESS-конфигурацию. Скрипт не
печатает токены. `--panel-only` ограничивает работу регистрацией в 3x-ui.
Страна определяется по `VPN_NODE_IP` через HTTPS API `ipwho.is` и
сохраняется как ISO-код и название. При недоступности сервиса регистрация
останавливается, чтобы в Telegram не появилась нода без страны. Для тестового
адреса или ручной коррекции можно явно задать `VPN_NODE_REGION=DE|Германия`.

## Выпуск и перевыпуск ключа

Откройте `http://localhost:8000`, выберите пользователя и нажмите
«Перевыпустить», затем укажите ID логической ноды. Приложение создаёт один тип
ключа — VLESS Reality xHTTP — через
`/panel/api/clients/add`, переносит срок действия и Telegram ID, удаляет прежний
управляемый клиент и формирует URI из конфигурации inbound. Старую gRPC-запись,
которой уже нет в 3x-ui, приложение пропускает, что позволяет миграцию.

## Проверка и диагностика

```bash
curl -fsS http://127.0.0.1:8000/health
curl -u "$ADMIN_USERNAME:$ADMIN_PASSWORD" http://127.0.0.1:8000/admin
curl -H "Authorization: Bearer $SERVICE_API_TOKEN" http://127.0.0.1:8000/vpn/nodes
```

- `404 from remote panel`: неверный base path или несовместимая версия.
- `EOF`: панель слушает loopback, но прокси до неё не работает.
- `context deadline exceeded`: master не достигает адреса/порта child.
- Панель online, но VLESS не подключается: проверяйте `VPN_PUBLIC_PORT`, SNI,
  public key, short ID, xHTTP path/mode и версию Xray-клиента.
- UUID виден только в 3x-ui: он создан вручную и не управляется VPN Admin.

Firewall намеренно вне этого скрипта. Его отдельная настройка и аварийный откат
описаны в `docs/new-node-firewall.md`.
