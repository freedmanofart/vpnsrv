#!/usr/bin/env bash
set -euo pipefail

# Operator wrapper for renewing the public master/site certificate.
# Run on the master host, not inside the api container.

cert_domain="${TAILSCALE_CERT_DOMAIN:-freedomvpn.taile485ac.ts.net}"
funnel_service="${TAILSCALE_FUNNEL_SERVICE:-vpn-tailscale-funnel.service}"
cert_service="${TAILSCALE_CERT_SERVICE:-vpn-tailscale-cert.service}"

echo "[master-cert] renewing certificate for ${cert_domain}"

if systemctl list-unit-files "${cert_service}" >/dev/null 2>&1; then
  systemctl start "${cert_service}"
else
  TAILSCALE_CERT_DOMAIN="${cert_domain}" scripts/renew_tailscale_cert.sh
fi

if systemctl list-unit-files "${funnel_service}" >/dev/null 2>&1; then
  systemctl restart "${funnel_service}"
fi

if command -v openssl >/dev/null 2>&1 && [[ -f /etc/ssl/tailscale/cert.pem ]]; then
  openssl x509 -in /etc/ssl/tailscale/cert.pem -noout -subject -issuer -dates || true
fi

curl -fsS --max-time 15 "https://${cert_domain}/health"
echo
echo "[master-cert] done"
