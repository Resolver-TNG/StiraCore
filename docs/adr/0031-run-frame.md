# ADR 0031: One run frame — the shared limits, stop causes and record of every unattended run; the trigger and the approver are options

**Path:** `docs/adr/0031-run-frame.md`
**Status:** Proposed (2026-11-15, revision 2 after an independent fact review) — **docs only: nothing in
this ADR is built.** Operator direction 2026-11-15 16:53 (the "T1" discussion): *make the modules
shared and change only the options; the person-permission side and the AI-autonomy side may stay apart
where sharing is hard.* Related: [ADR 0008](0008-scheduler.md) / [0021](0021-scheduler-job-base.md)
(scheduled jobs), [ADR 0009](0009-eventstore-chain.md) (the shared chain; §9), [ADR 0011](0011-agent-self-scheduling.md)
(grants, authority), [ADR 0012](0012-yolo-mode.md) / [0025](0025-allow-again-from-the-approval-card.md) (YOLO,
allow-again), [ADR 0013](0013-audit-event-contract.md) (audit contract), [ADR 0022](0022-trust-labels-and-taint.md)
(trust frames), [ADR 0028](0028-round-mode-loop.md) (round ledger, Q10), [ADR 0030](0030-autonomy-timeline-and-task-board.md)
(the console).

