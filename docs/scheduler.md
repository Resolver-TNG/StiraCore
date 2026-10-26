# Scheduled jobs and health notifications (#239)

Design and rationale: [ADR 0008](adr/0008-scheduler.md). Code:
`src/stiracore/scheduler/` (schedule, job schema, chained log, scheduler, executors) and
`src/stiracore/observability/scheduler_api.py` (HTTP API). Operator guide (what to switch on,
the first job, checks, troubleshooting): [guide-scheduler-heartbeat.md](guide-scheduler-heartbeat.md).

## Enabling (opt-in, off by default)

`STIRA_SCHEDULER=1` when starting `dashboard/stira_server.py`. Off: no lock file, no log,
no thread, every change returns 503. On the chat server the Gateway actions `health_check` and
`memory_harvest` are registered; free-prompt Agent-layer jobs are refused there. With
`memory_autonomy` on (ADR 0018 phase 0), the fixed **memory** job kinds it enables
(`"layer": "memory"`, `"action": "memory_rate_backfill" | "memory_consolidate" |
"memory_tidy"`) run in their own executor and write plan files only — see
[memory-autonomy.md](memory-autonomy.md). Model-calling memory kinds come up paused after a
restart until a person resumes them.

## Job schema version 2 and model-calling jobs (ADR 0021 S1)

[ADR 0021](adr/0021-scheduler-job-base.md) phase S1. New definitions are `schema_version: 2`
(the default); version 1 definitions stay valid and mean the same thing, and an update is
written as version 2. Version 2 adds three fields (a version 1 definition that names one is
refused, 400):

| Field | Values | Default | Notes |
| --- | --- | --- | --- |
| `unattended_approval` | `deny` / `wait` | `deny` | agent jobs only; `wait` (ADR 0024 phase 2) needs gateway `agent_jobs` on (else 403 `scope`) |
| `approval_wait_max_sec` | 60–86400 | 3600 with `wait` | only with `wait`; the wait is also bounded by `hitl_timeout_sec` |
| `requires_heartbeat` | `true` / `false` | `true` for model-calling jobs | ignored by Gateway jobs and `memory_tidy` |

*Model-calling jobs* are Agent-layer jobs and the memory kinds `memory_rate_backfill` /
`memory_consolidate`. For them:

* `missed_policy: "catch_up"` is refused (403 `scope`); use `skip` (default) or `latest`. A
  definition stored before S1 that still says `catch_up` runs as `latest` (at most the newest
  missed fire).
* **A healthy heartbeat is a start condition** (`requires_heartbeat`, default `true`): the
  heartbeat is enabled, running, not degraded and its last tick is within
  `max(3 × interval, 120 s)` — read at each tick and again right before the run body. Otherwise
  the fire is recorded `run_skipped(heartbeat)` (or the run is cancelled with reason
  `heartbeat`) and the alert `heartbeat_required` is raised once; it resolves when the heartbeat
  is healthy again. With the heartbeat off (its default), these jobs do not run unless they say
  `"requires_heartbeat": false`. A scheduler with no heartbeat wired at all (an embedder; never
  The chat server) does not apply the condition. Gateway jobs ignore it.
* Agent-layer jobs share one pool, `agent_daily_token_budget` (50,000 tokens per UTC day,
  never unlimited; a `Scheduler` argument), next to the per-job caps and under the server-wide
  `daily_token_budget`. On the chat server the pool is `agent_jobs.agent_daily_token_budget` in
  `gateway-config.json`.
* After a start, one `jobs_paused_at_start` notification gives the number of jobs the start
  paused (the model-calling memory kinds, ADR 0018 Q4, and every Agent-layer job, ADR 0021 §9).

**ADR 0021 S2 — the gated Agent-layer executor.** Off by default; `gateway-config.json`
`agent_jobs.enabled` (settings page, "Scheduled agent jobs") builds it at the next start.
`GatedJobExecutor` (`stiracore.control.agent_jobs`) runs the job's prompt on a child built
like `delegate`'s (`create_nxp_subagent`: the server agent's tool rules, write / read floors,
`IdentityGuard`), with three guards registered before that gate: `JobToolGuard` (only
`JOB_READ_ONLY_TOOLS` ∩ the job's tools, and the per-run token cap), `ServerBreakerGuard` (the
server's breaker, read before every model and tool call; leaving NORMAL ends the run,
`cancelled(breaker)`) and `JobRulesGuard` (a rule set to Never refuses, Ask stops the run with
`approval_required` — unattended = deny unless the job sets `wait`, below). The child's Neural State is a fresh
copy (no stimulus, no episode). The reply is stored masked inside a `<job-output>` frame
(neutralized so it cannot close the frame), marked as data, not instructions. Agent-layer jobs
come up `paused` after every start, and a stored Agent-layer job cannot be resumed while the
layer is off (`403 scope`). An interrupted run is closed `run_cancelled(interrupted)` and never
resumed from a checkpoint. The heartbeat tick never reaches the executor (tested on the chat
server with tripwires). The child uses the chat server's model and provider (ADR 0021 §5
names the `ExecutionContext.CRON` model; its ids are Bedrock-only, so this is deferred).

