#!/usr/bin/env bash
# stira_ctl.sh - stira_server の起動/確認/停止 (macOS/Linux 版。stira_ctl.ps1 の設計を踏襲)
#
#   scripts/stira_ctl.sh start|status|stop|restart|distill [--dry-run|--apply-forgetting|...]
#   scripts/stira_ctl.sh staging list|show KEY|approve KEY|reject KEY [--reason TEXT]|reindex
#   scripts/stira_ctl.sh procedural list|show KEY|propose KEY --source-turn ID --file PATH|approve KEY|reject KEY|stage-legacy KEY
#
# 設計メモ:
#   - distill はサーバーを起動せず同期 CLI を実行する。既定 rule mode は Bedrock 課金ゼロ。
#   - staging は Harvest の staging (人間レビュー待ち) を一覧/承認/却下する同期 CLI (#293)。
#   - procedural は手続き記憶の提案を一覧/承認/却下する同期 CLI (#144)。承認者は OS ユーザー。
#   - start は投げっぱなし起動。health は status で別途確認する。
#   - 停止は PID ファイルの PID のみ対象。PID 生存 + コマンドラインがこの checkout の
#     絶対パス dashboard/stira_server.py を完全な引数として含むことを確認する。
#     ポート所有だけでは停止しない。
#   - start 前にポートが使用中なら使用プロセスを表示して失敗する (上書きしない)。
#   - データディレクトリは 1 サーバーだけが所有する (#285, server.lock)。start は所有者が
#     生きていれば起動しない。起動後、サーバーが所有権を取るか拒否して終了するまで待ち、
#     拒否なら理由を表示して PID ファイルを残さない。status/stop は PID ファイルが無くても
#     server.lock の PID (この checkout のチャットサーバーと確認できたもの) を所有者として扱う。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTRY="$ROOT/dashboard/stira_server.py"
PORT="${STIRA_PORT:-5555}"
BASE="http://127.0.0.1:$PORT"
PY="${STIRA_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
    PY="$(command -v python3 || true)"
fi
# STIRA_DATA_DIR (#272): the server's data directory when set, so the lock, PID file and
# memory commands follow it. Checked by the server's own validator (the same accept/
# refuse rules, blank = unset); a refusal prints the reason only, never the value.
STATE_DIR="$ROOT/.local/stira"
if [[ -n "${STIRA_DATA_DIR:-}" ]]; then
    if [[ -z "$PY" ]]; then
        echo "stira: python not found (needed to check STIRA_DATA_DIR)" >&2
        exit 2
    fi
    code=0
    checked="$(PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$PY" -m stiracore.config.data_dir_check 2>&1)" || code=$?
    if ((code != 0)); then
        printf '%s\n' "$checked" | grep -E '^Stira: ' >&2 ||
            echo "stira: STIRA_DATA_DIR could not be checked" >&2
        exit 2
    fi
    checked="$(printf '%s\n' "$checked" | sed -n 's/^data_dir=//p' | tail -n 1)"
    if [[ -n "$checked" ]]; then
        STATE_DIR="$checked"
    fi
fi
PID_FILE="$STATE_DIR/stira_server.pid"
LOCK_FILE="$STATE_DIR/server.lock"
OUT_LOG="$ROOT/.local/stira.out.log"
ERR_LOG="$ROOT/.local/stira.err.log"
MEMORY_BASE="$STATE_DIR/memory"
AGENT_CONFIG="$STATE_DIR/agent-config.json"

usage() {
    echo "usage: $(basename "$0") {start|status|stop|restart|distill [--dry-run|--apply-forgetting]}" >&2
    echo "       $(basename "$0") staging {list|show KEY|approve KEY|reject KEY [--reason TEXT]|reindex}" >&2
    echo "       $(basename "$0") procedural {list|show KEY|propose KEY --source-turn ID --file PATH|approve KEY|reject KEY|stage-legacy KEY}" >&2
    echo "  env: STIRA_PORT (default 5555), STIRA_PYTHON (default .venv/bin/python), STIRA_DATA_DIR (default .local/stira)" >&2
}

