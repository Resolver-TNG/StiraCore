# ADR 0008: Operator-defined scheduled jobs and health notifications

**Path:** `docs/adr/0008-scheduler.md`
**Status:** Accepted (two open decisions are marked **Open** below; the implementation
uses the fail-closed option for both)
**Issue:** #239 (split ② of #105). Related: #238 (heartbeat daemon, owner lock), #240 /
ADR 0006 (durable approvals), #171 (EventStore), #146/#189/#190 (approval gate), #185
(Origin/CSRF, masking), #95 (checkpoints), #104 (memory refinement triggers).
**Decision:** A model-free *Gateway layer* scheduler evaluates human-defined jobs on an
injectable clock and records every job / run decision in an append-only, hash-chained
JSONL log. Model-calling jobs (*Agent layer*) run only with explicit per-run and per-day
token budgets and only with read-only tools. Anything that cannot be verified, or that
would need a human, does not run.

> **To be amended by ADR 0011 (Proposed).** On 2026-10-16 the owner authorized the design
> and development of (1) an autonomous goal loop that runs only while the user's explicit
> grant is active, with an expiry (default 12 h, at most 36 h), a budget and a scope, and
> (2) bounded self-scheduling: inside an envelope a human defined, the agent may propose and
> create jobs. Both stop when the emotional state worsens. Autonomy without a grant,
> always-on event-driven operation, self-expansion of permissions and the agent changing its
> own envelope stay out of scope. The design is ADR 0011 (Proposed: `docs/adr/0011-agent-self-scheduling.md`, PR #295, tracking
> #296). Until ADR 0011 is accepted and implemented this
> ADR describes `main` unchanged: only jobs a human defined and confirmed run, and "Let
> Stira propose jobs" (Alternatives) and "self-created jobs" (Out of scope) are not
> implemented. ADR 0011 changes them only *inside* a human-defined envelope.

> **Follow-up (Accepted 2026-10-21): [ADR 0021](0021-scheduler-job-base.md)** — one job base
> for Gateway, Memory and Agent jobs, a gated Agent-layer executor, the heartbeat as a start
> condition for model-calling runs; Open (a) and Open (b) are answered on the approval side by
> [ADR 0024](0024-approvals-beyond-the-turn.md) (Accepted 2026-10-21). S1 is scheduled, S2 and
> S3 need their own go; until each phase ships this ADR describes `main` unchanged.

## Context

`HeartbeatDaemon` (#238) keeps a zero-model tick alive, but nothing runs operator-defined
work on a schedule, and nothing tells the operator that the heartbeat stalled, the breaker
went CRITICAL or a job keeps failing. #26 asked for job / run events, create / pause /
resume, operator confirmation and audit. The `cron` tool of `strands_tools` is an exec
tool (hidden unless `STIRA_ENABLE_EXEC_TOOLS=1`) and unrelated: it edits the host crontab.

Constraints not up for change here:

* **Only jobs a human defined and confirmed run.** Stira never creates, widens or resumes
  a job by itself (no tool, no route reachable from a turn). Fully autonomous /
  event-driven operation (third-generation conditions) is not authorized (for the
  grant-scoped loop and envelope-bounded self-scheduling see the note at the top).
* Notifications stay inside the dashboard and the server log. No external channel.
* The approval gate is never bypassed; no environment variable changes that.

## Decisions

### 1. Job definition schema (version 1)

`stiracore.scheduler.jobs.parse_job` validates a strict JSON object (unknown keys are
refused, so a newer client cannot smuggle fields an older server ignores):

| Field | Values | Default |
|---|---|---|
| `schema_version` | `1` | `1` |
| `job_id` | `[a-z0-9][a-z0-9_-]{0,47}` | server-generated |
| `name` | 1–80 printable characters | required |
| `layer` | `gateway` (model-free) / `agent` (model call) | `gateway` |
| `action` | a registered Gateway action (`health_check`) | required for `gateway` |
| `prompt` | ≤ 4000 characters, refused if `mask_secrets` would change it | required for `agent` |
| `tools` | subset of `JOB_READ_ONLY_TOOLS` | `[]` |
| `schedule` | `{"type": "cron", "expr": "m h dom mon dow", "timezone": "UTC"}` / `{"type": "interval", "every_sec": ≥60, "anchor": epoch}` / `{"type": "once", "at": epoch}` | required |
| `missed_policy` | `skip` / `catch_up` / `latest` | `skip` |
| `max_catch_up` | 1–10 (only `catch_up`) | 3 |
| `overlap_policy` | `skip` | `skip` |
| `hold_max_sec` | 0–86400 | 3600 |
| `budget` | `max_runs_per_day`, `max_tokens_per_run`, `max_tokens_per_day` (positive ints or null) | all null |

* Cron is the classic five-field form (`*`, lists, ranges, `/step`, numeric only).
  Day-of-month / day-of-week follow Vixie cron exactly: a field whose text starts with
  `*` (including `*/n`) is a *star* field; if either field is a star the two are AND-ed,
  only when both are restricted are they OR-ed (`0 0 */1 * 1` = Mondays only).
  Times are evaluated in the job's IANA timezone (default UTC, so a fake clock test is
  deterministic). A wall time that does not exist (DST gap) is skipped; an ambiguous one
  fires once (first occurrence).
* An `agent` job **must** set `max_tokens_per_run` and `max_tokens_per_day`.
* The definition never contains a secret: `job_id`, `name`, `action` and `prompt` are
  refused if `mask_secrets` would change them. Free text the scheduler stores itself
  (pause reasons, failure reasons, tool names of an approval stop, notifications,
  summaries) is masked and capped. Wrong JSON types are `invalid` (400), never a 500.
* `created_by` / `state` are not client fields: the server records the principal.

### 2. Time source

`Scheduler(now_fn=...)` reads one injectable clock (default `time.time`). `tick()` is
deterministic, so tests drive it with a fake clock (`start(background=False)` + `tick()`),
like `HeartbeatDaemon.poll()`. The background worker re-reads the clock at least every
30 s, so a host sleep or a wall-clock jump is noticed promptly. A clock that moves
backwards produces no new fire until it passes the evaluated point again; the run key
(§4) makes a re-computed fire a no-op.

### 3. Missed runs

A fire is *due* when it is at most `grace_sec` (60 s) old at the tick that sees it, and
*missed* when it is older (the server was stopped, the host slept, the process was busy).
Per job:

* `skip` (**default**): no missed fire runs; one `run_missed` event records count, first
  and last time. Nothing is fired in bulk.
* `latest`: only the newest missed fire runs (if no fire is due); the rest are recorded.
* After an outage longer than 7 days, fires older than the window are not enumerated;
  the `run_missed` event is marked `truncated`.
* `catch_up`: up to `max_catch_up` of the newest missed fires run, oldest first, one at
  a time (the overlap policy applies between them); the rest are recorded.

Several fires *due* in one tick (a tick slower than the schedule) are coalesced: the
newest runs, the others are recorded `run_skipped(coalesced)`.

The evaluated point of a job is derived from the log (its creation / resume / update time
and the newest fire recorded for it), so a restart neither re-runs nor silently forgets
fires: everything between the last recorded point and "now" is classified again by the
same rule. A pause is not a miss: `resume` moves the evaluated point to the resume time.

### 4. Overlap and idempotency

* `run_key = "<job_id>@<scheduled_at>"`. A key already present in the log (started,
  skipped, missed, held) is never started again — duplicate delivery after a clock
  rewind, a second `tick()` or a restart is a no-op.
* `overlap_policy = skip` (the only v1 value): while a run of the job is active, a new
  fire is recorded `run_skipped(overlap)`. Queuing is not offered (it would accumulate
  work while the operator is away).
* A run that has `run_started` but no terminal event when the log is opened (the process
  died) is closed as `run_cancelled(interrupted)`; it is **not** retried (at most once).

### 5. Owner lock

One scheduler per data directory: an OS file lock on `<state_dir>/scheduler.lock`
(`HeartbeatOwnerLock` with another file name, same kernel-released semantics as #238;
since #322 the shared no-follow implementation of `server.lock`: a symlink, a hard link,
a non-regular file or another user's file is refused with `UnsafeLockFileError`, nothing
is written to it and the scheduler stays STOPPED).
Only the lock owner opens the log for writing. A second server on the same directory
reports `SchedulerLockError` and refuses every schedule change (503); it does not run jobs.
(the chat server's own data-directory lock, `server.lock`, #285, refuses a second server before
the scheduler is reached; this path remains for any other holder of `scheduler.lock`.)
`stop()` separates *accepting* the stop from *finishing* it: while a dispatched worker is
still alive (e.g. an action that ignores cancellation) the state stays STOPPING, `stop`
returns `False` and the lock is kept; the last worker to finish releases it, so a new
owner can never run the same job concurrently. Cancellation callbacks (e.g.
`Agent.cancel`) never run synchronously under the scheduler lock and are never dropped:
each cancelled run queues its callbacks once, and a fixed pool of at most
`cancel_workers` (4) threads runs them (started on demand, exiting after 1 s idle), so
the thread count stays bounded however many runs are cancelled at once. While
`max_cancel_backlog` (16) batches are queued or running, every new run is refused before
its body (`cancel_backlog`).

**Definition of "stopped":** the tick thread and every dispatched job worker have ended;
the owner lock is then released. Cancellation-callback threads are *not* waited for
beyond the stop deadline: they hold no scheduler lock, cannot start or execute a job
body, and are reported in `status().cancel_workers` / `cancel_backlog` until they end.

### 5a. Pre-execution re-check

Starting a run and executing its body are separate (a worker thread runs the body). Right
before every body — the first of a batch included — the scheduler re-checks under its
lock: still running, no open recovery, job not paused, **same job generation** (every
`job_updated` increments it; an update also cancels the job's runs of older generations,
so a queued run never executes with removed tools, an old prompt or an old per-run cap),
breaker NORMAL (read just before), cancellation backlog below the cap, the run's ordinal
of the day within the current `max_runs_per_day` (Gateway and Agent), and for agent runs
the token budget **of the run's persisted charge day** (used + every unsettled
reservation of that day, its own included) — a run queued across midnight is checked
against the day that holds its reservation, not the new calendar day. Any failure ends the run as
`cancelled(<reason>)` without running the body.

