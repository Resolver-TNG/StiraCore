# ADR 0024: Approvals that outlive their turn — breaker transitions, job-origin approvals and what a resume can and cannot repeat

**Path:** `docs/adr/0024-approvals-beyond-the-turn.md`
**Status:** Accepted (2026-10-21, commander decisions below; every recommendation taken).
**Phase 1b (§8) is implemented** (see "Implementation notes (phase 1b)" at the end). **Phase 2
(§4 opt-in wait, §6 job resume) is implemented** (go 2026-10-21, after ADR 0021 S2; #276 is
still open — see "Implementation notes (phase 2)"). Numbered 0024 (first drafted as 0020; that number is ADR 0020, workspace artifacts).
**Issue:** Refs #240 (split ③ of #105). Related: [ADR 0006](0006-hitl-persistence.md) (durable
approvals, Accepted — this ADR builds on it and does not replace it), [ADR 0008](0008-scheduler.md)
(scheduler; its Open (a) and Open (b) are answered here for the approval side),
[ADR 0021](0021-scheduler-job-base.md) (scheduler job base, Accepted 2026-10-21 —
the job side of the same two questions), [ADR 0010](0010-tool-policy-gate-stack.md) (gate stack),
[ADR 0011](0011-agent-self-scheduling.md) (restart contract, permits, chat-channel direction),
[ADR 0012](0012-yolo-mode.md) (YOLO window), [ADR 0013](0013-audit-event-contract.md) (audit
contract), [ADR 0016](0016-subagent-orchestration.md) (`delegate`),
[ADR 0018](0018-autonomous-memory.md) (memory jobs), #146 (binding, operation ledger), #95
(checkpoints), #189 (approval queue), #239, #276 (single pre-execution boundary), #273 (ledger
restored from a backup), #285 (`server.lock`).
**Decision (proposed):** keep ADR 0006's store, binding and single-use rules unchanged and close
the gaps that are left: (1) a pending approval is **frozen** while the breaker is stricter than
when it was raised — reject always works, approve / edit wait for recovery and the expiry is
never extended; (2) unattended jobs keep **deny** as the default, and waiting for a human is a
per-job opt-in that only exists once jobs run under the same gate as turns (phase 2), and only
inside one process lifetime; (3) a replayed batch stops repeating write-capable calls that were
not approval-gated; (4) the approver boundary is fixed per channel: the dashboard decides, a
future chat channel may reject and acknowledge, nothing else approves.

## Where we are (measured against `main` at `4ad82b8`)

The issue text of #240 predates PR #263. Read from the code and tests, not from the issue:

| # | Item of #240 | State | Where |
|---|---|---|---|
| 1 | Durable request (agent, tool, args hash, expiry, correlation id, origin) | **done** | `core/approval_store.py` (`submit`, hash chain + head anchor) |
| 2 | Restore after a restart | **done** | `HITLApprovalBroker` lists `restored: true`; `tests/test_hitl_persistence.py` |
| 3 | Asynchronous approve → resume of a **turn** | **done** | `agent/hitl_resume.py`, the chat server `resume_paused_turn` |
| 4 | Expiry / cancel / EDIT as a new request | **done** | ADR 0006 §3 |
| 5 | Double approval / double resume at most once | **done** | `decided` / `consumed` / `resume_claimed` + ledger |
| 6 | Argument binding, refusal of a changed call | **done** | `check_resumable`, `binding_mismatch` |
| 7 | Fail-closed on a corrupt / tampered store, clock anomaly | **done** | ADR 0006 §5 |
| 8 | Decisions in the shared EventStore | partial | own chain; mirror is ADR 0009 stage 3 / #277 |
| 9 | Resume of a **job** | **not started** | `scheduler/executors.py`: `UNATTENDED_APPROVAL_MODES = {"deny"}` |
| 10 | Pending approvals vs. a CRITICAL transition | **not started** | the store and broker never read the breaker |
| 11 | Remote approval | boundary only (ADR 0006 §4) | `local:` is the only principal |

The `TODO(#105)` in `engine.request_hitl` is gone: the comment now points at ADR 0006
(`core/engine.py`, `request_hitl`). The synchronous contract (no approver connected = REJECT) is
unchanged and tested (`test_sync_request_hitl_without_an_approver_still_rejects`).

In the terms of the issue's suggested phases: **phase 0 (persist only)** and **phase 1 (turn
resume)** shipped together in PR #263. This ADR adds **phase 1b** (gaps of the turn path) and
**phase 2** (job resume), §8.

