#!/usr/bin/env bash
set -euo pipefail

# Deliberately Fedora/RHEL-only: use the official AmneziaVPN COPR and never
# install packages merely because the copy helper was run.
[[ $EUID -eq 0 ]] || { echo "Run as root on the VPN node" >&2; exit 2; }
[[ ${INSTALL_AWG:-0} == 1 ]] || {
  echo "Installation not confirmed. Re-run with INSTALL_AWG=1." >&2
  exit 2
}
[[ -r /etc/os-release ]] || { echo "Cannot identify this operating system" >&2; exit 2; }
. /etc/os-release
case ${ID:-} in
  fedora|rhel|centos|rocky|almalinux) ;;
  *) echo "This installer supports Fedora/RHEL-family nodes only" >&2; exit 2 ;;
esac
command -v dnf >/dev/null || { echo "dnf is required" >&2; exit 2; }

dnf install -y dnf-plugins-core
dnf copr enable -y amneziavpn/amneziawg
kernel_release=$(uname -r)
kernel_devel="kernel-devel-$kernel_release"
dnf list --available "$kernel_devel" >/dev/null 2>&1 || rpm -q "$kernel_devel" >/dev/null 2>&1 || {
  echo "The development package for the running kernel ($kernel_devel) is unavailable." >&2
  echo "Update/reboot into a supported kernel, then run this installer again." >&2
  exit 3
}
dnf install -y "$kernel_devel" amneziawg-dkms amneziawg-tools

command -v awg >/dev/null
command -v awg-quick >/dev/null
echo "AmneziaWG dependencies installed. No interface was started."
