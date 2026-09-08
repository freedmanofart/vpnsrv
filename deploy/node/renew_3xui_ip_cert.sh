#!/usr/bin/env bash
set -euo pipefail

# Renew the short-lived Let's Encrypt certificate issued directly for a 3x-ui
# node public IP and reinstall it into the paths used by the panel.
#
# Run on the 3x-ui node as root. Designed for a systemd timer every 5 days.

ip_address="${THREEXUI_IP_CERT_ADDRESS:-$(curl -fsS --max-time 10 https://api.ipify.org || true)}"
cert_dir="${THREEXUI_IP_CERT_DIR:-/root/cert/ip}"
acme_home="${ACME_HOME:-/root/.acme.sh}"
reload_cmd="${THREEXUI_IP_CERT_RELOAD_CMD:-systemctl restart x-ui}"
export HOME="${THREEXUI_IP_CERT_HOME:-/root}"

if [[ -z "${ip_address}" ]]; then
  echo "THREEXUI_IP_CERT_ADDRESS is empty and public IPv4 auto-detection failed" >&2
  exit 1
fi

if [[ "${ip_address}" != *.* ]]; then
  echo "Detected address '${ip_address}' does not look like an IPv4 address" >&2
  exit 1
fi

if [[ ! -x "${acme_home}/acme.sh" ]]; then
  echo "acme.sh was not found at ${acme_home}/acme.sh" >&2
  exit 1
fi

echo "[3xui-ip-cert] renewing certificate for ${ip_address}"

"${acme_home}/acme.sh" \
  --home "${acme_home}" \
  --set-default-ca \
  --server letsencrypt

if ! "${acme_home}/acme.sh" \
  --home "${acme_home}" \
  --renew \
  -d "${ip_address}" \
  --ecc \
  --force; then
  echo "[3xui-ip-cert] renew failed; trying initial issue for ${ip_address}"
  "${acme_home}/acme.sh" \
    --home "${acme_home}" \
    --issue \
    --standalone \
    --server letsencrypt \
    --keylength ec-256 \
    -d "${ip_address}" \
    --force
fi

install -d -m 0750 "${cert_dir}"

"${acme_home}/acme.sh" \
  --home "${acme_home}" \
  --install-cert \
  -d "${ip_address}" \
  --ecc \
  --key-file "${cert_dir}/privkey.pem" \
  --fullchain-file "${cert_dir}/fullchain.pem" \
  --reloadcmd "${reload_cmd}"

echo "[3xui-ip-cert] installed:"
echo "[3xui-ip-cert]   cert: ${cert_dir}/fullchain.pem"
echo "[3xui-ip-cert]   key:  ${cert_dir}/privkey.pem"

if command -v x-ui >/dev/null 2>&1; then
  # Keep panel/subscription certificate paths aligned with the menu action:
  # SSL Certificate -> Get SSL for IP Address -> set certificate for panel.
  x-ui setting -cert "${cert_dir}/fullchain.pem" -key "${cert_dir}/privkey.pem" >/dev/null 2>&1 || true
  x-ui setting -subCert "${cert_dir}/fullchain.pem" -subKey "${cert_dir}/privkey.pem" >/dev/null 2>&1 || true
fi

if command -v openssl >/dev/null 2>&1; then
  openssl x509 -in "${cert_dir}/fullchain.pem" -noout -subject -issuer -dates || true
fi

echo "[3xui-ip-cert] done"
