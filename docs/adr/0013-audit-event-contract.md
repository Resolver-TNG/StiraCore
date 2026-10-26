# ADR 0013: Audit event contract, separated from operational observability

**Path:** `docs/adr/0013-audit-event-contract.md`
**Status:** Proposed (revision 5, after design review round 4; needs an operator decision on
the points marked **Open** before any implementation; nothing here is implemented)
**Issue:** #277. Related: #171 / ADR 0009 (hash-chained EventStore, audit adapters), #240 /
ADR 0006 (durable approvals), #239 / ADR 0008 (scheduler), #190 / ADR 0010 (tool rules),
#296 / ADR 0011 (grants, authority epoch, lifecycle events), #365 / ADR 0012 (YOLO window,
Proposed), #275 (stop reasons), #276 (single pre-execution boundary), #144
(procedural writes), #146 (approval binding, operation ledger), #350 (`/new` audit), #194
(EventStore timeline), #294 (retention), #271 (data-directory floors), #318 (separate OS
user).
**Decision (proposed):** *Audit* (who did what to which target, under which decision, with
which result) and *operational observability* (logs, turn events, latencies, counts) are two
paths with different rules. Audit rows have **one versioned, allow-listed schema** — enums,
codes, numbers, ids and keyed hashes, never free text — are never sampled, and are written to
the hash-chained `events.db` of ADR 0009. Every audited operation has an internally
generated **operation id**; its intent row returns a **receipt** that is bound to the
operation, target, arguments and policy generations, and **nothing that loosens authority
or causes a protected effect can be consumed without a receipt that verifies at the
consume point** (§2, the one contract). Every human operation that may loosen is bound to
a server-issued token and the target state the person saw, checked and applied atomically,
so a delayed or resent request is never applied as a new loosening (§2f). Every entry that can perform such an operation is
either wired to the same pre-audit gate or closed (§4a). When an audit row cannot be
written, every loosening and every protected effect is refused; a narrowing (stop, reject,
revoke) is never blocked by the audit.

## Goals and non-goals

**Goals**

* One record shape for every audited action, defined by an allow-list (fields and values),
  so no secret, user/model text or internal NeuralState value can reach an immutable row by
  accident. "Remove what looks secret" is a second layer only.
* Every actor, every call surface, every target kind and every decision are distinguishable;
  an unknown surface is recorded as `unknown`, never as ordinary chat.
* An approval *wait* and a *rejection* are different rows.
* Audit is complete (no sampling); diagnostics may be aggregated and sampled elsewhere.
* The fail-closed rule for "the audit cannot be written" is fixed per operation class.
* The #171 chain is used by the production paths, at a measured cost.
* The audit events required by ADR 0006 / 0008 / 0010 / 0011 / 0012 and #275 / #276 are
  listed in one place with one naming rule.

**Non-goals**

* SIEM export, external sending of audit rows, a hosted log service.
* The timeline UI (#194) — only its boundary is fixed here (§7).
* Replacing the approval store or the scheduler log (ADR 0009 stage 3) — decided in §6.
* Adding a permission. The permission-increasing designs (ADR 0011 goal grants, ADR 0012
  YOLO) are decided in their ADRs; this ADR fixes the audit invariants they must keep. The
  audit grants nothing: a verified receipt is a precondition for some actions, never a
  reason to allow one.
* Changing the turn-event store (`turns.db`) or what the turn views show.

## Audit vs. operational observability

| | Audit | Operational observability |
|---|---|---|
| Question it answers | Who (principal), through which surface, did what (operation) to which target, what was decided, what happened | Is it working, how fast, how often, what did the turn look like |
| Store | `events.db` (`SQLiteEventStore`, hash chain, ADR 0009) | `turns.db` (`TurnEventStore`, #286), Python logging, optional exporters (Langfuse), in-memory counters |
| Sampling | Never | Allowed (aggregation, thinning, caps) |
| Content | Allow-listed fields only (§1): enums, codes, numbers, ids, keyed hashes | Masked previews, durations, token counts, error classes (#185 masking) |
| Mutability | Append-only, tamper-evident; deletion only by the retention rule (§3) | Rotatable, deletable, may be lost |
| When writing fails | Protected and loosening actions are refused (§4) | Logged and swallowed; never blocks a turn |
| Readers | Operator inspection, `verify_chain()`, #194 timeline (audit lane) | Chat ribbon, `/observability`, `/api/turns`, Stage |

Rules that follow from the table:

* A fact that matters for both (for example "tool `file_write` was denied by the floor") is
  written **twice, by two writers**: an audit row and a turn event. Neither is derived from
  the other; the correlation ids (§1) join them. ADR 0009 "Source of truth for turn events"
  stays: no turn event is copied into `events.db`.
* Latency, token counts, queue lengths and per-tool durations are **not** audit fields
  (they would turn the chain into a metrics store and grow it without bound). The stop
  breakdown of #275 is computed from audit rows, with its coverage stated (§5a).
* No audit row is written for chat text, model output, memory content or NeuralState
  values. NeuralState / EMOTION_MATRIX / Layer 3 are not read by the audit writer.

## Decisions

### 1. Event contract (allow-list, version `audit/1`)

An audit row is one `SQLiteEventStore` event. Its `event_type` comes from a fixed registry
(§5); `message` and `usage` are always empty; `attributes` holds only the fields below. The
registry names, for every event type, its **required** and **optional** fields and the
values each may take. One function builds every row: `audit_record.build(event_type,
**fields)`.

#### 1a. Common fields (every row)

| Field | Values | Required |
|---|---|---|
| `schema` | `audit/1` (a reader refuses an unknown major version) | yes |
| `operation_id` | 32 lowercase hex (128 bits from a CSPRNG), generated by the writer side at the first row of an operation, never taken from a client, a model or a stored request | yes |
| `phase` | `decision` / `intent` / `outcome` / `transition` / `mirror` | yes |
| `event_id` | `op:<operation_id>:<phase>[:<n>]` (direct rows) or `mirror:<source>:<seq>:<hash>` (mirror rows) — deterministic, so the `UNIQUE` column of `events` refuses a second row for the same step | yes |
| `actor_kind` | `human` / `agent` / `job` / `system` | yes (`unknown` allowed only for mirror rows of legacy sources) |
| `actor` | `local:operator`, `job:<id>`, `system:<component>`, `agent:main`, `agent:subagent` (ADR 0006 principal form, `[A-Za-z0-9._:@-]{1,64}`) | yes |
| `surface` | `chat` / `job` / `webhook` / `delegate` / `acp` / `admin` / `cli` / `startup` / `system` / `unknown` (§1d) | yes |
| `incarnation_ref` | keyed hash of the process incarnation (ADR 0011 §5) | yes |
| `key_id` | id of the HMAC key used for every keyed value in the row (§3) | yes when the row has a keyed value |
| `op_token_ref`, `expected` | keyed hash of the server-issued operation token, and the target version the person saw (§2f) | when `actor_kind = human` **and** the row's operation is a loosening (`delta_class = loosening`, or `decision` = `approved` / `edited`); optional otherwise |

#### 1b. Families (required / optional per event type)

Every row also carries the common fields of §1a. "Human op" = the issued-operation binding
of §2f (`op_token_ref`, `expected`), required on every row that records a human operation
that may loosen.

| Family (types) | Required | Optional |
|---|---|---|
| `audit.approval.requested` (direct) | `request_id`, `operation` (tool), `target_kind`, `target`, `args_ref`, `policy` (§1c), `decision` = `approval_wait`, `origin_kind` (`turn` / `job`) + `origin_id` | `turn_id`, `tool_use_ref`, `supersedes` |
| `audit.approval.decided` (direct, `phase = decision`) | as `requested` + `decision` (`approved` / `edited` / `rejected` / `responded`); human op (§2f; the card's `request_id` and `pending` state are the expected state) **required for `approved` / `edited` only** — `rejected` / `responded` are narrowings and carry it only if present | `reason_code` |
| `audit.approval.expired` / `.cancelled` / `.resume_refused` / `.binding_mismatch` (direct, narrowing) | `request_id`, `decision` (`expired` / `cancelled` / `none`), `reason_code` | `operation` |
| `audit.approval.<ADR 0006 kind>` (mirror, `phase = mirror`; includes `consumed`, `resume_claimed`) | `source` = `approval`, `source_seq`, `source_hash`, `kind` | `request_id`, `mirrors_operation_id` |
| `audit.approval.source_unverified` / `.source_diverged` / `.unmatched_direct` / `.unmatched_source` (mirror checks, §2d) | `source`; `.source_diverged`: `source_seq`; `.unmatched_direct` (a direct `decided` row with no source event): `mirrors_operation_id`; `.unmatched_source` (a source event carrying a receipt with no direct row): `source_seq` | `source_seq`, `request_id` (not required: an unverifiable source cannot supply it) |
| `audit.tool.denied` (`decision`), `.started` (`intent`) | `operation`, `target_kind`, `target`, `args_ref`, `decision` (`allowed` / `denied` / `approved` / `auto_approved`), `authorizer_kind`, `policy`; `denied`: `reason_code`; `approved`: `request_id` + `decision_receipt_ref` (event id of the `decided` row); `auto_approved`: `window_id`; grant-authorized: `grant_id` | `turn_id`, `tool_use_ref`, `job_id`, `run_id` |
| `audit.tool.committed` / `.failed` / `.void` (`outcome`) | `intent_ref` (= the intent's `operation_id`), `result`; `failed` / `void`: `reason_code` | |
| `audit.procedural.requested` / `.written` / `.denied` | as `audit.tool.*`, `target_kind = path` | `reason_code` |
| `audit.policy.loosened` / `.tightened` / `.restored` (`decision` / `intent`) | `generation_from` (expected, §2f), `generation_to` (the generation the save **will** write: `generation_from + 1`, fixed under the save lock before the row, ADR 0010 "every save increments"), `delta_class` (§4b), `changed_count`; loosening: human op | `reason_code` |
| `audit.policy.applied` / `.not_applied` (`outcome`) | `intent_ref`, `generation_to` (the generation the save produced; must equal the intent's) or `reason_code` | |
| `audit.scheduler.job_created` / `job_updated` / `job_paused` / `job_resumed` (direct) | `job_id`, `job_version_from`, `job_version_to`, `delta_class`, `scope_ref` (keyed hash of the canonical tools / scope / budget / schedule), `policy`; human op **required when `actor_kind = human` and `delta_class = loosening`**; for `actor_kind = agent` / `job` (ADR 0011 stage B, creation inside a human-defined envelope) instead `grant_id` / `envelope_version` (the envelope is the human's bound decision, ADR 0011 §3, §5) | `reason_code` |
| `audit.scheduler.<run / recovery kind>` (mirror of ADR 0008 kinds) | `source` = `scheduler`, `source_seq`, `source_hash`, `kind` | `job_id`, `run_id`, `reason_code`, counts |
| `audit.grant.*` / `audit.yolo.*` (ADR 0011 / 0012) | `grant_id` or `window_id`, `generation_from`, `generation_to`, `authority_epoch`; issue / resume by a person: human op | `tier`, `cap`, `used`, `end_reason`, `destination` (Open (10)) |
| `audit.epoch.advanced` | `epoch_from`, `epoch_to`, `reason_code` | |
| `audit.authority.confirmed` (§4c) | `closure_id`, `targets` (kinds + stored versions), human op (§2f), `actor_kind = human`, `surface = cli` | |
| `audit.<family>.closed` (§4c start closure) | `closure_id`, `subject_kind` (`approval` / `job` / `rules` / `setting` / …), `count`, `reason_code` (`previous_unclean` / `previous_unknown` / `closure_open` / `marker_unreadable` / `confirm_missing`) | |
| `audit.<family>.revoke_failed` (§4c) | `subject_kind`, `subject_id`, `reason_code` (`effect_persist_failed` / `audit_only_failed`) | |
| `audit.lifecycle.<to_state>` (ADR 0011 §12) | `subject_kind`, `subject_id`, `from_state`, `to_state`, `reason_code`, `authority_epoch` | `stop_ref` (required when the transition was caused by a stop request, §5a) |
| `audit.stop.requested` / `.stopped` / `.stop_failed` / `.stop_unknown` (#275) | `subject_kind`, `subject_id`, `reason_code` (#275 enum), `stop_ref` (= `operation_id` of `requested`) | |
| `audit.admin.<operation>` | `operation`, `delta_class`, `target_kind`, `target`, `generation_from`; loosening: human op | `generation_to` (on the outcome row) |
| `audit.process.started` / `.stopped` | `started`: `previous_clean` (`true` / `false` / `unknown`); `stopped`: `clean` (bool, §4c) | |
| `audit.chain.pruned` (§3), `chain.recovered` (ADR 0009) | `first_seq`, `last_seq`, `anchor_seq`, `anchor_hash`, `count` | |
| legacy `session.*` (#350) | unchanged (registered as legacy shape) | |

Enumerations: `decision` = `approval_wait` / `approved` / `edited` / `rejected` /
`responded` / `expired` / `cancelled` / `allowed` / `denied` / `auto_approved` / `none`;
`result` = `ok` / `error` / `void` (refused at the consume point, not run) / `unknown`
(only ever derived by a reader: an intent with no outcome row); `delta_class` = `loosening`
/ `narrowing` (§4b; there is no "neutral"); `target_kind` = `tool` / `path` / `job` /
`approval` / `grant` / `window` / `session` / `setting` / `store` / `none`; `operation` =
registry tool name, admin-operation code, job kind, or `mcp:<keyed hash>` (never a dynamic
name in clear, ADR 0012 §11); `target` = id code, `<root label>:<keyed hash>` for a path
(Open (11)), or `none`; `reason_code` only from code enumerations
(`hitl_reasons.REASON_CODES`, `RESOLUTION_CODES`, ADR 0010 gate layer names, #275 stop
reasons, ADR 0011 / 0012 end reasons).

#### 1c. Policy generations (`policy`)

`policy` is one object of integers, all required on tool / procedural rows: `rules`
(ADR 0010 §6 tool-rule generation), `epoch` (ADR 0011 §5 authority epoch; `0` until ADR 0011
exists), `grant` / `window` (generation of the grant or window that authorized the call, or
absent), and `breaker` (the breaker level at the decision, as an enum). It is not only
recorded: the receipt carries it and the consume point compares it (§2b).

#### 1d. Validation — a missing required value is a failed audit

* A **required** field that is missing, outside its enumeration or shape, or that
  `mask_secrets` (#185) would change, makes `build()` raise `AuditContractError`. That is an
  audit-write failure (§4): no row is written and the operation it was for is refused (or,
  for a narrowing, the writer latches). A required id is **never** replaced by `invalid` or
  `unknown`.
* An **optional** field with a bad value is stored as the fixed word `invalid` (as ADR 0009
  §8); an optional field not in the registry entry is dropped and counted (`dropped_fields`)
  — or refused: **Open (2)**. A contract test fails CI if any production call site produces
  a dropped or `invalid` field.
* Limit (stated, as ADR 0009 §8 does for turn views): a code-shaped secret
  (`MY_PRIVATE_TOKEN_123`) passes the shape check. The contract is that callers pass only
  values taken from code constants, generated ids or keyed hashes — never user or model
  text.
* Call surfaces: `chat` (dashboard chat turn), `job` (scheduler run), `webhook` (inbound
  integration), `delegate` (subagent / delegated call), `acp` (not enabled today), `admin`
  (management API operation), `cli` (host command), `startup` (start-up parameters such as
  `STIRA_YOLO`), `system` (heartbeat tick, watchdog, recovery), `unknown`. The surface is set
  by the entry adapter that received the call (§4a); a surface not set is `unknown`, and
  `unknown` is never mapped to `chat` (contract test). For a protected or loosening
  operation `unknown` is not enough to pass: the entry is closed (§4a).

### 2. Authorization order, receipts and one-time consumption (the one contract)

**The contract.** *No state that loosens authority and no protected effect is consumed
unless the consume point holds a receipt for that operation that verifies against the
chain and against the current authority, and consumes it once.* *A human operation that may
loosen is applied only if it carries an unused server-issued token whose expected target
state is still current (§2f).* Everything below is how each
path meets it; a path that cannot meet it is closed (§4a).

#### 2a. Receipt

The intent row (or, for a stored loosening, the decision row) of an operation is written
first. `emit` returns after COMMIT (ADR 0009 §2: head before COMMIT, WAL, `synchronous=FULL`),
and the writer returns a **receipt**:

`{kind, operation_id, event_id, seq, hash, key_id, incarnation_ref, ...binding}`, where the
binding fields depend on the receipt kind and are all **required fields of the row it
names** (§1b), so a receipt can always be built from the stored row:

| Receipt kind | Issued by the row | Binding fields | Use |
|---|---|---|---|
| `call` | `audit.tool.started`, `audit.procedural.requested` | `operation`, `target`, `args_ref`, `policy` | **single use**: consumed once at the consume point (§2b) |
| `decision` | `audit.approval.decided` (`approved` / `edited`) | `request_id`, `operation`, `target`, `args_ref`, `policy.rules`, `op_token_ref` | **single use**: the approval is consumed once (ADR 0006 `consume`); the call it authorizes still gets its own `call` receipt |
| `standing` | `audit.scheduler.job_created` / `job_updated` (either direction) / `job_resumed`, `audit.grant.issued` / `.resumed` / `.narrowed`, `audit.yolo.started` / `.resumed`, `audit.policy.loosened` / `.tightened` / `.restored` | `job_id` + `job_version_to` + `scope_ref`, or `grant_id` / `window_id` + `generation_to`, or the rules' `generation_to` (fixed before the row, §1b) — every binding value is a required field of the issuing row | **referenced, not consumed**: it authorizes a state that is used repeatedly (each fire, each call in a window); it is valid exactly while the target's current version equals the bound version and becomes void when the version moves. Every use under it still needs its own `call` receipt (and ADR 0011 permit), which is single use |

* The receipt is bound by value to what will run: the binding fields are copied from the
  row as stored, not from the caller's arguments.
* A receipt is verified by reading the row with that `event_id`: it must exist at `seq`
  with `hash`, its fields must equal the receipt's, and the store must be healthy. A
  same-size rewrite of an older row is caught by the full re-hash at open and by
  `verify_chain()` (ADR 0009 §3), not by this lookup; the lookup proves the row was durably
  written by this chain.
* A receipt is held in memory by the operation that obtained it, or stored with the
  loosening it authorizes (§2c). It is never accepted from a client, a model or a tool
  argument.

#### 2b. Consume point: evidence step, then linearization step

The check is split in two, because reading the receipt row is I/O and the linearization
lock must never be held across I/O:

**(A) Evidence step — no lock held.** Read the row named by each receipt (`call`, and the
`decision` / `standing` receipt the call relies on) and check that it exists at `seq` with
`hash` under the receipt's `key_id`, and that its binding fields equal the receipt and the
call about to run (`operation` / `target` / `args_ref`). A committed row is append-only
and immutable in a healthy chain, so this result cannot become false by a later *legal*
write; a rewrite is tampering and is caught by verification (§2a). What *can* change between
(A) and (B) — writer health, generations, consumption — is checked in (B).

**(B) Linearization step — under one leaf lock held only for compare-and-set** (ADR 0011
§5's authority lock where it exists, the gate's own leaf lock until then; never across I/O,
a model call or another lock acquisition):

1. the writer's in-memory sticky health flag is healthy (an atomic read; a latch set after
   (A) refuses here);
2. the evidence of (A) is present for exactly these receipts;
3. **current** generations equal the receipts' bindings: tool rules generation (ADR 0010 §6
   — any save, including Ask → Never → Ask, voids it), authority epoch (ADR 0011 §5),
   grant / window generation (ADR 0011 §5, ADR 0012 §6a), the job's `job_version` for a
   `standing` job receipt, and the breaker level is not stricter than at the decision (ADR
   0010 §6);
4. the `call` receipt's incarnation equals the current one (a `call` receipt never crosses a
   restart; a `decision` receipt from an earlier process is accepted only under §2c / §4c);
5. the `call` receipt's `operation_id` is not in the consumed set; it is added, and the
   effect is registered **in flight**, in the same step. A `decision` receipt's single use
   is ADR 0006's `consume` (token, claim, agent / tool / `toolUseId` / argument hash /
   correlation id, atomically in the approval store's transaction), done before (B) because
   it is I/O. If (B) then refuses, the approval stays consumed and the call goes back to a
   person (the safe direction: a used approval is never handed out twice).

Any failure → the call is not run (`void`, reason `permit_stale` / `receipt_invalid` /
`audit_unhealthy`), `audit.tool.void` is written best effort, and the call goes back to a
person where the path has one (as ADR 0012 §6a). A narrowing linearized before step 5
refuses the effect; one after it finds the effect in flight (ADR 0011 §5).

**Lock order (one order for every path).** target lock (the store or save lock of the thing
being changed: tool-rules save lock, scheduler lock, approval broker lock) → `events.db`
write transaction (`BEGIN IMMEDIATE`, inside `emit`) → leaf lock (compare-and-set only). A
path never acquires a target lock or starts an `events.db` transaction while holding the
leaf lock, and never holds two target locks. The approval store's transaction and the
`events.db` transaction are never nested: the decision row is committed first, then the
approval-store transaction runs (§2c). This matches ADR 0011 §5's "scheduler lock →
authority lock".

**Who enforces it (boundary).** ADR 0013 defines the receipts and the checks (A) and (B). They run
at the single pre-execution boundary of #276; until #276 lands, at the chat turn's final
BeforeToolCall hook (`ApprovalBindingVerifier`, which already re-checks the rule
generation, ADR 0010 §6), with the intent row written by the hook immediately before it, so
a narrowing during the intent write is caught by the re-check. These references are
**preconditions**, not background: ADR 0006 §3 single-use `consume`, ADR 0010 §6
generation binding, ADR 0011 §5 permit / `consume` / in-flight set, ADR 0012 §6a
reservation and generation re-check. An implementation that lacks one of them for a path
must close that path (§4a), not rely on the audit row.

#### 2c. Two-step loosenings: stored now, consumed later

A loosening that is **stored** and used later by another actor (an approval resumed by
another worker or after a restart, a job created or resumed and fired later, a grant, a
YOLO window) follows audit-first **and** stores the receipt with the loosening. Step 0 is
the human-operation check of §2f, under the same target lock as steps 1–2.

| Step | Approval (ADR 0006) | Job create / update / resume (ADR 0008) |
|---|---|---|
| 0 | under the broker lock: §2f — the card's token is unused, the request is `pending`, the request's generation is the one the card showed | under the scheduler lock: §2f — token unused, `job_version` and state equal the `expected` the form showed |
| 1 | still under the broker lock: write `audit.approval.decided` → `decision` receipt; failure → refused (`audit_failed`), the request stays pending | still under the scheduler lock: write `audit.scheduler.job_*` (with `delta_class`, `job_version_from` / `_to`) → `standing` receipt; failure → refused, nothing stored |
| 2 | `ApprovalStore.decide(..., audit_receipt, op_token_ref)` in its own transaction (not nested in step 1's), re-checking `pending` atomically: the `decided` event carries both | the `job_*` line carries the receipt, the `operation_id` and the new `job_version` |
| 3 | live or restored resume: `consume` requires the `decided` receipt to verify ((A) of §2b) and, for a receipt from an earlier process, §4c's start condition | every fire: the `standing` receipt verifies and binds the job's **current** `job_version`; the fire's own effects each get a single-use `call` receipt |

**Every version change issues the receipt for the new version, narrowings included — in
the narrowing order of §4.** A narrowing update of an active job (smaller scope or budget,
rarer schedule) runs: (1) linearize the narrowing first, in memory under the leaf lock — the
old version stops being usable at once (its `standing` receipt is void because the version
moved), nothing waits for I/O; (2) persist the new version; (3) write
`audit.scheduler.job_updated` (`delta_class = narrowing`), whose `standing` receipt binds the
new version; (4) only then may the job fire under the new version. So a narrowed job keeps
running once (2)–(3) succeed, and the narrowing is never delayed by the audit. If the row
cannot be written, the narrowing is still applied (§4), the writer latches, and the job is
additionally left **paused** (`reason_code = audit_failed`): it does not fire under a version
that has no receipt, also after a restart; a person resumes it with a token (§2f), which
writes the receipt. The same rule holds for rules (`.tightened`) and grants (`.narrowed`).
A narrowing therefore never makes a target permanently unusable while the audit works, and
never leaves it usable without a receipt when the audit does not.

Crash and failure cases (each is an acceptance test, "Phases"):

* **Crash after step 1, before step 2:** an audit `decided` row exists, the approval store
  has no decision. The request is still pending; nothing is consumable. At open the mirror
  (§2d) reports `audit.approval.unmatched_direct`. The token died with the process (§2f), so the
  person decides again with a new card.
* **Crash after step 2, before the resume:** the decision and its receipt are durable. After
  the restart the resume may run only if the receipt verifies against the reopened chain
  **and** §4c's start condition holds; otherwise the resume is refused and the approval
  revoked (ADR 0006 §3 "refused and revoked"; if the revocation cannot be recorded, the store
  is quarantined).
* **Approval-store COMMIT fails after step 1:** the audit row is a decision without a
  matching store event (`unmatched_direct`); nothing is approved.
* **A decided event without a receipt** (written before this ADR is deployed, or by a
  writer that bypassed the broker): not consumable. At the first start of the version that
  deploys this, every approved-but-unconsumed request without a receipt is cancelled in new
  events ("a fresh human decision is required", ADR 0006 recovery wording); active jobs
  whose newest loosening has no receipt are paused (§6).
* **Delayed or replayed human operation:** §2f.

**One-step loosenings** (a tool-rules save, `unlatch`, an admin setting, `stira memory
restore`) are their own effect, all under the target lock: §2f check → decision / intent row
(with `generation_from` = expected) → the save, which **records the `operation_id` in the
target store** (tool-rules file and head, scheduler line, settings entry) → outcome row
`audit.policy.applied` with `generation_to` (or `.not_applied` with a reason). Whether a past
change was applied is answered by the target store's record of that `operation_id`, not by
comparing generations later (a later save would hide it). If the outcome row cannot be
written the writer latches (§4); the target store's record still answers the question.

#### 2d. Wiring the #171 chain and mirrors

ADR 0009 stage 2. One `AuditWriter` (the `AuditChain` adapter extended with §1) per the chat server
process, on the `events.db` that already holds the `/new` rows (#350) — **Open (1)**
compares a separate file.

* **Direct rows** are written by the paths themselves (approval broker, the boundary of
  §2b, procedural gate, scheduler API, config API, `/new`, host commands) with their own
  `operation_id`.
* **Mirror rows** (`mirror_approval_store()`, ADR 0009 §8; a scheduler-log mirror) copy the
  source stores' events after their `verify_chain()` succeeds, with deterministic
  `mirror:<source>:<seq>:<hash>` ids (idempotent, `UNIQUE`). A mirror row names
  `mirrors_operation_id` when the source event carries a receipt, so readers and the stop
  breakdown count the direct row once and use the mirror only to detect a source event
  that has no direct row (`unmatched_source`), a direct row with no source event
  (`unmatched_direct`), or a rewritten source (`source_diverged`). Mirrors
  run after each source write and at open, not on the heartbeat tick (the heartbeat is off
  by default, ADR 0008) — **Open (6)**. A mirror is evidence; it never produces or replaces
  a receipt.

#### 2e. Cost

Measured (ADR 0009 §6, `docs/research/eventstore-chain-overhead-2026-10-15.md`): ≈0.33 ms
extra per event over an unchained insert (≈480–494 µs per chained event with the
identifier checks), ≈0.26–0.3 ms of it the durable head; verification ≈9 µs per stored
event; open re-hashes every row (≈0.9 s per 100k rows); migration ≈31–80 µs per legacy row.

**Not measured yet** (the per-row figure does not cover them): rows per path, the mirror's
source verification (ADR 0006 re-hashes the whole approval chain on every read, so a mirror
pass grows with that store), HMAC per keyed field, the receipt lookup, and contention
between writers (`BEGIN IMMEDIATE` serializes them). Expected rows per path (counted from §1b,
not measured): denied call 1; Always-rule call 2 (intent, outcome); live approval 4 direct
(requested, decided, intent, outcome — the approval's consumption is attested directly by the
intent row's `request_id` + `decision_receipt_ref`, and recorded by the approval store) + 3
mirror rows (requested, decided, consumed); a restored resume adds the mirror rows
`resume_claimed` and `consumed` and its own intent / outcome; a one-step loosening 2
(decision, `applied`);
YOLO automatic approval 3 (auto_approved, intent, outcome) + the permit's `consumed` /
`void` row (ADR 0012 §6a).

**Measurement plan (part of phase 3, "Phases"):** `docs/research/audit-wiring-cost-<date>.md`
measures, on the reference machine, per path: rows written, added latency (median / p95)
per tool call and per turn, with 1 and 4 concurrent writers, the mirror pass at approval
stores of 1k / 10k events, HMAC and receipt-lookup time, and open time at 100k / 1M audit
rows. The acceptance threshold is **Open (13)**. Batching (one head per batch) is not used
for intent or decision rows: they must be durable before the consume point.

#### 2f. Human operations: issued token, expected state, atomic staleness check

A receipt proves that an operation was recorded before it ran; it does not prove that the
person still meant it. A delayed or resent request ("set `file_write` to Ask", sent before a
later change to Never) would otherwise be recorded as a *new* loosening against the current
state and pass §2b, because its receipt is fresh. So **every human operation that may loosen**
— approval decisions, tool-rules saves, admin settings, job create / update / resume, grant
issue / resume, `unlatch`, `stira memory restore`, restore of a backup — is bound to what the
person saw:

* **Server-issued operation token.** The server issues `op_token` (128 bits, CSPRNG) when it
  hands out the state a person acts on: the approval card, the settings / tool-rules form,
  the job's page, the grant form. The token is stored **in memory only**, bound to
  `(incarnation, target_kind, target, expected)`, where `expected` is the target's version at
  issue time (tool-rules generation, `job_version` + state, grant generation, approval
  `request_id` + `pending`, marker id for `unlatch`). A host command issues and uses its token
  inside its own run: it reads the state and the version under the target lock, shows it,
  takes the typed confirmation, and applies only if the version is unchanged (ADR 0012's
  `unlatch --latch-id` is this pattern).
* **Atomic staleness check and apply.** Under the target lock (§2b lock order): the token
  exists, is unused, belongs to this incarnation and target, and the target's **current**
  version equals `expected`; then the token is marked used, the decision / intent row is
  written, the change is applied (§2c). Any mismatch → refused `stale_operation` (HTTP 409)
  with the current state and a **new** token; nothing is applied, no loosening row is written
  (a `denied` row is written best effort). The class (§4b) is computed **at apply time**
  against the current state, not at issue time.
* **Replay.** A resend of a used token returns the recorded result of its first application
  (`operation_id`, outcome) and changes nothing; once the in-memory result has been evicted, a
  resend is refused (`stale_operation`) — never applied as new. Idempotency keys (#185 /
  ADR 0006 §2) stay in place for transport retries; the token is what makes a late request
  stale.
* **A new human operation** is one with a new token, which can only be obtained by fetching
  the current state. A person who sees "Never" and sets "Ask" again issues a legitimate new
  loosening; a request carrying a token issued before the "Never" cannot.
* **Restart.** Tokens never survive a restart (the incarnation changes, ADR 0011 §5). Every
  request carrying a pre-restart token is refused (`stale_operation`); the person reloads.
  Stored effects of operations that were applied before the restart keep their own receipts
  (§2c) and are subject to §4c's start condition.
* **Who needs a token.** Required exactly when `actor_kind = human` and the operation is a
  loosening by §4b at apply time. Agent- or job-initiated changes that ADR 0011 allows (job
  creation inside a human-defined envelope, stage B) are bound to the envelope version and the
  ADR 0011 permit instead, not to a human token; they stay inside what the person already
  decided.
* **Narrowing operations** (stop, pause, reject, revoke, tighten) do not need a token: a late
  narrowing narrows. A mixed request (tightens one tool, loosens another) is a loosening (§4b)
  and needs one.
* The row records `op_token_ref` (keyed hash of the token) and `expected`; the token itself is
  never written.

Example (the review's case): token *t1* issued at rules generation 7; a person tightens to
Never with *t2* (generation 8); the delayed "Ask" request with *t1* arrives: `expected` 7 ≠
current 8 → `stale_operation`, nothing applied. A successful job resume with *t3* (version 4
→ 5), then a pause (→ 6), then a resend of *t3*: *t3* is used → the first result is returned,
the job stays paused.

### 3. Retention, rotation, masking

**Retention.** Audit rows are kept until a person removes them; there is no automatic
deletion by default. The chain stays verifiable because removal uses ADR 0009's anchor rule:

* `clear()` already moves the anchor to the last deleted row. The prefix-retention call ADR
  0009 §6 names as the follow-up (`prune_before(seq)`) deletes the oldest rows and moves the
  anchor to the last deleted one in the same transaction, then appends `audit.chain.pruned`
  with `first_seq`, `last_seq`, `anchor_seq`, `anchor_hash`, `count` (§1b). A deletion done
  any other way is tampering (ADR 0009 §3).
* A receipt whose row was pruned no longer verifies, so pruning refuses a range that holds
  the receipt of an unconsumed stored loosening (an approved-but-unconsumed request, an
  active job's newest loosening transition, an active grant or window).
* Who prunes: a host command (`stira audit prune --before <date> --confirm PRUNE`, refused
  while a server holds the owner lock, #285), never the agent, a job or an API (`surface`
  `cli`, `actor_kind` `human`).
* Bound: a size / row-count warning in `chain_status()` and `stira doctor` (open time is
  ≈0.9 s per 100k rows, §2e). Whether pruning may also run automatically past a configured
  age is **Open (8)**; #294 owns the retention table of all stores and lists this one.
* Rotation into sealed segment files is an alternative (below), not the default.

**Masking and keys.**

* Allow-list first (§1), `mask_secrets` second (a required value it would change is a failed
  audit, §1d).
* Keyed hashes (`args_ref`, `tool_use_ref`, `incarnation_ref`, `mcp:` names, path targets)
  use HMAC-SHA-256 with one key per data directory, `0600`, on the agent's read and write
  floor (#271 / #312), created at first use, never logged, never in a row. Every row with a
  keyed value carries `key_id`. Key rotation creates a new `key_id`; old rows stay
  verifiable as a chain, their keyed values become unmatchable, and a receipt verifies only
  under its own `key_id`, so rotation is refused while an unconsumed stored loosening
  exists. Without the key a
  low-entropy argument (a short path, a yes/no) cannot be confirmed by guessing; a person
  with the key can test a suspected value. The #146 unkeyed `args_hash` instead: **Open (3)**.
* No field holds a NeuralState value, an emotion label, a stimulus text or a memory key.

### 4. Fail-closed when the audit cannot be written

"Cannot be written" = `build()` raised (§1d), the `AuditWriter` raised (`EventChainError`
from an unhealthy store, a non-transient SQLite error, a failed head write), or the store is
pending / busy (transient, ADR 0009 §4). Health is one call (`audit_ready()`, the one `/new`
uses today): `False` for a sticky failure, which lasts for the process lifetime (putting
files back does not re-enable it; a restart after inspection does).

| Operation class (by §4b) | Examples | Order | Audit write fails |
|---|---|---|---|
| **Loosening, one step** | tool-rules loosening (ADR 0010 §4), admin setting that widens, `unlatch`, restore of an older rules file | §2f staleness check → row → effect (target records `operation_id`) → `applied` row | refused, no effect (`audit_failed`) |
| **Loosening, stored** (§2c) | approve / edit an approval card; job create / resume / widening update (ADR 0008); grant issue / resume, envelope raise (ADR 0011); YOLO start / resume (ADR 0012 §6a) | §2f staleness check → decision row → receipt stored with the loosening → consume verifies it | refused, nothing stored; a stored loosening whose receipt does not verify is not consumable |
| **Protected effect** | file / editor write, procedural write (#144), memory delete / forgetting apply / restore, exec / actuate (when enabled), external send, `/new` | intent row → consume (§2b) → effect → outcome row | intent fails → refused, no effect (`audit_unavailable`, 503 on HTTP); outcome fails → the effect has happened, the writer latches (**Open (4)**), the intent reads as `unknown` |
| **Narrowing** | stop, pause, reject, cancel, revoke, expire, breaker escalation, rules tightening, recovery that cancels / pauses | effect first (never waits for I/O), then a best-effort row | effect stays; the writer latches; never undone (ADR 0012 §6a). See §4c for a failed *persistence* of the narrowing |
| **Read / talk** | chat reply without tools, read-only tools, views | no audit row required | unaffected |

Consequences:

* **Audit failure states (one table).**

  | Failure | Loosening / protected operation | Narrowing (already applied) | Writer | Clean stop / coverage |
  |---|---|---|---|---|
  | busy (transient) | refused, retry later | stays; row retried best effort | stays healthy | `audit_gap` set → no clean stop; coverage not `complete` |
  | write failed (non-transient) or `build()` refused | refused | stays | **latched** (read-and-talk) | no clean stop |
  | store unhealthy at open | refused | stays | latched from the start | no clean stop |
  | narrowing persistence failed (§4c) | refused (target unhealthy) | in memory only | unchanged | no clean stop → closure next start |

* A latched writer puts the process into a **read-and-talk state**: the character keeps
  talking and reading; every loosening and protected effect is refused with one code. The UI
  shows one banner (the existing `audit_unavailable` text: `監査ログに書けません` / `The
  audit log cannot be written`) and the chain status.
* Busy is transient (ADR 0006 / 0009): a loosening or protected operation is refused and
  nothing latches. A **narrowing** has already happened, so it cannot be refused: a narrowing
  row lost to busy (or to any other failure) sets the in-memory **`audit_gap`** flag, the row
  is retried best effort, and while `audit_gap` is set no clean stop is written (§4c), so the
  run's coverage is never `complete` (§5a).
* A contract test injects a failing writer into every entry of §4a and asserts *no effect*
  (loosening, protected) or *effect + latch* (narrowing).

#### 4a. Every entry is gated or closed (release condition)

An entry is any code path that can start an operation of the first three classes: the chat
turn's tool calls, the approval resume path, scheduler runs, the management API
(`config_api`, `tool_config_api`, `scheduler_api`, `memory_api`, `breaker_api`, `/new`),
host commands (`stira memory restore`, forgetting apply, `stira tool-rules`, `stira yolo
unlatch`), start-up parameters (`STIRA_YOLO`), inbound webhooks, subagents / delegation,
ACP, and inner-agent tools (`think`, `use_agent`, …, exec class).

* The entries are listed in **one registry in code** (the #276 entry table; until #276, a
  module-level table in the audit package). Each entry is either **`gated`** — it calls the
  same pre-audit gate (§2b / §2c) and has a failing-writer test — or **`closed`** — it
  refuses every operation of the first three classes with `audit_path_unwired` while it is
  closed. A CI test fails for an entry with neither state, and for a module that reaches a
  protected tool, a store write or a settings save without going through a registered
  entry (#276 "an entry that does not use the common decision cannot be registered").
* **Release condition for every phase ("Phases"):** a phase that turns on enforcement for an
  operation class ships only when every entry that can perform that class is `gated` or
  `closed`. "Listed as a gap" in `docs/tool-execution-paths.md` is documentation, not a
  state; it does not satisfy the condition.
* Entries that are off by default or not implemented (inbound webhook, ACP, delegation with
  write / exec tools, exec class tools) are `closed` by construction and must be `gated`
  before they can be enabled.
* How today's working entries reach the condition (wire all in one phase, or close some for
  a while) is **Open (12)**.

#### 4b. One loosening classifier

The class of an operation is the output of **one function**, not of the endpoint:
`authority_delta(before, after) -> loosening | narrowing`, over the effective authority
(tool rules, write roots, breaker policy, approvals, jobs and their scope / budget /
frequency, grants and envelopes, YOLO windows). It composes the judgements that already
exist — ADR 0010 `rules_loosening` (tool rules), ADR 0012 §2 "one loosening judgement",
ADR 0011 envelope comparison — and adds the rule: **anything not shown to be a narrowing is
a loosening** (unknown, mixed, unparsable → loosening).

* Restore, rollback and recovery go through it: a recovery that cancels or pauses (ADR 0006
  / 0008) is a narrowing; a restore of an older tool-rules file or approvals from a backup
  (#273), `stira memory restore`, or an unlatch is a loosening; a job update is a loosening
  if any of tools, scope, budget, frequency or catch-up widens, else a narrowing.
* Setting a value back to an older one is classified like any other change (Ask → Never →
  Ask is a narrowing then a loosening); approvals bound to the old generation are already
  void (ADR 0010 §6) and do not come back.
* `delta_class` on the row is this function's output; a contract test feeds every entry's
  changes through it.

#### 4c. Revocation: effect vs persistence, and the start condition

* **Narrowing applied and persisted, audit row failed** (`audit_only_failed`): the narrowing
  stays; the writer latches (read-and-talk).
* **Narrowing applied in memory, its persistence failed** (`effect_persist_failed`: approval
  store, scheduler log, epoch store, rules file): in this process the target is kept
  **unusable** — the owning store is marked unhealthy (ADR 0006 §5 / §3 quarantine; ADR 0008
  §9 log unavailable, scheduler stops; ADR 0011 §5 stage latched `unhealthy`), so nothing can
  consume it. A best-effort `audit.<family>.revoke_failed` row is attempted.
* **Across a restart.** The disk may then hold only the healthy *old* state (a valid approved
  request, an active job, the looser rules file, a wider setting): the store's own chain
  recovery does not see a narrowing that was never written. Grants and YOLO windows never
  survive a start (ADR 0011 §6 "every start revokes every grant"; ADR 0012 §11 window never
  read back, marker latch), so they need nothing more. Every other **persistent authority
  target** gets a closure:

  | Persistent authority target | What survives on disk | Closure at an unclean / unknown start (Open (14) (a), recommended; (b) / (c) narrow which rows are applied) |
  |---|---|---|
  | Approvals (ADR 0006) | approved-but-unconsumed requests | cancelled in new events ("a fresh human decision is required") |
  | Jobs (ADR 0008) | active jobs, their scope / budget / schedule | every active job paused (`system`, as ADR 0008's recovery) |
  | Tool rules (ADR 0010) | `tool-config.json` + head | loaded **locked** (every tool Never, as the #273 anchor lock) until the confirmation below |
  | **Authority settings** — every stored setting that §4b can classify as a loosening when it changes (today: `write_roots`, `allow_broad_write_roots`, and any later setting that exposes a tool class, an integration, an outbound destination or an MCP server) | the settings file | each loaded at its **safe value** — the shipped default, which project rules keep OFF / narrow (`write_roots = ["@workspace"]`, `allow_broad_write_roots = false`) — as an in-memory override; the file is kept for inspection, not rewritten |
  | Any future store that holds authority | — | must name its closure here before it can be added (contract test: every store the classifier knows has a closure entry) |

  * A setting is an *authority setting* by the §4b classifier, not by a list kept by hand: a
    setting for which `authority_delta` can return `loosening` must have a safe value, or the
    registry test fails. Settings with no authority effect (theme, language) are untouched.
  * **The closure is persistent until a person lifts it (closure marker).** A closure is not
    an in-memory state that a later clean stop can drop. Before the start serves anything it
    writes `authority-closure.json` in the data directory (read and write floor, #271 / #312;
    `0600`, temp file + fsync + rename + directory fsync) with `{closure_id (128-bit CSPRNG),
    state: closed, targets: [{kind, id or "all", stored_version}], incarnation_ref}`, then the
    `audit.<family>.closed` rows naming the `closure_id`. The marker is the durable form of the
    closure: **as long as it is `closed`, every start applies the closure again, whether or not
    the previous stop was clean** — a clean-stop record never lifts a closure.
  * **Start condition (complete form).** The closure of every target is applied before serving
    when **any** of these holds: (i) the previous incarnation has no clean record or that cannot
    be known; (ii) the marker exists with `state: closed`; (iii) the marker is unreadable,
    malformed, a symlink, has a foreign owner or a second link; (iv) the chain has an
    `audit.<family>.closed` row whose `closure_id` has no later `audit.authority.confirmed` row
    (this also catches a deleted marker). A new closure keeps the existing `closure_id` when the
    marker is still `closed` (one closure, one confirmation).
  * **Lifting it: `stira authority confirm` only.** A host command run by a person:
    `stira authority confirm --closure-id <id> --confirm CONFIRM` (typed word). It is refused
    while a server holds the owner lock (#285). Under the marker's lock it reads the marker,
    compares the id, shows the stored values next to the safe values for every target, takes
    the typed confirmation, writes `audit.authority.confirmed` (`closure_id`, targets,
    `actor_kind = human`, `surface = cli`, op token per §2f bound to the marker's id and the
    targets' `stored_version`) **first**, then replaces the marker by rename with `state:
    confirmed`. A stored version that changed since the marker was written, an old id, a missing
    audit row or a busy lock → refused, nothing changes. The settings page shows the closure
    and the command; it cannot lift it (no API path lifts a closure, §4a `closed` entry). A
    person may instead change individual values with token-bound saves (§2f); the closure still
    stays until the command runs.
  * **Clean-stop record.** `audit.process.stopped` with `clean = true` is written at shutdown
    only if the process had no `effect_persist_failed` narrowing, no `audit_gap` (§4), and the
    writer never latched. A crash, a kill, a latched writer, a lost narrowing row or a failed
    narrowing leaves no clean record.
  * **Start condition.** At start, if the previous incarnation has no clean record (or
    `events.db` cannot be read or verified, so it cannot be known), the start applies the
    closure of **every** row of the table before serving anything, and
    `audit.process.started` records `previous_clean = false` / `unknown` plus one
    `audit.<family>.closed` row per target (best effort after the closure itself).
  * **No closure, no service.** If any closure cannot be applied (an approval cannot be
    cancelled, a job cannot be paused, the safe-value override cannot be built, the rules
    cannot be loaded locked) **or the closure marker cannot be written or read back**, the
    server **does not start serving**: no chat, no API, no
    scheduler, no heartbeat; it exits with `start_closure_failed` and `stira doctor` names the
    target. A partial start with one target still open is not allowed.
  * A `decision` receipt from an earlier process (§2c step 3) is therefore usable only after a
    clean previous stop **and** with no closure in force (marker absent or `confirmed`, no
    unconfirmed `closed` row). Deleting the clean record can only cause the closure (safe
    direction); forging it needs a consistent chain rewrite (ADR 0009 §2 limit, Open (9)).
* A narrowing is never retried in a way that could re-open the target, and never waits for
  the audit.
* Acceptance test, multi-restart: failure → `kill -9` → closed start (P2) → **clean stop
  without confirmation** → start (P3): every target is still closed (marker `closed`); the same
  with the marker deleted (chain row without `confirmed` → closed) and with the marker made
  unreadable; after `stira authority confirm`, the next start serves the stored values; a
  confirmation with an old id or after a stored version changed is refused.
* Acceptance test, **for every row of the table** (approvals, jobs, tool rules, each authority
  setting): inject a persistence failure into the narrowing, `kill -9` immediately, restart →
  the target is not usable at its old, wider value (approval not resumed, job not fired,
  rules not looser than the narrowing asked, `write_roots` at its safe value) and
  `previous_clean = false` is recorded; with a closure made to fail, the server does not
  serve. The same with a narrowing row lost to busy (`audit_gap`).

### 5. Events other ADRs require

One registry, one naming rule: new types are `audit.<domain>.<action>`; the `/new` types
(`session.*`) are kept unchanged as registered legacy names (rows already chained cannot be
renamed) — **Open (7)**. ADR 0012 writes `yolo.*`; under this rule they become
`audit.yolo.*` (see "Changes needed elsewhere"). Fields per family: §1b.

| Source | Required rows | Class / notes |
|---|---|---|
| ADR 0006 / #240 approvals | `audit.approval.requested` (`approval_wait`), `.decided` (`approved` / `edited` / `rejected` / `responded`), `.expired`, `.cancelled`, `.consumed`, `.resume_claimed`, `.resume_refused`, `.binding_mismatch`; mirror `source_unverified` / `source_diverged` / `unmatched_direct` / `unmatched_source` | approve / edit = stored loosening (§2c); the rest narrowing. Wait and rejection are different rows. The approval store stays the source of truth |
| #146 / ADR 0010 tool gate | `audit.tool.denied` (`reason_code` = deciding layer: rule / breaker / floor / binding), `.started`, `.committed`, `.failed`, `.void` | protected effect; `authorizer_kind` = `human` / `rule` / `yolo` / `grant` |
| #144 procedural | `audit.procedural.requested` / `.written` / `.denied` | protected effect |
| ADR 0010 tool rules | `audit.policy.loosened` / `.tightened` / `.restored`, outcome `.applied` / `.not_applied` (generations from / to, changed count; no values) | §4b decides the type; today `settings-history.jsonl` (unchained, masked values) — §6 |
| ADR 0008 / #239 scheduler | direct: `audit.scheduler.job_created` / `job_updated` / `job_paused` / `job_resumed` (with `delta_class`); mirror of the log's run kinds: `run_started` / `run_completed` / `run_failed` / `run_cancelled(*)` / `run_skipped(*)` / `run_missed` / `run_held` / `run_awaiting_approval`, `recovery_started` / `recovery_completed` | job create / resume / widening update = stored loosening (§2c); the log's masked failure summaries are not copied |
| ADR 0011 / #296 | `audit.lifecycle.<to_state>`; `audit.grant.issued` / `.narrowed` / `.resumed` / `.revoked` / `.expired`; `audit.epoch.advanced` | issue / resume = stored loosening; ADR 0011 §12 does not name the store; this ADR proposes `events.db` |
| ADR 0012 / #365 | `audit.yolo.started` / `.paused` / `.resumed` / `.ended` / `.auto_approved` / `.consumed` / `.void` | start / resume / each automatic approval = loosening rows written before the effect (ADR 0012 §6a already orders them so); destination per Open (10) — decided (b), keyed hash only (ADR 0012 rev. 8). **Phase 7 (ADR 0012 §9 option C):** `requested` / `confirmed` / `started` carry `exec` (bool; true only with tier external), `started` also `exec_cap`; `ended` carries `exec_used`; every `auto_approved` row carries `tool_class` (`internal` / `external` / `exec`) and an exec row `exec_slot` (1-based) and `exec_limit` — the command only as the keyed `args_ref`, never in clear (`yolo/audit_schema.py`). **Phase 7c (PR #444):** `audit.yolo.settings_saved` (`decision`, loosening, `actor_kind = human`, `surface = admin`) — a person's confirmed save on the settings page that loosens the `yolo` section of `gateway-config.json`, written **before** the file; a row that cannot be written refuses the save (§4). Required `fields` (the loosened field names) and `changed` (every changed field name), sorted names from the section's fixed list, **never a value**; no window: `window_id` is the zero id, generations 0 → 0. A narrowing save writes no row. Not copied into `settings-history.jsonl` (one record) |
| #275 stops | `audit.stop.requested` / `.stopped` / `.stop_failed` / `.stop_unknown`, `reason_code` from #275's enum (`user_stop`, `approval_wait`, `approval_expired`, `budget_exhausted`, `deadline`, `stalled`, `breaker_critical`, `error`, `unknown`) | narrowing; `approval_wait` is a hold, not a stop |
| #276 boundary | the `audit.tool.*` rows, `surface` from the entry (§4a); admin-API changes as `audit.admin.<operation>` | same operation from two entries → same `operation`, different `surface` |
| #350 `/new` | `session.reset` / `.started` / `.reset_refused` / `.reset_failed` | wired today, audit-first |
| Revocation failures (§4c) | `audit.<family>.revoke_failed` (`effect_persist_failed` / `audit_only_failed`) | best effort; the clean-stop rule covers its loss |
| Process / chain | `audit.process.started` / `.stopped`, `chain.recovered` (ADR 0009), `audit.chain.pruned` (§3) | coverage for §5a, start condition of §4c |

#### 5a. Stop breakdown (pure function) and its coverage

`stop_breakdown(rows, recovery_table) -> {surface: {reason_code: {"recoverable": n,
"terminal": n, "open": n}}, "coverage": {...}}`.

* **Unit:** one stop episode. Its key is the `stop_ref` (the `operation_id` of
  `audit.stop.requested`); a lifecycle transition **caused by** that request carries the same
  `stop_ref` (required, §1b) and joins that episode. A lifecycle stop with no request (expiry,
  breaker halt, `heartbeat_stale`) is its own episode keyed by ADR 0011's dedupe key `(subject,
  incarnation, epoch)`. Each episode counts once, by its terminal row (`stopped` /
  `stop_failed` / `stop_unknown`, or the lifecycle row's `to_state`); a `requested` with no
  terminal row is `open`.
* **Deduplication:** mirror rows are not counted when a direct row with the same
  `operation_id` (`mirrors_operation_id`) exists; within an episode every row after the first
  terminal one is ignored.
* **Recoverable / terminal** comes from #275's reason → recovery-authority table, passed as
  an argument and echoed in the result (like ADR 0009 §7 thresholds). A surface or reason
  outside the enums counts under `unknown`, never `chat` or a success bucket.
* **Coverage (missing data is shown, not hidden).** Narrowing rows are best effort (§4), and a
  latched writer cannot record its own latch, so coverage is derived only from rows that are
  written when things go *right*: per incarnation, `complete` = an `audit.process.started` and
  an `audit.process.stopped` with `clean = true` (§4c: never written after a latch or a failed
  narrowing) and no `chain.recovered` or pruned range in between; `incomplete` = a `started`
  without that clean stop, or a `chain.recovered`; `unknown` = the range is pruned, or rows
  needed to decide are missing (for example a `started` row cannot be found for a stop row's
  incarnation). The running incarnation is `in_progress`. Counts over any non-`complete`
  incarnation are **lower bounds**; the UI shows them as `≥ n` with the list of incarnations
  and their coverage word.

### 6. Migration of existing logs

| Log | Today | Decision |
|---|---|---|
| `events.db` (`session.*` from #350) | chained (ADR 0009) | kept; legacy types registered; new rows follow `audit/1` |
| Approval store (ADR 0006) | own SQLite chain | **not moved** (ADR 0009 stage 3 deferred): its recovery rule and the #263 attack tests are its guarantee; it gains the receipt in the `decided` body (§2c) and the mirror (§2d). At the first start of the deploying version, approved-but-unconsumed requests without a receipt are cancelled (§2c). Revisit moving it only if two chains cause a real defect — **Open (5)** |
| Scheduler log (ADR 0008) | own JSONL chain | **not moved**, same reason (#265 tests, durable recovery ban); job loosenings carry a receipt (§2c), runs are mirrored. Active jobs whose newest loosening has no receipt are paused at that first start (a person resumes them, which writes the receipt) |
| `settings-history.jsonl` (ADR 0010) | unchained JSONL, masked from / to values | kept as the settings page's history (observability); **not imported** (its values are text). New `audit.policy.*` rows start at deployment |
| `forgetting-log.jsonl`, removal archive | own chains, digests only | kept; host commands that use them are entries of §4a |
| `turns.db` | turn events | not audit; never imported |

Rows written before deployment are attested only from the migration on (ADR 0009 §6
`migrated_through_seq`); nothing is back-filled from the unchained logs.

**External anchor / signature (#171 remainder).** An attacker who can write both
`events.db` and its head can rebuild a consistent chain (ADR 0009 §2), and then also forge
receipts. The floors (#271) keep the agent's tools out of both; the remaining attacker is
another process of the same OS user. Options in **Open (9)**; the minimum recorded here:
the limit is stated in the `chain_status()` documentation, and the existing opt-in anchor
pattern (`STIRA_TOOL_RULES_ANCHOR_DIR`, #273) is the candidate place for an audit head
anchor.

### 7. Boundary with the UI (#194)

* The #194 timeline is a **reader**: it reads audit rows through an exit contract like
  `turns_view` (ADR 0009 §8): only registry fields leave, values are re-validated on the
  way out, unknown types become the fixed type `event` with no data. It has no write path to
  the chain and never sees a receipt or the HMAC key.
* Two lanes, **audit** (`events.db`) and **turn** (`turns.db`), joined by `turn_id` /
  `operation_id`; a row's provenance is always visible. A turn event never appears as an
  audit fact.
* Chain verification is shown from `chain_status()` / `verify_chain()` (status words, no
  paths); a failure shows the banner of §4. Stop counts show the coverage of §5a.
* Human annotations (#194) are free text: their own store, referencing an `event_id`; never
  in the chain.
* Outlier bands and metrics are observability (`turns.db` / trajectory functions).

## Changes needed elsewhere (at implementation)

* ADR 0006 §3: `consume` also requires the `decided` event's audit receipt to verify (§2c)
  and, for a receipt from an earlier process, a clean previous stop (§4c); the card carries
  an operation token (§2f); §6 notes stage 3 deferred, mirrored instead.
* ADR 0008 §9: job definitions get a `job_version`; job loosening lines carry the
  `standing` receipt and `operation_id`; a fire verifies the receipt against the current
  version (§2c); job forms carry an operation token (§2f); an unclean previous run pauses
  every active job (§4c).
* ADR 0009 §9 stage 2: its open decision ("must an audit-write failure block the action?")
  is answered by §4; the heartbeat-tick mirror becomes on-write (Open (6)). §5: the audit
  writer refuses (does not replace) a required identifier that `mask_secrets` would change
  (§1d) — consistent with `emit`'s refusal.
* ADR 0010 §6: the final verifier also runs (B) of §2b; `rules_loosening` becomes one input
  of `authority_delta` (§4b); the rules form and save carry an operation token with the
  expected generation (§2f); the rules file records the `operation_id` of its last save;
  §4c closure (locked load) after an unclean run.
* ADR 0011 §5: a permit carries the receipt of its intent row; `consume` adds checks 1–2 of
  §2b (B); grant forms carry an operation token (§2f). §12: name `events.db` and `audit.lifecycle.*`.
* ADR 0012 §11: row names `yolo.*` → `audit.yolo.*` (if Open (7) is accepted); its §6a
  already writes loosening rows before the effect and re-checks generations at consume.
* `docs/tool-execution-paths.md`: the entry registry of §4a replaces the gap list as the
  source; the page renders it.
* `docs/configuration.md` / `_ja`: no new variable (Open (8) (b) would add one); each
  authority setting's row names its safe value and that it is restored after an unclean run
  (§4c). `docs/operations.md` / `_ja`: the `stira authority confirm` step after a crash
  (proposed host command), the `authority-closure.json` marker (what each state means, that a
  clean restart does not lift it, never edit or delete it by hand), and the backup rule that a
  restored data directory starts closed.

## Phases and acceptance criteria

Order (each phase its own PR; nothing is implemented by this ADR).

* **Enforcement switch-on rule.** Phases 1–5 may merge one by one, but **enforcement**
  (refusing operations on audit failure, requiring receipts at consume) is turned on only in
  the PR that completes phase 5, when receipts, the consume checks, the human-operation
  staleness check and the revocation fault tests all exist. Phase 2 alone never enables it.
* **No stored receipt becomes usable before the start closure.** A receipt stored with a
  loosening (an approval's `decided`, a job's `standing` receipt) is honoured across a restart
  only once the clean-stop record, the §4c closure of every persistent authority target and
  its fault tests exist (phase 5); before that, a restart treats every stored loosening as
  having no receipt.
* **Every PR** that wires or changes an entry runs the §4a registry test as a gate (no entry
  without `gated` / `closed`); whatever Open (12) decides, every entry not wired at switch-on
  is `closed`.

1. **Registry, required fields and classifier.** `audit_record` registry with required /
   optional fields per type (§1), `build()`, `operation_id` generation, the entry registry
   (§4a) with every entry listed, and `authority_delta` (§4b) with its tests (restore,
   rollback, recovery, job update, set-back, unknown → loosening), the closure-marker
   inheritance contract (§4c: marker states, start conditions (i)–(iv), confirm-only lifting),
   the token-requirement rule per actor and decision, and the classifier ↔ closure test (every authority setting has a safe
   value, every authority store a §4c closure). Tests: a field outside the
   allow-list is not stored; a missing or invalid required field raises and writes nothing;
   `approval_wait` and `rejected` are different rows; `edited` / `responded` are valid
   decisions; an unknown surface stays `unknown`; every receipt kind can be built from its
   row's required fields; no call site produces a dropped / `invalid` field.
2. **Authorization order.** Audit-first decisions and receipts stored with stored
   loosenings (§2c) for approvals and jobs; target stores record `operation_id`. Tests:
   **crash before and after the approval-store COMMIT** (after step 1; after step 2 with a
   healthy, a missing and a tampered `events.db`); a decided event without a receipt is not
   consumable and is cancelled at the first start; approval-store COMMIT failure after the
   audit row approves nothing; "was it applied" answered from the target store after a later
   save.
3. **One-time and generation binding.** (A) / (B) of §2b with the lock order, at the chat
   turn's final hook. Tests: **revocation during the audit I/O** (narrowing between intent
   and (B) → `void`, not run); a latch set between (A) and (B) refuses; a receipt from another
   incarnation, operation, target or argument hash is refused; a consumed `operation_id` is
   refused the second time; a `standing` job receipt is void after the job version moves.
   Cost measurement of §2e.
4. **Human-operation staleness (§2f).** Tokens for every human loosening entry. Tests: the
   **delayed "Ask" after a later "Never"** is refused `stale_operation`; a **resent resume
   after a later pause** returns the first result and the job stays paused; a resend after
   eviction is refused, not applied; a pre-restart token is refused; **value set back** by a
   new token after reading the current state is accepted as a new loosening; Ask → Never →
   Ask voids an approval bound to the old generation.
5. **Fault injection and switch-on.** Multi-restart fault injection mixing kills and clean
   stops (failure → kill → closed start → clean stop without confirmation → start: still
   closed; marker deleted / unreadable: still closed); a failing writer in every entry (§4 table);
   revocation persistence failure + `kill -9` + restart keeps the target unusable (§4c),
   `previous_clean = false`; the closure of **every** §4c target, including each authority
   setting (`write_roots` back at its safe value), and "no closure, no service"; latched
   read-and-talk state.
   Enforcement turned on here.
6. **Remaining entries gated.** With #276: admin API, scheduler, host commands, start-up
   parameters wired (until then `closed` or wired individually per Open (12)); webhook /
   delegate / ACP / exec stay `closed`.
7. **Mirrors and aggregation.** Approval and scheduler mirrors with `mirrors_operation_id`
   deduplication; `stop_breakdown()` with episode keys and coverage (§5a).
8. **Retention and anchor.** `prune_before()` + `audit.chain.pruned` (refuses ranges with
   live receipts), host command, size warning; #294 table; anchor per Open (9).

ADR 0011 / 0012 rows are added by those implementations against this registry and under
§2b / §2c.

Mapping to #277's acceptance criteria: versioned allow-list schema and contract tests →
phase 1; adapters on production paths (approvals, tools, procedural) → phases 2, 3, 6;
protected operations not run unrecorded → phases 2–5; wait vs rejection → phase 1;
`unknown` surface → phase 1; stop breakdown → phase 7; approval store / scheduler log
"migrate or record why not" → §6; signing / external anchor → §6 and Open (9); diagnostics
separate and audit not sampled → "Audit vs. operational observability" and phase 1.

## Open points (operator decision)

* **(1) Store.** (a) the existing `events.db` (one chain with `/new`, one open-time scan);
  (b) a separate `audit.db` namespace (separate head and retention, two scans).
  **Recommended: (a)** — ADR 0009 already names `events.db` the evidence store, and turn
  events are not in it.
* **(2) Unknown optional field.** (a) drop and count, the row is still written; (b) refuse
  the row (the operation then fails closed). Required fields are not open: a missing one is
  always a failed audit (§1d). **Recommended: (a)** with the CI test; (b) would let a coding
  mistake in an optional field stop every write.
* **(3) Argument hash.** (a) keyed HMAC (§3); (b) the #146 unkeyed `args_hash` (already
  computed, but a low-entropy argument can be confirmed by guessing). **Recommended: (a)**.
* **(4) Outcome row fails after the effect.** (a) latch the writer (read-and-talk);
  (b) warn only. **Constraint:** (b) lets later loosenings and protected effects run after an
  unrecorded outcome, which is incompatible with requirement 4 (an audit-write failure stops
  loosening) unless (b) is limited to the one failed row and the next operation still needs
  a healthy writer — which is (a) in effect. **Recommended: (a)**.
* **(5) Approval store / scheduler log.** (a) keep, add receipts and mirror; (b) migrate to
  namespaces of the shared primitives (ADR 0009 stage 3). **Recommended: (a)**.
* **(6) Mirror trigger.** (a) after each source write + at open; (b) heartbeat tick (off by
  default). **Recommended: (a)**.
* **(7) Naming.** (a) `audit.<domain>.<action>` for every new type, legacy `session.*`
  kept; (b) domain names without prefix (`yolo.*`). **Recommended: (a)**.
* **(8) Automatic pruning.** (a) never automatic, host command only; (b) opt-in age limit
  (`STIRA_AUDIT_RETENTION_DAYS`, off by default, never pruning live receipts).
  **Recommended: (a)** for v1, (b) only with #294's table.
* **(9) Anchor / signature.** (a) none, limit documented; (b) opt-in head anchor outside the
  data directory (pattern of `STIRA_TOOL_RULES_ANCHOR_DIR`); (c) signed heads with a key held
  by a separate OS user (#318). **Recommended: (a) now, (b) as the next opt-in step**; (c)
  only with #318.
* **(10) Destination of external effects** (one decision with ADR 0012 Open (10)):
  (a) full host after masking — readable, but an unknown secret encoded in a subdomain stays
  in clear in an immutable row;
  (b) keyed hash only — no plaintext, a person can only test a suspected host;
  (c) registrable domain (public-suffix list) after masking — subdomain-encoded data dropped,
  the domain shown, needs a maintained suffix list;
  (d) (c) + keyed hash of the full host — readable domain plus exact matching by a person
  with the key;
  (e) clear text only for destinations on a fixed allow-list set by a person — a list of
  values that may be **published** (a person naming a domain does not make it non-secret; the
  list is meant for public service domains such as an API host) — (the domains the
  tier may reach), keyed hash for everything else.
  **Constraint:** the public-suffix list is not a secret remover — the registrable label
  itself (`<secret>.example` registered by an attacker, or an identifying customer domain)
  can carry a secret or an identity. (a), (c) and (d) therefore meet requirement 2 (no
  secrets in the row) only for destinations whose domain is known in advance; only (b) and
  (e) meet it unconditionally. **Recommended:** the operator decides together with ADR 0012;
  if readability matters, (e) (with (b) for unlisted hosts) is the option that keeps
  requirement 2 without conditions.
  **Decided 2026-10-19 12:28 (operator, with ADR 0012 revision 8): (b)** — the keyed hash
  only (as implemented in ADR 0012 phase 6).
* **(11) Path target.** (a) root label + keyed hash; (b) relative path in clear after
  masking. **Constraint:** (b) can carry an unknown secret or a name in a file name, which is
  incompatible with requirement 2 (no secrets in the row); it is listed only for comparison.
  **Recommended: (a)**.
* **(12) Today's working entries and the release condition.** (a) wire every working entry
  (chat, approval resume, scheduler API, config / tool-config API, memory API, `/new`, host
  commands) before enforcement is switched on (phase 6's wiring moves into or before the
  phase-5 PR); (b) switch enforcement on at phase 5 and keep
  unwired entries `closed` meanwhile (features such as settings loosening or job resume are
  unavailable until wired); (c) per-entry: close where wiring is large, wire the rest.
  **Recommended: (a)**, (c) as fallback per entry; never "enforce on chat, leave others
  open".
* **(13) Cost threshold.** (a) report the §2e measurements and let the operator accept them
  per phase; (b) a fixed budget (for example p95 added latency per tool call) that blocks the
  phase when exceeded — the number to be chosen from the phase-3 measurement, not guessed
  here. **Recommended: (a)** for the first measurement, (b) afterwards.
* **(14) Persistent authority targets after an unclean previous run** (§4c table: approvals,
  jobs, tool rules, authority settings, and any later store that holds authority). The start
  cannot know whether a narrowing was lost: the clean-stop record is missing after *every*
  crash, kill or power loss, with or without a lost narrowing.
  Options that **meet the requirements** (requirement 4 and "keep the invariants" hold with no
  residual):
  (a) **Close every target** on every unclean / unknown start (approvals cancelled, jobs
  paused, rules locked, authority settings at safe values), held by the persistent closure
  marker until `stira authority confirm` (§4c). Availability cost: only unclean ends pay it (a
  normal `SIGTERM` stop, #285, writes the clean record), but after each crash or power loss an
  unattended host (#318's always-on setup) stays narrowed — no tools, writes only in the
  workspace, no jobs — until a person confirms, also across later clean restarts; pending
  approvals must be decided again.
  (b′) **Journal as an optimisation that always falls back to (a):** a common `0600` narrowing
  journal on the floor (target, intended version, digest, fsync before any narrowing is
  persisted) is used only to *show the person* which target lost a narrowing; the closure is
  still (a). Same availability as (a), better diagnosis.
  Options with a **residual, not compatible with the current requirements** (listed for
  comparison only):
  (b) **Journal that closes only journaled targets.** If the journal write itself fails and
  the process is killed, the next start sees "unclean, nothing journaled" — indistinguishable
  from an unclean end that needed no journal — and leaves that target at its old, wider value.
  A second copy outside the data directory (`STIRA_TOOL_RULES_ANCHOR_DIR` pattern) makes it
  rarer (both writes must fail), not impossible.
  (c) **Per-target split** (approvals and jobs closed, rules and settings by (b)): the same
  residual for rules and settings.
  (d) **Keep the disk state**: fail-open after a lost narrowing.
  **Recommended: (a)**, with (b′) if the diagnosis is wanted. Choosing (b), (c) or (d) would
  need a change of requirement 4, not only an operator preference.

## Alternatives considered

* **Free-text audit with secret masking only.** Readable, but unknown secrets and model text
  would become immutable rows. Rejected: allow-list first.
* **Derive audit from turn events.** One writer, but turn events are capped and carry masked
  previews; a lost turn event would be a lost audit fact. Rejected.
* **Store-first approvals with a "not consumable until audited" state** (COMMIT the decision,
  then the audit row, then flip a flag). Also meets the contract, but adds a state to the
  approval store's replay and a second write per decision; audit-first + receipt in the
  decided body needs neither. Rejected in favour of §2c.
* **Idempotency keys alone for human operations.** They stop transport retries, but a
  request delayed past a later change still carries a valid key and would be applied as a new
  loosening against the current state. Rejected for the token + expected state of §2f.
* **Durable operation tokens** (kept across restarts). Would let a person's pending form
  survive a restart, but the token store becomes another state an attacker or a rollback can
  restore. Rejected: tokens live in memory, a restart means "reload".
* **Asynchronous audit queue.** Removes the ≈0.5 ms per row from the calling thread, but an
  intent row could be lost after its effect started. Rejected for decision and intent rows.
* **Sealed segment rotation** (new file per period, genesis names the previous final hash).
  Bounded open time without deletion; more files and a cross-file verifier, and receipts
  must name their segment. Kept as the alternative if `prune_before()` proves insufficient.
* **Per-endpoint loosening checks.** Each API decides its own class; rejected for one
  classifier (§4b), because restore / rollback / job-update paths would otherwise each need a
  correct private rule.
* **Move every chained store into one file now.** Rejected for the reasons of ADR 0009 ("One
  chain across all namespaces").

## Out of scope

SIEM export, external sending, the #194 timeline itself, migrating the approval store /
scheduler log / ledger / `turns.db`, signing (Open (9)), the content of the grant / YOLO
designs (ADR 0011 / 0012), any change to approval, breaker or tool-rule policy beyond the
receipt check.

## Known design gaps (to be detailed before implementation)

This ADR is merged as **Proposed**. The gaps below are known after four review rounds; each
is to be detailed, and the whole ADR re-reviewed, **before** any phase that enables
enforcement starts. None of them is decided here.

1. **Full rollback of the data directory.** Restoring an old copy of the closure marker
   *and* `events.db` + head together (or deleting both) is not detected; the same residual as
   ADR 0012 Open (8). Candidate: mirror the marker and the chain head in an anchor directory
   outside the data directory (Open (9) (b)).
2. **Shutdown timeouts become closures.** A service manager that kills the process after its
   stop timeout (for example a systemd `TimeoutStopSec`, #318) leaves no clean record, so the
   next start closes everything. The graceful-stop time budget and its documentation are not
   fixed.
3. **Recovery without a shell.** Only a host command lifts a closure; an operator who reaches
   the host only through the browser cannot recover. Whether a token-bound, typed browser
   path may lift it (and under which authentication, #148) is open.
4. **Lock order in practice.** §2b fixes target lock → `events.db` → leaf lock, but the
   approval broker lock is shared with `/new` (`timed_lock`) and the resume path takes the
   turn lock; a deadlock / timeout analysis of all real lock pairs is not done.
5. **Bounds of the in-memory sets.** The token map (results kept for replays), the consumed
   set of `call` receipts and the busy-retry queue of narrowing rows need size and time
   bounds; what happens at the bound (refuse vs evict) is only stated for tokens.
6. **Classifier for settings without an order.** `authority_delta` for list- or map-valued
   settings (MCP servers, integration destinations) needs a per-setting comparison; "unknown
   → loosening" is safe but may close or refuse more than needed.
7. **HMAC key unreadable or lost.** Receipts and keyed values cannot be verified; the
   expected behaviour (closure + `start_closure_failed`, or a new key + closure) is not
   specified.
8. **First start of the deploying version.** The migration cancellations and pauses (§2c,
   §6) and an unclean-start closure can happen together; their order and the single
   confirmation that covers both are not specified.
9. **Agent-created jobs** (ADR 0011 stage B): their envelope binding across a closure (jobs
   paused; does confirming the closure resume them or does each need its own decision?) is
   not specified.
10. **Receipt lookup vs older-row rewrite.** The evidence step (§2b (A)) proves the row exists
    with its hash, not that no older row was rewritten since the last full verification
    (ADR 0009 §3); whether some paths need a periodic `verify_chain()` is open.
11. **Cost.** The production-path cost is not measured (§2e); thresholds are Open (13).
12. **Completeness.** A deployment where phase 7 (mirrors, stop breakdown) is not done does
    not yet record every event §5 requires; it is not the complete version of this contract.

