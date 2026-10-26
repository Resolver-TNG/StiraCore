# ADR 0011: Bounded agent self-scheduling and grant-scoped autonomous goal loops

**Path:** `docs/adr/0011-agent-self-scheduling.md`
**Status:** Proposed (revision 5, after design review rounds 1–4; needs an operator
decision before any implementation; open points are collected at the end). Operator
decisions of 2026-10-16 are recorded in "Operator decisions" at the end; they do not
change the text above.
**Implementation status (2026-10-26):** implemented through W6d (alpha), see
[docs/autonomy.md](../autonomy.md) (W1 + W2 + W4 + W5 + W5b + W6a–W6d of #296). Stages A and B
are not built; stage C exists as an **alpha, off by default**:
one iteration per person's press (W5) or, opt-in twice, at most one per scheduler wake
started by the model-free `grant_tick` (W6d), with progress by outcome (W6a), pre-emption by
every turn holder (W6b) and claim evidence (W6c); the person verifies every completion.
The **Status** above stays Proposed (decision 2026-10-26: kept Proposed until the operator
accepts the ADR; nothing above records that acceptance). Part
of the common foundation (§13 item 2) exists and is **shared**, see
[authority-service](../authority-service.md). Component map:
* *shared, process-wide* (`src/stiracore/authority/`): one incarnation (also the
  heartbeat's `INCARNATION` and the YOLO desk's), permits with single-use `consume` (§5,
  incl. optional policy version / reservation id / TTL pair; the W5 executor binds the TTL pair and the reservation id), the authority
  epoch persisted in `authority-epoch` (§5: `0600`, fsync, seq + previous hash; stored +
  1 at start, a missing file is a first start; epoch values are unique only together
  with the incarnation; the chain detects corruption, not same-user tampering;
  unreadable / mismatch / replaced → `unhealthy`, every `consume` refused), the lock-free
  kill flag read first by `consume`, and the in-flight set with cancel callbacks. The
  epoch moves on process start, a tool-rules save, a breaker change away from NORMAL or
  between non-NORMAL levels (also through a checkpoint restore), the kill switch and
  YOLO's own narrowings — with or without YOLO; a YOLO issuance is fenced by any move of
  the shared epoch (ADR 0012 §6b row 7). W2: the latch store `authority-latches` (§5, §6
  item 4: `kill`, `unhealthy`, `halted:<reason>` with fixed reason codes; stop id
  `(incarnation, epoch)`; unreadable = every latch set, and so is a store that is missing,
  older or different from the commitment the epoch store carries or that lacks a latch the
  lifecycle log shows as set; repaired only by `stira authority reset-latches` (every latch
  set); cleared only by the host command `stira authority unlatch` with the server stopped, an operation bound to incarnation,
  subject, stop id, expected generation and op id — stale → `resume_stale`, same op id →
  the original result); the kill switch route `POST /api/authority/kill` (the kill latch
  survives a restart); the §12 `lifecycle_event` contract (codes and ids only) in a
  hash-chained log with an outbox (dedupe subject + incarnation + epoch + to, unread / ack,
  rebuilt at start and on every pass, `stopped (reason_not_recorded)` for a latch without
  a record; a failing write latches `unhealthy`), fed by the kill switch, breaker changes
  and YOLO's pause / end; the model-free watchdog (heartbeat staleness, its last pass and
  `gateway_down`, outbox repair, abandoning a cancel callback past 5 s);
* *grant foundation, execution off* (W4, `src/stiracore/grants/`, behind gateway
  `autonomy.stage_c`, default off; [grants](../grants.md)): the grant + goal record (§2 /
  §8 fields; v1 caps TTL default 2 h / cap 6 h, `max_iterations` 10 / 20, verifier `user`
  only; `run_checks` in the scope only with `autonomy.run_checks_unattended` **and** a
  verified `os_sandbox`), the §8 / §11 goal × grant state machine as pure functions, the
  `grants.json` store (`0600`, chained; the goal text only there; never persisted as
  active, so every start revokes: `revoked(restart)`), the human control plane (issue:
  typed confirmation + a host operation of `yolo/host_ops.py`; revoke / pause / resume /
  verify; at most one live grant), automatic stops in the watchdog's pass (kill → revoked,
  unhealthy → revoked, breaker → halted, clock anomaly → revoked, TTL → expired, `/new` →
  `revoked(session_reset)`) with an epoch advance and lifecycle events (subjects `grant` /
  `goal`), the settings-page panel. Deviations from the text above: emotional stops are the
  breaker only (operator decision 2026-10-24); no `halted:*` latch is set (a grant never
  survives a restart); TTL default / cap 2 h / 6 h instead of 12 h / 36 h;
* *goal iteration executor* (W5, `src/stiracore/grants/executor.py` +
  `src/stiracore/agent/grant_child.py`, behind gateway `autonomy.stage_c_exec`, default off,
  needs `stage_c`; [grants](../grants.md)): a person's press (`POST /api/grants/<id>/step`)
  runs **one** iteration in the session `grant:<id>` (a fresh Neural State, the server's
  rules / floors / gate, write roots narrowed to the subtree, checkpoints in memory). §5:
  every model call, tool call, approval wait and report takes a permit (TTL pair; a model
  call also its reservation id) consumed right before the I/O after a fresh look; a stop in
  between → `permit_stale`; the in-flight set cancels a running effect and withdraws a card.
  §5 hard budget: a local upper bound of the final input (one token per UTF-8 byte; no
  provider request to count), `bound + max_tokens` is reserved
  against the grant total / per-tick / shared pools, settled with the reported usage; no
  count → no call. §8: tools = scope ∩ the v1 allow-list ∩ the live rule; an Ask is a
  person's card (unanswered → `blocked(approval_timeout)`); the agent reports codes only
  (`goal_report`; a claim → `awaiting_verification`); the trusted playbook and the goal are
  the trailing context block (#254); every turn-lock holder pre-empts at the next boundary
  (`yielded(user_turn)`; W6b: one `TurnLock` raises the signal before the lock for the chat
  turn, the webhook, the automatic continue, a restored card's resume, `/new` and a
  procedure approval); a wall time per iteration → `blocked(iteration_timeout)`.
  Deviations: the trigger is a person's press, or (W6d, opt-in twice) the scheduler's
  model-free `grant_tick` through the same `step` — at most one iteration per wake; the
  iteration runs in a dedicated session, not the user's (operator decision 2026-10-24);
  no YOLO coverage (W7); no episodic write-back (W8); emotional stops = the breaker only;
* *YOLO only* (ADR 0012): the window, its latch / marker, its watchdog, host operation
  tokens;
* *scheduler / heartbeat*: unchanged (own owner locks, ADR 0008 start; they do not
  consume permits yet);
* *missing*: §6 unconditional start, stop-id-bound tokens for jobs, verification beyond the
  user, the identity check, taint persistence (W8), YOLO coverage (W7), a shared day budget
  pool, a real-model cost measurement (W6e), notification grouping per goal in `/schedules`
  (§12). (The heartbeat start is W6d and no-progress / loop detection is W6a, both above /
  below.)
**Issue:** #296 (tracking; stages A #297, B #298, C #299). Related: #105 (autonomous
survival base), #238 (heartbeat daemon), #239 / ADR 0008 (scheduler), #240 / ADR 0006
(durable approvals), #171 / ADR 0009 (EventStore), #190 / ADR 0010 (gate stack), #249
(agent steering proposals), #252 (memory self-maintenance as a job kind), #254 (prompt
cache prefix stability), #275 (stop-reason contract), #276 (single pre-execution
boundary), #277 (audit contract), #285 (owner lock), #292 / #293 / #294 (memory bugs and
retention), #143 (ablation harness).
**Decision (proposed):** three independently opt-in, default-off stages — **A** the agent
may only *propose* schedules and heartbeat changes; **B** agent-created read-only jobs
inside a human-defined *envelope* (its relation to grants is **Open (12)**); **C** while a
human-issued, time-boxed, budgeted and scoped *grant* is active, a heartbeat-driven goal
loop may act on its own. Every effect is authorized by one function against one
authority version (§2, §5). Nothing in this ADR changes a default.

