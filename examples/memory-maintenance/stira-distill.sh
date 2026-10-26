#!/usr/bin/env bash
# One memory-maintenance pass for a timer (cron / launchd / systemd). See
# docs/memory-maintenance.md (pattern B).
#
# - Model-free by default: the arguments are built once and the same ones are given to a
#   --dry-run and then to the run. More than STIRA_DISTILL_MAX_CALLS (default 0) model
#   calls in the preview refuses the run (exit 4); the run itself gets
#   --max-model-calls too, so a config or memory change between the two cannot exceed it.
#   The rule distiller makes no model call.
# - One writer: the run holds the data directory's server.lock (--owner-lock); while a
#   server owns the directory the run is refused (exit 3). STIRA_DISTILL_OWNER_LOCK=0
#   drops that (a server running at the same time is then not excluded).
# - Forgetting is previewed only; pass --apply-forgetting yourself if you mean it.
# - Never silent: every attempt appends one JSON line to STIRA_DISTILL_LOG, and a
#   non-zero exit prints one line on stderr (cron mail, launchd StandardErrorPath,
#   journald) and is returned to the timer. Of Python's own stderr only the last
#   STIRA_DISTILL_ERR_LINES lines are kept in STIRA_DISTILL_ERR_LOG, masked.
# - Exit codes: the CLI's (0, 1, 2, 3, 4), and 2 = bad setting or missing data directory,
#   5 = unsafe log path (symlink or not a regular file; nothing ran), 6 = a log line
#   could not be written (the run's own code is on stderr).
set -uo pipefail
umask 077

fail() {  # $1 = message, $2 = exit code
    echo "stira-distill: $1" >&2
    exit "$2"
}

uint() {  # $1 = name, $2 = value: only 1-6 decimal digits, never evaluated otherwise
    [[ "$2" =~ ^[0-9]{1,6}$ ]] || fail "$1 must be an integer from 0 to 999999" 2
}

ROOT="${STIRA_ROOT:?set STIRA_ROOT to the StiraCore checkout}"
DATA_DIR="${STIRA_DATA_DIR:-$ROOT/.local/stira}"
AGENT_ID="${STIRA_AGENT_ID:-stira}"
PY="${STIRA_PYTHON:-$ROOT/.venv/bin/python}"
LOG="${STIRA_DISTILL_LOG:-$DATA_DIR/distill-runs.jsonl}"
ERR_LOG="${STIRA_DISTILL_ERR_LOG:-$DATA_DIR/distill-runs.err.log}"
MAX_CALLS="${STIRA_DISTILL_MAX_CALLS:-0}"
ERR_LINES="${STIRA_DISTILL_ERR_LINES:-20}"
OWNER_LOCK="${STIRA_DISTILL_OWNER_LOCK:-1}"

uint STIRA_DISTILL_MAX_CALLS "$MAX_CALLS"
uint STIRA_DISTILL_ERR_LINES "$ERR_LINES"
[[ "$OWNER_LOCK" == 0 || "$OWNER_LOCK" == 1 ]] || fail "STIRA_DISTILL_OWNER_LOCK must be 0 or 1" 2

if [[ ! -d "$DATA_DIR" ]]; then
    fail "data directory $DATA_DIR does not exist" 2
fi
for path in "$LOG" "$ERR_LOG"; do
    if [[ -L "$path" ]] || { [[ -e "$path" ]] && [[ ! -f "$path" ]]; }; then
        fail "log path $path is a symbolic link or not a regular file; not running" 5
    fi
    [[ -d "$(dirname -- "$path")" ]] || fail "log directory of $path does not exist" 5
done

TMP_ERR="$(mktemp "$DATA_DIR/.stira-distill.XXXXXX")" || fail "cannot create a file in $DATA_DIR" 6
trap 'rm -f -- "$TMP_ERR"' EXIT
LOG_FAILED=0

keep_stderr() {  # $1 = phase, $2 = exit code: the last lines of Python's stderr, masked
    local masked
    if [[ "$ERR_LINES" == 0 || ! -s "$TMP_ERR" ]]; then
        : >"$TMP_ERR"
        return
    fi
    masked="$(tail -n "$ERR_LINES" -- "$TMP_ERR" | "$PY" -c 'import sys
from stiracore.observability.masking import mask_secrets
sys.stdout.write(mask_secrets(sys.stdin.read()))' 2>/dev/null)" ||
        masked="(stderr not kept: masking unavailable)"
    printf -- '--- %s %s exit %s (last %s lines, masked)\n%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$ERR_LINES" "$masked" >>"$ERR_LOG" ||
        { echo "stira-distill: cannot write $ERR_LOG" >&2; LOG_FAILED=1; }
    : >"$TMP_ERR"
}

record() {  # $1 = exit code, $2 = phase, $3 = the CLI's JSON line (may be empty)
    local line="${3:-}"
    [[ "$line" == "{"* ]] || line=null
    keep_stderr "$2" "$1"
    printf '{"at":"%s","phase":"%s","exit":%s,"result":%s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" "$1" "$line" >>"$LOG" ||
        { echo "stira-distill: cannot write $LOG" >&2; LOG_FAILED=1; }
    if [[ "$1" != 0 ]]; then
        echo "stira-distill: $2 exit $1 (details: $LOG)" >&2
    fi
}

finish() {  # $1 = exit code of the attempt
    if [[ "$LOG_FAILED" == 1 ]]; then
        echo "stira-distill: log not written; the attempt itself exited $1" >&2
        exit 6
    fi
    exit "$1"
}

# Built once: the preview and the run get exactly the same arguments.
args=(-m stiracore.control.distillation_cli
    --base-dir "$DATA_DIR/memory" --agent-id "$AGENT_ID" --config "$DATA_DIR/agent-config.json")
if [[ "$OWNER_LOCK" == 1 ]]; then
    args+=(--owner-lock "$DATA_DIR")
fi
args+=(--max-model-calls "$MAX_CALLS" "$@")

plan="$("$PY" "${args[@]}" --dry-run 2>>"$TMP_ERR" | tail -n 1)"
code=$?
if [[ $code -ne 0 ]]; then
    record "$code" "$([[ $code == 4 ]] && echo limit || echo dry-run)" "$plan"
    finish "$code"
fi
calls="$(printf '%s' "$plan" | "$PY" -c \
    'import json,sys; print(json.load(sys.stdin)["estimate"]["model_calls"])' 2>/dev/null)"
if [[ ! "$calls" =~ ^[0-9]{1,6}$ ]]; then
    record 1 dry-run "$plan"
    finish 1
fi
# Both sides are validated decimal strings; 10# keeps a leading zero from meaning octal.
if ((10#$calls > 10#$MAX_CALLS)); then
    record 4 limit "$plan"  # e.g. distiller_mode "llm" in agent-config.json
    finish 4
fi

result="$("$PY" "${args[@]}" 2>>"$TMP_ERR" | tail -n 1)"
code=$?
record "$code" run "$result"
finish "$code"