### 6. Budget

* Per job: `max_runs_per_day`, `max_tokens_per_run`, `max_tokens_per_day`; per scheduler:
  `daily_token_budget` (server-wide, across agent jobs). Days are UTC days of the run
  start. Usage is replayed from `run_completed` / `run_failed` events, so a restart does
  not reset the budget.
* Every started, unsettled agent run holds a **reservation** of its `max_tokens_per_run`
  (in memory, under the scheduler lock), counted against the job and the server-wide day
  budget for every later start — across jobs, in the same tick or not — until the run
  reports its usage. The reservation and its charge day are **persisted in
  `run_started`** (`reserved_tokens`, `charge_day`), so a crash is settled with the
  values the run started with, never with a later definition.
* Settlement: a run with known usage is charged what it reported. A run whose final
  usage is unknown — the executor raised (observed usage so far is kept), the process
  died, or it was closed at shutdown — is charged `max(observed, reservation)`.
* Before a start: if the next run could exceed the remaining day budget (used + reserved
  + `max_tokens_per_run` > cap, or the run count is reached), the run is recorded
  `run_skipped(budget)`, the job is **paused by `system`** and a notification is raised.
  A human has to resume it (next day or after raising the budget).
* During an agent run, usage is checked after every model call; reaching
  `max_tokens_per_run` cancels the invocation (`Agent.cancel()`), the run fails with
  `budget_exceeded`, the job is paused and a notification is raised. At most one model
  response can overrun the cap.
