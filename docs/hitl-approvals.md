# HITL approval binding and resume idempotency (#146)

The CRITICAL HALT gate (`agent/checkpoint_hooks.py`) asks a human before a
write-capable tool runs. The same gate asks before **every** call of a tool whose
rule is **Ask** (#190, [ADR 0010](adr/0010-tool-policy-gate-stack.md)); an Ask
approval answers the rule only and never lifts the breaker or the floor, and a rule
changed while the request waits voids the approval. Saving a tool rule voids every
open approval at once (#287): restored cards leave the queue (approving one is refused
with 409), a waiting turn gets a rejection, and the history shows `cancelled`
(`tool rules changed (#190)`). If that revocation fails, the rule stays saved but the save
answers `500 approvals_not_revoked` and approvals are suspended (`/api/hitl/pending` →
`approvals_suspended`; approving answers `503 suspended`, rejecting still works) until a
later sync reconciles the open approvals with the rules (#330). An approval is **bound** to
exactly what was shown:

| Bound field | Source | Refused when |
|---|---|---|
| `agent_id` | `StiraEngine.agent_id` | another agent tries to use it |
| `tool_name` | the tool call | another operation tries to use it |
| `args_hash` | sha256 of `canonical_json(tool_input)` (sorted keys, no NaN/∞) | any argument differs, including a hook swapping it after approval |
| `expires_at` | now + `StiraConfig.hitl_timeout_sec` (default 120 s) | the answer arrives late, or nobody answers |
| `request_id` | engine-assigned | a decision names another request |

**With `STIRA_YOLO_ALLOW` (ADR 0012 §8b)** an approve or edit decision stays **one press**
(phase 2c, operator decision 2026-10-19; phases 2–2b asked for a confirmation per card):
it is a single decision bound to the card id and its `args_hash`, not a loosening of what
may run. The only addition is the refusal while an agent tool call runs (409
`tool_call_running`; reject and respond are never refused for it). The confirmation dialog
is used for the loosening operations (tool-rule / hub / rollback saves, `RECOVER`,
scheduler create / resume, the YOLO request). Without the allowance nothing here changes
([admin-api-permissions.md](admin-api-permissions.md)).

**While a YOLO window is active (ADR 0012 phase 5a)** the Ask of an internal tool in a chat /
Stage turn shows **no card**: the gate's approver answers it with an approval bound like
the one above (`args_hash`, the same expiry, a `yolo-…` request id) and recorded as
`audit.yolo.auto_approved`, never in the approval store; the chat log, the Stage backlog and
its raw view show one line instead (`Auto-approved (YOLO internal): file_write #a3f2 —
37/2000`, phase 5b — [yolo-quickstart.md](yolo-quickstart.md)). **In an "External allowed"
window (`STIRA_YOLO_ALLOW=external`, phase 6)** the Ask of an external tool (`web_fetch`,
`web_search`, `webhook_send`, …) is answered the same way when its arguments carry no secret
(`Auto-approved (YOLO external): …`), at most `STIRA_YOLO_MAX_EXTERNAL` per window, the
destination recorded only as a keyed hash; used up, external tools show their card again.
**In a window whose press chose "Include exec" (phase 7, `STIRA_ENABLE_EXEC_TOOLS` +
`STIRA_YOLO_ALLOW=external`)** the Ask of `shell` and `batch` is answered the same way when
the arguments (the command included) carry no secret, at most `STIRA_YOLO_MAX_EXEC` per
window; the approval is bound to that call's arguments and opens its exec gate mark once
(#362), the log line names the tool and `#ref` only. `batch`'s inner calls pass the gate one
by one. `cron` and `use_agent` / `think` / `swarm` / `graph` keep their card in every window
(a crontab line runs later without YOLO; child agents run without Stira's hooks). A command
approved this way runs as the server's OS user and can bypass Stira's protected locations
and audit (accepted for that choice, ADR 0012 §9).
Every other Ask — an external tool in an internal window, a call whose arguments look like
they carry a secret or that names a non-ASCII host, exec (outside an "Include exec" window,
and `cron` / the child-agent tools in every window), MCP tools, webhook / bot / resume
/ job turns, a paused window, a cap whose slots are
momentarily all reserved, a call whose path resolves outside the workspace / write roots
(e.g. `~/.ssh`, `~/.zshrc`, a symlink out) or cannot be decided, `diagram` — and every
`CRITICAL_HALT` card stay a person's card as above; Never and the floors stay refused, as
without YOLO.

## Rules (all fail-closed)

- **No approver, callback error, invalid decision, unnormalizable input** → reject.
- **Timeout / UI never reached / tab closed / server shutdown** (`broker.close()`) → reject.
  The request is recorded as `expired`/`closed` with resolver `system`, and can
  never be approved afterwards (HTTP 410).
- **Edit** is never executed directly. The edited input becomes a *new* request
  (`supersedes` = old id, new hash and expiry) that must itself be approved.
  The old id is closed (HTTP 409 on a second answer). At most
  `hitl_max_edit_rounds` (default 3) edits, then reject.
- **One decision per request.** Concurrent tabs race under one lock: exactly one
  wins, every other answer gets HTTP 409 `already_decided`. Retransmits with the
  same `Idempotency-Key` are already rejected by `install_security` (#185).
- **HTTP**: `POST /api/hitl/<id>/decision` must quote the displayed `args_hash`
  for `approve`/`edit`/`respond` (400 if missing, 409 `binding_mismatch` if
  different; the request stays pending so the UI can reload it). `reject` needs
  no hash and may carry a `reason`.
- The hook re-checks the binding against the arguments it is about to run, and a
  final verifier hook (registered after every other hook) checks again right
  before execution.

## Crash-resume: no duplicate side effects

Checkpoints are at-least-once (`docs/checkpoints.md`): restoring re-runs the
stored tool batch. For HITL-approved operations, `core/approval.OperationLedger`
records, keyed by `operation_key(agent, tool, args_hash, toolUseId)`:

1. `started` — immediately before execution (last BeforeToolCall hook);
2. `committed` (tool result `success`) or `failed` — first AfterToolCall hook.

On replay of the same tool call (any breaker level):

| Ledger state | Action |
|---|---|
| none | normal policy; at CRITICAL HALT a **fresh** approval is required (restore is never an approval) |
| `committed` | not executed; the model receives "already applied" |
| `started` / `failed` (outcome unknown) | the tool's reconciler is asked: `True` → treat as applied, `False` → proven not applied, ask again; no reconciler / `None` / error → **refused**, never blindly re-executed |

The operation key is passed to the tool as an idempotency key
(`tool_context.invocation_state["stira.hitl.idempotency_keys"][toolUseId]`), so an
external API can also deduplicate on its side; a reconciler typically checks the
external system for that key. The ledger stores only identities and states, no
arguments or results. `NXPAgent(operation_ledger=OperationLedger(path))` makes it
durable (the chat server: `.local/stira/hitl-operations.db`); the default is
process-local.

**Ungated writes and `delegate` (ADR 0024 §5, phase 1b).** Every write-capable call
(`config.blocked_tool_names` / `WRITE_TOOLS`) and every `delegate` call gets a ledger entry
right before it runs **also when no approval was asked** (rule Always): the last
BeforeToolCall hook records `started` with an empty `request_id`
(`OperationRecord.ungated`), the first AfterToolCall hook `committed` / `failed`. A refused
call records nothing. On replay the table above applies to them too, without a reconciler
for `delegate`: a `delegate` that ran is answered "not repeated after an interruption" (no
second child, no new model calls); the model may ask for it again in its continuation (a
new call, a new `toolUseId`). Only **read-only** calls still run again (no side effect; the
cost is the call). An ungated entry answers a call only while that call is **replayed** from
a restored checkpoint (`NXPAgent.resume_from_checkpoint` marks the batch's `toolUseId`s until
the next turn that replays nothing): in a fresh turn the same key means the provider repeated
a `toolUseId` with the same arguments, and the call runs and is recorded anew (an entry still
`started` — the same id in flight — is refused). Approved entries keep the stricter #146
rule above (refused at any time).

**The paused call is always gated on a resume (ADR 0024 §7).** The replay of an approved
request asks the gate for that call (answered from the store, consumed once) whatever the
rule and the breaker say at replay time. An approved EDIT therefore runs only the edited
input, never the original one, also when the breaker recovered in between; the YOLO
approver never answers it.

Tests: `tests/test_hitl_binding.py` (binding, expiry, double/concurrent decisions,
edit re-review, argument swap, and crash-resume with a mock external update tool).

## Pending approvals and the breaker (ADR 0024 §3, phase 1b)

A pending approval is **held** while the breaker is stricter than when it was requested
(levels ranked `normal < warning = coherence < critical`; the level now is
`engine.breaker_status()`, an error or an unknown level counts as `critical`):

- **Reject** works at every level, for every request.
- **Approve / edit** are refused with `409 breaker` (nothing recorded, the request stays
  pending) while the level now ranks above the request's `break_level`. The card says
  "On hold" (`breaker_hold: true` in `/api/hitl/pending`) and disables both buttons until
  the next poll sees the breaker recovered. A request raised under CRITICAL `halt` has
  `break_level = critical` and stays decidable at CRITICAL (that is what `halt` is for).
  A request whose `break_level` is missing (rows from before ADR 0024) is held at every
  level above `normal` (the strict reading).
- The **expiry is never extended** by a hold: a hold that outlasts it ends as `expired`.
- The **resume claim** repeats the comparison under the store's write lock: a breaker
  that got stricter between the approval and the replay refuses the resume
  (`resume_refused` with body `{"reason": "breaker", "level", "requested_level"}`, levels
  only) and revokes the approval (`cancelled`, `circuit breaker stricter than at request
  time`). Nothing of the batch runs.
- A held approve / edit is logged as a warning line with the request id, the two levels
  and an argument-hash prefix only (no arguments); the store records nothing for it.

**Request origin (ADR 0024 §2).** `ApprovalStore.submit` accepts only `{}` (no turn),
`{"kind": "turn", "id": <short id>}` or `{"kind": "job", "job_id", "run_id", "generation",
"incarnation"}` (unknown keys refused; a job request is correlated by its run id) and only a
known `break_level`. Nothing writes `kind: "job"` yet (job resume is ADR 0024 phase 2); a
pending job request found at start is cancelled (`job runs do not survive a restart`).

**Restored cards and the rest of the batch (ADR 0024 §5).** A restored card counts the other
calls of its paused batch that a resume answers from the ledger ("already ran; answered
from the ledger, not repeated") and those whose outcome the ledger cannot tell ("may already
have run; not repeated"). Counts only (`replay` in `/api/hitl/pending`), never a name or an
argument.

**Fail-closed additions (ADR 0024 §7).** `audit_events()` re-verifies the chain and head
before returning rows; the EDIT ancestor walks (`chain_root`, `resume_context`) stop at 16
links and a longer chain refuses the resume.

Tests: `tests/test_hitl_breaker_hold_ledger.py` (hold, claim refusal, origin forms, the
ungated ledger with a mutant run that removes it and shows the repetition, the forced gate
with a mutant run that shows the original input of an EDIT running).

## UI (#189)

Three surfaces read the same server state (`/api/hitl/pending`, `/api/hitl/history`),
so they always agree and a reload restores them:

- **Chat cards** (`/chat`, `approvals.js`, built on the #188 card parts): one card per
  pending request with target (tool · agent), cwd (when the operation names one),
  normalized arguments, expiry countdown, *why you are asked* (breaker level and
  reason), origin turn (links to the Turn Inspector), request id and args hash.
  Each request carries `reason` (the server's sentence, also the audit text in the
  store), `reason_code` (#279): `breaker_critical_halt` / `rule_ask` for the fixed
  reasons in `core/hitl_reasons.py`, `null` otherwise, and `reason_params` (#279): the
  variables the UI words the code with, from an allowlist (only `{"level": "<normal|warning|critical|coherence>"}`
  for a coded reason, `{}` otherwise; free text is never a variable). Both are API-only:
  the SSE `approval.requested` event keeps its shape (#186). The card words a known
  code in the UI language only when `reason` is that code's exact sentence, shows the
  sentence otherwise, and always puts the breaker level in front (`reason_params.level`
  first, then `break_level`). A `reason_code` the page has no wording for (a newer
  server) is shown as "Other reason (code: …)" (the code only when it has the shape of
  a code, else just "Other reason"), never as the bare code or a guessed sentence. A
  request without a `reason_code` still shows its sentence as sent. What is approved,
  and the decision itself, never depend on `reason` or `reason_code`.
- **Nav chip** "保留中の承認 N": links to `/approvals`; the cards publish the live
  count (`stira:approvals` event) so it never lags the queue.
- **Approvals page** (`/approvals`, the chat server): the same cards plus the decision
  history (decision, reason, origin turn, resolver, expiry, args hash; newest first,
  process-local since server start). With the durable store each entry shows the
  store's final state: an approval the store later cancelled or expired reads
  `cancelled`/`expired`, not `approved` (#287). History entries carry `resolution_reason`
  (audit text) and `resolution_reason_code` (#279): set only when the server itself
  closed the request (resolver `system`) with one of the fixed sentences in
  `core/hitl_reasons.py` (`expired`, `closed`, `replaced`, `binding_mismatch`,
  `live_only`, `rules_changed`), `null` otherwise. An operator's reject reason is never
  coded and is shown as typed.

Basic operations are **approve once** and **reject** (optional reason, returned to the
agent). Edit (resubmitted as a new request) and respond are under "other operations".
Two more buttons (ADR 0025, [allow-again.md](allow-again.md)): **approve, and allow the same
for this session** (an in-memory allowance for one tool and one target class — a folder subtree,
a host, a layer — for the conversation and at most 2 hours) and **approve, and set this tool to
Always** (after the call has run, the hub's own save with the typed tool name; history source
`approval_card`). They are offered only where the server's view says so (`allow_again` in
`/api/hitl/pending`: `{offered, class_kind, label, whole_root, token}` or `{offered: false,
reason}`); a card that is not offered them says why in one line. A press sends `action:
"approve"`, `allow_again: "session" | "always"` and the view's one-time `view_token`; the server
recomputes the class from the stored request, so a view that went stale (a rule save, a stop,
`/new`, a breaker change after the card was shown) approves nothing (409 `stale_view`) and the
card re-reads its view. The live list is `GET /api/allowances`, revoked one by one or all at once
(`POST /api/allowances/<id>/revoke`, `POST /api/allowances/revoke-all`). Cards whose expiry has passed disable approval locally; the server refuses it
anyway (410). Every decision quotes the displayed `args_hash`, and carries an
`Idempotency-Key` that is reused when the same decision is retried after a network
error. All controls are native buttons/textareas with labels; after a decision the
focus moves to the next request (`tests/a11y/approvals_axe.cjs`).

Manual check without a model: `STIRA_PORT=5612 python scripts/hitl_harness.py` starts
The chat server on a temporary data dir and injects fake pending requests and history.
