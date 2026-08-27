#!/usr/bin/env bash
set -euo pipefail

# Independent AmneziaWG data-plane test. AmneziaWG runs in a privileged Podman
# container with host networking; no DKMS module is built on the host.

INTERFACE=${INTERFACE:-awg-test}
STATE_DIR=${STATE_DIR:-/etc/vpn-standalone-awg}
AWG_PORT=${AWG_PORT:-51820}
AWG_SUBNET=${AWG_SUBNET:-10.77.77.0/24}
SERVER_ADDRESS=${SERVER_ADDRESS:-10.77.77.1/24}
CLIENT_ADDRESS=${CLIENT_ADDRESS:-10.77.77.2/32}
PUBLIC_HOST=${PUBLIC_HOST:-}
WAN_INTERFACE=${WAN_INTERFACE:-}
Jc=${Jc:-4}; Jmin=${Jmin:-40}; Jmax=${Jmax:-70}
S1=${S1:-0}; S2=${S2:-0}
H1=${H1:-1}; H2=${H2:-2}; H3=${H3:-3}; H4=${H4:-4}

FIREWALL_STATE_DIR=${FIREWALL_STATE_DIR:-/run/vpn-node-firewall}
AWG_IMAGE=${AWG_IMAGE:-$(cat /etc/vpn-amneziawg-image 2>/dev/null || echo docker.io/amneziavpn/amneziawg-go:latest)}
AWG_CONTAINER=${AWG_CONTAINER:-amneziawg-$INTERFACE}
awg_exec() { podman exec "$AWG_CONTAINER" awg "$@"; }
awg_quick() { podman exec "$AWG_CONTAINER" awg-quick "$@"; }

usage() { echo "Usage: $0 [--check|--status|--remove]" >&2; }
(( $# <= 1 )) || { usage; exit 2; }
case ${1:-} in ""|--check|--status|--remove) ;; *) usage; exit 2 ;; esac
[[ $EUID -eq 0 ]] || { echo "Run as root on the VPN node" >&2; exit 2; }
command -v podman >/dev/null || { echo "Podman is required" >&2; exit 2; }
# Container provides the official `awg` and `awg-quick` tools (command -v awg;
# command -v awg-quick).
command -v ip >/dev/null
command -v curl >/dev/null
command -v sysctl >/dev/null

ACTION=${1:-}
CONFIG="$STATE_DIR/$INTERFACE.conf"
# A saved port is operational state for status/removal only. An explicit
# AWG_PORT must remain effective when the operator starts a later test.
if [[ $ACTION =~ ^--(status|remove)$ && -s $STATE_DIR/listen-port.txt ]]; then
  SAVED_AWG_PORT=$(cat "$STATE_DIR/listen-port.txt")
  [[ $SAVED_AWG_PORT =~ ^[0-9]+$ ]] && (( SAVED_AWG_PORT > 0 && SAVED_AWG_PORT < 65536 )) || {
    echo "Invalid saved AmneziaWG port in $STATE_DIR/listen-port.txt" >&2
    exit 3
  }
  AWG_PORT=$SAVED_AWG_PORT
fi
if [[ $ACTION == --status ]]; then
  # equivalent to: awg show "$INTERFACE"
  awg_exec show "$INTERFACE" || { echo "Interface $INTERFACE is not running" >&2; exit 3; }
  ip -s link show dev "$INTERFACE"
  echo "Live packet capture: tcpdump -ni any udp port $AWG_PORT"
  exit 0
fi

preflight() {
  [[ ! -e $FIREWALL_STATE_DIR/pending ]] || {
    echo "A hardened firewall change is awaiting --confirm or --rollback in $FIREWALL_STATE_DIR" >&2
    return 2
  }
  command -v firewall-cmd >/dev/null || {
    echo "firewalld is required" >&2; return 2;
  }
  firewall-cmd --state >/dev/null 2>&1 || {
    echo "firewalld must be active" >&2; return 2;
  }
  local wan=$WAN_INTERFACE
  if [[ -z $wan ]]; then
    wan=$(ip -4 route show default | awk '$1 == "default" {print $5; exit}')
  fi
  [[ -n $wan && -e /sys/class/net/$wan ]] || {
    echo "Could not determine WAN_INTERFACE" >&2; return 2;
  }
  echo "Preflight passed: firewalld is active; WAN interface is $wan; no firewall change is pending."
}

