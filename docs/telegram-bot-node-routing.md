# Маршрутизация Telegram-бота через VPN-ноду

Этот runbook описывает перенос сетевого канала Telegram API с master-сервера на
дочернюю ноду и откат обратно на прямое подключение.

Контейнеры `vpn-bot` и `vpn-api` остаются на master. Через ноду передаётся только
TLS-трафик к `api.telegram.org`; VPN-трафик клиентов и остальные исходящие
соединения не меняются.

## Текущая схема

```text
vpn-bot / vpn-api
        |
        | api.telegram.org -> Docker host-gateway:443
        v
master 172.17.0.1:443
        |
        | Tailscale
        v
sw-node2 100.117.14.51:18443
        |
        v
api.telegram.org:443
```

Текущие параметры:

- master: `freedomvpn`;
- child: `sw-node2`, публичный IP `89.127.212.239`;
- Tailscale IP child: `100.117.14.51`;
- порт реле на child: `18443`;
- Docker host-gateway master: `172.17.0.1`;
- systemd-unit child: `telegram-api-upstream.service`;
- systemd-unit master: `telegram-api-relay.service`.

Не привязывать реле к `0.0.0.0`. Upstream должен слушать только Tailscale-IP,
а master relay только локальный Docker gateway.

## Когда нужен перенос

Характерные признаки блокировки прямого маршрута:

```bash
docker inspect vpn-bot --format '{{.State.Status}} restarts={{.RestartCount}}'
docker logs vpn-bot --since 10m 2>&1 | tail -100
curl -4 -sS --connect-timeout 8 -o /dev/null \
  -w 'code=%{http_code} tls=%{time_appconnect}\n' \
  https://api.telegram.org
```

Если контейнер работает, но в журнале повторяется `Failed to fetch updates`, а
прямой `curl` зависает на TLS или получает reset, проверить Telegram с child:

```bash
ssh root@89.127.212.239 \
  "curl -4 -sS --connect-timeout 8 -o /dev/null \
  -w 'code=%{http_code} tls=%{time_appconnect}\\n' \
  https://api.telegram.org"
```

Код `302` для корня `https://api.telegram.org` означает, что TLS и HTTP работают.

## Включение маршрута через child

### 1. Проверить Tailscale и socat

На master:

```bash
tailscale status
docker network inspect bridge \
  --format '{{(index .IPAM.Config 0).Gateway}}'
command -v socat
```

На child:

```bash
tailscale ip -4
command -v socat
ss -ltn 'sport = :18443'
```

Ожидаемые адреса для текущей конфигурации: `100.117.14.51` на child и
`172.17.0.1` на master.

### 2. Настроить upstream на child

Создать `/etc/systemd/system/telegram-api-upstream.service`:

```ini
[Unit]
Description=Telegram API upstream TLS relay
Wants=network-online.target
After=network-online.target tailscaled.service
Requires=tailscaled.service

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:18443,bind=100.117.14.51,reuseaddr,fork TCP:api.telegram.org:443
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict

[Install]
WantedBy=multi-user.target
```

Включить сервис:

```bash
systemctl daemon-reload
systemctl enable --now telegram-api-upstream.service
systemctl is-enabled telegram-api-upstream.service
systemctl is-active telegram-api-upstream.service
```

### 3. Настроить relay на master

Создать `/etc/systemd/system/telegram-api-relay.service`:

```ini
[Unit]
Description=Telegram API TLS relay for Docker services
Wants=network-online.target
After=network-online.target tailscaled.service docker.service
Requires=tailscaled.service docker.service

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:443,bind=172.17.0.1,reuseaddr,fork TCP:100.117.14.51:18443
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict

[Install]
WantedBy=multi-user.target
```

Включить и проверить:

```bash
systemctl daemon-reload
systemctl enable --now telegram-api-relay.service
systemctl is-enabled telegram-api-relay.service
systemctl is-active telegram-api-relay.service

curl -sS --connect-timeout 8 --max-time 12 \
  --resolve api.telegram.org:443:172.17.0.1 \
  -o /dev/null -w 'code=%{http_code} tls=%{time_appconnect}\n' \
  https://api.telegram.org
```

До изменения контейнеров последняя команда должна вернуть `302`.

### 4. Направить API и бот на локальное реле

В `/home/freedman/vpn-service/docker-compose.yml` добавить в сервисы `api` и
`bot`:

```yaml
extra_hosts:
  - "api.telegram.org:host-gateway"
```

У сервиса `api` уже может быть секция `extra_hosts`. В таком случае добавить
строку в существующий список, не создавать вторую секцию.

Проверить конфигурацию и пересоздать только API и бот:

