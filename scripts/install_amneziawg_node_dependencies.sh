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

# Исправление для Fedora: подключаем совместимый epel-9 chroot,
# так как нативных сборок для Fedora 44+ в репозитории AmneziaVPN нет.
if [[ "${ID:-}" == "fedora" ]]; then
  echo "Fedora detected. Enforcing epel-9-x86_64 COPR chroot..."
  dnf copr enable -y amneziavpn/amneziawg epel-9-x86_64
else
  dnf copr enable -y amneziavpn/amneziawg
fi

kernel_release=$(uname -r)
kernel_devel="kernel-devel-$kernel_release"

# Для Fedora пакет ядра может называться просто kernel-devel (без версии в имени пакета)
# или иметь другую структуру метаданных, поэтому проверяем доступность через dnf repoquery
if [[ "${ID:-}" == "fedora" ]]; then
  kernel_devel="kernel-devel"
fi

dnf list --available "$kernel_devel" >/dev/null 2>&1 || rpm -q "$kernel_devel" >/dev/null 2>&1 || {
  echo "The development package for the running kernel ($kernel_devel) is unavailable." >&2
  echo "Update/reboot into a supported kernel, then run this installer again." >&2
  exit 3
}

dnf install -y "$kernel_devel" amneziawg-dkms amneziawg-tools

command -v awg >/dev/null
command -v awg-quick >/dev/null
echo "AmneziaWG dependencies installed. No interface was started."