* Gateway-layer jobs never call a model and consume no tokens; only `max_runs_per_day`
  applies to them.

### 7. Breaker interaction

The breaker level is read from `engine.breaker_status()` (no turn is counted). An
exception or an unknown level is treated as CRITICAL.

* `WARNING` / `COHERENCE`: new fires are **held** (`run_held`), at most one per job. When
  the level returns to NORMAL, the held fire runs if it is not older than `hold_max_sec`,
  otherwise it is recorded `run_skipped(held_expired)`.
* `CRITICAL`: no run starts; held fires are dropped (`run_skipped(breaker_critical)`);
  active runs are cancelled (`run_cancelled(breaker_critical)`); a notification is raised.
* Schedule changes that widen what runs (create, update, resume) are refused at WARNING
  and above. Pausing a job, cancelling a run and stopping the scheduler (`stop`) are
  always allowed, at every level and also when the log is unavailable: they take effect
  in memory first and their recording is best effort.

**Open (a) — CRITICAL vs. pending approvals and jobs.** Options:
(a1) stop all job starts, leave pending approvals to the #240 expiry rule, allow only
stop-like operations (**implemented**); (a2) additionally cancel every pending approval
on the CRITICAL transition, so nothing approved during the incident can run after it.
(a2) is stricter but discards human work that the replay already re-checks under the
current breaker policy (ADR 0006 §5: under CRITICAL, actuate/exec stay removed and
writes stay gated). Needs an operator decision; (a1) is the default until then.

