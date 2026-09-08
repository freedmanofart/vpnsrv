#!/usr/bin/env bash
set -euo pipefail

cert_domain="${TAILSCALE_CERT_DOMAIN:-$(tailscale status --json | sed -n 's/.*"DNSName": "\([^"]*\)".*/\1/p' | head -n1 | sed 's/\.$//')}"
cert_dir="${TAILSCALE_CERT_DIR:-/etc/ssl/tailscale}"
reload_service="${TAILSCALE_CERT_RELOAD_SERVICE:-}"

if [[ -z "$cert_domain" ]]; then
  echo "TAILSCALE_CERT_DOMAIN is empty and the local MagicDNS name was not detected" >&2
  exit 1
fi

install -d -m 0750 "$cert_dir"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

tailscale cert \
  --min-validity=336h \
  --cert-file="$work_dir/cert.pem" \
  --key-file="$work_dir/key.pem" \
  "$cert_domain"

install -m 0644 "$work_dir/cert.pem" "$cert_dir/cert.pem"
install -m 0600 "$work_dir/key.pem" "$cert_dir/key.pem"

if [[ -n "$reload_service" ]]; then
  systemctl reload "$reload_service"
fi

echo "Updated certificate for $cert_domain in $cert_dir"
