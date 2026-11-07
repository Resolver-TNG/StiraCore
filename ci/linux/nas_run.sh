#!/usr/bin/env bash
# Run the Linux CI of a git ref on a remote Docker host (the NAS) from your own machine.
#   ci/linux/nas_run.sh [--py 3.11|3.12|both] [--ref REF] [--shards N]
# Defaults: --py 3.11  --ref origin/main  --shards 4.
# Environment: STIRA_CI_HOST (ssh name, default "nas"), STIRA_CI_DIR (work dir on the host, default /volume3/docker/ci-work).
# The host needs: Docker, python3, and ci/linux/{run_sharded.sh,images.sh,Dockerfile} copied by this script.
# What it does: clone the ref (with .git: some tests run git), ship it without macOS metadata (COPYFILE_DISABLE; the `._*`
# files broke the tests that scan the sources), run the shards there, print the result table.
set -eu
PY=3.11; REF=origin/main; SHARDS=4
while [ $# -gt 0 ]; do case "$1" in
  --py) PY="$2"; shift 2;; --ref) REF="$2"; shift 2;; --shards) SHARDS="$2"; shift 2;;
  -h|--help) sed -n 2,9p "$0"; exit 0;; *) echo "unknown option $1" >&2; exit 2;; esac; done
HOST="${STIRA_CI_HOST:-nas}"; DIR="${STIRA_CI_DIR:-/volume3/docker/ci-work}"
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(git -C "$here" rev-parse --show-toplevel)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
git -C "$repo" fetch -q origin
git clone -q --no-hardlinks "$repo" "$tmp/src"
git -C "$tmp/src" fetch -q "$repo" "+refs/remotes/$REF:refs/heads/ci-under-test" 2>/dev/null \
  || git -C "$tmp/src" fetch -q "$repo" "$REF:ci-under-test"
git -C "$tmp/src" checkout -q ci-under-test
echo "ref $REF = $(git -C "$tmp/src" log -1 --format='%h %s' | cut -c1-90)"
ssh "$HOST" "mkdir -p '$DIR'; chmod a+rx '$DIR'"   # the containers read the scripts through a bind mount of this directory
COPYFILE_DISABLE=1 tar -C "$here" -cf - Dockerfile run_sharded.sh images.sh normalize_modes.sh | ssh "$HOST" "tar -C '$DIR' -xf - 2>/dev/null; chmod a+rx '$DIR'/*.sh; chmod a+r '$DIR'/Dockerfile"
# the old source tree may hold files that only a container user can remove: let a container do it
ssh "$HOST" "docker run --rm --user 0 -v '$DIR':/w alpine sh -c 'rm -rf /w/src' >/dev/null 2>&1 || rm -rf '$DIR/src'; mkdir -p '$DIR/src'"
COPYFILE_DISABLE=1 tar -C "$tmp/src" -cf - . | ssh "$HOST" "tar -C '$DIR/src' -xf - 2>/dev/null; find '$DIR/src' -name '._*' -delete; chmod -R a+rX '$DIR/src'"
versions=("$PY"); [ "$PY" = both ] && versions=(3.11 3.12)
rc=0
for v in "${versions[@]}"; do
  ssh "$HOST" "cd '$DIR'; docker image inspect stira-ci:py$v >/dev/null 2>&1 || ./images.sh build $v; nice -n 10 ./run_sharded.sh $v '$DIR/src' $SHARDS" || rc=1
done
exit $rc
