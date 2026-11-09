#!/usr/bin/env bash
# Продлевает сертификат внутренней панели child и перечитывает Nginx при замене.
set -euo pipefail
umask 077

tailnet_name=${1:?Укажите полное имя child в Tailscale}
[[ $tailnet_name =~ ^[a-zA-Z0-9.-]+\.ts\.net$ ]] || exit 2
certificate_directory=/root/cert/tailscale
install -d -m 0700 "$certificate_directory"
previous=$(sha256sum "$certificate_directory/fullchain.pem" 2>/dev/null | awk '{print $1}' || true)
tailscale cert --min-validity=720h \
  --cert-file="$certificate_directory/fullchain.pem" \
  --key-file="$certificate_directory/privkey.pem" "$tailnet_name"
chmod 0600 "$certificate_directory/fullchain.pem" "$certificate_directory/privkey.pem"
current=$(sha256sum "$certificate_directory/fullchain.pem" | awk '{print $1}')
if [[ $current != "$previous" ]]; then
  nginx -t
  systemctl reload nginx
fi
