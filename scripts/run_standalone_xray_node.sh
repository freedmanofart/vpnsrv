#!/usr/bin/env bash
set -euo pipefail

# Изолированный VLESS Reality на самой VPN-ноде. Скрипт не обращается к API,
# PostgreSQL, node-agent или домашнему control plane и создаёт ровно одного
# статического клиента для проверки data plane.

XRAY_IMAGE=${XRAY_IMAGE:-ghcr.io/xtls/xray-core@sha256:4198aa816f04eeacde6470f4f07947eb4f701a1bba3657e366636681eaae5855}
XRAY_PORT=${XRAY_PORT:-443}
REALITY_SNI=${REALITY_SNI:-www.cloudflare.com}
STATE_DIR=${STATE_DIR:-/etc/vpn-standalone}
CONTAINER_NAME=${CONTAINER_NAME:-vpn-xray-standalone}
CLIENT_CONTAINER_NAME=${CLIENT_CONTAINER_NAME:-vpn-xray-standalone-client}
SOCKS_PORT=${SOCKS_PORT:-10808}
PUBLIC_HOST=${PUBLIC_HOST:-}

if [[ ${1:-} == "--remove" ]]; then
  podman rm -f "$CLIENT_CONTAINER_NAME" >/dev/null 2>&1 || true
  podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  echo "Standalone container removed; configuration remains in $STATE_DIR"
  exit 0
fi

if [[ $EUID -ne 0 ]]; then
  echo "Run as root on the VPN node" >&2
  exit 2
fi
command -v podman >/dev/null
command -v openssl >/dev/null
command -v curl >/dev/null
command -v python3 >/dev/null
command -v ss >/dev/null

if [[ -z $PUBLIC_HOST ]]; then
  PUBLIC_HOST=$(curl -4fsS --max-time 10 https://api.ipify.org) || {
    echo "Set PUBLIC_HOST to the node's public IPv4 address" >&2
    exit 2
  }
fi
if ss -H -lnt "sport = :$XRAY_PORT" | grep -q .; then
  cat >&2 <<EOF
TCP port $XRAY_PORT is already in use. Stop the production listener first or use
an explicitly opened alternate port, for example XRAY_PORT=8443. The standalone
test never stops production services automatically.
EOF
  exit 3
fi

umask 077
install -d -m 700 "$STATE_DIR"
podman pull --quiet "$XRAY_IMAGE" >/dev/null
KEY_OUTPUT=$(podman run --rm "$XRAY_IMAGE" x25519)
PRIVATE_KEY=$(sed -n 's/^PrivateKey: //p' <<<"$KEY_OUTPUT")
PUBLIC_KEY=$(sed -n 's/^Password (PublicKey): //p' <<<"$KEY_OUTPUT")
CLIENT_ID=$(cat /proc/sys/kernel/random/uuid)
SHORT_ID=$(openssl rand -hex 8)
test -n "$PRIVATE_KEY" && test -n "$PUBLIC_KEY"

python3 - "$STATE_DIR/config.json" "$XRAY_PORT" "$REALITY_SNI" "$PRIVATE_KEY" "$SHORT_ID" "$CLIENT_ID" <<'PY'
import json
import sys

path, port, sni, private_key, short_id, client_id = sys.argv[1:]
config = {
    "log": {"loglevel": "info", "access": "/var/log/xray/access.log"},
    "inbounds": [{
        "tag": "vless-reality",
        "listen": "0.0.0.0",
        "port": int(port),
        "protocol": "vless",
        "settings": {
            "clients": [{"id": client_id, "email": "standalone-test"}],
            "decryption": "none",
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": f"{sni}:443",
                "xver": 0,
                "serverNames": [sni],
                "privateKey": private_key,
                "shortIds": [short_id],
            },
        },
    }],
    "outbounds": [{
        "tag": "direct",
        "protocol": "freedom",
        "settings": {"domainStrategy": "UseIPv4"},
    }],
}
with open(path, "w", encoding="utf-8") as destination:
    json.dump(config, destination, indent=2)
    destination.write("\n")
PY

# Закреплённый образ Xray работает не от root (UID 65532). Конфигурация должна
# принадлежать этому UID уже во время `run -test`; одного SELinux relabel `:Z`
# недостаточно, если файл создан root с umask 077.
chown 65532:65532 "$STATE_DIR/config.json"
chmod 600 "$STATE_DIR/config.json"
podman run --rm -v "$STATE_DIR/config.json:/config.json:ro,Z" \
  "$XRAY_IMAGE" run -test -config /config.json
