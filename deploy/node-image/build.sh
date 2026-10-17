#!/usr/bin/env bash
set -Eeuo pipefail

PANEL_REF=95f19b192f477b59cc368dcb7751bcf2e0180e5b
KIT_SUB_SHA256=eac3e47491ce86487f05160952ff5b4ad8a3821954f082eee3516e40575fb574
IMAGE_TAG=${1:-freedomvpn/3x-ui-kit:95f19b1-kit1.2}
BASE_TAG=freedomvpn/3x-ui-fork:95f19b1
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

actual=$(openssl dgst -sha256 -r "$ROOT/deploy/node-image/kit-sub.py" | awk '{print $1}')
[[ $actual == "$KIT_SUB_SHA256" ]] || { echo 'KIT subscription source checksum mismatch' >&2; exit 1; }

git init -q "$WORK/panel"
git -C "$WORK/panel" remote add origin https://github.com/freedmanofart/3x-ui.git
git -C "$WORK/panel" fetch -q --depth 1 origin "$PANEL_REF"
git -C "$WORK/panel" checkout -q --detach FETCH_HEAD
[[ $(git -C "$WORK/panel" rev-parse HEAD) == "$PANEL_REF" ]]

docker buildx build --load --platform linux/amd64 --build-arg TARGETARCH=amd64 \
  -t "$BASE_TAG" "$WORK/panel"
docker buildx build --load --platform linux/amd64 --build-arg XUI_BASE_IMAGE="$BASE_TAG" \
  -f "$ROOT/deploy/node-image/Dockerfile" -t "$IMAGE_TAG" "$ROOT"
docker image inspect "$IMAGE_TAG" --format '{{.RepoTags}} {{.Id}}'
