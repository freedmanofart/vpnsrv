#!/usr/bin/env bash
set -euo pipefail

# Apply a default-deny firewalld zone without risking an unattended SSH lockout.
# A systemd rollback timer is armed before the public interface is moved.  The
# operator must confirm from a NEW SSH session; otherwise the old zone is
# restored automatically.

ZONE=${ZONE:-vpn-node}
STATE_DIR=${STATE_DIR:-/run/vpn-node-firewall}
ROLLBACK_SECONDS=${ROLLBACK_SECONDS:-300}
XRAY_TCP_PORTS=${XRAY_TCP_PORTS:-443}
SSH_ALLOW_CIDRS=${SSH_ALLOW_CIDRS:-}
SSH_ACCESS_MODE=${SSH_ACCESS_MODE:-key-only}
INSTALL_FIREWALL=${INSTALL_FIREWALL:-0}
ROLLBACK_UNIT=vpn-node-firewall-rollback
SELF_PATH=$(readlink -f "$0")

usage() {
  cat >&2 <<EOF
Usage: $0 [--apply|--confirm|--status|--rollback]

Apply example (keep this SSH session open):
  $0 --apply                         # key-only SSH from any address
  SSH_ACCESS_MODE=cidr SSH_ALLOW_CIDRS=198.51.100.7/32 $0 --apply
Then log in through a second SSH session and run:
  $0 --confirm
EOF
}

ACTION=${1:---apply}
case "$ACTION" in --apply|--confirm|--status|--rollback) ;; *) usage; exit 2 ;; esac
(( $# <= 1 )) || { usage; exit 2; }
[[ $EUID -eq 0 ]] || { echo "Run as root on the VPN node" >&2; exit 2; }

status() {
  firewall-cmd --zone="$ZONE" --list-all 2>/dev/null || true
  systemctl status "$ROLLBACK_UNIT.timer" --no-pager 2>/dev/null || true
}

rollback() {
  [[ -s $STATE_DIR/interface ]] || { echo "No pending firewall change"; exit 0; }
  local iface old_zone restored_zone expected_zone
  iface=$(cat "$STATE_DIR/interface")
  old_zone=$(cat "$STATE_DIR/old-zone" 2>/dev/null || true)
  if [[ -f $STATE_DIR/restore-zone ]]; then
    # A reapply edits the zone that is already active.  Moving the interface
    # back to that same zone is not enough: restore the permanent allow-list
    # and target that existed before this invocation changed them.
    local port rule old_target
    for port in $(firewall-cmd --permanent --zone="$ZONE" --list-ports); do
      if ! firewall-cmd --permanent --zone="$ZONE" --remove-port="$port" >/dev/null; then
        echo "Failed to clear port $port while restoring zone $ZONE; rollback state retained in $STATE_DIR." >&2
        return 1
      fi
    done
    while IFS= read -r rule; do
      [[ -n $rule ]] || continue
      if ! firewall-cmd --permanent --zone="$ZONE" --remove-rich-rule="$rule" >/dev/null; then
        echo "Failed to clear a rich rule while restoring zone $ZONE; rollback state retained in $STATE_DIR." >&2
        return 1
      fi
    done < <(firewall-cmd --permanent --zone="$ZONE" --list-rich-rules)
    while IFS= read -r port; do
      [[ -n $port ]] || continue
      if ! firewall-cmd --permanent --zone="$ZONE" --add-port="$port" >/dev/null; then
        echo "Failed to restore port $port in zone $ZONE; rollback state retained in $STATE_DIR." >&2
        return 1
      fi
    done <"$STATE_DIR/zone-ports"
    while IFS= read -r rule; do
      [[ -n $rule ]] || continue
      if ! firewall-cmd --permanent --zone="$ZONE" --add-rich-rule="$rule" >/dev/null; then
        echo "Failed to restore a rich rule in zone $ZONE; rollback state retained in $STATE_DIR." >&2
        return 1
      fi
    done <"$STATE_DIR/zone-rich-rules"
    old_target=$(cat "$STATE_DIR/zone-target")
    if ! firewall-cmd --permanent --zone="$ZONE" --set-target="$old_target" >/dev/null; then
      echo "Failed to restore target $old_target in zone $ZONE; rollback state retained in $STATE_DIR." >&2
      return 1
    fi
  fi
  if [[ -n $old_zone ]]; then
    if ! firewall-cmd --permanent --zone="$old_zone" --change-interface="$iface" >/dev/null; then
      echo "Failed to restore interface $iface to zone $old_zone; rollback state retained in $STATE_DIR." >&2
      return 1
    fi
    expected_zone=$old_zone
  else
    if ! firewall-cmd --permanent --zone="$ZONE" --remove-interface="$iface" >/dev/null; then
      echo "Failed to remove interface $iface from zone $ZONE; rollback state retained in $STATE_DIR." >&2
      return 1
    fi
    expected_zone=$(firewall-cmd --get-default-zone)
  fi
  if ! firewall-cmd --reload; then
    echo "Failed to reload firewalld; rollback state retained in $STATE_DIR." >&2
    return 1
  fi
  restored_zone=$(firewall-cmd --get-zone-of-interface="$iface" 2>/dev/null || true)
  if [[ $restored_zone != "$expected_zone" ]]; then
    echo "Rollback verification failed: interface $iface is in ${restored_zone:-no zone}, expected $expected_zone; state retained in $STATE_DIR." >&2
    return 1
  fi
  rm -rf "$STATE_DIR"
  echo "Firewall change rolled back; interface $iface restored to ${old_zone:-the default zone}."
}

case "$ACTION" in
  --status) status; exit 0 ;;
  --rollback) rollback; exit 0 ;;
  --confirm)
    [[ -f $STATE_DIR/pending ]] || { echo "No firewall change is awaiting confirmation" >&2; exit 3; }
    # A different SSH connection proves that the new rule admits a fresh login.
    apply_connection=$(cat "$STATE_DIR/apply-connection")
    [[ -n ${SSH_CONNECTION:-} && ${SSH_CONNECTION} != "$apply_connection" ]] || {
      echo "Refusing confirmation: connect through a NEW SSH session first." >&2; exit 3;
    }
    systemctl stop "$ROLLBACK_UNIT.timer" >/dev/null 2>&1 || true
    rm -f "$STATE_DIR/pending"
    echo "Firewall confirmed from a new SSH session; automatic rollback cancelled."
    exit 0
    ;;
