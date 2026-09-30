#!/usr/bin/env bash
# Cross-build release binaries into dist/. Pure Go, so no C toolchain needed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
rm -rf dist && mkdir dist
for target in linux/amd64 linux/arm64 windows/amd64 windows/arm64; do
  os=${target%/*} arch=${target#*/} ext=""
  [ "$os" = windows ] && ext=.exe
  echo "building $os/$arch"
  CGO_ENABLED=0 GOOS=$os GOARCH=$arch \
    go build -trimpath -ldflags "-s -w" -o "dist/teams-refresher-$os-$arch$ext" .
done