# STIRA_PORT reaches lsof/curl arguments: a port number only (#160).
require_port() {
    if [[ ! "$PORT" =~ ^[0-9]{1,5}$ ]] || ((10#$PORT < 1 || 10#$PORT > 65535)); then
        echo "stira: STIRA_PORT must be a port number (1-65535)" >&2
        exit 2
    fi
}

require_python() {
    if [[ -z "$PY" ]]; then
        echo "stira: python not found (set STIRA_PYTHON or create .venv)" >&2
        exit 1
    fi
}

# Prints listener info for $PORT; returns 0 if the port is in use.
port_owner() {
    if ! command -v lsof >/dev/null 2>&1; then
        return 1
    fi
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null
}

read_pid() {
    local pid=""
    if [[ -f "$PID_FILE" ]]; then
        pid="$(tr -d '[:space:]' <"$PID_FILE")"
    fi
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$pid"
    fi
}

# PID note of the data-directory owner lock (#285). The server's kernel lock decides;
# the note is only trusted after is_stira_pid verified the process.
read_lock_pid() {
    local pid=""
    if [[ -f "$LOCK_FILE" ]]; then
        pid="$(head -c 32 "$LOCK_FILE" 2>/dev/null | tr -d '[:space:]' || true)"
    fi
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$pid"
    fi
}

# Does $1 have server.lock open? 0 = yes, 1 = no, 2 = cannot tell (no lsof, or no
# regular lock file). The note and the command line alone prove nothing: the note may
# be stale and its PID reused. Having the file open is the closest check available
# from a shell; it is not the kernel lock itself (a server that is being refused has
# the file open for a moment, without the lock).
# On 2, LOCK_UNVERIFIED_REASON says why.
LOCK_UNVERIFIED_REASON=""
lock_holder_status() {
    local pid="$1"
    if [[ -L "$LOCK_FILE" ]]; then
        LOCK_UNVERIFIED_REASON="$LOCK_FILE is a symbolic link"
        return 2
    fi
    if [[ ! -e "$LOCK_FILE" ]]; then
        LOCK_UNVERIFIED_REASON="$LOCK_FILE does not exist (a server older than #285?)"
        return 2
    fi
    if [[ ! -f "$LOCK_FILE" ]]; then
        LOCK_UNVERIFIED_REASON="$LOCK_FILE is not a regular file"
        return 2
    fi
    if ! command -v lsof >/dev/null 2>&1; then
        LOCK_UNVERIFIED_REASON="lsof not found"
        return 2
    fi
    if lsof -t -a -p "$pid" -- "$LOCK_FILE" 2>/dev/null | grep -qx "$pid"; then
        return 0
    fi
    return 1
}

# Verified chat server owner of this checkout's data directory, if any: the PID named by
# the note is a chat server process of this checkout AND has the lock file open.
data_dir_owner() {
    local pid
    pid="$(read_lock_pid)"
    if [[ -n "$pid" ]] && is_stira_pid "$pid" && lock_holder_status "$pid"; then
        printf '%s\n' "$pid"
    fi
}

is_stira_pid() {
    local pid="$1" cmd
    kill -0 "$pid" 2>/dev/null || return 1
    cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    [[ -n "$cmd" ]] || return 1
    case " $cmd " in
        *" $ENTRY "*) return 0 ;;
        *) return 1 ;;
    esac
}

show_status() {
    local body owner
    require_port
    if ! command -v curl >/dev/null 2>&1; then
        echo "stira: cannot check status (curl not found)"
        return 0
    fi
    if ! body="$(curl -fsS --max-time 5 "$BASE/api/gateway/status" 2>/dev/null)"; then
        owner="$(data_dir_owner)"
        if port_owner >/dev/null; then
            echo "stira: port $PORT in use but HTTP not ready (or not stira):"
            port_owner || true
        elif [[ -n "$owner" ]]; then
            echo "stira: no response on port $PORT, but pid $owner owns the data directory"
            echo "  (still starting, or serving on another STIRA_PORT)"
        else
            echo "stira: DOWN (no response on port $PORT)"
        fi
        return 0
    fi
    owner="$(data_dir_owner)"
    echo "stira: UP (port $PORT${owner:+, data directory owner pid $owner})"
    if [[ -n "$PY" ]] && printf '%s' "$body" | "$PY" -c '
import json, sys
r = json.load(sys.stdin)
print("breaker: %s   model: %s" % ((r.get("breaker") or {}).get("level"), r.get("model")))
hb = r.get("heartbeat") or {}
if hb.get("running"):
    print("heartbeat: running")
elif hb.get("enabled"):
    print("heartbeat: enabled, not running (%s)" % (hb.get("last_error") or "unknown"))
else:
    print("heartbeat: off")
print("Neural State:")
for k, v in sorted((r.get("neural_state") or {}).items()):
    print("%-13s %s" % (k, v))
' 2>/dev/null; then
        return 0
    fi
    printf '%s\n' "$body"
}

