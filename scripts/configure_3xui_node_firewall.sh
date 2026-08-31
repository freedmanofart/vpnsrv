#!/usr/bin/env bash
set -Eeuo pipefail

PROGRAM=${0##*/}
SELF=$(readlink -f "$0")
STATE_DIR=/var/lib/vpn-node-firewall-guard
DEFAULT_PORT=60628
DEFAULT_TIMEOUT=180

usage() {
  cat <<EOF
Использование:
  sudo $PROGRAM apply --master-ip <TAILSCALE_IPV4> [--port 60628] [--timeout 180]
  sudo $PROGRAM confirm <token>
  sudo $PROGRAM rollback <token>
  sudo $PROGRAM status <token>

apply включает firewalld, сохраняет SSH и разрешает порт панели только от
Tailscale IPv4 master. Если confirm не выполнен до истечения timeout, изменения
автоматически откатываются, а исходное состояние службы firewalld восстанавливается.
EOF
}

fail() {
  echo "ОШИБКА: $*" >&2
  exit 1
}

require_root() {
  [[ ${EUID} -eq 0 ]] || fail "запустите скрипт через sudo"
}

validate_token() {
  [[ ${1:-} =~ ^[A-Za-z0-9._-]+$ ]] || fail "некорректный token"
}

state_file() {
  printf '%s/%s.env\n' "$STATE_DIR" "$1"
}

load_state() {
  local token=$1 file
  validate_token "$token"
  file=$(state_file "$token")
  [[ -f $file ]] || fail "состояние $token не найдено"
  # Файл создаётся этим скриптом, доступен только root и содержит только
  # проверенные числа/IP. Не изменяйте его вручную.
  # shellcheck disable=SC1090
  source "$file"
}

unit_name() {
  printf 'vpn-node-firewall-rollback-%s' "$1"
}

rollback() {
  local token=$1 file unit
  load_state "$token"
  file=$(state_file "$token")
  unit=$(unit_name "$token")

  systemctl start firewalld
  if [[ $ADDED_RULE == 1 ]]; then
    firewall-cmd --permanent --zone=public \
      --remove-rich-rule="$RICH_RULE" >/dev/null 2>&1 || true
  fi
  if [[ $ADDED_SSH == 1 ]]; then
    firewall-cmd --permanent --zone=public \
      --remove-service=ssh >/dev/null 2>&1 || true
  fi
  firewall-cmd --reload >/dev/null

  if [[ $WAS_ENABLED == 0 ]]; then
    systemctl disable firewalld >/dev/null 2>&1 || true
  fi
  if [[ $WAS_ACTIVE == 0 ]]; then
    systemctl stop firewalld
  fi

  rm -f "$file"
  systemctl stop "$unit.timer" >/dev/null 2>&1 || true
  echo "Откат $token выполнен; исходное состояние firewalld восстановлено."
}

confirm() {
  local token=$1 file unit
  load_state "$token"
  file=$(state_file "$token")
  unit=$(unit_name "$token")
  systemctl stop "$unit.timer" >/dev/null 2>&1 || true
  rm -f "$file"
  echo "Изменения подтверждены. Автоматический откат $token отменён."
}

status() {
  local token=$1 unit
  load_state "$token"
  unit=$(unit_name "$token")
  systemctl status "$unit.timer" --no-pager || true
  firewall-cmd --zone=public --list-all
}

apply_rules() {
  local master_ip='' port=$DEFAULT_PORT timeout=$DEFAULT_TIMEOUT
  while (($#)); do
    case $1 in
      --master-ip)
        (($# >= 2)) || fail "для --master-ip требуется значение"
        master_ip=$2
        shift 2
        ;;
      --port)
        (($# >= 2)) || fail "для --port требуется значение"
        port=$2
        shift 2
        ;;
      --timeout)
        (($# >= 2)) || fail "для --timeout требуется значение"
        timeout=$2
        shift 2
        ;;
      *) fail "неизвестный параметр: $1" ;;
    esac
  done

  [[ $master_ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] \
    || fail "--master-ip должен быть IPv4-адресом"
  [[ $port =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535)) \
    || fail "порт должен находиться в диапазоне 1..65535"
  [[ $timeout =~ ^[0-9]+$ ]] && ((timeout >= 60 && timeout <= 3600)) \
    || fail "timeout должен находиться в диапазоне 60..3600 секунд"

  command -v firewall-cmd >/dev/null || fail "firewalld не установлен"
  command -v systemd-run >/dev/null || fail "systemd-run не установлен"

  install -d -m 700 "$STATE_DIR" /var/backups/firewalld
  local token file unit rich_rule was_active=0 was_enabled=0 added_ssh=0 added_rule=0
  token=$(date -u +%Y%m%dT%H%M%SZ)-$$
  file=$(state_file "$token")
  unit=$(unit_name "$token")
  rich_rule="rule family=\"ipv4\" source address=\"$master_ip/32\" port port=\"$port\" protocol=\"tcp\" accept"

  systemctl is-active --quiet firewalld && was_active=1
  systemctl is-enabled --quiet firewalld && was_enabled=1
  systemctl enable --now firewalld

  firewall-cmd --list-all-zones \
    >"/var/backups/firewalld/before-$token.txt"

  firewall-cmd --permanent --zone=public --query-service=ssh >/dev/null \
    || added_ssh=1
  firewall-cmd --permanent --zone=public --query-rich-rule="$rich_rule" >/dev/null \
    || added_rule=1

  umask 077
  {
    printf 'WAS_ACTIVE=%q\n' "$was_active"
    printf 'WAS_ENABLED=%q\n' "$was_enabled"
    printf 'ADDED_SSH=%q\n' "$added_ssh"
    printf 'ADDED_RULE=%q\n' "$added_rule"
    printf 'RICH_RULE=%q\n' "$rich_rule"
  } >"$file"

  systemd-run --quiet --unit="$unit" --on-active="${timeout}s" \
    "$SELF" rollback "$token"

  if [[ $added_ssh == 1 ]]; then
    firewall-cmd --permanent --zone=public --add-service=ssh >/dev/null
  fi
  if [[ $added_rule == 1 ]]; then
    firewall-cmd --permanent --zone=public --add-rich-rule="$rich_rule" >/dev/null
  fi
  firewall-cmd --reload >/dev/null

  echo "Правила применены. Token аварийного отката: $token"
  echo "Проверьте НОВУЮ SSH-сессию и доступ master → child."
  echo "После успешной проверки выполните:"
  echo "  sudo $SELF confirm $token"
  echo "Без подтверждения откат произойдёт автоматически через $timeout секунд."
}

require_root
command_name=${1:-}
[[ -n $command_name ]] || { usage; exit 2; }
shift

case $command_name in
  apply) apply_rules "$@" ;;
  confirm) (($# == 1)) || fail "укажите token"; confirm "$1" ;;
  rollback) (($# == 1)) || fail "укажите token"; rollback "$1" ;;
  status) (($# == 1)) || fail "укажите token"; status "$1" ;;
  -h|--help|help) usage ;;
  *) usage; fail "неизвестная команда: $command_name" ;;
esac
