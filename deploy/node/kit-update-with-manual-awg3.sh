#!/usr/bin/env bash
set -uo pipefail

status=0
/usr/local/bin/kit update --unattended || status=$?
/usr/local/sbin/kit-disable-auto-awg3
exit "$status"
