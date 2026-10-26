# Guide: scheduler and heartbeat — what to switch on, how to check it, how to turn it off

English | [日本語](guide-scheduler-heartbeat_ja.md)

The chat server has two optional background parts. Both are **off by default**:

* the **heartbeat** — a model-free tick (decay, a state snapshot, a breaker query) on a
  timer; reference: [heartbeat-daemon.md](heartbeat-daemon.md);
* the **scheduler** — runs jobs that a person created; reference: [scheduler.md](scheduler.md).

This guide is the order of steps, what each costs, how to see that it works and how to turn
it off. Every command and output below was run on 2026-10-26 on a scratch instance (macOS
arm64, own `STIRA_DATA_DIR`, empty `HOME`, no AWS credentials, so no model was called);
outputs are trimmed. Port `5555` is the default; use yours.

## 1. Which of these do I need?

| Setup | What runs in the background | Tokens | What it can touch | Switch | Default |
|---|---|---|---|---|---|
| **Nothing** | nothing; decay is worked out at the next turn | 0 | — | — | **yes** |
| **Heartbeat only** | one tick per interval (below) | **0** (no model call) | saves `neural-state.json` and keeps the in-memory state history; no memory, approval or network access | `"hb_enabled": true` in `gateway-config.json` (settings page, section "Heartbeat") or `STIRA_HEARTBEAT=1` | off |
| **Scheduler** (operator jobs) | jobs a person created: `health_check`, `memory_harvest` (rule distiller, staging only) | **0** for these two actions | `health_check`: reads the heartbeat and breaker status; `memory_harvest`: writes `semantic/staging/` only ([memory guide](guide-memory-routines.md)) | `STIRA_SCHEDULER=1` (environment only; no config field) | off |
| **Scheduler + heartbeat** | the above; in addition, model-calling jobs may start (they need a healthy heartbeat) and the grant heartbeat start becomes possible ([autonomy.md](autonomy.md)) | heartbeat 0; model-calling jobs spend tokens within their caps | model-calling jobs: memory plan files only, or the Agent layer's read-only tools | both of the above, plus each feature's own switch | off |

