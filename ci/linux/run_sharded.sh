#!/usr/bin/env bash
# Linux CI in N isolated containers: the pytest files are split into shards (balanced by file size),
# each shard runs in its own container with its own copy of the sources, network namespace and temp dirs,
# so tests that use fixed ports / lock files / the repository directory cannot disturb each other
# (pytest-xdist inside one container did: 102 failures). A "misc" container runs the non-pytest steps.
# Usage: ci/linux/run_sharded.sh <py: 3.11|3.12> <source dir> [shards=4]
# Needs: Docker, python3 and the images from ci/linux/images.sh. <source dir> must be a git checkout (some tests run git).
# Knobs: CI_CPU_SHARD (cpus per shard, default 1.0), CI_MEM_SHARD (memory per shard, default 3g).
set -u
PY="${1:?python version}"; SRC="$(cd "${2:?source dir}" && pwd)"; N="${3:-4}"
IMG="stira-ci:py${PY}"; RUN="$(mktemp -d "${TMPDIR:-/tmp}/stira-ci-run.XXXXXX")"; chmod a+rwx "$RUN"
CPU_SHARD="${CI_CPU_SHARD:-1.0}"; MEM_SHARD="${CI_MEM_SHARD:-3g}"
docker volume create stira-pip-cache >/dev/null

# 1) shards: greedy longest-first on file size (a rough proxy for run time)
python3 - "$SRC" "$N" "$RUN" <<'PY'
import os, sys, glob
src, n, run = sys.argv[1], int(sys.argv[2]), sys.argv[3]
files = sorted(glob.glob(os.path.join(src, "tests", "**", "test_*.py"), recursive=True) + [os.path.join(src, "dashboard", "test_multimodal.py")])
files = [(os.path.getsize(f), os.path.relpath(f, src)) for f in files if os.path.exists(f)]
files.sort(reverse=True)
bins = [[0, []] for _ in range(n)]
for size, rel in files:
    b = min(bins, key=lambda x: x[0]); b[0] += size; b[1].append(rel)
for i, (_, rels) in enumerate(bins):
    open(os.path.join(run, f"shard{i}.txt"), "w").write(" ".join(rels))
print("shards:", [len(b[1]) for b in bins], "files")
PY

common=(--rm --pids-limit 4096 -e PY="$PY" -e AWS_CONFIG_FILE=/tmp/.no-aws/config -e AWS_SHARED_CREDENTIALS_FILE=/tmp/.no-aws/credentials -e AWS_EC2_METADATA_DISABLED=true
        -v "$SRC":/src:ro -v "$(cd "$(dirname "$0")" && pwd)":/ci:ro -v "$RUN":/run-out:rw -v stira-pip-cache:/home/ci/.cache/pip)
prep='set -u; cp -a /src/. /work/; cd /work; git config --global --add safe.directory "*"; bash /ci/normalize_modes.sh; python -m venv /tmp/venv && . /tmp/venv/bin/activate && python -m pip install -q -e ".[dev]"'

start=$(date +%s)
pids=()
for i in $(seq 0 $((N-1))); do
  ( docker run "${common[@]}" --cpus "$CPU_SHARD" --memory "$MEM_SHARD" --name "stira-ci-$PY-s$i-$$" stira-ci:py${PY} bash -c \
      "( $prep ) > /run-out/shard$i.prep.log 2>&1 || { echo prep-failed > /run-out/shard$i.rc; exit 1; }; . /tmp/venv/bin/activate; cd /work; pytest \$(cat /run-out/shard$i.txt) -q -p no:cacheprovider > /run-out/shard$i.log 2>&1; echo \$? > /run-out/shard$i.rc" > /dev/null 2>&1 ) &
  pids+=($!)
done
# misc: PowerShell, node, lint/format (3.12), a11y (3.12), provider extras
( docker run "${common[@]}" --cpus 1.0 --memory 3g --name "stira-ci-$PY-misc-$$" stira-ci:py${PY} bash -c "$prep; . /tmp/venv/bin/activate; cd /work; fail=0
  step() { echo; echo \"=== \$1\"; shift; \"\$@\" || { echo \"!!! step failed: \$*\"; fail=1; }; }
  if [ \"\$PY\" = 3.12 ]; then step 'ruff check' ruff check .; step 'ruff format --check' ruff format --check .; fi
  step 'powershell' pwsh -NoProfile -File ./tests/stira_ctl_test.ps1
  NODE_TESTS=\"\$(grep -o 'node --test.*' .github/workflows/ci.yml | head -1)\"; step 'node tests' bash -c \"\$NODE_TESTS\"
  if [ \"\$PY\" = 3.12 ]; then step 'a11y' bash -c 'npm install --no-save --no-package-lock jsdom axe-core >/dev/null 2>&1; STIRA_REQUIRE_AXE=1 pytest tests/test_a11y_axe.py tests/test_ui_contrast.py -q'; fi
  step 'provider extras' bash -c 'python -m pip install -q -e \".[dev,providers]\" && pytest tests/test_providers.py tests/test_provider_contract.py tests/test_provider_live.py tests/test_provider_capabilities_usage.py tests/test_provider_credentials.py -q'
  pytest tests/ dashboard/test_multimodal.py --collect-only -q 2>/dev/null | tail -3 > /run-out/collected.txt
  echo \$fail > /run-out/misc.rc" > "$RUN/misc.log" 2>&1 ) &
pids+=($!)
wait "${pids[@]}" 2>/dev/null
end=$(date +%s)

rc=0
echo "=== results (py$PY, $N shards, $((end-start))s)"
for i in $(seq 0 $((N-1))); do
  r=$(cat "$RUN/shard$i.rc" 2>/dev/null || echo "?"); sum=$(grep -E "[0-9]+ (passed|failed)" "$RUN/shard$i.log" | tail -1)
  echo "shard$i rc=$r  $sum"; [ "$r" = "0" ] || rc=1
done
m=$(cat "$RUN/misc.rc" 2>/dev/null || echo "?"); echo "misc rc=$m"; [ "$m" = "0" ] || rc=1

# coverage guard: the shards together must account for every test that a plain collection sees
python3 - "$RUN" "$N" <<'PY'
import re, sys
run, n = sys.argv[1], int(sys.argv[2])
total = 0
for i in range(n):
    line = [l for l in open(f"{run}/shard{i}.log") if re.search(r"\d+ (passed|failed|skipped|error)", l)][-1]
    total += sum(int(m.group(1)) for m in re.finditer(r"(\d+) (?:passed|failed|skipped|xfailed|xpassed|errors?|deselected)", line))
txt = open(f"{run}/collected.txt").read() if __import__("os").path.exists(f"{run}/collected.txt") else ""
m = re.search(r"(\d+) tests? collected", txt) or re.search(r"(\d+)/(\d+) tests collected", txt)
coll = int(m.group(m.lastindex)) if m else None
print(f"coverage guard: shards accounted for {total} tests, plain collection sees {coll}")
if coll is not None and total != coll:
    print("!!! MISMATCH: some tests are not in any shard (or were collected twice)"); sys.exit(1)
PY
[ $? -eq 0 ] || rc=1
grep -h "^FAILED\|^ERROR " "$RUN"/shard*.log 2>/dev/null | head -40 | cut -c1-200
grep -h "^!!! step failed" "$RUN/misc.log" 2>/dev/null
[ $rc -eq 0 ] && echo "LINUX CI: PASS" || echo "LINUX CI: FAIL (logs: $RUN)"
exit $rc