install -d -m 700 "$STATE_DIR/log"
chown 65532:65532 "$STATE_DIR/log"
podman run -d --name "$CONTAINER_NAME" --network host --restart=unless-stopped \
  -v "$STATE_DIR/config.json:/usr/local/etc/xray/config.json:ro,Z" \
  -v "$STATE_DIR/log:/var/log/xray:Z" \
  "$XRAY_IMAGE" run -config /usr/local/etc/xray/config.json >/dev/null

# Второй Xray играет роль эталонного клиента прямо на ноде. Это проверяет UUID,
# Reality keys, VLESS и HTTPS egress независимо от AmneziaVPN и control plane.
# 127.0.0.1 используется только как адрес тестового server outbound; Reality SNI
# и ключи остаются теми же, что в напечатанном публичном URI.
python3 - "$STATE_DIR/client-config.json" "$XRAY_PORT" "$SOCKS_PORT" "$REALITY_SNI" "$PUBLIC_KEY" "$SHORT_ID" "$CLIENT_ID" <<'PY'
import json
import sys

path, port, socks_port, sni, public_key, short_id, client_id = sys.argv[1:]
config = {
    "log": {"loglevel": "warning"},
    "inbounds": [{
        "tag": "socks",
        "listen": "127.0.0.1",
        "port": int(socks_port),
        "protocol": "socks",
        "settings": {"udp": True},
    }],
    "outbounds": [{
        "tag": "proxy",
        "protocol": "vless",
        "settings": {"vnext": [{
            "address": "127.0.0.1",
            "port": int(port),
            "users": [{"id": client_id, "encryption": "none"}],
        }]},
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "serverName": sni,
                "fingerprint": "chrome",
                "publicKey": public_key,
                "shortId": short_id,
            },
        },
    }],
}
with open(path, "w", encoding="utf-8") as destination:
    json.dump(config, destination, indent=2)
    destination.write("\n")
PY
chown 65532:65532 "$STATE_DIR/client-config.json"
chmod 600 "$STATE_DIR/client-config.json"
podman run --rm -v "$STATE_DIR/client-config.json:/config.json:ro,Z" \
  "$XRAY_IMAGE" run -test -config /config.json
podman run -d --name "$CLIENT_CONTAINER_NAME" --network host \
  -v "$STATE_DIR/client-config.json:/usr/local/etc/xray/config.json:ro,Z" \
  "$XRAY_IMAGE" run -config /usr/local/etc/xray/config.json >/dev/null

rm -f "$STATE_DIR/egress-ip.txt"
for _ in {1..20}; do
  if curl --fail --silent --show-error --max-time 10 \
    --proxy "socks5h://127.0.0.1:$SOCKS_PORT" https://api.ipify.org \
    > "$STATE_DIR/egress-ip.txt"; then
    break
  fi
  sleep 1
done
if [[ ! -s $STATE_DIR/egress-ip.txt ]]; then
  echo "Built-in VLESS/Reality egress test failed; inspect both container logs" >&2
  podman logs "$CLIENT_CONTAINER_NAME" >&2 || true
  exit 4
fi

URI="vless://${CLIENT_ID}@${PUBLIC_HOST}:${XRAY_PORT}?type=tcp&security=reality&encryption=none&sni=${REALITY_SNI}&fp=chrome&pbk=${PUBLIC_KEY}&sid=${SHORT_ID}#standalone-node-test"
printf '%s\n' "$URI" > "$STATE_DIR/client-uri.txt"
chmod 600 "$STATE_DIR/config.json" "$STATE_DIR/client-uri.txt"

cat <<EOF
Standalone Xray is listening on ${PUBLIC_HOST}:${XRAY_PORT}.
Built-in Xray client egress succeeded with IP: $(cat "$STATE_DIR/egress-ip.txt")
Import this URI into a NEW client profile (Vision/flow is intentionally disabled):

$URI

After connecting from another network, open https://api.ipify.org in a browser.
Watch requests on the node with:
  tail -f $STATE_DIR/log/access.log
  podman logs -f $CONTAINER_NAME
  podman logs -f $CLIENT_CONTAINER_NAME
Remove the test container with:
  $0 --remove
EOF