Model-calling jobs exist only with a further switch: the memory kinds `memory_rate_backfill`
/ `memory_consolidate` need `memory_autonomy` in `agent-config.json`
([memory-autonomy.md](memory-autonomy.md)); Agent-layer jobs (an operator's prompt) need
`agent_jobs.enabled` in `gateway-config.json` (settings page, "Scheduled agent jobs"; pool
`agent_daily_token_budget`, default 50,000 tokens per UTC day,
[configuration.md](configuration.md), section `agent_jobs`). This guide does not
switch either on.

**Heartbeat interval.** `interval = ceil × stability²`, kept between the shortest and the
longest interval (`hb_interval_floor_sec`, default 300 s; `hb_interval_ceil_sec`, default
10800 s; the settings page accepts 1 min–24 h). At the baseline stability 0.7 that is
10800 × 0.49 = 5292 s, about 88 minutes (the status showed `"interval_sec": 5291.99…`). The
first tick runs at start-up.

**Nothing leaves the machine.** Alerts and notifications are shown in `GET /api/scheduler`
and on the page `/schedules` only; `notify_targets` is stored and shown, nothing sends to it.

## 2. Step by step

All switches are read at start-up: stop the server, change them, start it again from the
repository root. The variables go into the server's environment, for example:

```bash
STIRA_HEARTBEAT=1 STIRA_SCHEDULER=1 scripts/stira_ctl.sh start
```

### 2.1 Switch the heartbeat on

One of:

* settings page → section **Heartbeat** (`/settings#sec-heartbeat`) → **Use the heartbeat**,
  save, restart (the page says a saved change waits for a restart);
* `.local/stira/gateway-config.json`: `"hb_enabled": true`;
* `STIRA_HEARTBEAT=1` (`0` forces it off; while it is set the page shows the switch pinned
  with the variable's name).

Check — the start-up lines and the status:

```text
   Heartbeat: on (zero-model tick)
```

```bash
curl -s http://127.0.0.1:5555/api/gateway/status | python3 -m json.tool
```

```json
"heartbeat": {
  "consecutive_failures": 0, "degraded": false, "enabled": true,
  "interval_sec": 5291.999999999999, "last_error": null, "last_level": "normal",
  "last_tick_at": 1793021488.738549, "next_tick_at": 1793026780.738549,
  "running": true, "state": "running", "tick_count": 1
}
```

Off, the same object is `{"enabled": false, "running": false}`.

### 2.2 Switch the scheduler on

`STIRA_SCHEDULER=1` (only `1`, `true`, `yes`, `on` turn it on). Start-up line:
`   Scheduler: running` (off: `   Scheduler: off`).

```bash
curl -s http://127.0.0.1:5555/api/scheduler | python3 -m json.tool
```

```json
"status": {
  "actions": ["health_check", "memory_harvest"], "agent_jobs": false, "alerts": [],
  "breaker_level": "normal", "enabled": true, "error": null, "heartbeat_healthy": true,
  "memory_job_kinds": [], "running": true, "state": "running", "tokens_today": 0
}
```

Off: `{"jobs":[],"notifications":[],"runs":[],"status":{"enabled":false,"running":false}}`.
Right after a start `heartbeat_healthy` can read `false` until the scheduler has read the
heartbeat's first tick (observed once; it read `true` at the next read).

### 2.3 Create the first job: an hourly health check (model-free)

**Limitation: creating a job from `/schedules` is not possible; it is API only**
(`POST /api/scheduler/jobs`). The page lists jobs and offers pause / resume / cancel.

Creating, resuming or changing a job lets something run later without a person, so it needs:

1. the dashboard's CSRF token (`GET /api/csrf`) and a same-origin request;
2. `"confirm": true` and a NORMAL breaker (WARNING and above: 409);
3. while the YOLO allowance is on (the default), a **confirmed operation** of kind
   `scheduler_loosen`, confirmed **by a person**: the first request answers 428
   `host_confirmation_required` with the operation (`id`, `short`, `channel`); the person
   confirms it; then the **same** request is sent again with the header
   `X-Stira-Host-Operation: <id>`.

> **Never give an agent or an unattended script the ability to confirm its own operations.**
> The confirmation is the person in the loop: nothing that sends the request should also
> confirm it.

Where the person confirms depends on the channel (`STIRA_YOLO_HOST_CONFIRM`, read at start-up;
it applies to every confirmed operation — tool-rule loosening, YOLO requests, grants):

| Channel | Set with | How the person confirms | Checked here (current `main`) |
|---|---|---|---|
| `command` | `STIRA_YOLO_HOST_CONFIRM=command` | `stira approvals decide <short>` in a terminal of the server's computer (a TTY, the same OS user, not a process started by the server); compare the code, type `APPROVE`. `--deny` refuses it. | **yes** — create and resume through the API, and Resume on `/schedules` (the page shows "Confirm on the host: run stira approvals decide … then type the word" and finishes by itself) |
| `dialog` / `auto` | `STIRA_YOLO_HOST_CONFIRM=dialog` or `auto` (macOS; `auto` uses the command outside a macOS desktop session) | the OS-authenticated desktop dialog | **no** (it would open on the desktop of the machine) |
| `browser` | the default (variable unset) | the dashboard's own confirmation dialog — the mechanism the UI uses, **not something to script** | create: no page exists, so a person has nowhere to confirm it. Resume on `/schedules`: **yes** (headless Chrome) — after the page's resume dialog the confirmation dialog opens (tick the box, then confirm); the job is resumed, and Cancel there applies nothing. Before the fix of #650 this confirmation did not open and the page said "Refused: Cancelled; nothing was applied". **Use `command` for scheduler jobs** (a create has no page to confirm in). |

`STIRA_YOLO_ALLOW=off` removes the confirmed operation altogether (from the code; not run
here) and with it this person step for every loosening — not recommended.

The recommended start for this guide therefore is:

```bash
STIRA_HEARTBEAT=1 STIRA_SCHEDULER=1 STIRA_YOLO_HOST_CONFIRM=command scripts/stira_ctl.sh start
```

A helper that sends the request, prints the operation and **waits for the person** — it
never confirms anything itself:

```bash
#!/bin/bash
# send-and-wait.sh <path> <json>: send a scheduler request; on 428 print the operation and wait
# until a PERSON has confirmed it on the host, then send the same request again with its id.
# It never confirms anything itself.
B=${B:-http://127.0.0.1:5555}
T=$(curl -s $B/api/csrf | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
H=(-H "X-CSRF-Token: $T" -H "Origin: $B" -H 'Content-Type: application/json')
R=$(curl -s -w '\n%{http_code}' -X POST "$B$1" "${H[@]}" -d "$2")
code=$(echo "$R" | tail -1); body=$(echo "$R" | sed '$d')
[ "$code" = 428 ] || { echo "$code $body"; exit 0; }
read -r OP SHORT CHANNEL < <(echo "$body" | python3 -c 'import json,sys;o=json.load(sys.stdin).get("operation") or {};print(o.get("id","-"),o.get("short","-"),o.get("channel","-"))')
[ "$OP" != - ] || { echo "$code $body"; exit 1; }
echo "Operation $SHORT ($CHANNEL channel) waits for a person."
[ "$CHANNEL" = command ] && echo "In a terminal of this computer: stira approvals decide $SHORT"
[ "$CHANNEL" = dialog ] && echo "Confirm it in the desktop dialog."
read -r -p "Press Enter after you confirmed it (Ctrl-C to give up) " _
curl -s -w '\n%{http_code}\n' -X POST "$B$1" "${H[@]}" -H "X-Stira-Host-Operation: $OP" -d "$2"
```

Terminal 1 (sends and waits):

```bash
./send-and-wait.sh /api/scheduler/jobs '{"job": {"job_id": "health-hourly",
  "name": "hourly health", "action": "health_check",
  "schedule": {"type": "cron", "expr": "0 * * * *", "timezone": "Asia/Tokyo"}},
  "confirm": true, "actor": "you"}'
```

```text
Operation 0007762a (command channel) waits for a person.
In a terminal of this computer: stira approvals decide 0007762a
Press Enter after you confirmed it (Ctrl-C to give up)
```

Terminal 2 (the person; `stira approvals pending` lists what waits):

```text
$ stira approvals decide 0007762a
Stira: スケジュールの作成・再開・変更 / create / resume / change a job
要求 / request: 0007762a
内容 / what: operation=create job=-
照合コード / code: eef30307
残り / expires in: 99 s
自分で要求していなければ拒否してください / Deny it if you did not ask for this.
確認するには APPROVE と入力 / Type APPROVE to confirm: APPROVE
Stira: confirmed; the page applies it now
```

Back in terminal 1, Enter:

```text
{"action":"health_check", ... "created_by":"local:you", ... "job_id":"health-hourly",
 "layer":"gateway", "missed_policy":"skip", ... "next_run_at":1793026800.0, ... "state":"active", ...}
200
```

The operation expires (the first answer said `expires_in` 120 s); confirm and resend promptly,
or send the request again for a new one. A cron schedule without `timezone` is UTC.

### 2.4 See it run

Wait for the next full hour, or create a test job that fires every minute (60 s is the
shortest interval) and pause it afterwards:

```bash
./send-and-wait.sh /api/scheduler/jobs '{"job": {"job_id": "health-test",
  "name": "health test (every minute)", "action": "health_check",
  "schedule": {"type": "interval", "every_sec": 60}}, "confirm": true, "actor": "you"}'
# a minute later
curl -s http://127.0.0.1:5555/api/scheduler | python3 -c \
  'import json,sys; print(json.dumps(json.load(sys.stdin)["runs"][:1], indent=1))'
```

```json
[{"job_id": "health-test", "run_id": "run-16a2b2d0bdba4bda",
  "run_key": "health-test@1793021627", "status": "completed", "reason": null,
  "summary": "breaker=normal, heartbeat=running", "tokens": 0}]
```

`/schedules` shows the same job, run and notification lists.

### 2.5 See it fail

Refusals (all observed):

| Answer | Body | Cause |
|---|---|---|
| 503 | `{"code":"unavailable","error":"the scheduler is not enabled"}` | `STIRA_SCHEDULER` not in the server's environment |
| 403 | `{"error":"Missing or invalid CSRF token."}` | no `X-CSRF-Token` |
| 428 | `{"code":"host_confirmation_required", … "operation":{… "kind":"scheduler_loosen" …}}` | the confirmed operation (2.3, step 3) |
| 428 | `{"code":"confirmation_required","error":"operator confirmation required"}` | `"confirm": true` missing |
| 400 | `{"code":"invalid","error":"every_sec must be between 60 and 31622400"}` | interval below 60 s |
| 403 | `{"code":"scope","error":"unknown gateway action 'send_mail'"}` | only `health_check` and `memory_harvest` exist |
| 409 | `{"code":"conflict","error":"the job is active"}` | resume of a job that is not paused |
| 409 | `{"code":"host_operation_denied", …}` | the person ran `stira approvals decide <short> --deny`; nothing was applied |
| 409 | `{"code":"host_confirm_on_host", …}` | the browser confirmation was tried while the channel is `command`: only the host can confirm |

At run time the scheduler raises **alerts** on its own loop (with or without a job) and keeps
**notifications**; the page words each code ([scheduler.md](scheduler.md#codes-the-ui-words-279)).
Reproduced here by replacing `heartbeat.lock` with a symlink before a start
([heartbeat-daemon.md](heartbeat-daemon.md#guarantees)):

```text
   Heartbeat: ⚠️ heartbeat.lock is unsafe (a symbolic link); not started
   Heartbeat: enabled, not running (UnsafeLockFileError)
```

```json
"status": {"alerts": ["heartbeat_stalled"], "heartbeat_healthy": false, ...},
"notifications": [{"code": "heartbeat_stalled", "params": {}, "severity": "warning",
  "state": "raised",
  "message": "heartbeat is enabled but not ticking (stopped, degraded or overdue)"}]
```

The health job's next run said `"summary": "breaker=normal, heartbeat=stopped"`. The page
shows "Heartbeat is not ticking (stopped, degraded or overdue)".

Fires that fall while the server is stopped are not run afterwards (`missed_policy: "skip"`,
the default). They are recorded only in `<data directory>/scheduler-events.jsonl`, not in
`runs`; after a stop of about three minutes:

```bash
grep '"kind":"run_missed"' .local/stira/scheduler-events.jsonl | tail -1
```

```json
{"body":{"count":2,"first":1793021987.209831,"job_id":"health-test","last":1793022047.209831,
 "policy":"skip","truncated":false}, ... "kind":"run_missed", ...}
```

### 2.6 Stop it

* **Pause a job** (always allowed, at every breaker level, no confirmed operation): **Pause**
  on `/schedules`, or

  ```bash
  B=http://127.0.0.1:5555; T=$(curl -s $B/api/csrf | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
  curl -s -X POST $B/api/scheduler/jobs/health-test/pause -H "X-CSRF-Token: $T" \
    -H "Origin: $B" -H 'Content-Type: application/json' -d '{"reason": "test done"}'
  ```

  The answer is the job with `"state": "paused"`. A paused job stays paused across restarts
  (observed). **Resume** needs `"confirm": true` and the confirmed operation, like a create:
  `./send-and-wait.sh /api/scheduler/jobs/health-test/resume '{"confirm": true}'` and the
  person's `stira approvals decide <short>`, or **Resume** on `/schedules` with the `command`
  channel (both checked; see 2.3 for the browser channel).
* **Cancel a running run:** **Cancel** on `/schedules`, or `POST
  /api/scheduler/runs/<run_id>/cancel {}`.
* **There is no delete.** The API has create, update, pause, resume and cancel only; a job
  you do not want stays paused (a `once` job ends by itself).
* **Everything off:** start the server without `STIRA_SCHEDULER` (or with
  `STIRA_SCHEDULER=0`). The jobs stay in `scheduler-events.jsonl` and are back the next time
  the scheduler is on. Heartbeat off: the settings switch, `"hb_enabled": false` or
  `STIRA_HEARTBEAT=0`, then restart.

On stop the server writes, for example,
`StiraCore: stopped (progress: stopped, chat_turn: off, approvals: closed, scheduler: stopped, memory_jobs: closed, heartbeat: stopped, yolo_host: stopped)`
(the last line of `.local/stira.out.log` with `scripts/stira_ctl.sh`).

## 3. Recommended preset for a personal Mac

| Setting | Recommended | Why |
|---|---|---|
| Heartbeat | **on**, default intervals (5 min / 3 h) | 0 tokens; decay and a breaker release by time become visible without a turn; it is the start condition of every model-calling job. The interval does not change recovery (decay is linear in time); it only sets how late a release is shown — at most one interval ([neural-saturation.md](neural-saturation.md), which also has a 30-minute variant). Breaker effects of decay: [heartbeat-daemon.md](heartbeat-daemon.md#breaker-interaction-active-decay). |
| Scheduler | **on** | needed for any job and for the grant heartbeat start |
| Jobs | `health-hourly` (2.3); optionally the weekly `memory_harvest` of the [memory guide](guide-memory-routines.md#3-recommended-profiles) | both model-free (0 tokens). The alerts come from the scheduler itself; the hourly job adds a dated run line, a visible sign that the scheduler is alive. |
| Agent-layer jobs, model-calling memory jobs | **off** (the defaults) | they spend tokens and widen what runs without a person; turn them on only for a purpose, with their caps |
| Confirmation channel | `STIRA_YOLO_HOST_CONFIRM=command` | a person confirms creates and resumes in a terminal (2.3); the default browser channel has no way to confirm a create |
| `missed_policy` | `skip` (default) | a Mac that sleeps records `run_missed` instead of a burst of runs on waking |

The limits that apply, so you do not have to look them up:

* **Schedules:** `cron` (5 fields, `timezone`, default UTC), `interval` (`every_sec`
  60–31,622,400) or `once` (`at`, epoch seconds). There are **no quiet hours** as a setting:
  write them into the cron expression (`0 9-22 * * *` = hourly from 09:00 to 22:00).
* **Flood limits:** one run of a job at a time (`overlap_policy: "skip"`), a fire is never
  started twice (`run_key`), a missed burst is not replayed (`skip`; `catch_up` is refused for
  model-calling jobs). For the grant heartbeat start: at most 5 starts in 60 s on heartbeat
  wakes and `autonomy.tick_min_spacing_sec` (default 300 s, floor 30 s) between iterations
  ([grants.md](grants.md#heartbeat-start-w6d)).
* **Budgets:** per job `budget.max_runs_per_day` / `max_tokens_per_run` /
  `max_tokens_per_day` (reaching one pauses the job, `budget_exhausted`); Agent-layer pool 50,000
  tokens per UTC day; memory jobs share 100,000 per UTC day ([memory-autonomy.md](memory-autonomy.md)).
  `health_check` and `memory_harvest` report 0 tokens.
* **Breaker:** at WARNING / COHERENCE one fire is held for up to `hold_max_sec` (3600) and run
  on recovery; at CRITICAL nothing starts and active runs are cancelled (`breaker_critical`);
  create / update / resume are refused at WARNING and above.
* **Heartbeat health** (for model-calling jobs): enabled, running, not degraded, last tick
  within `max(3 × interval, 120 s)` — about 4.4 h at the baseline interval.

## 4. Verification checklist

- [ ] Start-up: `Heartbeat: on (zero-model tick)` and `Scheduler: running`.
- [ ] `GET /api/gateway/status` → `heartbeat.running: true`, `consecutive_failures: 0`,
      `degraded: false`; `tick_count` grows by one per interval.
- [ ] `GET /api/scheduler` → `status.running: true`, `heartbeat_healthy: true`, `alerts: []`.
- [ ] Your job has `"state": "active"` and a `next_run_at`; after it fired, `runs[0].status`
      is `completed` with `"tokens": 0`.
- [ ] The data directory holds `heartbeat.lock`, `scheduler.lock`,
      `scheduler-events.jsonl` and `scheduler-events.jsonl.head`.
- [ ] The stop line names `scheduler: stopped` and `heartbeat: stopped`.

## 5. Troubleshooting

| What you see | Code | Cause | What to do |
|---|---|---|---|
| `Scheduler: off`; POST answers 503 | `unavailable` | `STIRA_SCHEDULER` not set for the server process | start with `STIRA_SCHEDULER=1` |
| `Heartbeat: off` | — | neither `hb_enabled: true` nor `STIRA_HEARTBEAT=1`, or `STIRA_HEARTBEAT=0` | 2.1 |
| `Heartbeat: enabled, not running (<Class>)`; `heartbeat.last_error` | `HeartbeatLockError`, `UnsafeLockFileError`, `HeartbeatIntervalsError` | another process holds `heartbeat.lock`; the file is a symlink / not yours; shortest interval above the longest in the file | [heartbeat-daemon.md](heartbeat-daemon.md#guarantees), [operations.md §1](operations.md#1-start-stop-status); fix the bounds ([configuration.md](configuration.md)) |
| alert / notification `heartbeat_stalled` | `heartbeat_stalled` | enabled but stopped, degraded (3 failures in a row) or overdue | read `heartbeat` in `/api/gateway/status` (`last_error`, `degraded`); restart after fixing the cause |
| `run_skipped(heartbeat)` in the log, alert `heartbeat_required` | `heartbeat_required` | a model-calling job while the heartbeat is not healthy | switch the heartbeat on, or give that job `"requires_heartbeat": false` knowingly |
| start-up warning `grants: heartbeat_start unavailable (reason=scheduler_off)` | `scheduler_off` | `autonomy.heartbeat_start` on, scheduler off | `STIRA_SCHEDULER=1` |
| `… (reason=heartbeat_off)`; grant card skips `heartbeat_unhealthy` | `heartbeat_off`, `heartbeat_unhealthy` | heartbeat off, or not healthy later | 2.1; [autonomy.md](autonomy.md#2-how-to-try-it-step-by-step) |
| notification `jobs_paused_at_start` | `jobs_paused_at_start` | model-calling memory jobs and Agent-layer jobs come up paused after every start | resume the ones you want (a person, confirmed) |
| `job_failing` | `job_failing` | three failures in a row | `runs[].reason` of that job |
| `budget_exhausted` | `budget_exhausted` | a job or day budget was reached; the job is paused | raise the budget or wait; resume |
| `approval_required` | `approval_required` | an Agent-layer job called a tool that needs a person | jobs may use read-only tools only |
| `breaker_critical`; 409 on create / resume | `breaker_critical` | the breaker is CRITICAL (409: not NORMAL) | wait for NORMAL; see [neural-saturation.md](neural-saturation.md) |
| `log_recovered` | `log_recovered` | the log was interrupted; every job was paused | check and resume ([operations.md §6](operations.md#6-scheduler)) |
| `stira approvals decide` says `not confirmed (nothing was decided; it expires by itself)` | — | the typed word was not `APPROVE` (or no TTY) | run it again in a terminal and type the word |
| Resume on `/schedules`: "Refused: Cancelled; nothing was applied", no confirmation dialog | — | the default browser channel on a version before the fix of #650 | update, or start with `STIRA_YOLO_HOST_CONFIRM=command` (2.3) |
| `status.error: log_unavailable`, everything 503 | `log_unavailable` | the event log failed verification | [operations.md §6](operations.md#6-scheduler) |
| "Other (code: harvest_failed)" | a job's own code | `memory_harvest` codes (`harvest_failed`, `harvest_cancelled`, `harvest_stopped`) have no page wording | [memory guide](guide-memory-routines.md#7-something-looks-wrong) |

## 6. Autonomy grants

The grant heartbeat start needs **all** of: the scheduler, the heartbeat,
`autonomy.heartbeat_start` (with `stage_c_exec`) and a grant issued with its own flag. The
steps, the tool rules for unattended iterations and the limits are in
[autonomy.md §2](autonomy.md#2-how-to-try-it-step-by-step), step 2; they are not repeated
here. Issuing, resuming and verifying a grant need a **host confirmation**: without a host
channel they are refused (`host_channel_required`; the start-up line says
`issue / resume / verify refused: no host confirmation channel (STIRA_YOLO_HOST_CONFIRM, or autonomy.allow_browser_confirm)`).
The heartbeat and the scheduler being on approves nothing and starts no grant by themselves.

## 7. Keeping the server running

Out of scope here: `scripts/stira_ctl.sh` ([operations.md §1](operations.md#1-start-stop-status)),
launchd on a Mac ([install.md §12](install.md#12-keep-it-running-launchd-and-task-scheduler)),
systemd on Linux ([deploy-ec2.md §5](deploy-ec2.md#5-running-as-a-service-systemd)). Put the
`STIRA_*` variables into the environment those start the server with.
