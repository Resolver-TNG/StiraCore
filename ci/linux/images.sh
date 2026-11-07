#!/usr/bin/env bash
# Manage the Linux CI images (stira-ci:py3.11 / py3.12). Docker's storage is on the SSD volume.
#   ci_images.sh list             images, sizes, build dates, disk use
#   ci_images.sh build [3.11 3.12] (re)build; --no-cache refreshes the OS/Node/PowerShell/uv layers
#   ci_images.sh prune            remove dangling images and build cache older than 7 days
set -eu
cd "$(dirname "$0")"   # the Dockerfile sits next to this script
cmd="${1:-list}"; shift || true
case "$cmd" in
  list)  docker images --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}' | grep -E 'REPOSITORY|stira-ci'; docker system df ;;
  build) nocache=""; vers=()
         for a in "$@"; do [ "$a" = "--no-cache" ] && nocache="--no-cache" || vers+=("$a"); done
         [ ${#vers[@]} -eq 0 ] && vers=(3.11 3.12)
         for v in "${vers[@]}"; do docker build $nocache -q --label "stira-ci.built=$(date -u +%FT%TZ)" --build-arg PY="$v" -t "stira-ci:py$v" .; done ;;
  prune) docker image prune -f; docker builder prune -f --filter 'until=168h' ;;
  *) echo "usage: $0 list | build [3.11 3.12] [--no-cache] | prune"; exit 2 ;;
esac