## Where the line is

This ADR authorizes, as a design, **one** form of autonomous operation:

* **In scope:** an autonomous goal loop that runs **only while an explicit user grant is
  active** — with an expiry, a budget and a scope — driven by the heartbeat tick, and
  stopped by the breaker and by deterioration of the emotional state (§9).
* **In scope:** agent-proposed schedules (stage A) and agent-created read-only jobs inside
  a human-defined envelope (stage B, subject to Open (12)).
* **Not in scope, and not proposed:** autonomy without a grant; always-on event-driven
  operation (webhooks, file watchers, inbound messages that start work); the agent
  widening its own permissions, tools or budget; the agent changing the envelope, a
  grant, the heartbeat floor or the breaker thresholds; write / exec / outbound tools in
  agent-created jobs; automatic resume after a breaker or emotional stop; enabling any of
  this by default.

ADR 0008 §Constraints ("Stira never creates, widens or resumes a job by itself") stays
true for every stage that is off.

## Context

StiraCore aims at **emotional memory built from dialogue with the user, and autonomous
operation that grows out of that memory.** The pieces exist separately: a model-free
heartbeat tick (#238, default off), a scheduler for human-defined jobs with budgets,
breaker holds and a hash-chained log (#239 / ADR 0008, default off), durable approvals
(ADR 0006), a single gate stack (ADR 0010) and an EventStore chain (ADR 0009). What is
missing is a way for the agent itself to say "check this again tomorrow", "summarize
memory at night" or "keep working on this goal for the next hours" — and a way for the
user to *see* what the agent scheduled, what it costs and why it stopped.

Today the heartbeat only decays state and ticks the breaker; automatic memory
maintenance is not implemented (#252), and the memory pipeline has open correctness bugs
(#292, #293) and no retention policy (#294).

Every security review of the previous waves found the same classes of hole: revocation
that fails open, recovery / rollback that *increases* permissions, missing generation
binding, relaxation checks duplicated per path, TOCTOU between check and use, and secrets
stored in clear. This ADR states the controls for those classes as **contracts** (§2–§7)
that every path must satisfy.

## Decisions

### 1. Stages (each independently opt-in, all default off)

| Stage | Agent may | Human must | Default |
|---|---|---|---|
| **A — propose** | `schedule_propose`, `heartbeat_propose_change`; read status | approve / reject / edit each proposal in the dashboard; an approved job is **human-owned** | off |
| **B — envelope** | create / pause / cancel jobs **it owns** (§2) that fit the envelope | define the envelope; Open (12) decides whether a grant is also required | off |
| **C — grant loop** | pursue one goal on heartbeat ticks inside the grant | issue the grant (goal, scope, budget, TTL, verification); explicitly resume after every stop of §9 | off |

* Stages are separate switches. With all stages off no new tool is registered and no new
  route accepts agent-originated input. The proposal inbox and `heartbeat_propose_change`
  exist whenever **any** stage is on.
* **Heartbeat changes are always proposals**, in every stage. The heartbeat floor (minimum
  interval; "the agent cannot disable the heartbeat") is changed by a human only.
* **Rejected: no envelope** (agent schedules anything the tools allow).

**Open (12) — relation of stage B to grants.**

* **(i) B by the envelope alone** (human-defined; `health_check` / `memory_review`;
  read-only; job TTL 7 days). Calling it "registration inside a human frame" does not
  change its nature: an agent decision leads to **unattended runs on later days without
  an active grant**. Stops and budgets must still be the common ones of §5 / §9, not a
  copy.
* **(ii) B only under an active grant.** Jobs are grant-owned (§2) and inherit the
  grant's TTL, budget, stops and revocation; when the grant ends, its jobs end. Shares
  one stop and budget mechanism with C. Long-lived agent-suggested jobs go through A.
  Needs the grant foundation before B.
* **(iii) No stage B; A → C only.** The simplest.

**Recommendation: (ii)** (no autonomy without a grant, one stop / budget mechanism). Not
decided here.

### 2. Authority types, intersection and ownership

Every subject that can cause an effect has exactly one **authority type**. One function,
`authorize(subject, effect, permit)` (in the #276 boundary), handles all types; only the
source of authority differs:

| Type | Source of authority | May cause effects | Bound to |
|---|---|---|---|
| `proposal` | none | **none** — stored, shown, queued for approval only | proposer turn, definition hash |
| `human_job` | a human approval (dashboard) | its approved definition ∩ tool policy ∩ `JOB_READ_ONLY_TOOLS` | `(approval_id, definition hash)` |
| `grant_job` / `grant_iteration` | an **active** grant | grant scope ∩ envelope (for jobs) ∩ tool policy ∩ `JOB_READ_ONLY_TOOLS` (jobs) ∩ individual approval | `(grant_id, generation)` + permit (§5) |
| `grant_verification` | an **active** grant whose goal is `awaiting_verification` | only the grant's verifier check (deterministic, read-only, no model call) | `(grant_id, generation, verifier id)` + permit (§5) |
| `envelope_job` | the envelope | only if Open (12)(i) is chosen; envelope ∩ tool policy ∩ `JOB_READ_ONLY_TOOLS` | `(envelope_version, definition hash)` |

* "Every effect needs an active grant" applies to the grant types only. Creating a
  `proposal` is a storage action inside a user-attended chat turn, authorized by the tool
  policy for that turn; a proposal never runs anything. This is how A works without a
  grant.
* An individual approval (ADR 0006) only narrows to one call; it **never widens** the
  grant or envelope scope. An approval for a tool outside the scope is refused.
* A **grant** carries `grant_id` (never reused), `generation` (monotonic, incremented on
  every narrowing edit), `goal` (immutable — another goal is a new grant), `scope`,
  `principal` (authenticated dashboard user), `session_id`, `issued_at` (wall clock +
  monotonic), `ttl`, `budget`, `verification` (§8).
* **Ownership is the type and binding, not `origin.kind`.** A job proposed by the agent
  and approved by a human is a `human_job` (`proposed_by = agent`). The agent may pause or
  cancel only `grant_job`s of its current grant and generation — or, only under Open
  (12)(i), `envelope_job`s created under the current envelope version; it never resumes,
  edits or widens any job, and never touches `human_job`s.
* **One loosening function.** Every path that could increase what runs — create, approve,
  edit, resume, recover, retry, envelope change, grant change — calls
  `rules_loosening(before, after, context)` in the #276 boundary, which decides whether
  the change loosens anything and who may perform it. No path has its own copy.

### 3. The envelope (stage B)

A human-only object, versioned (`envelope_version`), stored in the scheduler's chained log:

| Field | Meaning | Proposed default |
|---|---|---|
| `allowed_kinds` | job kinds the agent may create (`health_check`, `memory_review`, later `memory_maintenance` from #252) | `health_check` only |
| `allowed_tools` | subset of ADR 0008 `JOB_READ_ONLY_TOOLS`; never wider | `current_time`, `memory_read`, `memory_search` |
| `min_interval_sec` | floor between fires of one job, enforced on computed next fires | 3600 |
| `max_jobs` | agent-owned jobs alive at once | 3 |
| token budgets | per run / per job per day / agent total per day, ADR 0008 §6 reservations | **Open (2)** |
| `job_ttl_sec` | lifetime of an agent job (under (ii): min(this, grant remaining)) | 7 days |
| `breaker_levels` | levels at which agent jobs may start | `NORMAL` only |

* Edited only in the dashboard with typed confirmation (RECOVER pattern, Origin / CSRF,
  ADR 0008 §10). No tool, turn-reachable route or environment variable changes it.
* Lowering the envelope advances the authority epoch (§5) and pauses jobs that no longer
  fit (`system`). Raising it resumes nothing.
* Anything that does not fit becomes a proposal (A on) or is refused with the limit hit.

### 4. Agent-facing tools

| Tool | Registered when | Effect |
|---|---|---|
| `schedule_list`, `heartbeat_status` | any stage on | read-only status, budgets, envelope summary |
| `schedule_propose`, `heartbeat_propose_change` | any stage on | creates a `proposal`; nothing runs |
| `schedule_create` | B on | `grant_job` (or `envelope_job` under (i)) if `authorize` + envelope pass, else proposal / refusal |
| `schedule_pause`, `schedule_cancel` | B on | only on `grant_job`s of the current grant and generation (under (i): `envelope_job`s of the current envelope version) |

* There is no agent `schedule_resume`: resuming is loosening.
* **Cache stability (#254).** With every stage off the tools are not registered (tool list
  hash unchanged). When on, they are one static block with fixed definitions and fixed
  position; descriptions contain no volatile values. `scripts/cache_probe.py` hash tests
  cover each stage combination.
* **No free text in arguments**: kind (enum), cron / interval, tools (enum subset),
  `reason_code` (enum). Items are displayed by a **fixed label** `<kind>#<short id>`
  (e.g. `memory_review#7f3a`); there is no title or description field (§7).
* Every mutating call carries an **idempotency key** (turn id + tool call id); a replay
  returns the earlier result.
* **Provenance**: type and binding (§2), session, turn, and the taint label (§10).

### 4a. Proposal inbox (stage A)

* States: `pending` → `approved` (a `human_job` is created, `proposed_by = agent`) /
  `rejected` / `edited_and_approved` / `expired`.
* Proposals expire after 72 h (Open (2)) and never become jobs by themselves. Approving
  is a dashboard route with Origin / CSRF checks and goes through `rules_loosening`; a
  turn cannot approve.
* An approved job is pinned to the definition hash the human saw; an edit creates a new
  definition and hash; a job runs only the definition it was approved with.
* At most `max_pending_proposals` (default 10) are pending; more are refused with the
  reason. Proposals are not free (tokens, storage, attention), so this is part of the
  flood bound.

### 5. Authority epoch and the dispatch contract

**One authority version.** `authority_epoch` is a single monotonic counter that advances
**whenever any authority goes down or stops**: grant narrowed, revoked or its TTL reached;
envelope lowered; any tool-policy change (ADR 0010; every change counts, without judging
direction); any breaker level change away from `NORMAL` and any change between non-NORMAL
levels; a §9 halt (emotional, breaker, identity); kill switch; any latch set; stage
switched off; process start; recovery. Raising authority never advances it by itself
(widening is a separate, human-only `rules_loosening` path and always mints new
bindings).

The epoch lives in a separate `authority-epoch` store (one small file, `0600`, fsync on
every advance, sequence number + hash of the previous value) and in memory as an atomic
value.

**Incarnation.** Every process start generates a fresh, unpredictable `incarnation` (128
bits from a CSPRNG). It is **never read back from storage**: if it is written anywhere
(e.g. into events, for display), a start always generates a new one. The authority epoch
is monotonic only *within* one incarnation — a full data-directory rollback can roll it
back and reproduce old epoch values — so identity across restarts always uses
`(incarnation, epoch)`, never the epoch alone.

**Permit.** `authorize` returns a permit for exactly **one** effect, stamped with
`(incarnation, authority_epoch, grant generation, envelope_version, policy version,
budget reservation)`; `consume` also requires the permit's incarnation to equal the
current one. A permit is never reused, cached across effects or carried across a
retry, an approval wait or a resume.

**Serialization points (fixed).**

* One **authority lock**, a leaf lock: held only for in-memory compare-and-set, never
  across I/O, a model call or another lock acquisition. Lock order where both are needed:
  scheduler lock → authority lock.
* **Consume.** Immediately before an effect is dispatched, `consume(permit)` runs under
  the authority lock: check permit epoch == current epoch, kill-switch flag clear, TTL not
  reached on either clock (§5 clock rule, checked directly, not only via the watchdog),
  then mark the permit consumed and register the effect as **in flight**. The effect is
  dispatched only after `consume` returned success.
* **"Running" starts right after a successful `consume`.** A stop or narrowing linearized
  before `consume` refuses the effect; one linearized after it treats the effect as in
  flight: cancelled where possible, otherwise recorded as completed-after-stop.
* **Revocation / narrowing** runs under the authority lock: advance the epoch, take the
  in-flight set, release the lock, then cancel in-flight work (`Agent.cancel()` for model
  calls). Persistence of the epoch follows; if it fails, the stage is latched `unhealthy`
  in memory (never "not revoked").
* **Kill switch** sets a lock-free atomic flag first (read by `consume`), then advances the
  epoch through the authority lock. Since the lock is a leaf held only for compare-and-set,
  the kill switch cannot be delayed by a running effect.

**Every side-effect boundary** gets its own permit: each model call, each tool call of a
multi-tool batch, each job fire, the replay of an approval that arrived while waiting,
each retry and each continuation after a resume.

**Not undone** after an effect was consumed: tokens spent, read-only tool calls made,
notifications raised, episodic entries written (tagged, can be quarantined). A tool
already executing is not pre-empted; the bound is the tool's timeout (the scope sets a
maximum). Approved wider tools (ADR 0006) are not rolled back.

**Clock.** TTL expires at the earlier of wall-clock expiry and monotonic elapsed time
since issuance (always available: grants never survive a process start). A wall clock
moving backwards beyond a small tolerance, or a disagreement beyond tolerance, revokes
with `clock_anomaly`. A clock never extends a TTL.

**Unknown state stops.** If the log, the epoch store, the latch store or the budget state
cannot be read or verified, the stage is `unhealthy`: no agent effect runs and no job
fires until a human repairs it.

**Hard budget.** Each model call's permit reserves `input tokens (counted before the call)
+ max_output_tokens` and the call is made with that `max_tokens`; if the reservation does
not fit the remaining budget, `authorize` refuses. Only provider accounting variance
remains, and it is recorded.

### 6. Start-up and recovery: unconditional conservative contract

Holes this closes: revoke in memory → record fails → crash → start from an older valid
log loses the revocation; a hash chain does not detect a rollback to an older **valid**
snapshot — of the log or of the whole data directory. Any marker stored in the data
directory ("this installation used stage A", "agent items exist") can be rolled back as
well, so the contract does **not** depend on one.

**Once this ADR is implemented, every scheduler start** — with all stages off too, and
for `human_job`s defined before this ADR — does:

1. advance the authority epoch; revoke every grant (`revoked(restart)`) — grants are never
   revived, continuing needs a **new** grant with a new id;
2. start **every job paused (`system`)**; only an explicit human resume (§9 operation
   tokens) makes a job run again;
3. treat the **current day's budgets of every job and the agent total as fully used**;
   replayed usage is shown, but restored accounting is never a reason for more remaining
   budget. A human may release the day budget explicitly (a token-bound operation);
4. keep every stop latch (kill switch, §9 halts, `unhealthy`); an unreadable latch store
   counts as all latches set;
5. emit one lifecycle event `paused(start)` per job (§12).

**This changes ADR 0008's normal start** (which restarts active jobs and replays budgets;
it pauses all jobs only on a detected recovery). Reason: a rollback to a state from
before any agent item, or from before a human paused a job, would otherwise let a job run
without a human decision. **Compatibility impact:** after every restart, also on
installations that only use human-defined `health_check` jobs, the operator must resume
jobs in the dashboard (a "resume selected" action is offered, each job still confirmed
by its own token); fires that fall into the paused period follow ADR 0008's missed-run
rule; nothing runs model-calling work on the day of the restart unless a human releases
the budget. ADR 0008 will carry an amendment note pointing here.

**Residual risk, separate from the above (Open (6)).** A human who resumes jobs and
releases budgets after each of repeated rollbacks can spend more than one day's budget in
total; "one day" is not a cumulative bound across repeated rollbacks. Closing that needs
an anchor outside the data directory.

### 7. Secrets and sensitive data

**Accepted threat model.** Pattern detection cannot recognise unknown secrets. This ADR
keeps free text out of every store except two, and **accepts** that those two hold plain
text: the **goal** (grant store) and **episodic memory** (memory store), both `0600`
files inside the data directory. They are protected against other local users and
against leakage into logs, notifications and audit; they are **not** protected against
anyone who can read the user's account or a copy of the data directory, and an unknown
secret the user typed stays there until its retention ends. Encryption at rest is not
part of this ADR.

* **Structured only** elsewhere: proposals, jobs, envelope, notifications, lifecycle events,
  audit and log events hold enums, ids, numbers, hashes and the fixed label
  `<kind>#<short id>` — no title, no description, no reason text.
* **Tool arguments** are never stored; only the tool name and a keyed hash (HMAC with a
  local key), also for refused calls. Exceptions are recorded by type and reason code,
  not message text.
* **Goal save protection:** the grant dialog warns not to include secrets; the goal passes
  through `mask_secrets` and **the save is refused if masking changes it**; it is stored
  only in the grant store, referenced elsewhere by `grant_id`, deleted a retention period
  after the grant ends (#294; Open (2)).
* **Goal in use vs. at rest.** Every tick the goal is *sent* to the model and *used in
  memory* (§8); that is allowed. It must not be *persisted* anywhere else. To verify in
  implementation: the re-injected block is temporary (the #254 injector does not write it
  into conversation history), it is excluded from model-call traces / trace exports, and
  it does not reach error collection through exception messages.
* **Copies of the goal in model output.** Model output, summaries and episodic entries
  may quote the goal. Such copies are allowed **only** where memory writes are allowed
  anyway (episodic, through staging #293, retention #294) and inherit those rules; they
  are never copied into logs, notifications, audit or lifecycle events. This is an
  **explicit risk acceptance**, not a fix: the plain text then exists in two stores.
* **Episodic memory written by the loop** follows the memory pipeline's rules (staging
  #293, retention #294) and is never copied into logs, notifications or audit.
* `mask_secrets` is defence in depth for the two free-text stores, not a guarantee.

### 8. Goal loop (stage C), alignment with OpenClaw goals

**Goal vs. grant.** As in OpenClaw, a *goal* is a durable objective, neither a permission
nor a background task. Stira keeps two objects: the **goal** (objective, status,
counters) and the **grant** (execution eligibility, §2). A tick advances the loop only if
goal `active` ∧ grant `active` ∧ no latch.

* **Heartbeat start (W6d, implemented; off by default).** The tick is the scheduler's fire
  kind `grant_tick` (ADR 0021): model-free admission on every scheduler wake (periodic, and
  event-driven after a heartbeat tick), then the **same** executor `step` as a person's
  press, with its clauses checked again on `step`'s own look. Only with the gateway switch
  `autonomy.heartbeat_start` **and** a grant issued with its own `heartbeat_start` flag
  (host-confirmed at issue, fixed afterwards). At most one iteration per wake; a busy or
  ineligible wake is recorded `tick_skipped(<reason>)` and waits for the next one — no queue,
  no catch-up (§11's `tick_skipped(overlap|user_turn)` are `iteration_running` / `turn_busy`
  there). Added clauses: a healthy heartbeat, `tick_min_spacing_sec` since the issue and the last end, a
  flood guard on event-driven wakes, and the no-action gate (two `iteration_no_action` ends
  in a row stop the ticks until a person presses or resumes). With `verifier = user` a goal
  `awaiting_verification` is a skip — the verification path above runs nothing. An
  unattended iteration never waits for an approval (`blocked(approval_timeout)` at once).
  The check and the act are one fenced step (fix rounds 1–2): the wake's stamp — authority
  epoch, person's-turn counter, grant stop epoch, scheduler run token, executor identity;
  single use — is validated inside the publication's `commit_at_epoch`, read whole again
  right before the first permit, the first consume (a permit of the wake's epoch), the first
  provider request and the first terminal request, and its memory marks at every later
  boundary: every change readable at those points refuses before a request or a write.
  **Accepted residual:** the heartbeat is a sampled external observation — a loss after the
  last read before a request is not seen by it, and after the first request a heartbeat
  loss is no stop (a start condition, ADR 0021 §8); a scheduler stop in that final window
  likewise (a real shutdown stops the executor first).
  Not done: a day pool, the YOLO two-binding, a model-based judge. Details:
  [grants](../grants.md) "Heartbeat start".

* **Verification path (separate admission rule).** A tick with goal
  `awaiting_verification` ∧ grant `active` ∧ no latch may run **only the verifier**: a
  permit of type `grant_verification` whose scope is the verifier check id (deterministic,
  read-only, no model call). At most one attempt per tick, 3 attempts in total before
  `blocked(verification)`. With `verifier = user` nothing runs; the dashboard waits for
  the user while the TTL keeps running.
* **Goal not `active` stops the whole grant's work.** When the goal is `blocked(*)`,
  `budget_limited` or `paused`, the grant moves to `paused(goal_<status>)`: no iteration
  and **no `grant_job` of that grant fires** (`run_skipped(goal_inactive)`); TTL keeps
  running. A user resume (§9 operation token) reactivates goal and grant together.

**Grant fields** (all required): `goal`, `scope` (read-only tools by default; wider only
through ADR 0006 approvals and never beyond the scope), `token_budget` (total and per
tick), `ttl` (default **12 h**, hard cap **36 h**), `max_iterations`, `no_progress_ticks`
(default 3), `identity_check_interval` (Open (4)), `verification` = *target*, *verifier*
(a deterministic read-only check id, or `user`) and *evidence* (check result hash, run
id). A grant without verification cannot be issued.

**Time limits.** Default TTL 12 h, hard cap 36 h, because the quality of a long-running
agent degrades with continuous operation. No automatic extension; extending means a new
grant.

**Exclusivity.** One goal per session, one active grant per session, one in-flight
iteration per session (session lock) and single flight per grant. A user chat turn in the
same session has priority: a tick that finds a user turn running is skipped
(`tick_skipped(user_turn)`); a user turn arriving during an iteration makes the iteration
yield at its next effect boundary (`yielded(user_turn)`).

**Goal statuses** use OpenClaw's names (`active`, `paused`, `blocked`, `budget_limited`,
`complete`) plus `awaiting_verification` and `unmet` (shown when the grant ended before
completion). Rules:

* `complete` only when the verifier passed while the grant was `active`; the evidence is
  stored. An agent claim moves the goal to `awaiting_verification`.
* From `awaiting_verification`: verifier fails → `active` (counts as a no-progress tick);
  inconclusive 3 times → `blocked(verification)`; grant ends meanwhile → `unmet` with the
  grant's reason; `verifier = user` → the user confirms or rejects in the dashboard.
* **Claim evidence (W6c, implemented).** When an iteration ends with the agent's claim, the
  harness keeps **one** evidence record on the grant (in the grant store, replaced by the
  next claim, kept after a confirmation as the audit trail): the iteration k / max, the
  `run_checks` verdict, fail closed (`pass` / `fail` / `not_run` / `unknown`: every call since
  the last write combined — any fail is `fail`, anything unreadable or inconsistent `unknown`;
  `pass` / `fail` only for the workspace identity the checks ran on, else `not_run` /
  `unknown`) with its per-check rows (kind, status, counts), the workspace `changed` / `unchanged` /
  `unknown` since the previous claim (the W6a identity; no digest or path), the claiming
  iteration's progress code, tokens used / budget left, TTL left and the claim instant.
  Codes measured by the harness only — no model text, no goal (OpenClaw `announce.md`: the
  status comes from the runtime outcome). It informs the person and decides nothing: no
  automatic reject or confirm; the verify route is unchanged (docs/grants.md "Completion
  claims").
* **Grant terminal states are final.** A verification result that arrives after the grant
  ended is recorded as `late_verification(pass|fail)` on the goal; it never turns
  `revoked` / `expired` into `complete`. The user may then mark the goal complete
  explicitly (the goal only; the grant stays as it ended).
* **Actual vs. displayed state.** Goal and grant each keep their *actual* state; the
  display is derived and never written back. The dashboard shows both; the headline is
  the higher-priority one of §11 (e.g. goal `blocked(no_progress)` with grant
  `paused(goal_blocked)` is shown as *blocked: no progress*; grant `halted(emotional)`
  with goal `active` as *halted: emotional*). A grant pause never overwrites a goal's
  `blocked` or `budget_limited`.
* **Resume returns to the state before the stop.** A halt or pause that interrupted
  `awaiting_verification` returns to `awaiting_verification` on resume (only the verifier
  path runs); one that interrupted `active` returns to `active`.
* **New grant for the same goal**: the goal is re-bound to the new `grant_id`; iteration
  count and repetition history carry over; the budget is the new grant's.
* **Same blocker** (OpenClaw rule): `blocked(same_blocker)` after the same normalized
  blocker code on **3 consecutive goal ticks**. *W6a:* an agent's `goal_report(blocked)`
  still blocks the goal (at the iteration's end, after its outcome is committed), so the
  count survives the person's resume after such a block (otherwise it could never reach 3)
  and restarts on any other counted end; only known, committed outcomes feed it.
* **Stira extensions, own reason codes:** `blocked(no_progress)`, `blocked(loop_detected)`,
  `blocked(max_iterations)`, `blocked(approval_timeout)` (a needed approval unanswered
  within the grant's approval timeout ≤ ADR 0006 expiry), `blocked(verification)`.
* Resume from `blocked(max_iterations)` is an explicit resume with a user-chosen number of
  **additional** iterations; budget and TTL are not reset.
* **Budget accumulates per grant.** OpenClaw resets a goal's budget window on resume; the
  grant total never resets.
* **Only the user changes the goal** (by a new grant). External content asking for a goal
  change is ignored; where recognised it is recorded as `goal_change_ignored` (source tag
  only). Detection is best effort (§10).

**Repetition detection — differences from OpenClaw `tools.loopDetection`.**

| Aspect | OpenClaw | Stira loop |
|---|---|---|
| History scope | per run id; heartbeat cycles start fresh | in-run: same; plus a **grant-scoped** cross-tick history in the grant store |
| Post-compaction guard | arms after a compaction-retry for a short window; aborts after the same `(tool, args, result)` repeats enough times — read from the installed 2026.10.4: window **3** calls, abort at **3** identical triples (baseline 16) | same window and count (3 / 3) when an in-run detector is built (deferred: the per-tick tokens and the wall time bound one iteration) |
| Normalization | tool-specific (e.g. `exec`: status, exit code, output; volatile ids, durations stripped) | **per-tool normalizer table**; no global timestamp stripping; a tool without a normalizer uses its raw result hash |
| Result hash | taken before a warning note is appended | same |
| Escalation | warning note → block the whole batch → second critical ends the run | same; ending the run ends the iteration (`run_loop_blocked`) |
| Cross-tick | — | one keyed outcome fingerprint per iteration (W6a: a fresh read of every file the grant wrote — a missing one an explicit marker — plus the iteration's normalized per-check `run_checks` rows as one set; the agent's report is not part of it; anything unreadable = unknown: no progress, not in the history); not new to the last 16 for `no_progress_ticks` iterations in a row → `blocked(no_progress)`; a recurrence within the last 6 (A-A, A-B-A-B, A-B-C-A) → `blocked(loop_detected)`; interrupted iterations (also a stop of the grant read at the commit) change no streak and no history, an iteration with no consumed effect is not counted; a cycle over more than 256 files is not guaranteed to be found; resume resets streaks (not the same-blocker one), not history (docs/grants.md "Progress by outcome") |

Reference: OpenClaw 2026.9.8 docs `tools/goal.md` and `tools/loop-detection.md`; the
post-compaction values above are re-pinned to the installed **2026.10.4**
(`embedded-agent` loop guard; 2026.9.8 is no longer on disk — W6a, docs only).
Checking the implementation against that pinned version (and its values) is an
acceptance criterion; a newer OpenClaw does not change this ADR silently. False positives
are tested per normalizer (§14).

**Context degradation.** Every tick re-injects a trusted block built from the grant store
— full goal, scope, remaining budget and TTL, stop state, verification criteria — as the
trailing context of the latest user-role message (the #254 method, cached prefix
unchanged). Authority and taint live in the harness, so compaction cannot lose them.

**Identity check** (interval: Open (4)). What is compared:

* *model-free*: the hashes of the character definition and the static system prompt
  against those recorded at grant issue;
* *model*: after re-injecting the character definition, the loop answers a structured
  self-check — goal id, scope tools, remaining budget, active stop conditions, and the
  last three actions with their memory origin tags; each field is compared with the grant
  store and the action log. A mismatch, or an action attributed to a user origin that the
  log records as self-generated, is a failure.

Failure → `halted(identity)`. Resume: explicit user resume (§9); the next tick runs the
check first; a second consecutive failure revokes the grant (`revoked(identity)`).

### 9. Emotional memory, breaker and emotional stops

* **Read path:** each iteration chooses its next action with the same retrieval the chat
  turn uses (episodic, semantic, current state). No separate scoring.
* **Write-back path:** outcomes become episodic entries with `origin = grant:<id>`.
  Retrieval shows the origin; **self-generated entries never count as user statements**
  and cannot satisfy a "user confirmed" condition. Promotion to semantic / procedural
  memory or to the character definition goes through the existing human gate. Depends on
  #292 / #293.
* **Stops (decided, not open):** breaker `WARNING` / `COHERENCE` →
  `halted(breaker_warning)`; `CRITICAL` → `halted(breaker_critical)` with in-flight work
  cancelled; `stability`, `trust` or `vigilance` beyond the **existing breaker
  thresholds** → `halted(emotional)`. No new formula; `bcScore` is not used. Every halt
  advances the authority epoch.
* **Every halt — breaker (including WARNING) and emotional — is released only by an
  explicit resume of the authenticated user** in the dashboard with typed confirmation.
  The resume re-checks TTL, budget, breaker `NORMAL` and the current values against the
  thresholds, and is refused if any fails. Decay, the breaker returning to `NORMAL`, time
  passing or a chat message never release a halt. A human cannot override `expired`,
  `revoked` or a still-dangerous state.
* **Human operation tokens (bound to the stop generation).** Every human state transition
  — resume of a halt, a pause or a blocked goal, resume of a job, budget release,
  proposal approval — carries `incarnation + subject id + stop id + expected generation +
  op_id`. The **stop id is `(incarnation, epoch value recorded with that stop)`** (every
  stop advances the epoch; no new counter). The UI issues tokens only for the current
  incarnation; a restart invalidates every outstanding token and the UI must fetch new
  ones. Under the authority lock the request is compared atomically with the current
  incarnation and the subject's *current* stop id and generation, and then consumed. A
  token of an older incarnation can never change state, also when a rollback has erased
  the record that it was used and the epoch value happens to match again. Example: halted (stop 41) → resume → halted again (stop 57); a re-sent or
  late resume for stop 41 is refused (`resume_stale`) and stop 57 stays. `op_id` is bound
  to a hash of the payload: the same `op_id` with the same payload returns the original
  result without a second transition; the same `op_id` with another payload is refused.
  Result look-ups are bound to the incarnation as well (an older incarnation's result
  is reported as `unknown`, never re-applied). This is separate from the agent-tool
  idempotency key of §4.
* `halted(...)` is a grant state of this ADR; it is unrelated to the breaker setting
  `critical_action = "halt"` (glossary), which is not a full stop.
* The core algorithms of the emotional layer are not specified here.

### 10. Taint (auxiliary defence)

* **Unit and rule (conservative):** the harness labels every input it places into a
  model context — user turn, Stira-internal, self-generated, external (web, files outside
  the workspace, outside tool results, pasted external documents). The label of a turn
  or iteration is the **union of the labels of everything it read**; every output it
  produces (memory entries, files, summaries, proposals, jobs) inherits that union.
  Unknown = external — explicitly including existing files without a label, files
  overwritten by an outside process (detected by content hash / mtime against the label
  record) and content whose origin metadata is missing. The model's own statement about
  origin is not used. Labels are
  kept across a copy into the workspace, a summary, a compaction and a re-read from
  memory; complete identification of pasted documents is not expected — what is tested
  is that labels are not lost on these paths.
* **Consequence, accepted:** after an ordinary external search the turn is tainted, so it
  cannot create a stage-B job; it can only propose.
* **What taint restricts:** job creation, any authority change and any goal change. It
  does **not** stop reading or the loop: a grant *starts* only when the user issues it,
  and a tick that reads external information continues within its scope.
* **Not the main defence.** Complete detection of external goal-change attempts is not
  expected. The main defences are the immutable goal and scope enforcement at every
  effect (§2, §5).

### 11. State machine (one table)

Grant states: `active`, `halted(reason)` (§9 resume only), `paused(reason)` (user resume),
terminal `expired`, `revoked(reason)`, `complete`, `failed`. **Stop-reason priority**
(highest wins and is shown): `kill_switch` > `revoked` > `expired` > `unhealthy` /
`clock_anomaly` > `halted(breaker_critical)` > `halted(emotional)` > `halted(identity)` >
`halted(breaker_warning)` > `budget_limited` > `blocked(*)` > `paused(operator)` >
`paused(error)`.

| Event | Transition | Reason | Who recovers |
|---|---|---|---|
| user revokes / kill switch | → `revoked` | `revoked(user)` / `kill_switch` | new grant |
| TTL reached (either clock, incl. heartbeat stopped) | → `expired` (watchdog, §12) | `expired` | new grant |
| clock backwards / disagreement | → `revoked` | `clock_anomaly` | new grant |
| log / epoch / latch / budget unreadable | → `revoked`, stage `unhealthy` | `unhealthy` | human repair, then new grant |
| scheduler start (every start, §6) | → `revoked`; every job `paused(start)`; day budgets at cap | `revoked(restart)` | new grant; human resumes jobs |
| session reset (`/new`, `/reset`) | → `revoked` | `revoked(session_reset)` | new grant |
| breaker WARNING / COHERENCE / CRITICAL | → `halted` | `breaker_warning` / `breaker_critical` | explicit resume (§9) |
| emotional values beyond thresholds | → `halted` | `emotional` | explicit resume (§9) |
| identity check failed (1st / 2nd) | → `halted` / `revoked` | `identity` | explicit resume / new grant |
| grant budget exhausted | goal → `budget_limited`; grant → `paused(goal_budget_limited)`, its jobs stop | `budget_limited` | new grant |
| model error / timeout (no effect outside the model) | one retry with a new permit (re-authorization, not deduplication); again → `paused` | `paused(error)` | user resume |
| tool error / timeout with **unknown outcome** | retried **only** if the outcome can be confirmed by a query or the tool honours an idempotency key; otherwise no retry → `paused`, user notified | `paused(outcome_unknown)` | user resume after checking |
| model ends without a tool call | iteration ends; no-progress tick (W6a: two in a row → `blocked(no_progress)`); a completion claim → `awaiting_verification` | `iteration_no_action` | — |
| same blocker / no progress / ping-pong / max iterations / approval timeout / verification | goal → `blocked`; grant → `paused(goal_blocked)`, its jobs stop | per §8 | user resume (max iterations: with extra count) |
| goal `awaiting_verification` | only the verifier runs (§8), ≤ 1 per tick, ≤ 3 total | — | — |
| resume for an older stop id / re-sent `op_id` | refused / original result returned | `resume_stale` | — |
| heartbeat stops | no ticks; TTL keeps running; at most one tick on return | `heartbeat_stale` | heartbeat restored |
| duplicate tick / user turn running | dropped | `tick_skipped(overlap|user_turn)` | — |
| stop or narrowing between `authorize` and `consume` | effect refused | `permit_stale` | — |
| approval arrives after revocation | refused at `consume` | `approval_void(revoked)` | — |
| verifier passes while active | goal + grant → `complete` | `complete` + evidence | — |
| verifier passes after grant ended | grant unchanged; goal `late_verification(pass)` | — | user may mark goal complete |

### 12. Stop-event contract, watchdog, UI

**One lifecycle-event contract ("never stop silently").** Creation, every transition of
a grant, goal or job **out of running**, expiry, and every health change, is one
`lifecycle_event` record with one schema: subject (type + id), from-state, to-state,
reason code, `(incarnation, epoch)`, time. Created: `created` (job, proposal, grant).
Stops: Covered:
`paused(*)` (incl. `paused(start)`), `blocked(*)`, `budget_limited`, `halted(*)`,
`awaiting_verification`, terminal states, `heartbeat_stale`, `unhealthy`, `gateway_down`.

* Each event produces exactly one notification (dedupe key = subject + incarnation +
  epoch) in a persisted outbox with unread state, shown until acknowledged. *W6c (checked):*
  a completion claim is exactly one event (`goal: active → awaiting_verification`,
  `goal_claimed`) and one stop notice under the existing key (`stop|goal|*|goal_claimed`),
  unread until acknowledged — by the person, or by their verification of that claim; its
  evidence record and every look add no event; a new claim after a reject is a new event that
  makes that notice unread again (the existing rule). The
  dashboard and the Stage board show the waiting claim with "since <time>", the TTL left and,
  past `autonomy.claim_stale_sec` (default 30 min), "still waiting for you" — display only. The UI
  **groups** notifications of one goal and its grant (and its jobs) into one entry with
  the derived headline of §8; the individual events stay visible inside it.
* **Reconstruction:** at every start and every watchdog pass, the outbox is rebuilt from
  the lifecycle events and the current states: every non-running state without an
  acknowledged notification gets one; a non-running state without a lifecycle event (record
  failed) gets `stopped (reason not recorded)`. If writing a lifecycle event fails, the stage
  is latched `unhealthy` in memory.

**Pre-emption (W6b, implemented).** Every holder of the shared turn lock — a person's
chat turn, a webhook turn, the automatic continue, the resume of a restored card, `/new`, a
procedure approval — takes it only through one `TurnLock` that raises the pre-emption
signal before the lock is tried: a running iteration yields at its next effect boundary
(`yielded(user_turn)`; how a yielded iteration counts toward the no-progress rule is
W6a's), no iteration is admitted from the signal until the lock is released, and an effect
that still finished is a residual. A syntactic (AST) test checks every use of the lock and
the named turn starts against allow-lists (its limit is in [grants](../grants.md)); this
ends the W5 note that the webhook, the automatic continue and the resume took the lock
without the signal. Independent background jobs (scheduler agent jobs, memory jobs) run
their own agents, never take the turn lock and do not pre-empt (W6b decision); they stay
independent of an unattended iteration as well — no lock is added; the iteration is
confined to its grant's workspace subtree, the path floors apply to every writer, and a
person must not rely on exclusivity there (W6-H decision for W6d, [grants](../grants.md)).
A W6d heartbeat-started turn or iteration registers itself in the enumeration test's
`W6D_STARTS` (W6-H placeholder) — done: the scheduler's `GrantTicker._press`, an `iteration:`
press of the executor's own `step`, pre-empted by every holder like a person's press. Its
skips are lifecycle events of the subject `grant_tick` (`tick_skipped_<reason>`, state-class reasons
only — the same reason again only after a heartbeat start or another state-class reason, at
most 32 per grant in a process; `tick_started` for a heartbeat start).

**Watchdog (model-free, never starts autonomous work).** A timer in the gateway process,
independent of the heartbeat and of page views:

* TTL reached (either clock): advance the epoch, record `expired` as a lifecycle event — also
  with nobody looking and the heartbeat dead;
* cancel in-flight work of stopped grants (via the §5 in-flight set) — separate from
  recording; `consume` refuses after TTL even if the watchdog is late;
* heartbeat stale (`max(3 × interval, 120 s)` as in ADR 0008 §11) → lifecycle event;
* writes its own last-pass time; at start, a gap longer than a few passes is recorded as
  `gateway_down(from, to)`.

**Limit:** the watchdog lives in the gateway process. While that process is down, nothing
is recorded or notified; the gap is recorded on the next start. External channels are
**Open (3)**.

**Dashboard `/schedules`:** type / proposer columns with filter and taint marker;
proposal inbox (shared with #249); envelope panel (edit with typed confirmation); usage
and remaining budget per job, agent total and grant; recent runs, stop reason and **who
may resume** (#275); grant panel (goal, state, remaining TTL, iterations, progress,
verification evidence, revoke, explicit resume); kill switch and per-item pause, always
available, also when unhealthy; heartbeat health from the watchdog's record plus the
request path's own staleness check.

### 13. Prerequisites and order

1. **#285** owner lock, **#275** stop-reason contract, **#277** audit contract, **#276**
   single pre-execution boundary (hosts `authorize`, `consume`, `rules_loosening`).
2. **Common safety foundation — before stage A** (part of #297), because approving a
   proposal creates a real job: authority epoch and permits / `consume` (§5), latch
   store, unconditional conservative start (§6), incarnation and human operation tokens
   (§5, §9), lifecycle-event contract and outbox, model-free watchdog (§12),
   structured-only stores (§7). No additional persistent counters: stop ids are
   `(incarnation, epoch)`; the incarnation is regenerated at every start.
3. Stage **A**.
4. **Grant-specific foundation** (first part of #299): grant store, TTL clocks, goal ×
   grant states, verification path, taint labels.
5. **#292**, **#293**, **#294** — before memory job kinds (#252) and the read / write
   paths of §9.
6. Stage **B** (per Open (12)) → stage **C** loop.

### 14. Evaluation and fault injection

Deterministic tests: fake model, fake wall and monotonic clocks, fixed seeds.

* **Authority types:** a `proposal` causes no effect; the agent cannot pause, edit or
  resume a `human_job` or a job of an older grant / generation / envelope version; an
  approval for an out-of-scope tool is refused.
* **Permit race:** inject each stop / narrowing (grant narrowed or revoked, envelope
  lowered, policy changed, breaker change, emotional halt, TTL reached, kill switch)
  **after `authorize` and before `consume`** → `permit_stale`; after `consume` → in flight,
  cancelled or recorded completed-after-stop.
* **Kill switch** while an effect holds the scheduler lock → takes effect without waiting.
* **Start-up (unconditional):** normal restart with all stages off; revocation-write
  failure → kill → restart; older valid log; whole data directory restored to a state
  from before any ADR 0011 item, and from before a human paused a job → every job paused,
  day budgets at cap, no grant active.
* **Human operation tokens:** halted → resume → halted again on the same grant; re-send
  and late delivery of the first resume → refused (`resume_stale`), second halt stays;
  same `op_id` + same payload → original result, no second transition; same `op_id` +
  different payload → refused.
* **Double restore to one snapshot:** restore the same data-directory snapshot twice;
  after the second restore re-send the first incarnation's resume and budget release
  (the epoch values match again) → refused (old incarnation), no state change; a token
  issued before a restart is refused after it.
* **Unknown tool outcome:** a timeout of a tool without query / idempotency support → no
  retry, `paused(outcome_unknown)`, notified.
* **Actual vs. displayed state:** grant pause does not overwrite goal `blocked` /
  `budget_limited`; a halt during `awaiting_verification` resumes to it.
* **Double start** refused (#285); **clock backwards / jump** → `clock_anomaly`;
  **duplicate tick**; **approval after revocation**.
* **Stops:** WARNING / CRITICAL / emotional thresholds halt; decay, NORMAL return, time
  and chat messages never release; explicit resume refused while still dangerous;
  `expired` cannot be resumed.
* **Goal × grant:** every row of §11, including the verification path, late
  verification, `max_iterations` resume and grant jobs stopping when the goal is not
  `active`.
* **Lifecycle events:** creation and every non-running state of §11 produce one
  notification each (grouped per goal / grant in the UI); outbox and
  lifecycle-event write failures are rebuilt; a gateway gap is recorded on start.
* **Repetition:** per-normalizer false-positive tests; cross-tick detection survives
  heartbeat cycles and compaction; post-compaction window / count match the pinned
  OpenClaw reference (§8).
* **Secrets, persisted vs. in use:** a high-entropy marker in tool arguments, refused
  calls and exceptions never appears in any persisted store; a goal whose text
  `mask_secrets` changes is refused; the goal marker is present in model input and in the
  grant store, and **absent** from conversation history, traces, logs, error collection,
  notifications, audit and lifecycle events; when the model **quotes** the goal in its
  output or a summary, the marker reaches only the allowed memory path (staging) and no
  other store.
* **Taint of unlabelled content:** an unlabelled existing file, a file overwritten by an
  outside process and content without origin metadata are treated as external.
* **Taint propagation:** a label survives a copy into the workspace, a summary, a
  compaction and a re-read from memory (a later turn reading that memory is tainted).
* **No flood / budget:** 1000 scripted attempts over a simulated week → limits hold;
  charged ≤ reservations; resume does not reset the grant total.
* **Quality with real model calls (before stage C is merged):** on the #143 harness, with
  a model-call count approved by the operator and **pass criteria pre-registered** (like
  `docs/eval/emotion-ablation-prereg.md`): goal and identity retention over a long run
  non-inferior to emotion off within a pre-registered margin; **planted misattribution**
  (a self-generated memory phrased as a user statement) never accepted as user
  confirmation; a **forced context compaction** mid-grant does not change goal, scope or
  stop conditions; the **identity check runs at its configured interval** in the long
  run and around the forced compaction, and its detections are reported; relevance of
  actions chosen from dialogue-derived emotional memory reported against emotion off. The identity check does not guarantee absence of
  semantic drift; this evaluation is what measures it.

## Alternatives considered

* **Stage A only, forever.** Safest, but autonomous operation never starts. Kept as the
  first step.
* **No envelope.** Rejected (§1).
* **Event-driven triggers.** Not authorized; out of scope.
* **Let the agent extend or resume its own grant.** Rejected: loosening is human-only.
* **Automatic resume after breaker / emotional stops.** Rejected (§9).
* **A new emotional score for stopping.** Rejected: existing breaker thresholds are
  reused.
* **Rely on `mask_secrets` for secrets.** Rejected (§7).

## Out of scope

Autonomy without a grant (subject to Open (12)(i)), always-on event-driven operation,
self-widening of tools / budgets / envelope / grants / heartbeat floor, automatic resume
after stops, write / exec / outbound tools in agent jobs, external notification channels,
enabling any stage by default, details of the emotional core.

## Open points (operator decision)

* **(1)** Which stage first (recommended: A, then the grant foundation).
* **(2)** Defaults: envelope budgets, proposal expiry (72 h), `max_jobs` (3),
  `min_interval_sec` (3600), `job_ttl_sec` (7 days), `no_progress_ticks` (3), goal
  retention after a grant ends.
* **(3)** External notification channel (dashboard only until decided).
* **(4)** Identity check interval (proposed every 6 h, at least once per 12 h of grant
  time).
* **(5)** Whether a chat command may *request* a grant (still confirmed in the dashboard)
  or the dashboard only.
* **(6)** An anchor outside the data directory against repeated full rollbacks (§6).
* **(12)** Relation of stage B to grants: (i) / (ii) / (iii), recommended (ii) (§1).

## Operator decisions (2026-10-16)

Recorded from the operator's answers. Nothing here is implemented; the rest of this ADR
stays Proposed until the common safety foundation (§13 item 2) exists.

* **(1) Decided: start with stage A (propose), then the grant foundation.** Stages B and
  C follow in the order of §13. Open (12) stays open (recommendation (ii) is unchanged);
  the operator's direction is "autonomy follows the grant, with approvals allowed to a
  degree that depends on how much autonomy the operator grants".
* **§6 restart contract accepted for every deployment, including always-on hosts.** After
  every start all jobs are `paused(start)` and a **human resumes them** (§9 operation
  tokens). A long-running host (for example EC2) is not exempt: a person resumes after
  a restart. The deployment guide (#318) and the operations guide must say so and show
  the resume procedure.
* **Direction, not a decision: chat channels (Discord / Slack) as the notification and
  resume path.** This refines Open (3) and Open (5). Constraints to keep when it is
  designed (to be confirmed before it is implemented):
  * the resume / approval request is handled by the **host** (a command handler of the
    channel integration), never through a model-visible tool, so a prompt injection
    cannot issue it;
  * the sender must be an **authenticated, allow-listed operator identity** of that
    channel; it carries the same human operation token (§9: incarnation, subject, stop
    id, expected generation, `op_id`) and is refused when stale;
  * **which actions** may be done from a chat channel is a separate decision. Proposed
    split: resuming a `paused(start)` job and acknowledging notifications from chat;
    resuming a `halted(*)` grant, issuing a grant, changing the envelope and releasing a
    budget stay in the dashboard with typed confirmation (§9, §3);
  * a notification carries only the structured fields of §7 (no goal text, no tool
    arguments).


## Addendum (#365): notes from ADR 0012 (YOLO)

[ADR 0012](0012-yolo-mode.md) shares the authority boundary of §2 / §5: since W1 (#296)
the one process-wide service in `src/stiracore/authority/`, injected into the YOLO desk
(`src/stiracore/yolo/authority.py` re-exports it); the goal side (#299) is not built yet.

* **§2 — authority type `yolo`.** Source: a request a person confirmed
  (`host_confirmed_request`); may cause: an automatic answer to an Ask (`RULE_ASK`) within
  its tier; bound to `(incarnation, grant_id, generation)`. `kind` (`goal` | `yolo`) is a
  mandatory discriminator of every grant record: a YOLO window can never be handled as a
  goal grant, nor the reverse. The loosening contexts are `host_confirmed_request` and
  `breaker_resume` (same window, incarnation, tier, end and cap only).
* **§5.** A YOLO narrowing (Stop, `/new`, pause, end) advances the authority epoch; a
  permit is consumed once. The two-binding `consume` (a goal iteration inside a YOLO
  window) is phase 8 of ADR 0012 and not implemented.
* **§9.** A YOLO window paused by the breaker resumes on its own after ten minutes of
  NORMAL (ADR 0012 Open (1), as implemented; a fourth pause within an hour ends it). This
  does **not** apply to grants: §9's "no automatic resume" stays for goal grants and their
  loops.
