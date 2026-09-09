#!/usr/bin/env bash
set -euo pipefail

# Runs on the master host. The API container reads the generated JSON through
# the existing /var/backups/vpn-service bind mount.

health_file="${TAILSCALE_CERT_HEALTH_FILE:-/var/backups/vpn-service/tailscale-cert-health.json}"
cert_file="${TAILSCALE_CERT_FILE:-/etc/ssl/tailscale/cert.pem}"
warn_days="${TAILSCALE_CERT_WARN_DAYS:-7}"

timer_state="$(systemctl is-active vpn-tailscale-cert.timer 2>/dev/null || true)"
service_result="$(systemctl show vpn-tailscale-cert.service --property=Result --value 2>/dev/null || true)"
service_status="$(systemctl show vpn-tailscale-cert.service --property=ExecMainStatus --value 2>/dev/null || true)"
next_run="$(systemctl show vpn-tailscale-cert.timer --property=NextElapseUSecRealtime --value 2>/dev/null || true)"
last_trigger="$(systemctl show vpn-tailscale-cert.timer --property=LastTriggerUSec --value 2>/dev/null || true)"

cert_not_after=""
cert_seconds_left="-1"
if [[ -r "${cert_file}" ]] && command -v openssl >/dev/null 2>&1; then
  cert_not_after="$(openssl x509 -in "${cert_file}" -noout -enddate 2>/dev/null | sed 's/^notAfter=//' || true)"
  if [[ -n "${cert_not_after}" ]]; then
    cert_epoch="$(date -d "${cert_not_after}" +%s 2>/dev/null || true)"
    if [[ -n "${cert_epoch}" ]]; then
      cert_seconds_left="$((cert_epoch - $(date +%s)))"
    fi
  fi
fi

export HEALTH_FILE="${health_file}"
export CERT_FILE="${cert_file}"
export WARN_DAYS="${warn_days}"
export TIMER_STATE="${timer_state}"
export SERVICE_RESULT="${service_result}"
export SERVICE_STATUS="${service_status}"
export NEXT_RUN="${next_run}"
export LAST_TRIGGER="${last_trigger}"
export CERT_NOT_AFTER="${cert_not_after}"
export CERT_SECONDS_LEFT="${cert_seconds_left}"

python3 - <<'PY'
from datetime import datetime, timezone
import json
import os
from pathlib import Path

health_file = Path(os.environ["HEALTH_FILE"])
cert_file = Path(os.environ["CERT_FILE"])
timer_state = os.environ.get("TIMER_STATE") or "unknown"
service_result = os.environ.get("SERVICE_RESULT") or "unknown"
service_status = os.environ.get("SERVICE_STATUS") or "unknown"
next_run = os.environ.get("NEXT_RUN") or "unknown"
last_trigger = os.environ.get("LAST_TRIGGER") or "unknown"
cert_not_after = os.environ.get("CERT_NOT_AFTER") or "unknown"
seconds_left = int(os.environ.get("CERT_SECONDS_LEFT", "-1"))
warn_seconds = int(os.environ.get("WARN_DAYS", "7")) * 86400

status = "online"
problems = []
if timer_state != "active":
    status = "offline"
    problems.append(f"timer={timer_state}")
if service_result not in {"success", "unknown"} or service_status not in {"0", "unknown"}:
    status = "offline"
    problems.append(f"service result={service_result}, exit={service_status}")
if not cert_file.is_file() or seconds_left < 0:
    status = "offline"
    problems.append(f"certificate unreadable: {cert_file}")
elif seconds_left <= 0:
    status = "offline"
    problems.append("certificate expired")
elif seconds_left < warn_seconds and status == "online":
    status = "degraded"
    problems.append(f"certificate expires in {seconds_left // 86400} days")

summary = "; ".join(problems) if problems else "timer active, last renewal successful"
details = (
    f"{summary}; cert until {cert_not_after}; next run {next_run}; "
    f"last trigger {last_trigger}"
)
payload = {
    "status": status,
    "details": details,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "timer_state": timer_state,
    "service_result": service_result,
    "service_exit_status": service_status,
    "certificate_file": str(cert_file),
    "certificate_not_after": cert_not_after,
    "certificate_seconds_left": seconds_left,
    "next_run": next_run,
    "last_trigger": last_trigger,
}

health_file.parent.mkdir(parents=True, exist_ok=True)
temporary = health_file.with_suffix(health_file.suffix + ".tmp")
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
temporary.chmod(0o644)
temporary.replace(health_file)
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY
