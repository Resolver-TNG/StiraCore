# ADR 0006: Durable HITL approvals and asynchronous resume

**Path:** `docs/adr/0006-hitl-persistence.md`
**Status:** Accepted
**Issue:** #240 (split ③ of #105). Related: #146 (binding, ledger), #95 (checkpoints),
#171 (hash-chained EventStore), #189 (approval queue UI), #239 (scheduler), #185 (masking).
**Decision:** An append-only, hash-chained SQLite approval store; the pending queue is a
projection of that chain. A human decision on a request restored after a restart resumes
the paused tool batch from its checkpoint (#95), and the existing gate consumes the durable
approval exactly once. Everything that cannot be verified fails closed (nothing executes).

> **Note (2026-10-16).** On 2026-10-16 the owner authorized the design and development of
> (1) an autonomous goal loop that runs only while the user's explicit grant is active, with
> an expiry (default 12 h, at most 36 h), a budget and a scope, and (2) bounded
> self-scheduling: inside an envelope a human defined, the agent may propose and create
> jobs. Both stop when the emotional state worsens. Autonomy without a grant, always-on
> event-driven operation, self-expansion of permissions and the agent changing its own
> envelope stay out of scope. The design is ADR 0011 (Proposed: `docs/adr/0011-agent-self-scheduling.md`, PR #295, tracking
> #296). This ADR is unchanged: approval is always a
> human, and nothing here approves automatically inside or outside a grant. (The one
> exception is ADR 0012's YOLO window, phase 5a: its approver answers an internal tool's Ask
> before a card exists, records it in the audit chain and never writes the approval store;
> a card that exists stays the person's — single decision, as here.)

> **Follow-up (Accepted 2026-10-21): [ADR 0024](0024-approvals-beyond-the-turn.md)** — pending
> approvals under a stricter breaker (freeze), job-origin approvals and job resume (phase 2), and
> what a replayed batch repeats. Phase 1b is scheduled, phase 2 needs its own go; until each phase
> ships this ADR describes `main` unchanged.

## Context

`StiraEngine.request_hitl` is synchronous: the tool worker blocks in
`HITLApprovalBroker.request` until the UI answers, and an unanswered request is a
rejection (#146). A process restart therefore loses every pending approval, and the
operation it guarded can only be retried by the model asking again. Long-running work and
unattended jobs (#239) need the approval to survive a restart and the work to continue
once a human decides.

Constraints that are not up for change in this ADR:

* **Approval is always a human.** No auto-approval, no environment-variable bypass, no
  "system" approver. Expiry, shutdown, cancellation and every error are rejections.
* The #146 binding stays: an approval is valid only for agent + tool + hash of the
  normalized arguments + expiry (+ the `toolUseId` it was raised for). EDIT is a new
  request that must itself be approved.
* Fully autonomous / event-driven operation is not authorized; this ADR only lets a
  human-approved, already-planned tool call continue later.

## Decisions

### 1. Storage format: an event-sourced, hash-chained table

`ApprovalStore` (`src/stiracore/core/approval_store.py`) keeps one SQLite file
(`.local/stira/hitl-approvals.db` for the chat server, owner-only `0600`, WAL,
`synchronous=FULL`):

* `approval_events(seq, ts, request_id, kind, body, prev_hash, hash)` — append only.
  `hash = sha256(prev_hash || canonical_json([seq, ts, request_id, kind, body]))`,
  genesis `prev_hash = 64 × "0"`. Kinds: `requested`, `decided`, `expired`, `cancelled`,
  `consumed`, `resume_claimed`, `resume_refused`, `binding_mismatch`.
* The state of every request (pending / approved / edited / rejected / responded /
  expired / cancelled, consumed, resume claimed) is **derived by replaying the chain**.
  Every write **and every read** (queue, history, lookups, EDIT payload) re-reads and
  re-hashes the **whole** chain and re-checks the head under the SQLite write lock
  (`BEGIN IMMEDIATE`; the store is small), so a row changed or deleted by another
  connection after it was first read is caught before anything is shown, recorded or
  granted. A store found unhealthy stays unhealthy for the life of the instance (putting
  the files back does not re-enable it; only a reopen after operator inspection does).
  There is no separate mutable state table that could disagree with the audit trail.
* A sidecar head anchor (`<db>.head`, `{seq, hash, base_seq, base_hash}`, atomic,
  `0600`) is written **before** each COMMIT, while the write lock is held, and names the
  chain tail the transaction was built on (the rename is followed by a directory
  `fsync`; a malformed or wrongly typed head is tampering). Recovery rules:
  * head == DB tail → consistent;
  * head ahead of the DB *and built on the DB tail* → a crash between head write and
    COMMIT, **or rows deleted from the end** (the two cannot be told apart). The state is
    therefore not trusted as is: every unfinished request (pending, or approved / edited
    and not consumed) is **cancelled in new events** ("a fresh human decision is
    required") and the head re-anchored on them. Whatever tail row was deleted — a
    `cancelled`, a `consumed`, a `decided` — nothing can come back usable; a human has to
    decide again. If the recovery cannot be recorded, the store is unhealthy;
  * anything else — rows the head never covered (a forged `decided` row appended with SQL
    only), an older or missing head, a head on another base — is tampering → unhealthy.
  Writing the head under the DB write lock also serializes concurrent writers, so a slow
  writer cannot overwrite a newer head.

A request records: `agent_id`, `tool_name`, `args_hash`, `tool_use_id`, `expires_at`,
`created_at`, `correlation_id`, origin (`{"kind": "turn"|"job", "id": ...}`),
`checkpoint_id`, `supersedes`, the **masked** canonical arguments for display
(`mask_secrets`, #185, capped) and whether masking changed them.

Arguments are never stored in plaintext in the chain, and free text (request reason,
decision / cancel / refusal reasons) is stored through `mask_secrets` too. Audit
identifiers (`approver`, `by`, the browser `client_id`) are limited to
`[A-Za-z0-9._:@-]`, at most 64 characters, and must pass `mask_secrets` unchanged;
anything else is recorded as `unknown` (the principal kind, e.g. `local:`, is kept).

**Secrets are never persisted.** An EDIT replacement whose `mask_secrets` rendering differs
from the original (it contains something secret-looking, including arguments the edit
added) is refused by `submit(store_payload=True)`. Such an edit is handled **live only**:
the request is recorded with `live_only` and no payload, the waiting worker can still
approve it in the same process, and after a restart it is cancelled ("not restorable")
instead of being listed. A restored request cannot be edited into one with secrets
(`409 unsupported`).

**Plaintext exception (only this):** the replacement input of an EDIT that contains no
secret. It must be executed on resume and exists nowhere else, so it is kept in
`approval_payloads` in the same `0600` file, bound by its `args_hash` in the chain (a
modified payload is refused and marks the store unhealthy). It is deleted the moment the
request is consumed, superseded by a further EDIT, or reaches a terminal state
(rejected / responded / expired / cancelled), with `PRAGMA secure_delete=ON` (the row is overwritten) followed by
`PRAGMA wal_checkpoint(TRUNCATE)`; a checkpoint blocked by another reader is logged as a
warning. Not covered: blocks the filesystem freed earlier (no encryption at rest). The
original (unedited) call is never stored here; it is in the checkpoint file (#95, `0600`).

### 2. Idempotency keys

Three independent "at most once" layers:

| Layer | Key | Guarantees |
|---|---|---|
| Decision | `request_id` | First decision wins; a second one is `already_decided` (no event). |
| Grant | `request_id` → `consumed` | An approval is handed to the gate once, and a *restored* approval only inside its own claimed resume (§3). |
| Resume | `request_id` → `resume_claimed` | A decided request triggers at most one replay. |
| Execution | #146 `operation_key(agent, tool, args_hash, toolUseId)` | The ledger never repeats a started/committed side effect. |

The correlation id is the origin turn id for chat turns and the job id for #239 jobs
(same field, `origin.kind` distinguishes them), so turns and jobs share one format.

### 3. Resume mechanism

* While the process lives, the synchronous path is unchanged: the waiting worker gets the
  decision; the store records the decision and then `consumed`.
* On shutdown, waiting workers still get a rejection (`approval closed`), but the store
  keeps the request **pending**. After a restart, `HITLApprovalBroker` lists it again with
  `restored: true`.
* An approval of a restored request is recorded and handed to a resume callback (the chat server:
  a background thread under the chat turn lock). `resume_approved_request` claims the
  resume once, loads the request's checkpoint and refuses unless it belongs to the agent,
  is the newest generation for the thread (the conversation has not moved on), and its
  pending call with that `toolUseId` has the approved tool and argument hash. It then
  calls `NXPAgent.resume_from_checkpoint` + `agent(None)` inside
  `ApprovalStore.resume_context(request_id)`: Strands replays the stored tool batch
  without a model call, `CheckpointHooks` asks for HITL again under current breaker
  policy, and the broker answers that request from the store. The broker grants a
  restored approval **only** to the execution context that is running that resume: the
  context binds a random token to the request (and its EDIT ancestors) and sets it in a
  `ContextVar`. The SDK's tool path copies context variables into its worker thread and
  asyncio tasks (verified by the end-to-end resume tests); any other thread, turn or
  request handler has no token and is asked as a new request. `consume(resumed=True)`
  re-checks token, claim, agent / tool / toolUseId / argument hash and correlation id
  atomically in the write transaction. The ledger and the binding verifier then apply
  exactly as for a live approval.
* EDIT chains (A → B → C, each edited, C approved) are followed to the newest request
  (cycle-checked, at most 16 links): A's replay is answered with C's input and the
  engine's re-review of it consumes C's approval.
* If anything after the claim raises (including the `on_replay` callback), or the replay
  returns without having used the approval (e.g. the breaker recovered and the write ran
  under normal policy), the resume is recorded as refused and the approval revoked
  (unless the gate already consumed it — then the ledger owns the outcome); nothing
  stays "claimed but unused". If the revocation itself cannot be recorded (clock
  anomaly, I/O error), the store instance is **quarantined** (unhealthy for good), so the
  claimed approval can never be lent.
* **Single process.** The store, the turn lock that serializes a resume with chat turns,
  and the newest-checkpoint check assume one server process per data directory. The chat server
  enforces it (#285): it takes `server.lock` before it opens the store and refuses to
  start without it, so "pending at start-up" really means "its worker is gone" and a
  live approval is never offered as restored by another process. SQLite serializes the
  store's writes across processes, but a resume is not excluded against an ordinary turn
  of another *program* that opens the store directly; that stays unsupported.
* **Shutdown.** Closing the broker (the chat server's `SIGTERM` stop path, #285) rejects a
  waiting worker but leaves its request pending in the store, so it is restored after
  the restart; a request that arrives after the close is rejected without being stored.
  The closed check and the start of a save happen under one lock, and `close()` waits
  (bounded, `timeout`) for saves that had passed it. When `close()` returns `True` the
  store does not change afterwards; such a request is pending like any waiting one.
  When it returns `False` (a save was still running at the deadline), that one save may
  still complete after `close()` returned — no new request is accepted either way.
* **Save window (#400).** A live request is saved to the store before its worker
  registers it in memory. Until then it is neither listed (not even as restored) nor
  decidable (`not_found`, nothing recorded), and a replay with the same call identity does
  not cancel it as a leftover. Leaving "being saved" and entering the live queue is one
  step under the broker lock, so such a request is never decided on the restored path (an
  approval there would schedule a resume of a turn that is still running). Measured
  (`tests/test_hitl_pending_race.py`, store save intercepted): before the change an
  approval in that window was accepted as restored, the live call was rejected and the
  scheduled resume ran the call after the turn had reported it as failed.
* EDIT of a restored request records `edited` and creates a new pending request
  (`supersedes`, payload = replacement input). Approving that one resumes; during the
  replay the broker answers the original call with `EDIT(replacement)` and the engine's
  re-review of the replacement with the consumed approval — the same two-step contract as
  the live path. EDIT is refused for a restored request whose displayed arguments were
  masked (the human cannot see what they would be editing). RESPOND is refused for a
  restored request (there is no live turn to answer).
* Rejection, expiry and cancellation of a restored request never resume anything.

### 4. Approver identity and the future authentication boundary

The approver is recorded as a principal string. Today the only principal is
`local:<resolver>` (the dashboard session behind `install_security`: same-origin, CSRF and
idempotency, #189), plus the browser-supplied `client_id` for audit only. `system` may
expire, cancel or reject, and is **refused** for approve/edit at the store level.

Boundary for future remote approval (not implemented here): a remote approver must be a
separately authenticated principal (`remote:<id>`) whose decision carries a signature over
`request_id + args_hash + expires_at + action`, verified before `ApprovalStore.decide`.
A transport that cannot produce that is a notification channel, not an approval channel.

### 5. Fail-closed rules

* Store unreadable, not a database, chain broken, head anchor missing/mismatched, payload
  hash mismatch → the store is marked unhealthy and stays so until an operator moves the
  file aside; the broker rejects every new request, refuses every decision
  (`store_unavailable`, HTTP 503) and lists no restored request. The file is not
  overwritten (kept for inspection).
* SQLite errors: only busy/locked (`SQLITE_BUSY`, `SQLITE_LOCKED`) is transient (the
  operation fails with `ApprovalStoreError`, nothing recorded, retry later). Any other
  database error — a dropped table, schema change, corruption, a failed COMMIT — makes
  the instance unhealthy for good, also normalized to `ApprovalStoreError` so the
  broker fails closed; restoring the table does not re-enable the instance.
* Clock anomaly: a non-finite clock, or one earlier than the newest event by more than
  2 s, refuses the write (nothing is approved). A clock jump forward only expires
  requests early.
* A missing/unloadable checkpoint, a newer checkpoint, a binding mismatch or a busy turn
  lock refuses the resume and cancels the approval (recorded in the chain).
* CRITICAL: the replay runs `pre_turn` under current policy; under CRITICAL `actuate`/exec
  tools stay removed and writes stay gated, so a durable approval cannot widen what the
  breaker allows. Re-evaluating pending approvals on a CRITICAL transition is left to
  #239 (it owns the asynchronous tick).

### 6. EventStore (#171)

#171 is not implemented. *(Update: ADR 0009 adds the shared chain and an approval mirror; this store is unchanged, its migration is stage 3 there.)* Its acceptance criteria (migration of the existing
`SQLiteEventStore`, overhead measurement, trajectory metrics) are larger than this issue.
This ADR implements the minimum tamper evidence #240 needs inside the approval store and
exposes it through `ApprovalStore.verify_chain()` and `ApprovalStore.audit_events()`;
#171 can later mirror these events into the shared chain. Limitation (documented, not
solved): an attacker with write access to both the db and the head file can rebuild a
consistent chain; signing or an external anchor belongs to #171. Deleting rows from the
end alone is handled by the recovery rule in §1 (it revokes, never grants).

## Alternatives considered

* **Mutable state table + separate audit log.** Two sources of truth; tampering with the
  state table would not be visible in the log. Rejected.
* **Strands interrupts (`event.interrupt`) + session manager.** Would persist the whole
  agent state through the SDK session layer, which the chat server does not use, and moves the
  HITL contract out of `request_hitl`. Rejected for now; the checkpoint replay path
  (#95) already exists and is tested with the ledger.
* **Storing the full arguments for display.** Rejected (#185); the display copy is
  masked, the executable copy is the checkpoint's (or the EDIT payload).
* **Waiting for #171.** Would leave the approval history unverifiable; the minimal chain
  here is self-contained and replaceable.

## Out of scope

Remote/external-channel approval UI, auto-approval of any kind, the #239 policy for
unattended jobs (permanent wait vs. immediate deny; this ADR provides the store a job can
wait on), and re-evaluating pending approvals on breaker transitions.

## Addendum (#273): a ledger restored from a backup

**Measured** (`tests/test_tool_rules_restore.py`, offline, scripted model, real SQLite
files). A turn approves and runs call `r1`, then pauses on call `r2`; the server stops.
Before the restart `hitl-operations.db` is replaced by a copy from before `r1` ran; the
approval store is the current one. Approving the restored `r2` card replays the batch from
its checkpoint:

* the replay reaches `r1` again; the ledger no longer knows it ran, and the approval that
  covered it was consumed, so the gate asks a **new** approval for `r1` (an ordinary live
  card; it does not say that the call already ran);
* approved, `r1` runs a **second time** (the external system sees the same idempotency key
  `op-…` twice, so a system that deduplicates by key can still refuse it); rejected, `r1`
  is not repeated and `r2` runs once;
* with the current ledger the replay answers `r1` with "Already applied before the
  interruption" and asks nothing (unchanged, §2/§3).

With the approval store moved aside as the restore procedure requires
([operations.md §8](../operations.md#8-backup-and-restore)), nothing is pending, nothing is
resumable and nothing runs. Restoring an older approval store **together with** an older
ledger brings back cards that were pending at backup time; approving one replays its batch
under the older ledger as above (not measured end to end: it combines the two measured
steps).

**Decision.** No code change: a rolled-back ledger never runs anything without a fresh
human decision (the duplicate needs a new approval), so the gap is in what the card tells
the human, not in the gate. The restore procedure keeps the current ledger and moves the
approval store aside (operations.md §8 steps 3–4). **Open:** a ledger high-water mark
outside the data directory (like the tool-rule anchor of ADR 0010 / #273) so that a
replayed call whose key a newer ledger had seen is refused, and a card that says "this
call may already have run".
