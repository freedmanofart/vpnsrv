#!/usr/bin/env bash
set -euo pipefail

source_dir=/etc/letsencrypt/live/imba.shop
target_dir=/opt/imba-3x-ui/data/certs/imba.shop

if [[ -n ${RENEWED_LINEAGE:-} && ${RENEWED_LINEAGE} != "$source_dir" ]]; then
  exit 0
fi

umask 077
install -d -m 0700 "$target_dir"
changed=0
for name in fullchain.pem privkey.pem; do
  if ! cmp -s "$source_dir/$name" "$target_dir/$name"; then
    install -m 0600 "$source_dir/$name" "$target_dir/$name"
    changed=1
  fi
done

if [[ $changed == 1 && ${1:-} != --no-restart ]]; then
  docker restart 3xui_app >/dev/null
fi
