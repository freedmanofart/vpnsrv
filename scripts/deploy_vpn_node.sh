#!/usr/bin/env bash
set -euo pipefail

# Повторяемая начальная настройка VPN-ноды Xray/Reality. На целевом узле нужны
# только Fedora/Podman, systemd и SSH; пакеты на хост не устанавливаются.

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Обязательные значения описывают удалённый хост и его запись в control plane.
# `${VAR:?сообщение}` останавливает скрипт до удалённых изменений при нехватке данных.
NODE_SSH=${NODE_SSH:?Set NODE_SSH, for example root@159.223.22.59}
NODE_NAME=${NODE_NAME:?Set NODE_NAME, for example do-fra1-01}
NODE_PROVIDER=${NODE_PROVIDER:?Set NODE_PROVIDER, for example digitalocean}
NODE_REGION=${NODE_REGION:?Set NODE_REGION: us, nl or de}
NODE_IP=${NODE_IP:?Set the public NODE_IP}
NODE_HOSTNAME=${NODE_HOSTNAME:-$NODE_IP}
NODE_CAPACITY=${NODE_CAPACITY:-100}
CONTROL_PLANE_URL=${CONTROL_PLANE_URL:?Set the URL reachable from the node}
ADMIN_API_URL=${ADMIN_API_URL:-http://127.0.0.1:8000}
ADMIN_USERNAME=${ADMIN_USERNAME:?Set ADMIN_USERNAME}
ADMIN_PASSWORD=${ADMIN_PASSWORD:?Set ADMIN_PASSWORD}
REALITY_SNI=${REALITY_SNI:-www.cloudflare.com}
XRAY_IMAGE=${XRAY_IMAGE:-ghcr.io/xtls/xray-core@sha256:4198aa816f04eeacde6470f4f07947eb4f701a1bba3657e366636681eaae5855}

case "$NODE_REGION" in
  us|nl|de) ;;
  *) echo "NODE_REGION must be us, nl or de" >&2; exit 2 ;;
esac

# Проверяем локальные команды до выпуска учётных данных и изменения SSH на ноде.
command -v ssh >/dev/null
command -v scp >/dev/null
command -v curl >/dev/null
command -v python3 >/dev/null
umask 077
TMP_DIR=$(mktemp -d)
SSH_DIR=$(mktemp -d /tmp/vpn-node-ssh.XXXXXX)
trap 'rm -rf "$TMP_DIR" "$SSH_DIR"' EXIT
SSH_ARGS=(
  -o BatchMode=yes
  -o ConnectTimeout=15
  -o ConnectionAttempts=10
  -o ControlMaster=auto
  -o ControlPersist=120
  -o "ControlPath=$SSH_DIR/%C"
)

# Выполняет команду через мультиплексированное неинтерактивное SSH-соединение.
remote() {
  ssh "${SSH_ARGS[@]}" "$NODE_SSH" "$@"
}

# Все административные API-вызовы используют Basic auth, завершаются ошибкой при
# ответе не 2xx и запрашивают JSON. Тихий curl не раскрывает секреты в verbose-логах.
api() {
  curl --fail --silent --show-error \
    --user "$ADMIN_USERNAME:$ADMIN_PASSWORD" \
    -H 'Content-Type: application/json' "$@"
}

# Извлекает доверенное выражение из JSON-ответа. Вызывающий код передаёт выражения
# наподобие '["id"]'; Python устраняет зависимость от jq на машине оператора.
json_value() {
  python3 -c 'import json,sys; value=json.load(sys.stdin); print(value'"$1"')'
}

echo "[1/7] Checking target and pulling pinned Xray image"
remote "command -v podman >/dev/null && command -v systemctl >/dev/null"
remote "podman pull --quiet '$XRAY_IMAGE' >/dev/null"
scp "${SSH_ARGS[@]}" "$PROJECT_ROOT/deploy/ssh/00-vpn-hardening.conf" "$PROJECT_ROOT/deploy/ssh/01-vpn-preauth.conf" "$NODE_SSH:/etc/ssh/sshd_config.d/"
remote "restorecon -F /etc/ssh/sshd_config.d/00-vpn-hardening.conf /etc/ssh/sshd_config.d/01-vpn-preauth.conf 2>/dev/null || true; sshd -t; systemctl reload sshd"

echo "[2/7] Generating node-specific Reality material"
# Повторный запуск сохраняет существующие данные Reality. Неявная ротация сделала
# бы недействительными все клиентские URI, уже выданные для этой ноды.
if remote "test -s /etc/vpn-node/xray-config.json"; then
  scp "${SSH_ARGS[@]}" "$NODE_SSH:/etc/vpn-node/xray-config.json" "$TMP_DIR/xray-config.json" >/dev/null
  REALITY_PRIVATE_KEY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["inbounds"][1]["streamSettings"]["realitySettings"]["privateKey"])' "$TMP_DIR/xray-config.json")
  REALITY_SHORT_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["inbounds"][1]["streamSettings"]["realitySettings"]["shortIds"][0])' "$TMP_DIR/xray-config.json")
  KEY_OUTPUT=$(remote "podman run --rm '$XRAY_IMAGE' x25519 -i '$REALITY_PRIVATE_KEY'")