# stop [--force-unverified]: by default a PID is only signalled when it is shown to
# have server.lock open (#285). --force-unverified is the pre-#285 behaviour (PID file
# + command line only), for when that cannot be shown (no lsof, a server older than
# #285); it is never used by restart.
stop_stira() {
    local pid i force=0
    case "${1:-}" in
        "") ;;
        --force-unverified) force=1 ;;
        *)
            echo "stira: unknown stop option: $1 (only --force-unverified)" >&2
            return 2
            ;;
    esac
    pid="$(read_pid)"
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
        # No live PID file: the verified owner of the data directory, if any (#285).
        pid="$(data_dir_owner)"
    fi
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "stira: already down (no live pid file)"
        return 0
    fi
    if ! is_stira_pid "$pid"; then
        echo "stira: skipping pid $pid: not a verified chat server process for this checkout" >&2
        return 1
    fi
    # A stale PID file whose PID now belongs to another process with the same command
    # line: refuse unless it has the lock file open (#285). Cannot tell = refuse too.
    local holder=0
    lock_holder_status "$pid" || holder=$?
    if [[ "$holder" -eq 1 ]]; then
        echo "stira: skipping pid $pid: it does not hold $LOCK_FILE (stale pid?)" >&2
        return 1
    fi
    if [[ "$holder" -eq 2 ]]; then
        if [[ "$force" -ne 1 ]]; then
            echo "stira: not stopping pid $pid: cannot verify it owns the data directory" >&2
            echo "  ($LOCK_UNVERIFIED_REASON)" >&2
            echo "  if you are sure it is this checkout's server: $(basename "$0") stop --force-unverified" >&2
            return 1
        fi
        echo "stira: --force-unverified: stopping pid $pid on its command line only" >&2
        echo "  ($LOCK_UNVERIFIED_REASON)" >&2
    fi
    kill -TERM "$pid" 2>/dev/null || true
    for i in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
        if [[ "$i" -eq 10 ]] && is_stira_pid "$pid"; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "stira: STOP failed (pid $pid still alive)" >&2
        return 1
    fi
    rm -f "$PID_FILE"
    echo "stira: STOPPED (killed: $pid)"
}

start_stira() {
    local owner pid i code
    require_port
    require_python
    if owner="$(port_owner)"; then
        echo "stira: port $PORT already in use -- not starting:" >&2
        printf '%s\n' "$owner" >&2
        echo "  stop that process, or set STIRA_PORT to another port" >&2
        return 1
    fi
    pid="$(read_pid)"
    if [[ -n "$pid" ]] && is_stira_pid "$pid"; then
        echo "stira: already running (pid $pid) -- use 'restart' to relaunch"
        return 0
    fi
    owner="$(data_dir_owner)"
    if [[ -n "$owner" ]]; then
        echo "stira: pid $owner already owns the data directory $STATE_DIR -- not starting" >&2
        echo "  stop it first: scripts/stira_ctl.sh stop" >&2
        return 1
    fi
    mkdir -p "$STATE_DIR" "$(dirname "$OUT_LOG")"
    cd "$ROOT"
    STIRA_PORT="$PORT" nohup "$PY" "$ENTRY" >"$OUT_LOG" 2>"$ERR_LOG" </dev/null &
    pid="$!"
    echo "$pid" >"$PID_FILE"
    # Wait until the server owns the data directory or has exited (refused, #285).
    for i in $(seq 1 "30"); do
        kill -0 "$pid" 2>/dev/null || break
        [[ "$(read_lock_pid)" == "$pid" ]] && break
        sleep 1
    done
    if ! kill -0 "$pid" 2>/dev/null; then
        code=0
        wait "$pid" 2>/dev/null || code=$?
        rm -f "$PID_FILE"
        echo "stira: start failed (exit $code):" >&2
        grep -E '^Stira: ' "$ERR_LOG" >&2 || tail -n 5 "$ERR_LOG" >&2 || true
        return 1
    fi
    echo "stira: launch requested on port $PORT (pid $pid)."
    echo "  HTTP は数秒で応答。Sokudan のモデルは最初のチャットで読み込む (初回のみダウンロード)。"
    echo "  確認は: scripts/stira_ctl.sh status"
}

run_distill() {
    require_python
    "$PY" -m stiracore.control.distillation_cli \
        --base-dir "$MEMORY_BASE" --agent-id stira --config "$AGENT_CONFIG" "$@"
}

run_staging() {
    require_python
    "$PY" -m stiracore.control.staging_cli \
        --base-dir "$MEMORY_BASE" --agent-id stira "$@"
}

run_procedural() {
    require_python
    "$PY" -m stiracore.control.procedural_cli \
        --base-dir "$MEMORY_BASE" --agent-id stira "$@"
}

ACTION="${1:-status}"
case "$ACTION" in
    start) start_stira ;;
    status) show_status ;;
    stop)
        shift
        stop_stira "$@"
        ;;
    restart)
        stop_stira
        start_stira
        ;;
    distill)
        shift
        run_distill "$@"
        ;;
    staging)
        shift
        run_staging "$@"
        ;;
    procedural)
        shift
        run_procedural "$@"
        ;;
    -h | --help) usage ;;
    *)
        echo "stira: unknown action: $ACTION" >&2
        usage
        exit 2
        ;;
esac