## What a resume can and cannot repeat (read from the code)

A resume replays the tool batch saved by `CheckpointHooks.before_tools` (one checkpoint per
batch, *before* any call of the batch runs) and then lets the model continue (`agent(None)`).

| Case | Outcome today | Source |
|---|---|---|
| The paused call itself (Ask, or a write under CRITICAL `halt`) | runs once, with the approved input | ADR 0006 §3 |
| An earlier call of the batch that was **approval-gated** and committed | answered "Already applied before the interruption", not repeated | `_replay_guard`, `test_resume_does_not_repeat_a_side_effect_committed_before_the_interruption` |
| Same, outcome unknown (started, not finished) | refused, "reconcile manually" | `_replay_guard` |
| An earlier **read-only** call (Always) | **runs again** (no ledger entry) | `before_tool`: `if not write and not ask: return` before the guard |
| An earlier **write-capable call with rule Always** | **runs again**: the guard is consulted, but the ledger only has entries for gated calls (`mark_started` is called only in the gate path) | `checkpoint_hooks.py` |
| An earlier `delegate` call (Always) | **runs again**: a new child, new model calls and tokens | ADR 0016; `delegate` is not write-capable |
| The conversation moved on, a newer checkpoint exists, the checkpoint was pruned (20 per thread) | refused, approval revoked | `check_resumable` |
| Checkpoint store in metadata-only mode (`store_messages=False`) | refused: the stored call has no `input`, so its hash cannot match | `FileCheckpointStore` |
| EDIT carrying a secret (`live_only`) | not restorable, cancelled after a restart | ADR 0006 §1 |
| RESPOND to a restored request | refused (no live turn) | ADR 0006 §3 |
| A child of `delegate` | never raises a card (it only gets Always tools), so there is nothing to resume | ADR 0016 §3 |
| A scheduler job | **nothing to resume**: `AgentJobExecutor` runs a plain Strands `Agent` with `JobToolGuard`, no `CheckpointHooks`, no checkpoint, no ledger | `scheduler/executors.py` |
| Memory jobs (ADR 0018) and Gateway jobs | have no tools, never raise a card | `control/memory_jobs.py`, `scheduler/jobs.py` |

The three "runs again" rows are the LangGraph-style hazard named in the #240 review comment
(re-running a node re-runs what happened before the interrupt). They are not measured end to
end; they follow from the code paths cited. Phase 1b (§5) closes the write-capable rows and the
`delegate` row and adds the tests.

## Decisions

### 1. What does not change