esac

case "$SSH_ACCESS_MODE" in
  key-only) ;;
  cidr) [[ -n $SSH_ALLOW_CIDRS ]] || {
    echo "SSH_ALLOW_CIDRS is required in SSH_ACCESS_MODE=cidr" >&2; exit 2;
  } ;;
  *) echo "SSH_ACCESS_MODE must be key-only or cidr" >&2; exit 2 ;;
esac
[[ ! -f $STATE_DIR/pending ]] || {
  echo "A firewall change is already pending; confirm or roll it back first." >&2; exit 3;
}
[[ $ROLLBACK_SECONDS =~ ^[0-9]+$ ]] && (( ROLLBACK_SECONDS >= 60 && ROLLBACK_SECONDS <= 1800 )) || {
  echo "ROLLBACK_SECONDS must be between 60 and 1800" >&2; exit 2;
}

if ! command -v firewall-cmd >/dev/null; then
  [[ $INSTALL_FIREWALL == 1 ]] || {
    echo "firewalld is missing; rerun with INSTALL_FIREWALL=1 to install it" >&2; exit 2;
  }
  command -v dnf >/dev/null
  dnf install -y firewalld
fi
command -v python3 >/dev/null
command -v systemd-run >/dev/null
command -v sshd >/dev/null
systemctl enable --now firewalld

CURRENT_IP=${SSH_CONNECTION%% *}
[[ -n $CURRENT_IP ]] || { echo "Run --apply from an active SSH session" >&2; exit 2; }
if [[ $SSH_ACCESS_MODE == cidr ]]; then
  IFS=, read -r -a cidrs <<<"$SSH_ALLOW_CIDRS"
  CIDRS_TEXT=$SSH_ALLOW_CIDRS CURRENT_IP=$CURRENT_IP python3 - <<'PY'
import ipaddress, os
networks=[]
for raw in os.environ["CIDRS_TEXT"].split(","):
    network=ipaddress.ip_network(raw.strip(), strict=False)
    if network.prefixlen == 0:
        raise SystemExit("Refusing a world-open SSH CIDR")
    networks.append(network)
current=os.environ.get("CURRENT_IP", "")
if not current:
    raise SystemExit("Run --apply from SSH so the current source can be verified")
address=ipaddress.ip_address(current)
if not any(address in network for network in networks):
    raise SystemExit(f"Current SSH source {address} is not in SSH_ALLOW_CIDRS")
PY
else
  # A globally reachable SSH port is safe only when sshd itself rejects every
  # password and keyboard-interactive login.  Do not silently weaken sshd here.
  SSHD_EFFECTIVE=$(sshd -T)
  grep -qx 'pubkeyauthentication yes' <<<"$SSHD_EFFECTIVE" || {
    echo "sshd must have PubkeyAuthentication yes" >&2; exit 2;
  }
  grep -qx 'passwordauthentication no' <<<"$SSHD_EFFECTIVE" || {
    echo "Refusing public SSH: set PasswordAuthentication no first" >&2; exit 2;
  }
  grep -qx 'kbdinteractiveauthentication no' <<<"$SSHD_EFFECTIVE" || {
    echo "Refusing public SSH: set KbdInteractiveAuthentication no first" >&2; exit 2;
  }
