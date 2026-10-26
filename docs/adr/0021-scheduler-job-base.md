# ADR 0021: One scheduler job base — operator, memory and agent jobs; gated Agent-layer jobs; heartbeat and approval connection points

**Path:** `docs/adr/0021-scheduler-job-base.md`
**Status:** Accepted (2026-10-21, commander decisions below; every recommendation taken).
**Phase S1 (§12) is implemented** (PR #537: schema v2 parser, `catch_up` refused for
model-calling jobs, §8 heartbeat requirement, §10 `heartbeat_required` / `jobs_paused_at_start`,
the Agent pool as a `Scheduler` argument; `approval_waiting` waits for S3). **Phase S2 is
implemented** (go 2026-10-21; `stiracore.control.agent_jobs.GatedJobExecutor`, gateway
`agent_jobs` default off with `agent_daily_token_budget` on the settings page, Agent-layer jobs
paused at every start, `/schedules` shows the layer). Deviation: jobs use the chat server's
model, not the `ExecutionContext.CRON` model (§5) — see docs/scheduler.md. **S3 (ADR 0024
phase 2) is implemented** (go 2026-10-21; see ADR 0024 "Implementation notes (phase 2)").
**Issue:** Refs #239 (split ② of #105). Related: [ADR 0008](0008-scheduler.md) (scheduler,
Accepted — this ADR builds on it and does not replace it), [ADR 0018](0018-autonomous-memory.md)
(memory jobs, #251 / #252), [ADR 0011](0011-agent-self-scheduling.md) (self-scheduling and the
restart contract, Proposed, #296), [ADR 0024](0024-approvals-beyond-the-turn.md)
(approvals beyond the turn, Accepted 2026-10-21; first drafted as 0020 — the approval side of
the same questions),
[ADR 0006](0006-hitl-persistence.md) (durable approvals), [ADR 0010](0010-tool-policy-gate-stack.md)
(gate stack), [ADR 0012](0012-yolo-mode.md) (YOLO), [ADR 0013](0013-audit-event-contract.md)
(audit contract), [ADR 0016](0016-subagent-orchestration.md) (`delegate`, the gated child
pattern), #238 (heartbeat daemon), #95 (checkpoints), #171 / [ADR 0009](0009-eventstore-chain.md)
(EventStore), #276 (single pre-execution boundary), #285 (`server.lock`).
**Decision (proposed):** keep **one** scheduler (one schema, one log, one owner lock, one budget
and breaker machinery) for every kind of job and keep **separate executors** per layer:
Gateway (model-free), Memory (ADR 0018's fixed kinds) and Agent (an operator's prompt). The
Agent layer gets a **gated executor** — the job's agent runs through the same rule → breaker →
floor gate, checkpoint hooks and operation ledger as a turn, built like ADR 0016's child — and
stays off by default. Model-calling runs need a healthy heartbeat, come up paused after a
restart, never catch up in bulk, and stop at the first call that needs a human (ADR 0024 decides
whether they may wait). Only jobs a human defined and confirmed run; self-created jobs remain
ADR 0011.

## Where we are (measured against `main` at `4ad82b8`)

The issue text of #239 predates PR #265 and ADR 0018. Read from the code, not from the issue:

| # | Item | State | Where |
|---|---|---|---|
| 1 | cron / interval / once, versioned schema, strict parser | **done** | `scheduler/jobs.py`, `scheduler/schedule.py` |
| 2 | Injectable clock, fake-clock tests | **done** | `Scheduler(now_fn=…)`, `tests/test_scheduler.py` |
| 3 | Missed runs (default skip + record), coalescing, 7-day window | **done** | ADR 0008 §3 |
| 4 | Overlap `skip`, run key, interrupted runs closed (never retried) | **done** | ADR 0008 §4 |
| 5 | Owner lock (`scheduler.lock`, no-follow), the chat server `server.lock` | **done** | ADR 0008 §5, #285, #322 |
| 6 | Per-run / per-day / server-wide token budgets with reservations | **done** | ADR 0008 §6 |
| 7 | Breaker: hold at WARNING / COHERENCE, stop at CRITICAL, stop-like ops always | **done** | ADR 0008 §7 |
| 8 | Auth (same-origin + CSRF), confirmation, scope, breaker on changes | **done** | `observability/scheduler_api.py` |
| 9 | Read-only tools only; write / exec refused at validation and at run time | **done** | `JOB_READ_ONLY_TOOLS`, `JobToolGuard` |
| 10 | Hash-chained event log, recovery ban, notifications | **done** | `scheduler/event_log.py`, ADR 0008 §9, §11 |
| 11 | Third layer `memory` (fixed kinds, own executor, model kinds paused at start) | **done** (ADR 0018 phase 0) | `LAYERS`, `MODEL_CALLING_KINDS`, `_pause_memory_jobs_at_start` |
| 12 | Agent-layer jobs on the chat server | **off**: no `agent_executor` is passed, create is refused (`scope`) | `stira_server._start_scheduler` |
| 13 | Agent-layer executor under the gate | **no**: a plain Strands `Agent` + `JobToolGuard`; no `CheckpointHooks`, no rules / floor, no ledger, no breaker read during the run | `scheduler/executors.py` |
| 14 | Heartbeat health as a start condition (ADR 0018 §2a) | **not found** — `health_source` is read only for `heartbeat_stalled` | `Scheduler._health` |
| 15 | `idle` trigger (ADR 0018 §2a) | **not found** — schedule types are `cron`, `interval`, `once` | `schedule.py` |
| 16 | ADR 0011 §6 "every job paused at every start" | **not implemented** (only model-calling memory kinds) | `start()` |
| 17 | Checkpoint resume of an interrupted job (#95) | **no** (ADR 0008 §12) | — |
| 18 | Events in the shared EventStore, log rotation | **no** (own chain; ADR 0009 stage 3 / #277) | — |

So the issue's acceptance criteria are met for Gateway jobs (except the shared EventStore), and
the open work is the Agent layer, the connection points with #238 and #240, and the relation
with ADR 0018's memory jobs — which is what this ADR decides.

## Decisions

### 1. One job base, three layers, separate executors (relation to #252 / ADR 0018)

**Integrated, not side by side.** ADR 0018 already put the memory kinds on ADR 0008's scheduler
(`layer: "memory"`); a second scheduler for agent jobs would duplicate the owner lock, the
budget replay, the breaker holds and the chained log, and two schedulers could each stay inside
their own budget while together exceeding what the operator meant. So:

| | Gateway | Memory (ADR 0018) | Agent |
|---|---|---|---|
| What runs | a registered action (`health_check`, `memory_harvest`) | a fixed kind; the prompt is in code | the operator's `prompt` |
| Model | never | only `MODEL_CALLING_KINDS` | yes |
| Tools | none | none | ⊆ `JOB_READ_ONLY_TOOLS` |
| Executor | the action function | `MemoryJobExecutor` (own worker thread) | `GatedJobExecutor` (§5, new) |
| Token pool | none | `memory_daily_token_budget` (100,000, ADR 0018 Q5) | `agent_daily_token_budget` (new, Q3) |
| Default on the chat server | on with `STIRA_SCHEDULER=1` | with `memory_autonomy` | **off** (Q5) |

Shared by all three, unchanged from ADR 0008: schema and parser, run keys, missed-run and
overlap rules, per-job caps and reservations, the pre-execution re-check (§5a), breaker rules,
owner lock, the chained log, notifications, the API with confirmation. The only code that may
build a model stays the Memory and Agent executors; the heartbeat tick never dispatches, never
imports an executor and never builds a model (ADR 0018's tested invariant).

### 2. Job definition schema version 2 (additive)

Version 1 definitions stay valid and mean the same thing. Version 2 adds fields; unknown keys
are still refused, and a field whose phase is not enabled is refused (`403 scope`), so a newer
client cannot store a setting an older server ignores:

| Field | Values | Default | Layer |
|---|---|---|---|
| `unattended_approval` | `deny` / `wait` | `deny` | agent (`wait`: ADR 0024 phase 2 only) |
| `approval_wait_max_sec` | 60–86400 | 3600 | agent, with `wait` |
| `requires_heartbeat` | `true` / `false` | `true` for model-calling jobs, ignored otherwise | agent, memory (model kinds) |

`missed_policy: "catch_up"` is **refused for model-calling jobs** (Agent, and Memory's model
kinds): after an outage they run at most the newest missed fire (`latest`) or none (`skip`,
default). Bulk model calls after an outage are the case "do not fire in bulk" exists for.

The `idle` trigger ADR 0018 §2a describes is a schedule type of this schema when ADR 0018 builds
it (`{"type": "idle", "idle_min": …}`); it is not part of this ADR's phases.

### 3. Time, missed runs, overlap

Unchanged from ADR 0008 §2–§4 (injectable clock, skip + record by default, `overlap_policy =
skip` only, run key per scheduled time). One addition: a run in `awaiting_approval` (ADR 0024 §6)
counts as **active** for the overlap rule, so later fires are recorded `run_skipped(overlap)`
while it waits (ADR 0024 Q4). Queuing stays refused.

### 4. Budgets

* Per job: unchanged (`max_runs_per_day`, `max_tokens_per_run`, `max_tokens_per_day`; an agent
  job must set both token caps).
* Pools: Memory jobs share `memory_daily_token_budget`; Agent jobs share a separate
  `agent_daily_token_budget`, **required** when the Agent layer is on (no "unlimited"); the
  existing server-wide `daily_token_budget` stays as the ceiling over both. A run must fit its
  job cap, its pool and the ceiling (used + every unsettled reservation + its own
  `max_tokens_per_run`), checked at start and again right before the body (ADR 0008 §5a).
* A continuation after an approval (ADR 0024 §6) takes a **new** reservation on its own charge
  day; the parked segment was settled with what it used.
* "Zero cost" applies to Gateway jobs and the heartbeat tick only; notifications are model-free.

### 5. The gated Agent-layer executor

`GatedJobExecutor` replaces the plain-`Agent` path of `AgentJobExecutor` (which stays for tests
and non-Stira embedders, unchanged, `deny` only). It is built like ADR 0016's child agent
(`create_nxp_subagent`), with these job-specific rules:

* **Same gate as a turn**: its own `CheckpointHooks` with the server's live rule reader (a rule
  set to Never removes the tool; Ask puts the call on a card — §6), the write floor and read
  floor, `IdentityGuard`, and the **server's** operation ledger. `JobToolGuard` stays as the
  outermost allow-list (defence in depth): a call outside the job's tools is refused whatever
  the rules say.
* **Breaker**: the server's breaker (not a child breaker) is read before every model call and
  every tool call; leaving NORMAL ends the run at that boundary (`cancelled(breaker)`), like a
  memory job at its item boundary. Today an Agent run is only cancelled at CRITICAL by the tick.
* **Tools**: `JOB_READ_ONLY_TOOLS` minus tools whose rule is Never. Not offered to jobs in this
  ADR: `delegate`, `page_check`, `run_checks`, web / MCP / outbound tools, `propose_procedure`,
  `memory_rate`, scheduling tools (Q6).
* **Checkpoints**: one thread per job, `job:<job_id>`, in the data directory's checkpoint store
  (`0600`, 20 generations per thread). They exist so that ADR 0024 phase 2 can continue a run
  inside one process lifetime; they are never used to resume a run after a restart (§9).
* **No effect on Neural State**: a job run is not a turn — no stimulus, no stability reward or
  penalty, no episode written. Its result is the run summary (masked, capped) in the log and on
  `/schedules`.
* **Model**: the `ExecutionContext.CRON` model of `model_factory`; the settings page shows which
  model jobs use.
* **YOLO** never answers a job's Ask (ADR 0012 §3 covers human chat / Stage turns only).

### 6. Approvals in jobs

A call the gate would put on a card is the point where a human is needed. Default **deny**
(ADR 0008 §8, as implemented: `run_awaiting_approval` with tool name and argument hash, then
`run_failed(approval_required)` and a notification). With `unattended_approval: "wait"` and ADR
0024 phase 2 enabled, the call goes to the durable approval store with `origin.kind = "job"` and
the run waits as ADR 0024 §6 describes. The approval gate is never bypassed: neither a job
definition, nor `STIRA_ENABLE_EXEC_TOOLS`, nor `BYPASS_TOOL_CONSENT`, nor a YOLO window changes
it.

### 7. Breaker

Unchanged from ADR 0008 §7 (hold at WARNING / COHERENCE, drop and cancel at CRITICAL; pause,
cancel and stop always work), plus: at CRITICAL a run in `awaiting_approval` is cancelled and its
approval request with it (ADR 0024 §3); the per-call read of §5. Schedule changes that widen
what runs stay refused at WARNING and above. ADR 0008 Open (a) is answered by ADR 0024 Q1 (freeze
pending approvals), not by cancelling them on the transition.

### 8. Connection with the heartbeat (#238)

* The heartbeat stays the zero-model tick; the scheduler only **reads** its status (already
  wired: `health_source=heartbeat_status`).
* **Model-calling runs need a healthy heartbeat** (`requires_heartbeat`, default `true`): enabled,
  running, not degraded, last tick within `max(3 × interval, 120 s)` — the same test that
  raises `heartbeat_stalled`. Otherwise the fire is recorded `run_skipped(heartbeat)` and a
  notification is raised once. Reason (ADR 0018 §2a): the breaker level the scheduler reads must
  reflect applied decay, and an unattended model call should not run while the component that
  watches the process is down. Gateway jobs ignore it (`health_check` must run precisely then).
* The scheduler and the heartbeat keep separate owner locks under the one `server.lock`.
* **ADR 0011 W6d note (implemented):** the scheduler also evaluates the fire kind
  `grant_tick` (a goal grant's heartbeat start, not a job: no job definition, nothing in the
  log, attached only with `autonomy.heartbeat_start`) after the jobs of each of its own ticks,
  outside its lock, on its own thread. It uses this section's heartbeat health test (fresh;
  no heartbeat wired = not healthy), never catches up (one wake, at most one iteration), and
  an event-driven wake after each heartbeat tick only marks the scheduler's next tick — the
  heartbeat still never dispatches, imports an executor or builds a model.

### 9. Restart and shutdown

* **Model-calling jobs** (Agent, and Memory's model kinds) come up `paused(start)` after every
  start and a person resumes them — ADR 0018 Q4 extended to the Agent layer, the restart
  contract the operator accepted for ADR 0011 §6 (2026-10-16).
* Gateway jobs keep ADR 0008's normal start until ADR 0011 §6 ("every job paused at every
  start") is implemented (Q7).
* An interrupted run is closed `run_cancelled(interrupted)` and **never resumed from a checkpoint
  after a restart** (#95 was listed for this in #239; decided here, not deferred): a resumed run
  would continue unattended model work no human resumed. Job approval cards are cancelled at
  start (ADR 0024 §4). The next fire follows the missed-run rule.
* Graceful shutdown (`SIGTERM`, #285) cancels Agent runs at the next model or tool call boundary
  (`run_cancelled(shutdown)`), as today.

### 10. Health notifications

Unchanged codes and rules (transition only, `resolved` on clear, events in the log): add
`approval_waiting` (a run waiting for a human, with the request's expiry), `heartbeat_required`
(a model-calling fire skipped by §8) and `jobs_paused_at_start` (one per start, the count).
Delivery stays the dashboard and the server log. A chat channel (ADR 0011 direction of
2026-10-16: host-side handler, allow-listed operator, operation token) may later deliver
notifications and accept *acknowledge* and *resume a `paused(start)` job*; that is its own
design. De-duplication uses the persistent event id, not subject + incarnation (ADR 0011 review
item 3).

### 11. Who defines jobs

Only a human, through the authenticated API with `confirm: true` (ADR 0008 §10). No tool, no
route reachable from a turn creates, updates or resumes a job; scheduling tools are not
registered. Agent-proposed or envelope-bounded jobs are ADR 0011 stages A / B (#297 / #298) and
reuse this base when they are built.

### 12. Phases

| Phase | Content | Size (estimate) |
|---|---|---|
| S0 | ADR 0008 + Memory layer phase 0 | shipped (PR #265, ADR 0018 phase 0) |
| **S1** | §2 schema v2 parser (fields refused until their phase), `catch_up` refused for model-calling jobs, §8 heartbeat requirement, §10 notifications, Agent pool budget field | small |
| **S2** | §5 `GatedJobExecutor`, the chat server opt-in switch (default off), §9 `paused(start)` for Agent jobs, `/schedules` shows the Agent layer | large |
| **S3** | ADR 0024 phase 2 (`wait`, continuation segment) | medium, after S2 and #276 |

Each phase needs its own go. S1 only refuses or records more.

## Alternatives considered

* **A second scheduler for Agent jobs (side by side).** Two budget machineries and two logs to
  reconcile; rejected (§1).
* **Agent jobs on the plain `Agent` + `JobToolGuard` (today's executor).** It cannot apply the
  tool rules, the floors or the ledger, and the breaker is only read before the body; enough for
  "read-only or stop", not for Ask rules or a later wait. Rejected for the chat server.
* **Run jobs through the main `NXPAgent`.** Would mix job prompts into the conversation, its
  Neural State and its checkpoints. Rejected; jobs get their own gated agent.
* **Resume interrupted jobs from checkpoints after a restart.** Contradicts the restart
  contract; rejected (§9).
* **APScheduler / OS cron.** Rejected in ADR 0008; unchanged.

## Out of scope

Self-created or agent-proposed jobs (ADR 0011), write / exec / outbound tools in jobs, external
notification delivery, the `idle` trigger (ADR 0018), log rotation and the EventStore migration
(ADR 0009 / #277), anything enabled by default.

## Decisions (commander, 2026-10-21)

The commander accepted every recommendation of this ADR. The sections the questions refer to
are the decision text.

| # | Question | Resolution |
|---|---|---|
| Q1 | Memory and Agent jobs | **As recommended:** one job base, separate executors per layer (§1). |
| Q2 | Heartbeat as a start condition | **As recommended:** required for model-calling runs (`requires_heartbeat: true` by default, settable per job) (§8). |
| Q3 | Agent pool | **As recommended:** a separate `agent_daily_token_budget`, 50,000 tokens per UTC day by default, required when the layer is on. |
| Q4 | `catch_up` for model-calling jobs | **As recommended:** refused (only `skip` / `latest`). |
| Q5 | Agent-layer switch on the chat server | **As recommended:** a `gateway-config.json` key shown on the settings page, default off; `STIRA_SCHEDULER=0` still turns the whole scheduler off. |
| Q6 | Tools for Agent jobs | **As recommended:** `JOB_READ_ONLY_TOOLS` only (no web, MCP, `delegate`, `page_check`, `run_checks`). |
| Q7 | Gateway jobs after a restart | **As recommended:** ADR 0008's normal start until ADR 0011 §6 is implemented. |
| Q8 | Interrupted job runs after a restart | **As recommended:** closed, never resumed from a checkpoint (§9). |

**Scope of this acceptance.** S1 (§2 schema v2 parser with fields refused until their phase,
`catch_up` refused for model-calling jobs, §8 heartbeat requirement, §10 notifications, Agent
pool budget field) is scheduled now; it only refuses or records more. S2 and S3 each start only
after a separate go, in that order (S3 also after #276).

## Questions (operator decision)

All eight are resolved as recommended; see **Decisions (commander, 2026-10-21)** above. The
original text is kept for the record.

1. **Memory and Agent jobs: integrated or side by side?** **One base, separate executors** —
   **recommended** (§1); or a separate scheduler for Agent jobs.
2. **Heartbeat as a start condition for model-calling runs.** **Required (default
   `requires_heartbeat: true`, settable per job)** — **recommended** (§8); or not a condition.
3. **Agent pool.** **Separate `agent_daily_token_budget`, 50,000 tokens per UTC day by default,
   required when the layer is on** — **recommended**; or one pool shared with memory jobs.
4. **`catch_up` for model-calling jobs.** **Refused** (only `skip` / `latest`) — **recommended**;
   or allowed with `max_catch_up`.
5. **Agent-layer switch on the chat server.** **A `gateway-config.json` key shown on the settings page,
   default off; `STIRA_SCHEDULER=0` still turns the whole scheduler off** — **recommended**; or an
   environment variable only.
6. **Tools for Agent jobs.** **`JOB_READ_ONLY_TOOLS` only** (no web, MCP, `delegate`,
   `page_check`, `run_checks`) — **recommended**; or add read-only web search with its own
   decision.
7. **Gateway jobs after a restart.** **Keep ADR 0008's normal start until ADR 0011 §6 is
   implemented** — **recommended**; or pause every job at every start now.
8. **Interrupted job runs after a restart.** **Closed, never resumed from a checkpoint** —
   **recommended** (§9); or resume from the job thread's checkpoint after a human resumes the job.
