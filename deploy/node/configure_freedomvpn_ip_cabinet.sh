#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root on the VPN node." >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_conf="${1:-${script_dir}/freedomvpn_ip_cabinet.conf}"
main_conf="/etc/nginx/conf.d/kit.conf"
proxy_conf="/etc/nginx/conf.d/zz-freedomvpn-ip-cabinet.conf"
backup_dir="/var/backups/freedomvpn-nginx/$(date -u +%Y%m%dT%H%M%SZ)"

if [[ ! -s "/root/cert/kit-ip/fullchain.pem" || ! -s "/root/cert/kit-ip/privkey.pem" ]]; then
  echo "The IP TLS certificate is missing from /root/cert/kit-ip." >&2
  exit 1
fi
if [[ ! -s "${source_conf}" || ! -f "${main_conf}" ]]; then
  echo "The cabinet Nginx fragment or node Nginx configuration is missing." >&2
  exit 1
fi

install -d -m 0700 "${backup_dir}"
cp -p "${main_conf}" "${backup_dir}/kit.conf"
if [[ -f "${proxy_conf}" ]]; then
  cp -p "${proxy_conf}" "${backup_dir}/zz-freedomvpn-ip-cabinet.conf"
fi

rollback() {
  cp -p "${backup_dir}/kit.conf" "${main_conf}"
  if [[ -f "${backup_dir}/zz-freedomvpn-ip-cabinet.conf" ]]; then
    cp -p "${backup_dir}/zz-freedomvpn-ip-cabinet.conf" "${proxy_conf}"
  else
    rm -f "${proxy_conf}"
  fi
}

if grep -q '/root/cert/kit-nipio/fullchain.pem' "${main_conf}"; then
  sed -i 's#/root/cert/kit-nipio/fullchain.pem#/root/cert/kit-ip/fullchain.pem#' "${main_conf}"
  sed -i 's#/root/cert/kit-nipio/privkey.pem#/root/cert/kit-ip/privkey.pem#' "${main_conf}"
fi
install -o root -g root -m 0644 "${source_conf}" "${proxy_conf}"

if ! nginx -t; then
  rollback
  exit 1
fi
if ! systemctl reload nginx; then
  rollback
  nginx -t || true
  systemctl reload nginx || true
  exit 1
fi

echo "Cabinet proxy enabled for https://176.108.250.142/"
echo "Nginx backup: ${backup_dir}"