**In short.** Today there are three ways for work to happen while nobody watches (YOLO windows,
grants, scheduled jobs). They are not three alternatives; they are three **axes**: *when it starts*
(press / heartbeat / schedule), *what runs* (a goal loop / one prompt / a memory job) and *who answers
an Ask* (a person's card / a YOLO window / a refusal). All of them carry the same kind of **frame**:
limits (lifetime, count, tokens, tool scope, directory scope), **stop causes**, and a **record**. The
code builds that frame several times. This ADR names it **RunFrame**, keeps the two families that cannot
share a decision apart, and shares the genuinely common parts, **safest first**: the append-type record
(byte-compatible), then a read-only limits view, then the console. **It changes no authority decision,
no stop path and no file format.**

**日本語の要約.** 無人で動く仕組み(YOLO / grant / scheduler)は並んだ別物ではなく、「いつ始めるか」
「何を走らせるか」「承認待ちに誰が答えるか」という別々の軸だった。どれも同じ種類の**枠**(期限・回数・
トークン・道具と作業フォルダの範囲、止める原因、記録)を持つのに、コードは枠を何度も作っている。この ADR
は枠を `RunFrame`(実行枠)と名付け、「人間の許可」側と「AI の自律」側は分けたまま、共通にできる部分を
**安全な順**(追記型の記録 → 読み取り専用の限度ビュー → コンソール)で 1 つにする。権限の判断・各 run の
止め方・ファイルの形式は一切変えない。

## Context — what exists (measured on main 3a45d9ec; revision 2 corrects revision 1 after review)

| Piece | Where | Its frame |
|---|---|---|
| YOLO window | `yolo/requests.py`, `yolo/allowance.py`, `yolo/config_file.py` | `WINDOW_SEC` = 6 h fixed; scope internal / external; `max_external` default 200 (env / config; ≤ `max_auto_approvals`, default 2000), `max_exec` default 200; chat and Stage turns only (`yolo/origin.py`). Ends by expiry, a person's stop, flapping (> 3 pauses in 60 min), a sleep over 300 s, a clock anomaly, `audit_failed`, or a latch |
| Allow-again | `yolo/session_allow.py`, ADR 0025 | one tool × one target class, ≤ 2 h; never opens a window and never counts against one; answers Asks of chat / Stage turns |
| Grant | `grants/schema.py`, `grants/executor.py` | `ttl_sec`, `max_iterations`, `budget {total_tokens, per_tick_tokens}`, `scope {tools, workspace_subtree}`, `no_progress_ticks`, `verifier`, `report_files`, `heartbeat_start`; one live grant; an iteration in its own session `grant:<id>` |
| Scheduled job | `scheduler/jobs.py` | `schedule` (cron / interval / once), `layer` (gateway / agent / memory), `tools`, `Budget {max_runs_per_day, max_tokens_per_run, max_tokens_per_day}`, `unattended_approval` `deny` (default) / `wait`, `approval_wait_max_sec`, `requires_heartbeat` |
| Stop | `authority/` | permits (one per effect), an epoch that advances on every narrowing, a lock-free kill flag, latches only a person clears. **Used by grants, YOLO windows and the scheduler's `grant_tick`. Ordinary scheduled agent / memory jobs and allow-again do not use it** |
| Type tag | `authority/boundary.py` | `GRANT_KINDS = ("goal", "yolo")`; `require_kind` separates the two record kinds |
| Record | at least eight places | see below |

**Who answers an Ask today** (the table the rest of this ADR must not disturb):

| Run | Answer |
|---|---|
| chat / Stage turn | a person's card; an open YOLO window; a session allow-again |
| grant iteration started by a **press** | a person's card only |
| grant iteration started by the **heartbeat**, or with no approval broker | no card is shown; the iteration ends with `approval_timeout` (in effect a refusal) |
| scheduled agent job | `deny` (default), or `wait` for a person — only when the durable approval wait is wired |
| any run, ADR 0028 Q10 | a *model approver* is the operator's stated direction for unattended rounds; it is **not built**, and is to be built and reviewed by a person |

**The record exists in at least eight variants.** Three families:

| Family | Stores | Rule |
|---|---|---|
| **Sidecar head** (SQLite or JSONL, `<file>.head` written before the commit) | `observability/event_store.py` (`event_chain.py`), `core/approval_store.py` (ADR 0006), `scheduler/event_log.py` (ADR 0008) | `sha256(prev ‖ "\n" ‖ canonical_json(row-as-list))`; the row lists differ per store |
| **Append** (JSONL, `prev` inside the line, `O_APPEND`, rotation, sticky unhealthy) | `authority/lifecycle_events.py`, `grants/round_ledger.py` (its docstring: "limits: those of the lifecycle log") | `sha256("stiracore-…:v1:" + canonical JSON without hash)`. Lifecycle nests the payload under `event` and has no `v`; the ledger is flat with `v` and `k` and accepts older rows without `axis` |
| **Whole replace** (a file rewritten atomically) | `grants/store.py` (`grants.json`, bare list of the newest 4 records, 16 grants), `authority/latches.py` (`{"v":1,"records":…}`, 64 records), `authority/epoch_store.py` (a string-form hash, versions 1 and 2) | each has its own prefix; none rotates |

`authority/private_file.py` already shares the safe open / read / replace / append-line code. What is
duplicated is **parsing, verification, rotation and the sticky-unhealthy rule**. ADR 0009 §9 plans that
**stage 3** moves the approval store and the scheduler log onto the shared primitives (stage 2 is wiring
the audit adapters; how far it has gone is to be confirmed before this ADR is accepted). Nothing joins
the append family.

**The same limits are spelled at least seven ways:** `ttl_sec`, `WINDOW_SEC`, `max_runs_per_day`,
`max_iterations`, `max_auto_approvals`, `hold_max_sec`, `approval_wait_max_sec`, with different types,
caps and error codes, and each place validates for itself.

## What does not change (hard constraints)

1. **No authority decision changes.** Who may answer an Ask stays exactly as in the table above. YOLO
   inside a grant iteration (W7) stays unbuilt and needs its own ADR; so does any model approver
   (ADR 0028 Q10). **Changing the list of approvers needs its own ADR.**
2. **No run's stop path changes.** The frame *names* stop causes with shared codes; it does not route a run
   through `authority` that is not routed today. Bringing scheduled jobs or allow-again under `authority`
   (so that a kill would stop them) is a new authority decision and is out of scope.
3. **No file format changes.** Every existing file verifies unchanged; the same input writes the same
   bytes (golden files). Rotation, torn-tail, replaced-file and unhealthy behaviour stay as today, per store.
4. **No new way to start work and no new model call.**
5. The approval store, the YOLO state machine, the goal-iteration executor and the progress fingerprint
   are **not** merged (§4).
6. `GRANT_KINDS` and `require_kind` (the authority boundary) are **not** extended by this ADR.

## Decision (Proposed)

### 1. Vocabulary: a RunFrame, and two options

Naming: this ADR says **RunFrame** / 実行枠 everywhere. "Envelope" is taken by ADR 0011 (the range of
self-scheduling a person fixes in stage B, Open (12)); a bare "frame" is taken by ADR 0022's trust frame
(`labels.FRAME_VERSION`).

| Part | Fields (the names are the proposal; stored names are unchanged) |
|---|---|
| Limits | `lifetime_sec`, `max_runs`, `tokens_total`, `tokens_per_step`, `tools` (a set), `subtree` (a directory) |
| Stop causes | one list of **codes** (expiry, count, budget, kill, revoke, breaker, restart, latch, clock, audit failure, person's stop). It names causes; it does not change who stops what |
| Record | the allow-listed, bounded, chained log the run already writes; codes and counts only |

Two **options** vary per run: **trigger** ∈ {`press`, `heartbeat`, `schedule`} and **approver** — the set
of today's answerers in the table above. The set a family may use is fixed (§2).

### 2. Two families stay apart

| Family | What it is | Members | Options it may use |
|---|---|---|---|
| **Approval side** (人間許可) | removes the person's *answering* while the person is in the loop | YOLO window, allow-again | approver = `yolo_window` / `session_allowance`; trigger = `press` |
| **Autonomy side** (AI 自律) | runs while nobody is there, inside a frame | grant, scheduled agent / memory job | approver ∈ {`person_card`, `none→approval_timeout`, `deny`, `wait_for_person`}; trigger ∈ {`press`, `heartbeat`, `schedule`} |

`yolo_window` is **not** available on the autonomy side (constraint 1). The families share the limits
vocabulary, the stop-cause codes and the record helpers; they do not share a decision.

### 3. What becomes shared, in order of risk

**Step 1a — the append-type record (`chainlog`).** One module implements the append family once:
domain prefix, `prev` in the record, safe open (via `private_file`), `O_APPEND`, rotation, sticky
unhealthy, `verify`. The **lifecycle log** and the **round ledger** move onto it. Per-store differences
stay as small hooks, and the acceptance names them: the lifecycle payload nested under `event` with no
`v`; the ledger's flat `v` / `k` rows and its acceptance of older rows without `axis`; the ledger's
memory-only mode (2,048 rows); and the **different consequences of an unhealthy log** (lifecycle: latches
the authority service; ledger: only stops the heartbeat start). Acceptance: golden files of today's two
formats verify unchanged; identical inputs produce identical bytes; every existing fault test passes
unchanged.

**Step 1b (optional, later) — the whole-replace stores** (`grants.json`, latches, epoch). Their formats
differ more (bare list, `{"v","records"}` wrapper, a non-canonical-JSON hash); each keeps its consequence
(grants: issue refused and a live grant revoked). Only done if 1a shows a net gain.

The sidecar-head family is **not** part of step 1: it belongs to ADR 0009 stage 3 and keeps its own
recovery rules.

**Step 2 — the limits view (`run_frame()`).** A strict, immutable value type with one validator (built-in
types only, unknown keys refused, fixed refusal codes). Each existing record gets a read-only
`run_frame()` view built from its stored fields; nothing is migrated and nothing is stored under the new
names. Grant issue, YOLO window start and job save may call the shared validator for the parts they have.
Acceptance: the existing schema tests of grants, YOLO and the scheduler pass unchanged, and **every
existing error code and HTTP status stays as it is**.

**Step 3 — the console.** ADR 0030 draws the frame (lifetime, count, tokens, scope, stop causes) the same
way for a grant, a window and a job. The console is a read model; it can start, stop and approve nothing
the existing routes cannot.

Step 0 (docs only, no code): one page that puts the three side by side with this vocabulary and links the
ADRs, so the entry point is no longer scattered.

### 4. Deliberately not shared

The goal-iteration executor and the progress fingerprint (an autonomy-side loop), the approval store and
its resume rules (ADR 0006), the YOLO state machine and host channel (ADR 0012), the scheduler's cron and
catch-up logic, the sidecar-head family's recovery rules. Sharing them would merge decisions that differ on
purpose.

## Alternatives considered

* **One "autonomy engine" that absorbs YOLO.** Rejected: YOLO answers Asks *in a turn a person is in*; folding
  it into the loop that runs without a person is the shortest path to building W7 by accident.
* **Docs only (T1 option B) and stop.** Kept as step 0. It does not remove the duplicated chain code.
* **Move every store to the SQLite EventStore at once.** Rejected here: ADR 0009 §9 already stages it per
  store, with each store's recovery rule as a precondition.
* **Rename stored fields to the frame's names.** Rejected for now: it needs a migration of `grants.json`
  and a compatibility window; the read-only view in step 2 gets the benefit without it.
* **Route every run through `authority` so one kill stops all.** Rejected here as a new authority
  decision (constraint 2); worth its own ADR if the operator wants it.

## Open questions (with recommendations)

1. **(Q1) Name.** **Recommended: `RunFrame` / 実行枠**, and the view `run_frame()` (not `frame()`), because
   "envelope" (ADR 0011) and "frame" (ADR 0022) are taken.
2. **(Q2) Step 1 scope.** **Recommended: 1a only** (lifecycle log and round ledger); 1b later if 1a pays off;
   the scheduler log goes with ADR 0009 stage 3.
3. **(Q3) The frame's type tag.** **Recommended: keep it separate from the authority's `GRANT_KINDS`.**
   Adding `job` to `GRANT_KINDS` would widen the authority boundary, which this ADR does not do.
4. **(Q4) Review.** Steps 1a–2 touch the authority and approval neighbourhood. **Recommended: an independent
   Opus review of each step and the operator's go before each merge**, as for the permission layer so far.
5. **(Q5) Order against ADR 0030.** **Recommended: the console (step 3) after step 1a, in parallel with step 2**,
   so it does not grow its own spelling of the limits.
6. **(Q6) Should scheduled jobs and allow-again ever come under `authority`** (so a kill stops them)? Not decided
   here. **Recommended: a separate ADR, only if the operator wants one kill switch for every unattended run.**

## Consequences

One place states what a run may do, which causes stop it and what it leaves behind; the console, the docs
and the tests read the same words. The append-type chain code shrinks from two copies to one. The cost is
real: step 1a rewrites two security-relevant writers, so it is a refactor with byte-level proof and an
independent review, not a quick tidy. Nothing here widens what any run can do.