### 8. Tools, approvals and unattended jobs

* A job's tools must be in `JOB_READ_ONLY_TOOLS` (`current_time`, `memory_read`,
  `memory_search`, `memory_list`, `file_read`). A definition naming any other tool —
  including write, exec (`shell`, `cron`, …) and outbound tools — is refused at
  validation, and `STIRA_ENABLE_EXEC_TOOLS` / `BYPASS_TOOL_CONSENT` do not change the
  list (it is a constant, not read from the environment).
* Defence in depth at run time: a job guard hook refuses every tool call that is not in
  the job's allowed set before it executes. Such a call is the point where a human
  approval would be needed: the run records `run_awaiting_approval` (tool name and
  argument hash only, never the arguments), the tool is cancelled, the invocation is
  cancelled, and the run ends `run_failed(approval_required)` with a notification.

**Open (b) — unattended job waiting for approval.** Options: (b1) fail closed
immediately and notify (**implemented**); (b2) submit the call to the durable approval
store (#240, `origin = {"kind": "job", "id": run_id}`) and resume the job from its
checkpoint once a human approves. (b2) means an unattended process resumes work later;
that is closer to autonomous operation and needs an explicit decision. This ADR defines
the connection point only: the `awaiting_approval` run state and its event carry
`job_id`, `run_id`, tool and argument hash — the fields ADR 0006 needs for an `origin`
of kind `job`. `UnattendedApproval` accepts only `"deny"`; any other mode is a
start-up error. No automatic resume exists.

### 9. Event log (until #171)

`SchedulerEventLog` (`scheduler-events.jsonl`, owner-only `0600`) follows ADR 0006's
chain rule: each line carries `hash = sha256(prev_hash || canonical_json([seq, ts, kind,
body]))`, and a sidecar head (`<log>.head`, atomic, `0600`) names the newest line and the
tail it was built on; it is written before the line is appended and `fsync`ed.

* head == tail → consistent. Head one ahead and built on the tail → a crash between
  head and append, **or** the last line was deleted. Not trusted: the recovery first
  records `recovery_started` (a durable ban: while it is open no run starts and no job can
  be resumed), then pauses every active job (`system`), then records
  `recovery_completed`. A crash anywhere in between leaves the ban open and the next start
  repeats the recovery, so deleting a `paused` line can never re-enable a job. A partial
  last line (crash during append) is dropped.
* Anything else (a line changed, inserted or appended without the head, a missing head
  or log, a head on another base) → the log is **unavailable**: the scheduler does not
  start, every schedule change is refused (503), the file is kept for inspection.
* While running, the in-memory state is authoritative; before every append the log's
  size **and the exact bytes of its last line** are compared with what this process
  wrote, so an external append or a rewrite of the tail (also same-size) is detected
  before the chain is extended (the log is then unavailable for good). A same-size
  rewrite of an earlier line is detected at the next start (full re-verification).
* Free text (notification messages, failure summaries) is stored through `mask_secrets`
  and capped. Tool arguments are never stored (hash only).
* Interface: `append(kind, body)`, `events()`, `verify()`; a #171 EventStore can replace
  it without touching the scheduler. Limitation (as ADR 0006): an attacker with write
  access to both files can rebuild a consistent chain.
* Retention: the file is append-only and replayed at start-up; the process keeps only
  derived state (jobs, the newest 500 runs, 200 notifications, run keys inside twice the
  7-day look-back). Rotation / compaction is left to #171.

### 10. Operations API and audit

`/api/scheduler` (read) and `POST /api/scheduler/jobs`, `/jobs/<id>` (update),
`/jobs/<id>/pause`, `/jobs/<id>/resume`, `/runs/<run_id>/cancel`:

* Authentication is the dashboard capability of #185: loopback Host, same-origin
  (`Origin` / `Sec-Fetch-Site`) and the per-process CSRF token, enforced by
  `install_security` before the view runs.
* Operator confirmation: create / update / resume require `"confirm": true` in the body
  (the page asks first). Without it: `428 confirmation_required`, nothing recorded.
* Scope: only schema fields; `layer` and `action` cannot be changed by an update
  (create another job instead); tools outside the read-only set, unknown actions and
  (on the chat server) `agent` jobs are refused (`403 scope`).
* Breaker: create / update / resume are refused at WARNING and above (`409 breaker`).
* Audit: every accepted change is an event with the principal (`local:<name>`,
  restricted to `[A-Za-z0-9._:@-]{1,64}`, else `local:unknown`); every refusal is
  logged at WARNING without the body.

### 11. Health notifications

Evaluated on every scheduler tick (model-free), raised on the transition only (no
repetition), cleared when the condition ends (a `resolved` notification):

* `heartbeat_stalled`: the heartbeat is enabled but not running, degraded, or its last
  tick is older than `max(3 × interval, 120 s)`;
* `breaker_critical`;
* `job_failing`: a job failed `failure_alert_after` (3) times in a row;
* `budget_exhausted`, `approval_required`, `log_recovered` (raised by the events above).

Notifications are events in the log (so they survive a restart) and WARNING log lines.

### 12. Shutdown

`stop(timeout)` stops the tick worker, requests cancellation of every active run
(`run_cancelled(shutdown)`), waits up to the deadline and releases the owner lock.
The chat server runs it on its normal stop path (#285): `SIGTERM` (what `stira_ctl.sh stop`
sends) becomes a normal exit, and the server closes the approval broker, then stops the
scheduler and the heartbeat (4 s each) *before* interpreter shutdown, which would
otherwise first wait for an SDK worker still blocked on an approval. A SIGKILL still
skips it; the next start closes the interrupted run as above. A job
does not resume from a checkpoint (#95) after a restart in v1; the interrupted run is
closed and the next fire follows the missed-run rule.

## chat server wiring

Opt-in, default OFF: `STIRA_SCHEDULER=1` (no config-file switch yet). The
server registers one Gateway action, `health_check`; Agent-layer jobs are **not** enabled
on the chat server in this change (create is refused with `scope`), so no schedule can cause a
model call there yet. The `/schedules` page shows status, jobs, recent runs and
notifications; its buttons pause / resume / cancel through the authenticated API.

## Alternatives considered

* **APScheduler / OS cron.** An extra dependency or host state that Stira cannot audit,
  bound to its breaker or test with a fake clock. Rejected.
* **SQLite chain (like ADR 0006).** Equally sound; JSONL keeps the log append-only and
  trivially inspectable, and the scheduler needs no queries. Either can move to #171.
* **Queueing overlapping fires / catch-up as default.** Accumulates work while nobody
  watches. Rejected as default; `catch_up` is opt-in and bounded.
* **Let Stira propose jobs.** Self-extension is out of scope (not authorized). To be
  revisited inside a human-defined envelope by ADR 0011 (Proposed; see the note at the top).

## Out of scope

External notification channels, Agent-layer jobs on the chat server, write/exec tools in jobs,
waiting for or resuming after a durable approval (Open b), checkpoint resume of jobs,
self-created jobs, event-driven triggers.