if [[ $ACTION == --check ]]; then
  preflight
  exit 0
fi

if [[ $ACTION == --remove ]]; then
  [[ -f $CONFIG ]] && awg_quick down "/config/$INTERFACE.conf" >/dev/null 2>&1 || true
  # The runner container is kept alive so that --status can query awg. Remove
  # it only after the interface has been brought down.
  podman rm -f "$AWG_CONTAINER" >/dev/null 2>&1 || true
  if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    [[ -s $STATE_DIR/wan-zone.txt ]] && WAN_ZONE=$(cat "$STATE_DIR/wan-zone.txt") || WAN_ZONE=public
    if [[ $(cat "$STATE_DIR/port-added.txt" 2>/dev/null || true) == 1 ]]; then
      firewall-cmd --zone="$WAN_ZONE" --remove-port="$AWG_PORT/udp" >/dev/null 2>&1 || true
    fi
    firewall-cmd --zone=trusted --remove-interface="$INTERFACE" >/dev/null 2>&1 || true
    if [[ $(cat "$STATE_DIR/masquerade-added.txt" 2>/dev/null || true) == 1 ]]; then
      firewall-cmd --zone="$WAN_ZONE" --remove-masquerade >/dev/null 2>&1 || true
    fi
  fi
  if [[ -s $STATE_DIR/ip-forward-original.txt ]]; then
    sysctl -w "net.ipv4.ip_forward=$(cat "$STATE_DIR/ip-forward-original.txt")" >/dev/null
  fi
  echo "Standalone AmneziaWG interface removed; keys remain in $STATE_DIR"
  exit 0
fi

preflight >/dev/null

[[ $AWG_PORT =~ ^[0-9]+$ ]] && (( AWG_PORT > 0 && AWG_PORT < 65536 )) || {
  echo "Invalid AWG_PORT" >&2; exit 2;
}
[[ ! -e /sys/class/net/$INTERFACE ]] || { echo "Interface $INTERFACE already exists" >&2; exit 3; }
if [[ -z $PUBLIC_HOST ]]; then
  PUBLIC_HOST=$(curl -4fsS --max-time 10 https://api.ipify.org) || {
    echo "Set PUBLIC_HOST to the node public IPv4" >&2; exit 2;
  }
fi
if [[ -z $WAN_INTERFACE ]]; then
  WAN_INTERFACE=$(ip -4 route show default | awk '$1 == "default" {print $5; exit}')
fi
[[ -n $WAN_INTERFACE && -e /sys/class/net/$WAN_INTERFACE ]] || {
  echo "Could not determine WAN_INTERFACE" >&2; exit 2;
}
if ! command -v firewall-cmd >/dev/null || ! firewall-cmd --state >/dev/null 2>&1; then
  echo "firewalld must be active so the test does not expose an unmanaged UDP listener" >&2
  exit 2
fi

umask 077
install -d -m 700 "$STATE_DIR"
podman pull --quiet "$AWG_IMAGE" >/dev/null
podman rm -f "$AWG_CONTAINER" >/dev/null 2>&1 || true
podman run -d --name "$AWG_CONTAINER" --privileged --network host \
  -v "$STATE_DIR:/config:Z" "$AWG_IMAGE" sleep infinity >/dev/null
