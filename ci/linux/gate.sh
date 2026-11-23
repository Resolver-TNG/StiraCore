#!/usr/bin/env bash
# One gate run with a machine verdict:  ci/linux/gate.sh <ref> [--py 3.11|3.12|both] [--runs DIR]
# Runs nas_run.sh on <ref>, writes ONE new log (never appended to), marks the end of the run with "=== done rc=N",
# and judges the log with gate_verdict.py. Exit 0 PASS, 1 FAIL, 2 INCOMPLETE, 3 another gate is running.
# Only one gate at a time (they share the NAS): a lock directory with the pid of the owner.
set -u
REF="${1:?usage: gate.sh <ref> [--py 3.11|3.12|both] [--runs DIR]}"; shift
PY=both; RUNS="${STIRA_GATE_RUNS:-$HOME/projects/stira-wt/runs}"
while [ $# -gt 0 ]; do case "$1" in
  --py) PY="$2"; shift 2;; --runs) RUNS="$2"; shift 2;; *) echo "unknown option $1" >&2; exit 64;; esac; done
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(git -C "$here" rev-parse --show-toplevel)"
LOCK="${TMPDIR:-/tmp}/stira-gate.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  owner="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then echo "another gate is running (pid $owner)" >&2; exit 3; fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 3      # the owner is gone: take over
fi
echo $$ > "$LOCK/pid"; trap 'rm -rf "$LOCK"' EXIT
git -C "$repo" fetch -q origin
SHA="$(git -C "$repo" rev-parse --verify "$REF^{commit}")" || { echo "cannot resolve $REF" >&2; exit 64; }
mkdir -p "$RUNS"
LOG="$RUNS/gate-${REF//\//_}-${SHA:0:8}-$(date +%Y%m%d-%H%M%S).log"
{ echo "=== gate $REF $SHA (py $PY) $(date +%H:%M)"
  "$here/nas_run.sh" --py "$PY" --ref "$REF"; rc=$?
  echo "=== done rc=$rc $(date +%H:%M)"; } > "$LOG" 2>&1
n=2; [ "$PY" = both ] || n=1
python3 "$here/gate_verdict.py" "$LOG" --versions "$n" --sha "$SHA" --json > "${LOG%.log}.verdict.json"; vrc=$?
echo "GATE ${SHA:0:8} $REF: $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["verdict"])' "${LOG%.log}.verdict.json")  (log: $LOG)"
exit $vrc