* ADR 0006 in full: one append-only, hash-chained store; state derived by replay; head anchor;
  three single-use layers (decision, grant, resume) plus the #146 ledger; the resume context
  token; single process per data directory (`server.lock`, #285).
* Approval is always a human. No auto-approval, no "system" approver for approve / edit, no
  environment variable that changes any rule here. The YOLO window (ADR 0012) answers an Ask of a
  **human chat / Stage turn** before a card exists and never writes this store; it does not cover
  job-origin calls (ADR 0012 §3), and nothing here widens it.
* A durable approval never widens what the current breaker and rules allow: the replay runs
  `pre_turn` and the gate under the policy *at replay time* (ADR 0006 §5).

### 2. Request record: fixed `origin` forms

`submit` accepts any `origin` dict today. It becomes a closed form (unknown keys refused, like
ADR 0008's job schema), recorded in the `requested` event body:

```json
{"kind": "turn", "id": "<turn id>"}
{"kind": "job", "job_id": "<job id>", "run_id": "<run id>", "generation": 3, "incarnation": "<128-bit hex>"}
```

* `kind: "turn"` is what the chat server writes today (`chat_api._persist`); an empty origin (no turn id)
  stays accepted for old rows and is treated as `turn` without an id.
* `kind: "job"` is only written by the phase-2 job executor (§6). `generation` is ADR 0008's job
  generation at the run's start; `incarnation` is the process's random id (ADR 0011 §5: never
  read back from storage — a new process never matches it).
* `break_level` (already recorded) becomes **required** for new rows; a row without it is
  **always held at every level stricter than `normal`** for §3 (strictest reading; 指揮官確認済み
  2026-10-21 — first written as "counts as `critical`", which would never hold it).
* The correlation id stays the turn id for turns and becomes the **run id** for jobs (ADR 0006 §2
  said "job id"; the run id is the finer key and the job id is in the origin).

Old rows replay unchanged; the new checks apply to rows written after the change.

### 3. Pending approvals and the breaker (ADR 0008 Open (a), approval side)

**Rule: freeze, do not cancel.** Breaker levels are ranked `normal < warning = coherence <
critical`. At decision time the broker reads `engine.breaker_status()` (the read the scheduler
already uses; an exception or an unknown level counts as `critical`):

* **Reject** is accepted at every level, for every request.
* **Approve / edit** of a request is refused with `409 breaker` (nothing recorded) while the
  current level ranks **above** the request's `break_level`. The card stays pending and says so.
  A request raised under CRITICAL `halt` (the per-write approval of ADR 0002 / ADR 0010) has
  `break_level = critical` and stays decidable at CRITICAL — that is what `halt` is for.
* The **expiry is never extended** by a freeze (at most `hitl_timeout_sec`, capped at 24 h): a
  freeze that outlasts it ends as `expired`, i.e. a rejection.
* `claim_resume` repeats the check under the store's write lock; a level that rose between
  decision and claim refuses the resume (`resume_refused(breaker)`) and revokes the approval,
  as every other claim-time refusal does.
* **Job-origin requests** (phase 2) are **cancelled** on CRITICAL (`cancelled(breaker_critical)`):
  the scheduler already cancels every active run at CRITICAL (ADR 0008 §7), and a run waiting for
  approval is an active run (§6). At WARNING / COHERENCE they are frozen like turn requests.

Why freeze and not the two options ADR 0008 listed:

* **(a1) leave it to expiry** (today): a person can approve during the incident. Under the
  default `readonly` the replay then finds the tool removed and the approval is spent for
  nothing; under `halt`, the replay's gate asks again and the broker answers it from the store,
  so a write raised **before** the incident runs **during** it (read from `CheckpointHooks.
  before_tool` and ADR 0006 §3; not measured).
* **(a2) cancel everything on the transition**: needs an asynchronous transition hook (the
  breaker changes inside turns and, with #238 on, in the heartbeat tick), discards decisions the
  freeze would keep, and cannot apply to `halt` requests without breaking `halt`.

The freeze needs no transition hook: it is evaluated where the decision is made. It is the
Codex observation of the #240 review (a stop arrives after the action that caused it) handled
at the one point where a pending approval can become effective.

### 4. Unattended jobs (ADR 0008 Open (b), approval side)

* **Default: deny**, as implemented (`UnattendedApproval` / `UNATTENDED_APPROVAL_MODES`): the
  run records `run_awaiting_approval` (tool name and argument hash), ends
  `run_failed(approval_required)` and raises a notification. This matches the Hermes default
  cited in the #240 review (cron contexts deny instead of waiting).
* **Opt-in: wait**, per job (`unattended_approval: "wait"` in the job definition, ADR 0021 §2),
  available only in phase 2 (§8), only for a job whose executor runs under the gate (§6), and
  only at creation / update with the usual confirmation. The server-wide switch is a code
  constant until phase 2 is accepted; no environment variable enables it.
* **Within one process lifetime only.** At every start, every pending `kind: "job"` request is
  cancelled (`cancelled(restart)`, "job runs do not survive a restart"). This is the same rule
  as ADR 0008 §4 (an interrupted run is closed, never retried), ADR 0018 Q4 (model-calling memory
  jobs come up paused) and the restart contract the operator accepted for ADR 0011 §6. A
  restored job card would otherwise invite a person to approve work whose job a human has not
  resumed.

### 5. Replay of a batch (phase 1b)

* Every **write-capable** call (as `_write_capable` decides) gets an operation-ledger entry
  before it runs, also when its rule is Always and no approval was asked. On replay the existing
  guard then answers "Already applied before the interruption" (committed) or refuses (unknown).
  The ledger key stays `operation_key(agent, tool, args_hash, toolUseId)`; entries without a
  `request_id` are marked `ungated`.
* `delegate` is treated the same way (a ledger entry before the child starts): a replay answers
  "not repeated after an interruption" instead of starting a second child. The model can ask
  for it again in its continuation; that is a new call with a new `toolUseId`.
* Read-only calls still run again (no side effect; the cost is the call). This is stated in
  `docs/hitl-approvals.md` next to the resume description.
* The card of a restored request says when the batch contains calls before it that will be
  answered from the ledger, and (ADR 0006 addendum, #273) that a call "may already have run"
  when the ledger cannot say.

### 6. Job resume (phase 2)

Only after ADR 0021's gated job executor exists (a job's agent runs through `CheckpointHooks`,
the rule → breaker → floor stack and the operation ledger, with its own checkpoint thread
`job:<job_id>`). Then:

1. A job with `unattended_approval: "wait"` reaches an Ask (or a call outside its allowed set
   that the gate would put on a card): the request is stored with `kind: "job"` (§2), the run
   records `run_awaiting_approval` with `request_id`, the agent invocation is **ended** (the
   worker is released) and the run's token reservation is settled with the usage observed so far.
   The run is `awaiting_approval`: it still counts as active for `overlap_policy = skip` (§7 Q4).
2. A human approves it in the dashboard (§3 and §9 apply). The approval schedules a
   **continuation segment** of the same run, through the scheduler, never directly:
   `run_resumed(request_id)`.
3. Before the segment's body the scheduler repeats every ADR 0008 §5a check — job not paused,
   **same generation**, breaker NORMAL, cancellation backlog below the cap, run ordinal and
   token budget of the charge day (a **new** reservation of `max_tokens_per_run`), plus, once
   ADR 0011 §5 exists, a fresh permit — and that the incarnation in the origin is the current
   one. Any failure ends the run `cancelled(<reason>)` and revokes the approval.
4. The segment calls `resume_approved_request` with the job's agent, so `check_resumable`
   (agent, newest checkpoint of the job thread, tool, `toolUseId`, args hash), the resume
   context token, `consume(resumed=True)` and the ledger apply exactly as for a turn.
5. Reject, expiry, cancel, a job update (new generation), a pause of the job, CRITICAL and a
   restart end the run `cancelled(...)`; nothing of it runs.

At most **one** waiting request per run and at most **3** waiting runs per server (more are
denied as in §4); the wait is at most `min(hitl_timeout_sec, approval_wait_max_sec)` with
`approval_wait_max_sec` 3600 by default and 86400 at most (Q5).

### 7. Fail-closed additions

* `audit_events()` re-verifies the chain before returning rows (it reads SQL directly today).
* Ancestor walks in `chain_root` and `resume_context` stop at 16 links like the EDIT-chain walk;
  a longer chain refuses the resume.
* The test ADR 0006 left open: breaker recovery + EDIT resume never runs the original input.
* A store or scheduler that cannot record a `kind: "job"` cancellation at start-up stops the
  scheduler from starting (unavailable), rather than leaving a job card decidable.
* Unchanged: clock rules, quarantine on a failed revocation, `SQLITE_BUSY` transient, everything
  else unhealthy for good.

### 8. Phases

| Phase | Content | State |
|---|---|---|
| 0 | Durable store, restore after restart, no resume | shipped (PR #263) |
| 1 | Turn resume from the checkpoint | shipped (PR #263) |
| **1b** | §2 origin forms, §3 freeze, §5 ledger for ungated writes and `delegate`, card wording, §7 | this ADR; no new switch (stricter only) |
| **2** | §4 opt-in wait, §6 job resume | this ADR; after ADR 0021's gated executor and #276; own go |

Phase 1b only refuses or records more; it needs no opt-in. Phase 2 is off unless a job sets
`wait`, and the job layer it depends on is itself off by default.

### 9. Who may decide, per channel (future remote approval)

ADR 0006 §4 stays: a remote approver must be a separately authenticated principal whose
decision carries a signature over `request_id + args_hash + expires_at + action`. With the
operator's direction of 2026-10-16 (ADR 0011, "chat channels as the notification and resume
path, handled by the host, allow-listed identity, operation token"), the boundary per channel:

| Channel | Approve / edit | Reject | Acknowledge a notification |
|---|---|---|---|
| Dashboard (`local:<resolver>`, same-origin + CSRF, #185 / #189) | yes | yes | yes |
| Authenticated chat channel, host-side handler, allow-listed operator, operation token (future) | **no** | yes | yes |
| Anything else (notification only, unauthenticated, model-visible) | no | no | no |

Reason for "no approve from chat": a chat message cannot show the masked arguments and their
hash the way the card does, and it is not same-origin; a rejection can only make less happen.
Allowing approval from a chat channel needs its own ADR (signature, display, replay window).
Nothing in this table is implemented by this ADR.

### 10. Audit

Decisions stay in the store's own chain (ADR 0006 §6). The shared-chain mirror is ADR 0009
stage 3 and the event shape is ADR 0013 (#277); the new events (`resume_refused(breaker)`,
`cancelled(restart)` for job requests, `run_resumed`) use the same masking and identifier rules.

## Alternatives considered

* **Strands interrupts + a session manager** for job resume: rejected for the same reason as in
  ADR 0006 (a second persistence path; the checkpoint replay is tested with the ledger).
* **Cancel every pending approval on CRITICAL (a2):** see §3.
* **Restore job cards after a restart and resume when approved:** contradicts the restart
  contract (jobs come up paused, a person resumes them); rejected (§4).
* **Wait as the default for unattended jobs:** accumulates work while nobody watches and is
  closer to autonomous operation than the line the issue draws; rejected.
* **Checkpoint after every call instead of a ledger entry for ungated writes:** more files and
  conversation copies per batch; the ledger already exists for this purpose.

## Out of scope

Auto-approval of any kind; approval from a chat channel; a ledger high-water mark outside the
data directory (ADR 0006 addendum, ADR 0011 Open (6); Q8); cross-process stores; external
notification delivery.

## Decisions (commander, 2026-10-21)

The commander accepted every recommendation of this ADR. The sections the questions refer to
are the decision text.

| # | Question | Resolution |
|---|---|---|
| Q1 | Breaker vs. pending approvals | **As recommended:** (i) a pending approval is frozen while the breaker is stricter than at request time; reject always works; the expiry is never extended (§3). |
| Q2 | Unattended default | **As recommended:** **deny** stays the default; `wait` is a per-job opt-in, phase 2 only (§4). |
| Q3 | Job cards after a restart | **As recommended:** cancelled at start (§4). |
| Q4 | A run waiting for approval and the overlap policy | **As recommended:** counts as active; later fires are `run_skipped(overlap)` while it waits. |
| Q5 | Maximum wait of a job approval | **As recommended:** 1 h by default, 24 h at most, bounded again by `hitl_timeout_sec`. |
| Q6 | Ungated writes and `delegate` on replay | **As recommended:** a ledger entry before they run, answered from it on replay (§5). |
| Q7 | Chat channel rights (when it exists) | **As recommended:** reject and acknowledge only; approval stays in the dashboard (§9). |
| Q8 | Ledger high-water mark outside the data directory | **As recommended:** deferred to ADR 0011 Open (6), one anchor for both. |

**Scope of this acceptance.** Phase 1b (§2 origin forms, §3 freeze, §5 ledger for ungated writes
and `delegate`, card wording, §7) is scheduled now; it only refuses or records more. Phase 2 (§4
opt-in wait, §6 job resume) starts only after a separate go, after ADR 0021 S2 and #276.

## Questions (operator decision)

All eight are resolved as recommended; see **Decisions (commander, 2026-10-21)** above. The
original text is kept for the record.

1. **Breaker vs. pending approvals.** (i) freeze approve / edit while stricter than at request
   time, reject always, no extension — **recommended** (§3); (ii) keep today's behaviour (a1);
   (iii) cancel all on CRITICAL (a2).
2. **Unattended default.** Keep **deny** as the default and allow per-job opt-in `wait` in
   phase 2 — **recommended**; or deny only, never wait.
3. **Job cards after a restart.** **Cancel at start** — **recommended** (§4); or restore them and
   require both the approval and a human job resume.
4. **A run waiting for approval and the overlap policy.** **Counts as active** (later fires are
   `run_skipped(overlap)` while it waits) — **recommended**; or a new fire may start beside it.
5. **Maximum wait of a job approval.** **1 h by default, 24 h at most** (bounded again by
   `hitl_timeout_sec`) — **recommended**; or always `hitl_timeout_sec`.
6. **Ungated writes and `delegate` on replay.** **Ledger entry before they run, answered from it
   on replay** — **recommended** (§5); or document the repetition only.
7. **Chat channel rights (when it exists).** **Reject and acknowledge only; approval stays in
   the dashboard** — **recommended** (§9); or approve with a signed principal now; or nothing.
8. **Ledger high-water mark outside the data directory.** **Defer to ADR 0011 Open (6)** (one
   anchor for both) — **recommended**; or do it in phase 1b.

## Implementation notes (phase 1b)

What shipped, and where it reads the decision text more strictly than written:

* **§3 hold.** `HITLApprovalBroker(breaker_level=...)` (the chat server: `engine.breaker_status().level`)
  refuses approve / edit with `409 breaker` for live and restored requests; reject and
  respond are never held; `/api/hitl/pending` carries `breaker_hold`. `claim_resume(...,
  breaker_level=...)` repeats the comparison under the store's write lock
  (`resume_refused` `{"reason": "breaker", "level", "requested_level"}` + `cancelled`). A held
  click is logged (ids, levels, hash prefix) and **not** stored ("nothing recorded";
  指揮官確認済み 2026-10-21).
  During a live turn `breaker_status()` returns the turn's snapshot; the gate's existing
  fresh-stability check after the decision still applies there.
* **§2 `break_level` "required".** `submit` refuses an unknown level but still accepts a
  missing one (callers and tests that predate this); a request without a level is held at
  every level above `normal`. §2 first said "counts as `critical`", which would never
  hold it; the stricter reading was taken (fail-closed) and §2 now says so (指揮官確認済み
  2026-10-21). The chat server always records the level.
* **§2 origin.** `normalize_origin` (closed forms); a `kind: "job"` request must be
  correlated by its run id. Nothing writes it yet; a pending one is cancelled at start
  (`job runs do not survive a restart`, from §4, cheap and only stricter).
* **§5 ledger.** `ApprovalBindingVerifier._claim_ungated`: the last BeforeToolCall check
  records ungated write-capable calls and `delegate` (`request_id` empty,
  `OperationRecord.ungated`); `_replay_guard` answers them on replay (`delegate`: "not
  repeated after an interruption", no reconciler). The restored card shows counts
  (`replay_preview`). An ungated entry answers a call only while it is replayed from a
  restored checkpoint (`NXPAgent.resume_from_checkpoint` → `CheckpointHooks.begin_replay`);
  in a fresh turn the same key (a provider repeating a `toolUseId` with the same arguments,
  seen in a test model) is a new call, recorded anew.
* **§7, a further hole found while testing.** The checkpoint restores the Neural State of the
  pause, so a replay normally runs under the breaker of the pause; when it does not (the
  breaker recovered, or the rule became Always), the paused call itself ran **ungated with
  the original input** even after an approved EDIT (measured with a mutant test). The paused
  call is now always sent through the gate on a resume (`CheckpointHooks.gate_on_resume`),
  answered from the store and never by the YOLO approver.
* Tests: `tests/test_hitl_breaker_hold_ledger.py`, `tests/approvals_test.cjs`; operator text
  in `docs/hitl-approvals.md`.


## Implementation notes (phase 2)

* `stiracore.control.job_approvals`: `JobApprovalWaits` (in-process registry, at most 3
  waiting runs, one request per run, wait = `min(hitl_timeout_sec, approval_wait_max_sec)`,
  `cancel_at_start` for every open `kind: "job"` request; a failure keeps the scheduler
  UNAVAILABLE) and `JobApprovalGate` (replaces `JobRulesGuard` for `wait` jobs: Never refuses,
  the first Ask stores the request and ends the invocation, a continuation lets only the
  approved call through — same tool, `toolUseId`, argument hash — once, via
  `claim_resume` + `resume_context` + `consume(resumed=True)`). The child's own
  CheckpointHooks still gate the call; the child engine's approver is the job gate, which
  approves only the call it just let through.
* Scheduler: `run_awaiting_approval` (with `request_id`), `approval_waiting` notification, the
  run stays in the active set (overlap = skip), `resume_waiting_run(request_id)` repeats the
  §5a checks (`_preflight`: paused, generation, breaker NORMAL, backlog, budget) and records
  `run_resumed`; reject / expiry / cancel are read at each tick; pause, CRITICAL, job update,
  stop and restart end the run `cancelled` and revoke an unused approval. The request is
  stored (and listed in the dashboard) while the first segment is still ending; an approval
  in that window is kept for the run and its continuation is scheduled when the run parks,
  with the same checks (§6.2; before #296's A8 fix it was revoked and the run ended
  `cancelled(approval_cancelled)`). A run that ends instead of parking drops it.
* The chat server: a dashboard approval of a `kind: "job"` request is routed to
  `scheduler.resume_waiting_run`, never to the turn resume. Only with gateway `agent_jobs` on.
* Deviations: the run's token reservation is kept while it waits (not settled and
  re-reserved; stricter), and the continuation is checked against the same reservation; an
  ADR 0011 permit does not exist yet. The child's checkpoints use the engine's
  `FileCheckpointStore` (thread `job:<job_id>`), not a separate store with its own retention.
* Keeping the token reservation while a run waits (the stricter side of the decision) is a
  deliberate deviation, confirmed by the commander on 2026-10-21.
