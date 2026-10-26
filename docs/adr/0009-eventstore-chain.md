# ADR 0009: Hash-chained EventStore (tamper evidence) and trajectory metrics

**Path:** `docs/adr/0009-eventstore-chain.md`
**Status:** Accepted (stages 2–3 of §9 are planned, not implemented)
**Issue:** #171. Related: #240 / ADR 0006 (approval store chain), #239 / ADR 0008
(scheduler log chain), #146 (approval binding, operation ledger), #167 (exec tools),
#144 (procedural writes), #165 (saturation), #185 (masking).
**Decision:** `SQLiteEventStore` rows form a hash chain anchored by a sidecar head, with
the recovery and fail-closed rules ADR 0006 and ADR 0008 already use, written once in
`observability/event_chain.py`. A legacy database is migrated in place and its start
point recorded. Audit adapters put approval / tool-execution / procedural-write events on
a chained store **as evidence only**; the approval store and the scheduler log are not
replaced (§9). Trajectory metrics are pure functions with formulas fixed here.

> **Note (2026-10-16).** On 2026-10-16 the owner authorized the design and development of
> (1) an autonomous goal loop that runs only while the user's explicit grant is active, with
> an expiry (default 12 h, at most 36 h), a budget and a scope, and (2) bounded
> self-scheduling: inside an envelope a human defined, the agent may propose and create
> jobs. Both stop when the emotional state worsens. Autonomy without a grant, always-on
> event-driven operation, self-expansion of permissions and the agent changing its own
> envelope stay out of scope. The design is ADR 0011 (Proposed: `docs/adr/0011-agent-self-scheduling.md`, PR #295, tracking
> #296). This ADR is unchanged: the chain grants
> nothing.

## Context

`SQLiteEventStore` (`events` table) and `NeuralHistory` keep history but cannot show
that a row was changed, deleted or reordered. Two stores already carry their own chain:
the approval store (ADR 0006, SQLite) and the scheduler log (ADR 0008, JSONL). Both
note that #171 could replace them. Their reviews found the same classes of holes each
time, so the shared design has to close them by construction:

1. a recovery / rollback rule that works in the direction of *more* permission;
2. a revocation or verification failure that leaves the store usable (fail-open);
3. free text (ids, reasons, names) stored or hashed in plaintext;
4. a check that can be bypassed by swapping files (head of another store or older head).

Constraints not up for change here: no autonomous or event-driven operation, no
execution / ACP / external sending, nothing approved automatically. The chain grants
nothing.

## Decisions

### 1. Schema and hash

`events` gains two nullable columns, `prev_hash` and `hash`. A new table
`event_chain(key, value)` holds `version` (`1`), `namespace`, `anchor_seq`,
`anchor_hash` and, informationally, `migrated_through_seq`, `migrated_rows`,
`masked_rows`, `migrated_at`.

```
hash = sha256(prev_hash || "\n" || canonical_json([seq, event_id, timestamp, event_type,
       turn_id, duration_ms, model_id, tool_name, tool_use_id, status, usage_json,
       message, attributes_json]))
genesis = sha256("stira-eventstore/v1\n" || namespace)
```

* The hashed values are the row **as stored** (read back inside the inserting
  transaction, then `hash` is set), so SQLite column affinity (an integer written into
  `duration_ms REAL`) cannot make a correct row fail verification. `canonical_json` is
  the one ADR 0006 uses (sorted keys, no whitespace, no NaN); a value it cannot
  represent (a BLOB written by something else) is tampering.
* `seq` is part of the material: reordering rows breaks the link even if every row is
  individually intact.
* The first row links to the anchor (`anchor_hash`; genesis when `anchor_seq = 0`). The
  genesis is bound to the namespace, so a chain cannot be presented as another store's.

### 2. Head and recovery rules

A sidecar `<db>.head` (`0600`, temp file + `fsync` + rename + directory `fsync`) holds
`{v, ns, prev, next}`, where `prev` / `next` are the chain state
`{seq, hash, anchor_seq, anchor_hash}` before and after the transaction. It is written
**before COMMIT, while the SQLite write lock (`BEGIN IMMEDIATE`) is held**, which also
serializes writers across instances and processes.

On open, after re-hashing every row:

| Database state | Meaning | Result |
|---|---|---|
| `== next` | consistent | open |
| `== prev` | crash between head and COMMIT, **or the last transaction was deleted** (cannot be told apart) | a `chain.recovered` event is appended (it names the head's `seq`/`hash`), the head re-anchored on it; `store.recovery` is set |
| anything else (rows the head never covered, rows missing beyond the last transaction, an older head, a missing / malformed head, another namespace) | tampering or loss | **unhealthy** |

Recovery never restores anything; it records that something may be missing. Deleting
the recovery record itself is again "the last transaction is missing" and produces a new
record, so a gap can never be made to look clean; going further back is tampering.

The event store derives **no permission** from its rows, so "revoke everything
unfinished" (ADR 0006) has nothing to revoke here. A store that later moves its
authority onto this chain must, on `recovery`, revoke every unfinished grant in new
events before serving anything (stage 3, §9), exactly as ADR 0006 does today.

An attacker who can write both the database and the head can still rebuild a
consistent chain (as in ADR 0006 / 0008). The mirror of §8 is a partial answer: a
rewritten approval store is visible in the event chain unless that chain and its head
are rewritten too. Signing / an external anchor stays open.

### 3. Writes, `clear()` and verification

* Every write runs under `BEGIN IMMEDIATE`: the known tail row must still exist with its
  hash, rows appended by another instance are verified link by link, and the head must
  name the database state. Then the row is inserted, the head written, COMMIT.
* `clear()` deletes the rows **without restarting the chain**: the anchor moves to the
  last deleted row (in `event_chain` and in the head, same transaction), and the next
  row links to it. Deleting rows with SQL instead of `clear()` leaves the anchor where it
  was and is tampering.
* `verify_chain() -> (ok, error)` (same shape as `ApprovalStore.verify_chain`) re-reads
  and re-hashes every row under the write lock and compares the head. A same-size
  rewrite of an older row is caught there and at every open, not by the cheaper per-write
  check. `chain_status()` returns a small status (no paths, no content).
* Tested with real processes: four concurrent writer processes build one valid chain;
  `kill -9` between head write and COMMIT is recovered as in §2; `kill -9` at an
  arbitrary point always reopens consistent or recovered, never unverifiable.

### 4. Fail-closed rules

* Any verification failure, malformed metadata / head (also one that becomes malformed
  while the instance runs, on the write path and in `verify_chain`), a schema that lacks
  a chained column (e.g. `DROP COLUMN hash`), a non-transient SQLite error, any
  unexpected exception while verifying, or a failure after the head was written makes
  the instance **unhealthy for its lifetime**: every `emit` / `clear` raises
  `EventChainError`; putting the files back does not re-enable it (only a reopen after
  inspection). The constructor never raises for these; it opens unhealthy.
* The same split applies to initialization (pragmas, WAL switch, DDL): a file that is
  not a database opens unhealthy; a busy WAL switch leaves the store pending.
* Busy/locked is transient, as in ADR 0006: on a write it is raised and nothing is
  recorded; at open the store stays *pending* (not healthy, no error) and the open is
  retried on the next write or `verify_chain()`, which raise / return "busy" until it
  succeeds. A refused event (`ValueError`: not canonical, secret-looking identifier) is
  not a verdict on the chain.
* Reads (`get_events`, snapshots, SSE) stay available on an unhealthy store: they grant
  nothing and are needed for inspection. `healthy` / `error` / `chain_status()` expose
  the state. Dashboard hooks already log and swallow sink errors, so a broken chain never
  interrupts an agent turn; it stops recording.
* A failed head write (before or after the rename) is treated as "head may be ahead":
  the instance stops writing; the next open classifies the files.

### 5. Secrets

* `emit` passes `message`, `usage` and `attributes` through `mask_secrets` (#185) before
  storing them, in addition to the hooks' own masking (idempotent), so the hashed
  material never contains a plaintext secret. Identifier columns (`timestamp`,
  `event_type`, `turn_id`, `model_id`, `tool_name`, `tool_use_id`, `status`) are not
  rewritten silently: if `mask_secrets` would change one, the event is refused
  (`ValueError`, nothing stored).
* Migration redacts every legacy row **before hashing it** (`masked_rows` counts them):
  payload columns and identifier columns are masked, a secret-looking `event_id`
  (UNIQUE) is replaced by `redacted-<seq>` (a deterministic `-<n>` suffix on collision).
  The replacement is derived from nothing secret (no hash of the old value, which would
  allow guessing a low-entropy secret) and is deterministic, so a migration redone after
  a crash between its head write and COMMIT reproduces the head exactly. `PRAGMA secure_delete = ON`
  overwrites freed content.
* Erasure is confirmed, not assumed: the migration commits a durable marker
  (`purge_pending = 1`) together with the redaction. It is cleared only after
  `wal_checkpoint(TRUNCATE)` reports completion (`busy = 0`; up to 3 attempts per call,
  short busy timeout). A checkpoint blocked by a reader is logged and retried at every
  open, at `close()` and via `purge_secrets()`; a crash between COMMIT and checkpoint
  leaves the marker set, so the next open redoes it. `chain_status()["purge_pending"]`
  shows it.
* `event_id` (hashed, and served as the SSE cursor) is refused if `mask_secrets` would
  change it.
* The head holds only sequence numbers, hashes and the namespace.
* Audit adapters (§8) take identifiers and hashes only, never arguments, content or
  reasons (§8 rules).

### 6. Migration and overhead

A database without `event_chain` is migrated in one transaction: add the columns, create
the metadata, mask + hash every existing row in `seq` order, write the head (`prev =
null`), COMMIT. `migrated_through_seq` records the start point: rows up to it existed
before the chain and are attested **from the migration on**, not before. Old readers
keep working (the new columns are ignored); an old *writer* (a downgrade) inserts rows
without a hash, which the next open reports as tampering (fail closed, intended).

Crash during migration: the head exists but the database has no metadata. The migration
is redone and accepted only if the head is the migration head (`prev = null`) and the
result is *exactly* its `next`; anything else (e.g. metadata and columns dropped after
events were written) is tampering.

Measured (`docs/research/eventstore-chain-overhead-2026-10-15.md`): ≈0.33 ms extra per
event (median 386 µs vs 56–58 µs, ≈7×; ≈480–494 µs after the identifier checks of review round 1), ≈0.26–0.3 ms of it the durable head; verification
≈9 µs per stored event; migration ≈31 µs per legacy row (≈80 µs with identifier redaction). Batching (one head per batch of
events) is the option if a high-rate store needs it; not done here.

**Load on existing callers, and why open stays a full scan.** Today the only production
instance is the chat server's `events.db`, whose only writer is the `/new` audit (#350; turn events
are in `turns.db`, see "Source of truth" below), so neither cost applies to a turn yet. When hooks write to it, they emit synchronously from the agent thread: a
few dozen events per turn cost ≈10–20 ms against model calls of seconds. Open re-hashes
every row (≈0.9 s per 100k rows). A lazy or partial open (trusting a "verified up to"
mark) was considered and rejected: the mark would live in files the attacker can write,
and a rewrite of an old row would then only be found by an explicit `verify_chain()`,
which is exactly the gap ADR 0008 accepted only for the running process. The size is
bounded by retention instead (`clear()` moves the anchor; a prefix-retention API with the
same anchor rule is the follow-up if a store grows large). Writes stay incremental
(tail + new rows + head), independent of the store size.

### 7. Trajectory metrics (pure functions, `observability/trajectory.py`)

#165 does not define these metrics, so they are defined here by formula only, carry no
interpretation, and take every threshold as an argument (echoed in the result).

* **Total variation** of dimension *d*: `TV_d = Σ_i |x_d(i+1) − x_d(i)|` over samples in
  time order; `total = Σ_d TV_d`.
* **Recovery episode** for `below` / `recover_at` (default `recover_at = below`): starts
  at the first sample with `x_d < below` (or the first sample, `censored_start`), ends at
  the first later sample with `x_d >= recover_at`; `duration_sec` is the timestamp
  difference, `None` if not recovered. A threshold crossing of the *state*, not the
  breaker level (the breaker also has cooldown turns).
* **Saturation** for `eps`: fraction of samples with `x_d <= eps` (`low`) and with
  `x_d >= 1 − eps` (`high`); counted per sample, not weighted by time.
* **Export**: NeuralState series, stimuli, and *recorded* breaker transitions (rows
  that carry a `breaker` level). Levels are never inferred from the state.

Input is the `NeuralHistory` row shape; invalid rows are dropped as `history_payload`
does, plus rows whose timestamp is not a finite number within ±1e12 s (so `10**1000` or
±1e308 cannot raise or produce `inf`); thresholds that are not finite are refused. `bcScore` is untouched (its formula is still undefined, #168).

### 8. Audit adapters (`observability/audit_chain.py`)

`AuditChain(store)` writes `audit.approval.<kind>` (ADR 0006 kinds),
`audit.tool.<started|committed|failed|denied>` (#146 ledger phases + gate refusal) and
`audit.procedural.<requested|written|denied>` (#144). Rules:

* Kinds come from fixed sets (`ValueError` otherwise); no free-form event types.
* Identifiers must match `[A-Za-z0-9._:@-]{1,64}` and pass `mask_secrets` unchanged,
  else `unknown`; principals keep their kind (`local:` / `remote:` / `system:`); hashes
  must be 64 lowercase hex, else `invalid`. No arguments, content or reasons.
* An unhealthy chain raises (`EventChainError`); the caller decides (stage 2).
* `mirror_approval_store(store)` copies only after `store.verify_chain()` succeeds
  (otherwise records `audit.approval.source_unverified` and copies nothing), uses
  deterministic event ids (`approval:<seq>:<hash>`, idempotent), and first checks that
  every previously mirrored source event still has the same hash
  (`audit.approval.source_diverged` otherwise) — this detects an approval store whose db
  and head were rewritten consistently.

The adapters are not wired into the chat server in this change; they are an interface plus tests.

**Source of truth for turn events (#286).** On the chat server, `turns.db` (`TurnEventStore`,
written by `TurnEventRecorder`) is the source of truth for turn events: turn, tool call,
approval and breaker records. `/observability`, `/api/observability/{events,snapshot}`,
`/api/turns` and the chat ribbon all read it. No writer to `events.db` is added for them:
a second copy of the same events would have two truths to reconcile, and the chain grants
nothing. The chained `SQLiteEventStore` is the evidence/audit store (`/new` audit records,
the adapters of this section) and the store `sse.create_sse_blueprint` can stream for an
app that writes one (no app in this repository does today; `dashboard/app.py` serves no
event stream); stage 2 (§9) may add evidence rows to it but does not change
which store the turn views read.

*What leaves the turn views (exit contract).* `observability/turns_view.py` forwards only an
allow-list of fields, validated on the way out: numbers (`ts`, `seq`, durations, token
counts) must be finite, non-negative and in range, else the field is left out (a NaN or
infinity stored by something else cannot break the stream); `type` must be one of the turn
contract's types, else the fixed type `event` with no data. Values the recorder takes from
constants in the code are checked against those constants and read as a fixed word when
unknown: `reason_code` (`hitl_reasons.REASON_CODES`, else `approval`),
`resolution_reason_code` (`RESOLUTION_CODES`), `decision` (`HITLAction`), `stop`, approval and
tool `status`, `result_status`, breaker levels (`BreakLevel`). Values with no enumeration in
the code (tool name, model id, `resolver` principal, exception class name, stimulus
category, ids) must only look like a code (`[A-Za-z0-9_.:/@-]`, ≤ 64 characters; 200 for the
model id). That check cannot tell a secret from a code when the secret has the same shape
(for example `MY_PRIVATE_TOKEN_123`): the contract is that **the recorder writes only code
values into those fields and never user or model text** (previews and prose stay in
`/api/turns/<id>/events` and the tool-payload pages, which are masked there, #185). Free text
with spaces or punctuation is dropped. The two endpoints are read-only, have no
authentication of their own (the server has none, see `docs/admin-api-permissions.md`; the
loopback bind with the Host and same-origin guards is the boundary and is tested), and the
snapshot takes its counts, tail and turn list under the store's write lock so they agree.

### 9. Moving existing stores onto the shared EventStore (stages)

1. **This change:** shared chain primitives, chained `SQLiteEventStore`, migration,
   `verify_chain`, audit adapters + approval mirror. Approval store, scheduler log,
   operation ledger and `TurnEventStore` are unchanged; their guarantees are untouched.
2. **Wiring (separate issue):** the chat server mirrors the approval store on the heartbeat tick
   and records ledger phases / procedural writes through `AuditChain`. Open decision for
   that stage: whether an audit-write failure must block the action it audits (fail
   closed for exec / procedural writes) or only be reported.
3. **Migration of the stores (separate issues, one per store):** the approval store and
   the scheduler log become namespaces (`approval`, `scheduler`) of the shared
   primitives, keeping their projection / replay code. Preconditions: each store's
   recovery rule (ADR 0006: cancel every unfinished request; ADR 0008: durable recovery
   ban + pause every job) runs before the store serves anything, a failure to record it
   leaves the store unhealthy, and the existing test suites (#263 / #265 attack tests)
   pass unchanged. A store in another namespace keeps its own file and head (a separate
   chain per namespace, so retention and verification stay per store).
   `TurnEventStore` (UI turn events) is a candidate too; out of scope here.

## Alternatives considered

* **One chain across all namespaces in one file.** A total order of everything, but one
  store's retention (`clear`) or corruption would affect all, and the approval store's
  "re-verify everything on every write" would grow with dashboard traffic. Rejected:
  one chain per namespace, the namespace bound into genesis and head.
* **Hash the Python values before insert.** Breaks on column affinity (12 → 12.0) and
  any future type change. Rejected for read-back hashing.
* **Re-hash legacy rows without masking.** Would put plaintext secrets of old rows into
  the hashed material. Rejected; legacy rows are redacted first (`masked_rows`).
* **Treat a head one transaction ahead as consistent.** Would let the last event be
  deleted silently. Rejected; it is recorded.
* **Replacing the approval store / scheduler log now.** Their guarantees are tested
  against specific attacks; moving them in the same change as the chain itself would
  mix two reviews. Rejected for stage 3.

## Out of scope

Signing or external anchoring of heads, batching, wiring into the chat server (stage 2),
migrating the approval store / scheduler log / ledger / `TurnEventStore` (stage 3),
`bcScore`, any change to breaker or approval policy.