cleanup_container() { podman rm -f "$AWG_CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup_container EXIT
printf '%s\n' "$AWG_PORT" >"$STATE_DIR/listen-port.txt"
SERVER_PRIVATE=$(awg_exec genkey)
SERVER_PUBLIC=$(printf '%s' "$SERVER_PRIVATE" | podman exec -i "$AWG_CONTAINER" awg pubkey)
CLIENT_PRIVATE=$(awg_exec genkey)
CLIENT_PUBLIC=$(printf '%s' "$CLIENT_PRIVATE" | podman exec -i "$AWG_CONTAINER" awg pubkey)
PRESHARED_KEY=$(awg_exec genpsk)

cat >"$CONFIG" <<EOF
[Interface]
Address = $SERVER_ADDRESS
ListenPort = $AWG_PORT
PrivateKey = $SERVER_PRIVATE
Jc = $Jc
Jmin = $Jmin
Jmax = $Jmax
S1 = $S1
S2 = $S2
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4

[Peer]
PublicKey = $CLIENT_PUBLIC
PresharedKey = $PRESHARED_KEY
AllowedIPs = $CLIENT_ADDRESS
EOF

cat >"$STATE_DIR/client.conf" <<EOF
[Interface]
Address = $CLIENT_ADDRESS
DNS = 1.1.1.1, 1.0.0.1
PrivateKey = $CLIENT_PRIVATE
Jc = $Jc
Jmin = $Jmin
Jmax = $Jmax
S1 = $S1
S2 = $S2
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4

[Peer]
PublicKey = $SERVER_PUBLIC
PresharedKey = $PRESHARED_KEY
Endpoint = $PUBLIC_HOST:$AWG_PORT
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF
chmod 600 "$CONFIG" "$STATE_DIR/client.conf"

cat /proc/sys/net/ipv4/ip_forward >"$STATE_DIR/ip-forward-original.txt"
sysctl -w net.ipv4.ip_forward=1 >/dev/null
WAN_ZONE=$(firewall-cmd --get-zone-of-interface="$WAN_INTERFACE")
[[ -n $WAN_ZONE ]] || WAN_ZONE=$(firewall-cmd --get-default-zone)
printf '%s\n' "$WAN_ZONE" >"$STATE_DIR/wan-zone.txt"
if firewall-cmd --zone="$WAN_ZONE" --query-masquerade >/dev/null; then
  printf '0\n' >"$STATE_DIR/masquerade-added.txt"
else
  printf '1\n' >"$STATE_DIR/masquerade-added.txt"
fi
if firewall-cmd --zone="$WAN_ZONE" --query-port="$AWG_PORT/udp" >/dev/null; then
  printf '0\n' >"$STATE_DIR/port-added.txt"
else
  printf '1\n' >"$STATE_DIR/port-added.txt"
fi
cleanup_failed_start() {
  awg_quick down "/config/$INTERFACE.conf" >/dev/null 2>&1 || true
  if [[ $(cat "$STATE_DIR/port-added.txt" 2>/dev/null || true) == 1 ]]; then
    firewall-cmd --zone="$WAN_ZONE" --remove-port="$AWG_PORT/udp" >/dev/null 2>&1 || true
  fi
  firewall-cmd --zone=trusted --remove-interface="$INTERFACE" >/dev/null 2>&1 || true
  if [[ $(cat "$STATE_DIR/masquerade-added.txt" 2>/dev/null || true) == 1 ]]; then
    firewall-cmd --zone="$WAN_ZONE" --remove-masquerade >/dev/null 2>&1 || true
  fi
  sysctl -w "net.ipv4.ip_forward=$(cat "$STATE_DIR/ip-forward-original.txt")" >/dev/null 2>&1 || true
}
trap cleanup_failed_start ERR
if [[ $(cat "$STATE_DIR/port-added.txt") == 1 ]]; then
  firewall-cmd --zone="$WAN_ZONE" --add-port="$AWG_PORT/udp" >/dev/null
fi
if [[ $(cat "$STATE_DIR/masquerade-added.txt") == 1 ]]; then
  firewall-cmd --zone="$WAN_ZONE" --add-masquerade >/dev/null
fi
awg_quick up "/config/$INTERFACE.conf"
if ! firewall-cmd --zone=trusted --query-interface="$INTERFACE" >/dev/null; then
  firewall-cmd --zone=trusted --add-interface="$INTERFACE" >/dev/null
fi
trap - ERR
# Keep the container alive after a successful start: --status executes `awg`
# inside this container. It is removed by the explicit --remove action.
trap - EXIT

cat <<EOF
Standalone AmneziaWG listens on $PUBLIC_HOST:$AWG_PORT/udp.
Import this file into AmneziaVPN: $STATE_DIR/client.conf
On the operator/backend machine (not inside this VPN node), copy it with:
  scp root@$PUBLIC_HOST:$STATE_DIR/client.conf ./amneziawg-test.conf
Inspect handshakes and byte counters:
  $0 --status
  tcpdump -ni any udp port $AWG_PORT
Remove the test:
  $0 --remove
The cloud firewall must also allow $AWG_PORT/udp.
EOF
