#!/usr/bin/env bash
set -euo pipefail

cert_domain="${TAILSCALE_CERT_DOMAIN:-freedomvpn.taile485ac.ts.net}"
public_base_url="${PUBLIC_BASE_URL:-https://${cert_domain}}"
funnel_target="${TAILSCALE_FUNNEL_TARGET:-http://127.0.0.1:8000}"
cert_service="${TAILSCALE_CERT_SERVICE:-vpn-tailscale-cert.service}"
funnel_service="${TAILSCALE_FUNNEL_SERVICE:-vpn-tailscale-funnel.service}"

log() {
  printf '[tailscale-recover] %s\n' "$*"
}

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    log "Запустите скрипт от root: sudo $0"
    exit 1
  fi
}

run() {
  log "$*"
  "$@"
}

need_root

log "Домен: ${cert_domain}"
log "Публичный URL: ${public_base_url}"
log "Funnel target: ${funnel_target}"

run systemctl restart tailscaled.service
run tailscale status --peers=false

log "Сбрасываю старую публикацию Funnel, если она есть"
tailscale funnel reset || true

if systemctl list-unit-files "${cert_service}" >/dev/null 2>&1; then
  run systemctl start "${cert_service}"
else
  log "Unit ${cert_service} не найден, запускаю обновление сертификата напрямую"
  TAILSCALE_CERT_DOMAIN="${cert_domain}" scripts/renew_tailscale_cert.sh
fi

if systemctl list-unit-files "${funnel_service}" >/dev/null 2>&1; then
  run systemctl restart "${funnel_service}"
else
  log "Unit ${funnel_service} не найден, поднимаю Funnel напрямую"
  run tailscale funnel --bg --yes --https=443 "${funnel_target}"
fi

log "Текущий Funnel:"
tailscale funnel status || true

log "Проверяю локальный API"
curl -fsS http://127.0.0.1:8000/health
printf '\n'

log "Проверяю публичный сайт"
curl -fsSI --max-time 15 "${public_base_url}/" | sed -n '1,12p'

log "Готово"
