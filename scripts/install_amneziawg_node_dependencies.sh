#!/usr/bin/env bash
set -euo pipefail

# AmneziaWG is intentionally run in a Podman container.  This avoids DKMS and
# kernel-devel entirely (the broken dkms.conf shipped by older COPR packages
# must therefore never be parsed on the host).
# Legacy packages (amneziawg-dkms amneziawg-tools) are deliberately not used.
# kernel_devel="kernel-devel-$kernel_release" is intentionally obsolete here.
[[ $EUID -eq 0 ]] || { echo "Run as root on the VPN node" >&2; exit 2; }
[[ ${INSTALL_AWG:-0} == 1 ]] || {
  echo "Installation not confirmed. Re-run with INSTALL_AWG=1." >&2
  exit 2
}
command -v podman >/dev/null || {
  if command -v dnf >/dev/null; then dnf install -y podman; else
    echo "Podman is required (install podman with your OS package manager)" >&2; exit 2
  fi
}
AWG_IMAGE=${AWG_IMAGE:-docker.io/amneziavpn/amneziawg-go:latest}
podman pull "$AWG_IMAGE"
printf '%s\n' "$AWG_IMAGE" >/etc/vpn-amneziawg-image
echo "AmneziaWG container image pulled: $AWG_IMAGE"
echo "No DKMS, kernel-devel, or host awg packages are installed."
