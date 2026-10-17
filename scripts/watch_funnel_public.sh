#!/usr/bin/env bash
set -u -o pipefail

host="freedomvpn.taile485ac.ts.net"
state_dir="/var/lib/freedomvpn-funnel-watch"
failure_file="$state_dir/consecutive-failures"
recovery_file="$state_dir/last-recovery"
lock_file="/run/lock/freedomvpn-funnel-watch.lock"
cooldown_seconds=900

mkdir -p "$state_dir"
exec 9>"$lock_file"
flock -n 9 || exit 0

log() {
  logger -t freedomvpn-funnel-watch -- "$*"
}

public_ips() {
  local ips
  ips="$(dig +short +time=2 +tries=1 @1.1.1.1 A "$host" 2>/dev/null | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/')"
  if [[ -z "$ips" ]]; then
    ips="$(dig +short +time=2 +tries=1 @8.8.8.8 A "$host" 2>/dev/null | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/')"
  fi
  [[ -n "$ips" ]] || return 1
  printf '%s\n' "$ips" | sort -u
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
  log "public DNS lookup failed; recovery skipped"
  exit 0
fi

if ! probe_local; then
  log "public health check failed, local API is unhealthy; Tailscale recovery skipped"
  exit 0
fi
if ! probe_tailnet; then
  log "public health check failed, Tailscale backend is not running; recovery skipped"
  exit 0
fi

failures=0
[[ -r "$failure_file" ]] && read -r failures < "$failure_file" || true
failures=$((failures + 1))
printf '%s\n' "$failures" > "$failure_file"
if (( failures < 2 )); then
  log "public Funnel probe failed on all relay IPs ($failures/2 consecutive checks)"
  exit 0
fi

now="$(date +%s)"
last_recovery=0
[[ -r "$recovery_file" ]] && read -r last_recovery < "$recovery_file" || true
if (( now - last_recovery < cooldown_seconds )); then
  log "public Funnel probe still failing; recovery cooldown active"
  exit 0
fi
printf '%s\n' "$now" > "$recovery_file"

log "public Funnel probe failed twice on all relay IPs; refreshing Tailscale control connection"
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
  log "public DNS lookup failed after control connection refresh; daemon restart skipped"
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
  log "public DNS lookup failed after tailscaled restart; recovery result is unknown"
  exit 1
fi

log "public Funnel is still unavailable after Tailscale recovery"
exit 1