fi

SSH_PORT=$(sshd -T | awk '$1 == "port" {print $2; exit}')
IFACE=$(ip -4 route show default | awk '$1 == "default" {print $5; exit}')
[[ $SSH_PORT =~ ^[0-9]+$ && -n $IFACE ]] || { echo "Could not detect SSH port or WAN interface" >&2; exit 2; }
for port in ${XRAY_TCP_PORTS//,/ }; do
  [[ $port =~ ^[0-9]+$ ]] && (( port > 0 && port < 65536 )) || { echo "Invalid XRAY_TCP_PORTS" >&2; exit 2; }
done

install -d -m 700 "$STATE_DIR"
OLD_ZONE=$(firewall-cmd --get-zone-of-interface="$IFACE" 2>/dev/null || true)
printf '%s\n' "$IFACE" >"$STATE_DIR/interface"
printf '%s\n' "$OLD_ZONE" >"$STATE_DIR/old-zone"
printf '%s\n' "${SSH_CONNECTION:-}" >"$STATE_DIR/apply-connection"
touch "$STATE_DIR/pending"

# If the interface is already in our zone, rollback cannot recover the old
# configuration by moving it.  Snapshot everything this script replaces so
# rollback can reconstruct the previously confirmed configuration.
rm -f "$STATE_DIR/restore-zone" "$STATE_DIR/zone-ports" \
  "$STATE_DIR/zone-rich-rules" "$STATE_DIR/zone-target"
if [[ $OLD_ZONE == "$ZONE" ]]; then
  firewall-cmd --permanent --zone="$ZONE" --list-ports >"$STATE_DIR/zone-ports"
  firewall-cmd --permanent --zone="$ZONE" --list-rich-rules >"$STATE_DIR/zone-rich-rules"
  firewall-cmd --permanent --zone="$ZONE" --get-target >"$STATE_DIR/zone-target"
  touch "$STATE_DIR/restore-zone"
fi

firewall-cmd --permanent --new-zone="$ZONE" >/dev/null 2>&1 || true
firewall-cmd --permanent --zone="$ZONE" --set-target=DROP >/dev/null
# This zone is owned by this script.  Remove its old allow-list before adding
# the requested one so reapplying with narrower settings cannot leave ports or
# source CIDRs from an earlier confirmed run exposed.
existing_ports=$(firewall-cmd --permanent --zone="$ZONE" --list-ports)
for port in $existing_ports; do
  firewall-cmd --permanent --zone="$ZONE" --remove-port="$port" >/dev/null
done
existing_rich_rules=$(firewall-cmd --permanent --zone="$ZONE" --list-rich-rules)
while IFS= read -r rule; do
  [[ -n $rule ]] || continue
  firewall-cmd --permanent --zone="$ZONE" --remove-rich-rule="$rule" >/dev/null
done <<<"$existing_rich_rules"
for port in ${XRAY_TCP_PORTS//,/ }; do
  firewall-cmd --permanent --zone="$ZONE" --add-port="$port/tcp" >/dev/null
done
if [[ $SSH_ACCESS_MODE == key-only ]]; then
  firewall-cmd --permanent --zone="$ZONE" --add-port="$SSH_PORT/tcp" >/dev/null
else
  for cidr in "${cidrs[@]}"; do
    cidr=${cidr//[[:space:]]/}
    family=ipv4; [[ $cidr == *:* ]] && family=ipv6
    firewall-cmd --permanent --zone="$ZONE" \
      --add-rich-rule="rule family=$family source address=$cidr port port=$SSH_PORT protocol=tcp accept" >/dev/null
  done
fi

# Arm rollback BEFORE changing the active interface.  The transient timer calls
# this same, already-installed script and survives loss of the SSH connection.
systemd-run --unit="$ROLLBACK_UNIT" --on-active="${ROLLBACK_SECONDS}s" \
  --setenv="STATE_DIR=$STATE_DIR" --setenv="ZONE=$ZONE" \
  "$SELF_PATH" --rollback >/dev/null
firewall-cmd --permanent --zone="$ZONE" --change-interface="$IFACE" >/dev/null
firewall-cmd --reload

cat <<EOF
Firewall is active on $IFACE with DROP-by-default.
SSH port $SSH_PORT access mode: $SSH_ACCESS_MODE
Public Xray TCP ports: $XRAY_TCP_PORTS

DO NOT CLOSE THIS SESSION. Open a second terminal, log in again, then run there:
  $0 --confirm
If confirmation is not received, the old zone is restored automatically in
$ROLLBACK_SECONDS seconds. Manual rollback from this session: $0 --rollback
EOF