* `health_check`: model-free component summary.
* `memory_harvest` (#251): one Harvest sweep inside the server (it holds `server.lock`),
  output to `semantic/staging/` only, at most `harvest_job_max_episodes` episodes, stopped
  when the breaker leaves NORMAL. Rule distiller only, never a model call: with
  `distiller_mode` `"llm"` / `"jev"` the job cannot be created, updated or resumed (`403
  scope`, `distiller_not_rule`) and a run fails before scoring. Details and measurements:
  [memory-maintenance.md](memory-maintenance.md) pattern C.

## Jobs that wait for an approval (ADR 0024 phase 2, ADR 0021 S3)

With `unattended_approval: "wait"`, a call whose rule is Ask does not stop the run: the request
is stored (origin `kind: "job"`), the run shows **Waiting for approval** and raises
`approval_waiting`, and its worker is released. Later fires of the job are skipped (overlap)
while it waits. Approving it in the dashboard's approvals panel continues the run from its
checkpoint: only the approved call runs, once, with the approved arguments. Reject, expiry,
a pause, a job update, the breaker reaching CRITICAL (or not NORMAL when it would continue),
a stop or a restart end the run as cancelled; nothing of it runs. Waits do not survive a
restart: every job request is cancelled at start. At most 3 runs wait at a time.

## What runs

Only jobs a human created through the API with `"confirm": true` (Stira has no tool or
route to create one). Jobs may use read-only tools only (`current_time`, `memory_read`,
`memory_search`, `memory_list`, `file_read`); a call to anything else is where a human
approval would be needed, and an unattended job stops there (`approval_required`) and
raises a notification.

## API (same-origin + CSRF token, see `docs/admin-api-permissions.md`)

```text
GET  /api/scheduler                          status, jobs, recent runs, notifications
POST /api/scheduler/jobs                     {"job": {...}, "confirm": true, "actor": "name"}
POST /api/scheduler/jobs/<job_id>            {"changes": {...}, "confirm": true}
POST /api/scheduler/jobs/<job_id>/pause      {"reason": "..."}       (always allowed)
POST /api/scheduler/jobs/<job_id>/resume     {"confirm": true}
POST /api/scheduler/runs/<run_id>/cancel     {}                      (always allowed)
```

While the YOLO allowance is on (`STIRA_YOLO_ALLOW`, on by default) create, resume and a
confirmed update are also a confirmed operation (`scheduler_loosen`, ADR 0012 §8b): the first
request answers 428 `host_confirmation_required`; after **a person** confirmed it (e.g.
`stira approvals decide <short>` with `STIRA_YOLO_HOST_CONFIRM=command`) the same request is
sent again with `X-Stira-Host-Operation: <id>`. The `/schedules` page has no create form.
Steps: [guide-scheduler-heartbeat.md §2.3](guide-scheduler-heartbeat.md#23-create-the-first-job-an-hourly-health-check-model-free).

### Codes the UI words (#279)

The server sends codes and variables; `/schedules` builds the sentence from the catalog
(`catalog.{ja,en}.json`, keys `schedules.*`). The English `message` stays as masked
audit text (shown as a muted detail line).

| Field | Where | Content |
| --- | --- | --- |
| `status.alerts[]` | `GET /api/scheduler` | `"<code>"` or `"<code>:<job_id>"`; codes `breaker_critical`, `heartbeat_stalled`, `job_failing`, `heartbeat_required` |
| `runs[].reason` | recent runs | a run reason code (`approval_required`, `budget_exceeded`, `interrupted`, …; `scheduler.RUN_REASON_CODES`) or an exception class name |
| `notifications[].code` | notifications | `breaker_critical`, `heartbeat_stalled`, `job_failing`, `log_recovered`, `budget_exhausted`, `approval_required`, `heartbeat_required`, `jobs_paused_at_start` (`scheduler.NOTIFICATION_CODES`), or a job's own `[a-z][a-z0-9_]{0,39}` code |
| `notifications[].params` | notifications | variables from an allowlist, `{}` for none/older entries: `count` (consecutive failures, `job_failing`; paused jobs, `jobs_paused_at_start`), `tool` (tool name, `approval_required`), `kind` (`max_runs_per_day` / `max_tokens_per_day` / `daily_token_budget` / `max_tokens_per_run` / `agent_daily_token_budget`) and `limit` (`budget_exhausted`). Values of any other shape are dropped, never free text |

A code the page has no wording for is shown as "Other (code: …)" (just "Other" when it
is not shaped like a code), never bare. Tests: `tests/test_scheduler_ui_codes.py` keeps the
code sets, the page tables and both catalogs in step.

Example job:

```json
{"name": "hourly health", "action": "health_check",
 "schedule": {"type": "cron", "expr": "0 * * * *", "timezone": "Asia/Tokyo"}}
```

Refusals: 400 invalid, 403 scope, 404, 409 conflict / breaker (create, update and
resume are refused at WARNING and above), 428 confirmation required, 503 unavailable.

## Defaults

| Concern | Default |
| --- | --- |
| Missed run (server stopped, host slept) | `skip`: recorded as `run_missed`, not run (`catch_up` refused for model-calling jobs) |
| Heartbeat (model-calling jobs) | required: `run_skipped(heartbeat)` + `heartbeat_required` while it is off, stopped, degraded or overdue |
| Overlap | `skip`: a fire while the previous run is active is recorded, not queued |
| Duplicate delivery | `run_key = <job>@<time>` is never started twice (restart, clock rewind) |
| Breaker WARNING / COHERENCE | hold one fire, run it on recovery within `hold_max_sec` (3600) |
| Breaker CRITICAL | nothing starts, active runs cancelled, notification |
| Budget | per job/day runs and tokens; reaching it pauses the job (`system`) and notifies |
| Log tampering | scheduler unavailable (nothing runs); a deleted last line pauses every job (crash-safe recovery marker) |
| Stop | always accepted; the owner lock is kept until every dispatched worker has ended |
| Before each run body | re-check: running, not paused, same job generation (an update cancels older runs), breaker NORMAL, heartbeat healthy (model-calling jobs), runs/tokens budget (with reservations) |
| Unknown usage (error, crash, shutdown) | charged `max(observed, reservation)`; reservation persisted at start |

The page `/schedules` shows the same state and offers pause / resume / cancel.