else
  KEY_OUTPUT=$(remote "podman run --rm '$XRAY_IMAGE' x25519")
  REALITY_PRIVATE_KEY=$(printf '%s\n' "$KEY_OUTPUT" | sed -n 's/^PrivateKey: //p')
  REALITY_SHORT_ID=$(remote "openssl rand -hex 8")
  sed \
    -e "s|__REALITY_PRIVATE_KEY__|$REALITY_PRIVATE_KEY|g" \
    -e "s|__REALITY_SHORT_ID__|$REALITY_SHORT_ID|g" \
    -e "s|www.cloudflare.com|$REALITY_SNI|g" \
    "$PROJECT_ROOT/deploy/node/xray-config.example.json" > "$TMP_DIR/xray-config.json"
fi
python3 - "$TMP_DIR/xray-config.json" <<'PY'
# Включаем access log даже для сохранённой старой конфигурации: node-agent читает
# его и сообщает время последней активности клиента.
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as source:
    config = json.load(source)
config.setdefault("log", {})["access"] = "/var/log/xray/access.log"
# На VPS без рабочего IPv6 принудительно разрешаем домены в IPv4. Иначе Xray
# может принять VLESS-туннель, но не суметь открыть адрес назначения для клиента.
for outbound in config.get("outbounds", []):
    if outbound.get("tag") == "direct" and outbound.get("protocol") == "freedom":
        outbound.setdefault("settings", {})["domainStrategy"] = "UseIPv4"
with open(path, "w", encoding="utf-8") as destination:
    json.dump(config, destination, indent=2)
    destination.write("\n")
PY
REALITY_PUBLIC_KEY=$(printf '%s\n' "$KEY_OUTPUT" | sed -n 's/^Password (PublicKey): //p')
test -n "$REALITY_PRIVATE_KEY"
test -n "$REALITY_PUBLIC_KEY"
test -n "$REALITY_SHORT_ID"

