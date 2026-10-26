#!/usr/bin/env bash
set -u -o pipefail

host="freedomvpn.taile485ac.ts.net"
state_dir="/var/lib/freedomvpn-funnel-watch"
failure_file="$state_dir/consecutive-failures"
recovery_file="$state_dir/last-recovery"
known_ips_file="$state_dir/known-relay-ips"
known_ips_time_file="$state_dir/known-relay-ips-at"
dns_log_file="$state_dir/last-dns-warning"
cooldown_log_file="$state_dir/last-cooldown-warning"
lock_file="/run/lock/freedomvpn-funnel-watch.lock"
cooldown_seconds=1800
known_ips_max_age=900
failures_before_recovery=3

mkdir -p "$state_dir"
exec 9>"$lock_file"
flock -n 9 || exit 0

log() {
  logger -t freedomvpn-funnel-watch -- "$*"
}

log_dns_issue() {
  local now last=0
  now="$(date +%s)"
  [[ -r "$dns_log_file" ]] && read -r last < "$dns_log_file" || true
  if [[ ! "$last" =~ ^[0-9]+$ ]] || (( now - last >= 1800 )); then
    log "$1"
    printf '%s\n' "$now" > "$dns_log_file"
  fi
}

log_recovery_cooldown() {
  local now last=0
  now="$(date +%s)"
  [[ -r "$cooldown_log_file" ]] && read -r last < "$cooldown_log_file" || true
  if [[ ! "$last" =~ ^[0-9]+$ ]] || (( now - last >= cooldown_seconds )); then
    log "public Funnel probe still failing; 30-minute recovery cooldown active"
    printf '%s\n' "$now" > "$cooldown_log_file"
  fi
}

filter_public_ips() {
  python3 -c 'import ipaddress,sys; values=set();
for line in sys.stdin:
 value=line.strip()
 try:
  address=ipaddress.ip_address(value)
 except ValueError:
  continue
 if address.version == 4 and address.is_global: values.add(value)
print(*sorted(values), sep=chr(10))'
}

public_ips() {
  local ips now cached_at
  ips="$(dig +short +time=2 +tries=1 A "$host" 2>/dev/null | filter_public_ips || true)"
  if [[ -z "$ips" ]]; then
    ips="$(dig +short +time=2 +tries=1 @8.8.8.8 A "$host" 2>/dev/null | filter_public_ips || true)"
  fi
  if [[ -n "$ips" ]]; then
    printf '%s\n' "$ips" | sort -u > "$known_ips_file.tmp"
    mv "$known_ips_file.tmp" "$known_ips_file"
    date +%s > "$known_ips_time_file"
    cat "$known_ips_file"
    return 0
  fi

  if [[ -s "$known_ips_file" && -r "$known_ips_time_file" ]]; then
    now="$(date +%s)"
    read -r cached_at < "$known_ips_time_file" || cached_at=0
    if [[ "$cached_at" =~ ^[0-9]+$ ]] && (( now - cached_at <= known_ips_max_age )); then
      cat "$known_ips_file"
      return 0
    fi
  fi
  return 1
}

probe_public() {
  local ips ip total=0 healthy=0 code
  ips="$(public_ips)" || return 2
  while IFS= read -r ip; do
    [[ -n "$ip" ]] || continue
    total=$((total + 1))
    code="$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
      --connect-timeout 4 --max-time 8 \
      --resolve "$host:443:$ip" "https://$host/health" 2>/dev/null || true)"
    [[ "$code" == 200 ]] && healthy=$((healthy + 1))
  done <<< "$ips"
  (( total > 0 && healthy == total ))
}

probe_local() {
  curl --silent --fail --output /dev/null --connect-timeout 2 --max-time 5 \
    http://127.0.0.1:8000/health
}

probe_tailnet() {
  tailscale status --json | python3 -c \
    'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("BackendState") == "Running" else 1)'
}

run_public_probe() {
  local result=0
  probe_public || result=$?
  return "$result"
}

result=0
run_public_probe || result=$?
if (( result == 0 )); then
  rm -f "$failure_file"
  exit 0
fi
if (( result == 2 )); then
  rm -f "$failure_file"
  log_dns_issue "system DNS and fallback resolver failed; no recent relay address cache, recovery skipped"
  exit 0
fi

if ! probe_local; then
  rm -f "$failure_file"
  log "public health check failed, local API is unhealthy; Tailscale recovery skipped"
  exit 0
fi
if ! probe_tailnet; then
  rm -f "$failure_file"
  log "public health check failed, Tailscale backend is not running; recovery skipped"
  exit 0
fi

failures=0
[[ -r "$failure_file" ]] && read -r failures < "$failure_file" || true
failures=$((failures + 1))
printf '%s\n' "$failures" > "$failure_file"
if (( failures < failures_before_recovery )); then
  log "public Funnel probe failed on all relay IPs ($failures/$failures_before_recovery consecutive checks)"
  exit 0
fi

now="$(date +%s)"
last_recovery=0
[[ -r "$recovery_file" ]] && read -r last_recovery < "$recovery_file" || true
if (( now - last_recovery < cooldown_seconds )); then
  log_recovery_cooldown
  exit 0
fi
printf '%s\n' "$now" > "$recovery_file"

log "public Funnel probe failed $failures_before_recovery times on all relay IPs; refreshing Tailscale control connection"
tailscale debug break-tcp-conns || true
sleep 40
result=0
run_public_probe || result=$?
if (( result == 0 )); then
  rm -f "$failure_file"
  log "public Funnel recovered after control connection refresh"
  exit 0
fi
if (( result == 2 )); then
  rm -f "$failure_file"
  log_dns_issue "system DNS and fallback resolver failed after control connection refresh; daemon restart skipped"
  exit 0
fi

log "public Funnel still failing; restarting tailscaled and Funnel"
if ! systemctl restart tailscaled; then
  log "tailscaled restart failed"
  exit 1
fi
sleep 30
result=0
run_public_probe || result=$?
if (( result == 0 )); then
  rm -f "$failure_file"
  log "public Funnel recovered after tailscaled restart"
  exit 0
fi
if (( result == 2 )); then
  rm -f "$failure_file"
  log_dns_issue "system DNS and fallback resolver failed after tailscaled restart; recovery result is unknown"
  exit 1
fi

log "public Funnel is still unavailable after Tailscale recovery"
exit 1