```bash
cd /home/freedman/vpn-service
docker compose config -q
docker compose up -d --force-recreate api bot
```

`host-gateway` обычно соответствует `172.17.0.1`. Это нужно проверить после
пересоздания:

```bash
docker exec vpn-bot getent ahostsv4 api.telegram.org
docker exec vpn-api getent ahostsv4 api.telegram.org
```

### 5. Проверить работу

Проверка Telegram API без вывода токена:

```bash
docker exec vpn-bot python -c '
import os, httpx
r = httpx.get(
    "https://api.telegram.org/bot" + os.environ["BOT_TOKEN"] + "/getMe",
    timeout=15,
)
print(r.status_code, r.json().get("ok"))
'
```

Ожидаемый результат: `200 True`.

Проверить polling и API:

```bash
docker inspect vpn-bot vpn-api \
  --format '{{.Name}} status={{.State.Status}} restarts={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}'
docker logs vpn-bot --since 5m 2>&1 | tail -100
curl -fsS http://127.0.0.1:8000/health
```

После отправки `/start` в Telegram в журнале должна появиться строка
`Update id=... is handled`.

## Перенос канала на другую ноду

1. Проверить прямой доступ новой ноды к `https://api.telegram.org`.
2. Узнать её Tailscale IP командой `tailscale ip -4`.
3. Установить на новой ноде upstream-unit, заменив адрес в `bind=`.
4. Запустить upstream и проверить доступность порта с master.
5. На master заменить IP после `TCP:` в `telegram-api-relay.service`.
6. Выполнить `systemctl daemon-reload` и перезапустить relay.
7. Проверить `getMe`, polling и обработку `/start`.
8. Только после успешной проверки отключить upstream на старой ноде.

Пример проверки порта новой ноды с master:

```bash
timeout 5 bash -c '</dev/tcp/NEW_TAILSCALE_IP/18443'
```

При смене только child-ноды пересоздавать Docker-контейнеры не требуется:
локальная точка `api.telegram.org -> host-gateway` не меняется.

## Откат на прямой маршрут

Откат выполнять только после восстановления прямого TLS-доступа с master:

```bash
curl -4 -sS --connect-timeout 8 --max-time 12 \
  -o /dev/null -w 'code=%{http_code} tls=%{time_appconnect}\n' \
  https://api.telegram.org
```

Если команда не возвращает `302`, откат остановить: бот снова перестанет
отвечать.

### 1. Убрать DNS-привязку контейнеров

Удалить из сервисов `api` и `bot` в `docker-compose.yml`:

```yaml
- "api.telegram.org:host-gateway"
```

Если после удаления список `extra_hosts` пуст, удалить и сам ключ. Затем:

```bash
cd /home/freedman/vpn-service
docker compose config -q
docker compose up -d --force-recreate api bot
```

Проверить, что контейнер снова получает настоящий адрес Telegram:

```bash
docker exec vpn-bot getent ahostsv4 api.telegram.org
```

### 2. Проверить прямую работу до остановки реле

```bash
docker exec vpn-bot python -c '
import os, httpx
r = httpx.get(
    "https://api.telegram.org/bot" + os.environ["BOT_TOKEN"] + "/getMe",
    timeout=15,
)
print(r.status_code, r.json().get("ok"))
'
docker logs vpn-bot --since 2m 2>&1 | tail -50
```

Если проверка не проходит, вернуть строку `api.telegram.org:host-gateway` в оба
сервиса и снова выполнить `docker compose up -d --force-recreate api bot`.

### 3. Отключить реле

На master:

```bash
systemctl disable --now telegram-api-relay.service
```

На child:

```bash
systemctl disable --now telegram-api-upstream.service
```

Unit-файлы лучше оставить на месте до завершения наблюдения. Для повторного
включения достаточно выполнить `systemctl enable --now` сначала на child, затем
на master, вернуть `extra_hosts` и пересоздать `api` и `bot`.

## Диагностика реле

```bash
# master
systemctl status telegram-api-relay.service --no-pager
journalctl -u telegram-api-relay.service --since -30m --no-pager
ss -ltn 'sport = :443'
tailscale ping 100.117.14.51

# child
systemctl status telegram-api-upstream.service --no-pager
journalctl -u telegram-api-upstream.service --since -30m --no-pager
ss -ltn 'sport = :18443'
curl -4 -sS --connect-timeout 8 -I https://api.telegram.org
```

Частые причины отказа:

- изменился Tailscale IP child;
- `socat` не установлен или unit не запущен;
- порт `18443` занят другим сервисом;
- `host-gateway` изменился, а master relay остался привязан к старому адресу;
- на child также появился сетевой блок Telegram;
- контейнеры не были пересозданы после изменения `extra_hosts`.