echo "[3/7] Registering node and VLESS configuration"
# Регистрация идемпотентна по имени ноды. Перед заменой файлов или сервисов на
# удалённом хосте существующие публичные данные Reality сравниваются ниже.
NODES_JSON=$(api "$ADMIN_API_URL/vpn/nodes")
NODE_ID=$(printf '%s' "$NODES_JSON" | NODE_LOOKUP="$NODE_NAME" python3 -c '
import json, os, sys
for node in json.load(sys.stdin):
    if node["name"] == os.environ["NODE_LOOKUP"]:
        print(node["id"])
        break
')
if [[ -z "$NODE_ID" ]]; then
  NODE_BODY=$(NODE_NAME="$NODE_NAME" NODE_PROVIDER="$NODE_PROVIDER" NODE_REGION="$NODE_REGION" NODE_IP="$NODE_IP" NODE_HOSTNAME="$NODE_HOSTNAME" NODE_CAPACITY="$NODE_CAPACITY" python3 -c '
import json, os
print(json.dumps({
    "name": os.environ["NODE_NAME"],
    "provider": os.environ["NODE_PROVIDER"],
    "region": os.environ["NODE_REGION"],
    "ip_address": os.environ["NODE_IP"],
    "hostname": os.environ["NODE_HOSTNAME"],
    "capacity": int(os.environ["NODE_CAPACITY"]),
}))
')
  NODE_ID=$(api -X POST -d "$NODE_BODY" "$ADMIN_API_URL/vpn/nodes" | json_value '["id"]')
fi

CONFIGS_JSON=$(api "$ADMIN_API_URL/vpn/nodes/$NODE_ID/configs")
HAS_VLESS=$(printf '%s' "$CONFIGS_JSON" | python3 -c 'import json,sys; print(any(x["protocol"] == "vless" for x in json.load(sys.stdin)))')
if [[ "$HAS_VLESS" != "True" ]]; then
  CONFIG_BODY=$(NODE_HOSTNAME="$NODE_HOSTNAME" REALITY_SNI="$REALITY_SNI" REALITY_PUBLIC_KEY="$REALITY_PUBLIC_KEY" REALITY_SHORT_ID="$REALITY_SHORT_ID" python3 -c '
import json, os
print(json.dumps({"protocol": "vless", "config": {
    "api_address": "127.0.0.1:10085",
    "host": os.environ["NODE_HOSTNAME"],
    "port": 443,
    "type": "tcp",
    "security": "reality",
    "sni": os.environ["REALITY_SNI"],
    "fp": "chrome",
    "pbk": os.environ["REALITY_PUBLIC_KEY"],
    "sid": os.environ["REALITY_SHORT_ID"],
    "inbound_tag": "vless-reality",
}}))
')
  api -X POST -d "$CONFIG_BODY" "$ADMIN_API_URL/vpn/nodes/$NODE_ID/configs" >/dev/null
else
  REGISTERED_MATERIAL=$(printf '%s' "$CONFIGS_JSON" | python3 -c '
import json, sys
config = next(x["config"] for x in json.load(sys.stdin) if x["protocol"] == "vless")
print(config.get("pbk", "") + " " + config.get("sid", ""))
')
  if [[ "$REGISTERED_MATERIAL" != "$REALITY_PUBLIC_KEY $REALITY_SHORT_ID" ]]; then
    echo "Existing Reality material differs from the control-plane configuration; refusing unsafe overwrite" >&2
    exit 3
  fi
fi

echo "[4/7] Issuing a scoped node-agent token"
# Открытый токен возвращается только при ротации. При повторном запуске сохраняем
# развёрнутый токен, чтобы исправная нода не потеряла доступ к control plane.
if remote "test -s /etc/vpn-node/node-agent.env"; then
  NODE_TOKEN=$(remote "sed -n 's/^NODE_AGENT_TOKEN=//p' /etc/vpn-node/node-agent.env")
else
  NODE_TOKEN=$(api -X POST "$ADMIN_API_URL/agent/v1/credentials/$NODE_ID/rotate" | json_value '["token"]')
fi
test -n "$NODE_TOKEN"

cat > "$TMP_DIR/node-agent.env" <<EOF
CONTROL_PLANE_URL=$CONTROL_PLANE_URL
NODE_AGENT_TOKEN=$NODE_TOKEN
NODE_XRAY_API_ADDRESS=127.0.0.1:10085
NODE_AGENT_INTERVAL_SECONDS=30
NODE_XRAY_ACCESS_LOG=/var/log/xray/access.log
LOG_LEVEL=INFO
EOF

echo "[5/7] Uploading configuration and building the agent image"
# Секретные файлы остаются с правами 0600. Конфигурация Xray принадлежит
# непривилегированному UID контейнера, а юниты Quadlet ставятся для всей системы.
remote "install -d -m 700 /etc/vpn-node /opt/vpn-node/build /etc/containers/systemd; install -d -m 750 -o 65532 -g 65532 /var/log/vpn-xray"
scp "${SSH_ARGS[@]}" "$TMP_DIR/xray-config.json" "$NODE_SSH:/etc/vpn-node/xray-config.json"
scp "${SSH_ARGS[@]}" "$TMP_DIR/node-agent.env" "$NODE_SSH:/etc/vpn-node/node-agent.env"
scp "${SSH_ARGS[@]}" "$PROJECT_ROOT/deploy/node/NodeAgent.Dockerfile" "$NODE_SSH:/opt/vpn-node/build/NodeAgent.Dockerfile"
remote "rm -rf /opt/vpn-node/build/app"
# Передаём по SSH только исходники приложения, исключая кэши и xattr хоста для
# воспроизводимого build context и без создания промежуточного архива.
COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' --exclude='*.pyc' -C "$PROJECT_ROOT/api" -czf - app \
  | ssh "${SSH_ARGS[@]}" "$NODE_SSH" "tar -xzf - -C /opt/vpn-node/build"
scp "${SSH_ARGS[@]}" "$PROJECT_ROOT/deploy/node/vpn-xray.container" "$NODE_SSH:/etc/containers/systemd/vpn-xray.container"
scp "${SSH_ARGS[@]}" "$PROJECT_ROOT/deploy/node/vpn-node-agent.container" "$NODE_SSH:/etc/containers/systemd/vpn-node-agent.container"
remote "chown 65532:65532 /etc/vpn-node/xray-config.json && chmod 600 /etc/vpn-node/xray-config.json /etc/vpn-node/node-agent.env && podman build -q -t localhost/vpn-node-agent:latest -f /opt/vpn-node/build/NodeAgent.Dockerfile /opt/vpn-node/build >/dev/null"

echo "[6/7] Validating Xray and enabling services"
# Собственный парсер Xray должен принять конфигурацию до перезапуска production-
# юнитов systemd. Ошибка на этом шаге не затрагивает уже работающий юнит.
remote "podman run --rm -v /etc/vpn-node/xray-config.json:/config.json:ro,Z '$XRAY_IMAGE' run -test -config /config.json"
remote "systemctl daemon-reload && systemctl restart vpn-xray.service vpn-node-agent.service"

echo "[7/7] Verifying listeners and service health"
# Финальная проверка намеренно строгая: отсутствие сервиса или listener завершает
# развёртывание ошибкой вместо вывода вводящего в заблуждение сообщения об успехе.
remote "systemctl --no-pager --full status vpn-xray.service vpn-node-agent.service | sed -n '1,80p'; ss -lnt | grep -E '(:443|:10085)'"
echo "Node $NODE_NAME registered as id=$NODE_ID. Public key and agent token were not printed."
