# ADR 0025: "Allow the same again" on an approval card — a session allowance for one tool and one target class, and "make this tool Always" through the settings hub

**Path:** `docs/adr/0025-allow-again-from-the-approval-card.md`
**Status:** Accepted (revision 5, 2026-10-24: operator decision of 2026-10-24 13:30 — Q1–Q7 are
decided as recommended: both buttons shown by default with no setting (Q1 A), the file class is the
approved file's directory subtree capped at its write root (Q2 A), `http_request` GET / HEAD only
(Q3 A), any tool-rule save ends every allowance (Q4 A), every human stop ends them (Q5 A), Make
Always saves after the call's terminal result, page-driven (Q6 A), and its confirmation is the
hub's existing one (Q7 A). The decision text below is unchanged.)
**Implementation:** done in three PRs. PR 1 (#576) built the backend: the table, the fence,
the counters, the tokens, the audit rows and the one grant gate. PR 2 (#614) built the page:
the two buttons, the not-offered line, the allowance badge and list with revoke and "Revoke all",
the log line, the #448 `allowed_again` counter, i18n and a11y, plus `GET /api/allowances` and the
two revoke routes. PR 3 added Make Always. That covers the card's dialog, the `approval_card`
history source, `GET /api/config/hub/card/<request_id>` with `call_not_finished` and the
one-time save token spent before the audit row, with the fence checked again right before the
file. PR 3 also wired the gate: the approver slot after YOLO (§2), the consume step in the
verifier (§4c), and the class policy added to `open_checked` through the call's gate mark (§3).
It turned both buttons on together, with `session_allow.ENABLED` and the page's
`ALLOW_AGAIN_ON` as one code constant each and no setting. User guide:
[allow-again.md](../allow-again.md). The `containment` end of §5 has no source yet: ADR 0023
containment is still observe-only.
**History:** Proposed (revision 4, 2026-10-22, after design review round 3: a one-time save token for Make
Always (§9), the audit scope of both buttons (§7, "Dependencies"), roots reads serialized with
the roots save (§4.0); revision 3 answered round 2: the view snapshot
bound to the press (§4a), one lock for publication and consumption (§4.0), observed root
changes end allowances irreversibly (§5), path reads taken out of the offer list (§3),
evidence vs compare-and-set split (§4.0), the Make Always start condition (§9) and the
dependencies on ADR 0013 ("Dependencies"); revision 2 answered review round 1, B1–B5). Operator decision of
2026-10-22 12:14 (Issue #571): the card offers **both** buttons side by side in one UI (no
phases); scope = **tool + target class**; duration = **the session, and at most 2 hours**. This
ADR fixed the contract before any code; the points marked **Q** were decided on 2026-10-24 (see
Status).
**Issue:** Refs #571. Related: [ADR 0010](0010-tool-policy-gate-stack.md) (Always / Ask / Never,
rule generation, the one loosening check, §5 persistent permissions),
[ADR 0012](0012-yolo-mode.md) (YOLO window, tier classification in `yolo/tiers.py`, clocks,
incarnation, §6a linearization), [ADR 0013](0013-audit-event-contract.md) (audit row
contract, receipts, §2f human operations, §4 fail-closed audit),
[ADR 0024](0024-approvals-beyond-the-turn.md) (approvals beyond the turn, freeze on the
breaker), [ADR 0006](0006-hitl-persistence.md) (durable approvals), #174 (delegation grammar),
#448 (what ran in a window), #227 (settings hub, history, rollback), #350 (`/new`), #275 (stop
reasons), #190, #547.
**Decision (proposed):** an approval card gets two more buttons next to 「今回のみ承認」 /
"Approve once" and 「拒否」 / "Reject":

1. 「承認して、このセッション中は同じ操作を許可」 / "Approve, and allow the same for this
   session" — approves this call and creates an **allowance**: an in-memory, never persisted
   permission that answers later Ask calls of **the same tool on the same target class**
   (§3) automatically, until the session ends, 2 hours pass, the breaker leaves NORMAL, a
   person stops, or a tool rule or a root is saved — whichever comes first.
2. 「承認して、このツールを『常に許可』に変更」 / "Approve, and set this tool to Always" —
   approves this call and, after it has run, changes this one tool's saved rule Ask → Always
   through the settings hub's existing save (its confirmation, audit, history and rollback,
   #227).

Both are offered only where one eligibility function (§3) says so; everything else keeps
today's two buttons.

## Context

Measured on 2026-10-17 (ADR 0012 "Goals"): an approval wait takes 8.6–112.6 s (median 29.5 s).
When the agent edits five files of one project or fetches three pages of one site, the person
presses "Approve once" for each, with the same tool and the same place every time. The only
relief today is a YOLO window (six hours, every Ask in a tier, ADR 0012), which is far wider
than "this one kind of call, for a while", or making the tool Always on the settings page,
which is permanent and any-argument. Other harnesses offer an "allow for this session"
button; Stira does not.

Read from the code (`main` at `3c3c58f`):

* The gate (`CheckpointHooks.before_tool`) already has an approver slot at the rule layer
  (ADR 0012 §2 step 5): `_auto_decision` asks `auto_approver.approve(...)` before
  `engine.request_hitl` and, when it answers, returns an `APPROVE` bound like a person's
  (argument hash, `toolUseId`, short expiry) that passes every later check; the YOLO grant is
  consumed in the last hook (`ApprovalBindingVerifier` → `auto_approver.consume`). Never, the
  breaker write block, `CRITICAL_HALT` and the floors (`path_check`) are decided **before**
  that slot and never reach it.
* `yolo/tiers.py` already classifies tools (`INTERNAL_TOOLS`, `EXTERNAL_TOOLS`,
  `PERSON_TOOLS`, exec) and answers "inside the workspace / write roots?"
  (`scope_refusal`), "secret-looking or credential-by-reference arguments?"
  (`external_refusal`) and "which host?" (`destination`).
* The rule **generation** is one counter for the whole `tool-config.json` (ADR 0010 §6): any
  rule save — a rollback included, which never lowers it — voids every approval asked under
  the old generation, and the gate re-reads it right after a decision returns.
* Writes open their target with `open_checked` (`tools/checked_open.py`): no final symlink,
  the policies are asked again on the opened descriptor's path, and the inode must still be
  the one the path names.
* The engine has no counter of breaker transitions today (`_last_break_level` only); a
  NORMAL → WARNING → NORMAL round trip is invisible to a later reader of the level.
* New installs make reads **Always** and local writes **Ask**
  (`ToolConfig.first_run_defaults`): a file read only raises a card where a person chose Ask
  for it.

## Terms

* **Allowance** — `kind = "session_allow"`. One in-memory record per press:

  | Field | Meaning |
  |---|---|
  | `allow_id` | 128-bit random |
  | `incarnation` | the process's random id (ADR 0011 §5), never read back |
  | `session_epoch` | the conversation counter `/new` advances |
  | `tool`, `class_kind`, `class_key` | §3; `class_key` is the **resolved** directory, host or layer |
  | `class_version` | a code constant of the eligibility function; a different value never matches |
  | `fence` | the allowance fence (below) of the view snapshot the press carried (§4a) |
  | `breaker_departures` | the departure counter (below) of the view snapshot |
  | `rule_generation` | the tool-rule generation of the view snapshot |
  | `roots_generation`, `roots_digest` | the roots counter (below) and a digest of write roots, read roots and `protected_dirs` |
  | `issued_receipt` | the ADR 0013 `standing` receipt of `audit.allow.issued` |
  | `issued_at`, `expires_at` | clocks of ADR 0012 §7; `expires_at = issued_at + 2 h` |
  | `generation` | this record's own counter: 0 reserved, 1 active, +1 on its end |
  | `uses`, `state` | `reserved` → `active` → `ended(<reason>)` |

* **Allowance lock** — **one** leaf lock that guards the allowance table, the fence, the
  departure counter, the roots counter and the view tokens. Nothing else is acquired while it
  is held, and it is never held across I/O. Every publication of an end (the `bump`
  functions below) and every compare-and-set of §4 runs under it, so an end and a
  consumption are always ordered: one of them is first.
* **Allowance fence** — one in-memory integer per process, only ever incremented, advanced by
  `bump_fence(reason)` under the allowance lock for every event that ends *every* allowance
  (§5), **before** that event takes effect (a rule save bumps it before the new rules become
  visible, `/new` before the conversation is reset). A record, a view token or an automatic
  approval carrying an older fence is dead, whatever else is true.
* **Breaker departure counter** — a new in-memory integer, only ever incremented, kept under
  the allowance lock and advanced by `bump_departure()`. The engine calls it **inside the same
  `engine.state_lock` critical section** in which it commits a state change after which the
  level is not NORMAL, and in every evaluation that yields a level other than NORMAL
  (pre-turn, the live read `fresh_breaker_rank`, heartbeat ticks), an exception or an unknown
  level included. Lock order: `engine.state_lock` → allowance lock, never the reverse (the
  allowance code never takes `state_lock`). A level that is not NORMAL has therefore always
  been published before any reader can see it, and a round trip leaves a different value
  behind even if the `on_breaker` notification was lost.
* **Roots counter** — an in-memory integer under the allowance lock, advanced by
  `bump_roots()` (with the fence) by every save that writes write roots, read roots or
  `protected_dirs` (hub, legacy APIs, rollback, RECOVER) before the file is replaced, **and by
  every observation of a roots digest that differs from the last published one** (the loader,
  the evidence step of §4) — so a hand edit A → B → A that was seen once has ended every
  allowance for good; a value that comes back never matches again.
* **View token** — a one-time token the server issues with a card's pending view when it
  offers the buttons: it is stored on the server with the card's `request_id`, argument hash,
  the class it offered and the **view snapshot** (`fence`, `breaker_departures`,
  `roots_generation`, `rule_generation` at the moment the offer was computed). The page sends
  only the token. Memory only, under the allowance lock; a newer view of the same card replaces
  (voids) the older token; the card's end voids it.
* **Target class** — what "the same" means for one tool (§3). Computed by the server from the
  stored request of the card, never taken from the page.
* **Session** — the dashboard conversation of #350 (chat and Stage share it); `/new` ends it.
* **Allow-again** — the press of button 1. **Make Always** — the press of button 2.

## Decisions

### 1. What does not change

* Never, the breaker (write block, `CRITICAL_HALT`), the write / read floors
  (`protected_dirs`, the data directory, SOUL, settings, the secret-path floor),
  `IdentityGuard`, secret masking, budgets and loop limits, the taint hold (ADR 0022) and
  containment (ADR 0023) are untouched: they are decided before the approver slot or after
  it, exactly as for a person's approval.
* The tool list, tool definitions and the static system prompt do not change; the agent is
  not told about allowances (no prompt text, #254 cache prefix unchanged). No model call, no
  job, no token cost (ADR 0008).
* A card approval is still single-use and bound to its call (ADR 0006, #146). An allowance
  does not reuse an approval; it issues a **new** single-use approval per call, like YOLO.
* The agent cannot press either button through Stira: its HTTP tools cannot reach the own
  dashboard (ADR 0012 §8b premise 2, #499). A program of the same OS user outside the server
  can press them, exactly as it can press "Approve once" today (ADR 0012 §8a residual).

### 2. Where the allowance answers (gate position)

Step 5 of ADR 0012 §2, reason `RULE_ASK` only, in this order:

1. the YOLO approver, when a window is active (unchanged; its cap and statistics);
2. if it did not answer: the **allowance approver** — it answers only when an `active`
   allowance passes the use-time check (§4b);
3. otherwise the person's card, as today.

The answer goes through every later check unchanged (rule-generation re-check, live-breaker
re-check, `ApprovalBinding.check`, `IdentityGuard.path_denial`) and is consumed in the last
hook by the consume step of §4c. Covered origins: **human chat / Stage turns of the session
that issued it** only. Not covered: scheduler jobs (ADR 0024 phase 2 lets a job wait for a person's card; an allowance
never answers it), webhook ingress,
`delegate` children and sub-agents (they only get Always tools anyway), restored requests
after a restart (no allowance survives), Discord / Slack, ACP.

### 3. Target classes and the one eligibility function

**One function** decides eligibility everywhere: `allow_again_eligibility(tool, tool_input,
policy, origin) → (class_kind, class_key) | reason_code`. The card's offer (display), the
press (issuance, recomputed on the server from the stored request), and every later call (use,
on **that** call's arguments) call the same function with the same `class_version`. There is no
second copy in the page or in the approver.

It always applies, to **every** tool in the table, in this order: the tool is in the table; the
origin is a human chat / Stage turn of the current session; the arguments are plain JSON
values and `mask_secrets` leaves them unchanged (`secret_in_args`, for internal tools too); no
credential by reference; then the per-tool rule:

| Tool | Target class (`class_kind`: what "the same" is) | Per-tool condition |
|---|---|---|
| `file_write`, `editor` | `dir_subtree`: the **resolved** directory of the approved path and everything below it, never wider than the write root that contains it | `scope_refusal` is `None` (inside the workspace / write roots, one plain path, no glob or list); the resolved path has no symlink component below the class directory; for `editor` only the commands that change a file through `open_checked` (`view` and other read-only commands: `read_unbound`) |
| `memory_write` | `layer`: the same `layer` argument (today only `episodic` is writable) | — |
| `memory_read`, `memory_search`, `memory_list`, `memory_rate`, `journal`, `current_time` | `tool`: the tool, any arguments (that pass the common checks) | — |
| `web_fetch`, `rss` | `host`: the same host as `destination()` (lower-cased ASCII; no IDNA) | `external_refusal` is `None` |
| `http_request` | `host` + **read methods only** (`GET` / `HEAD`, Q3) | as above, and the call itself is `GET` / `HEAD` |
| `web_search`, `perplexity_search` | `tool` (the provider is fixed by the tool) | `external_refusal` is `None` |

Examples of the class text on the button (computed on the server, shown in the page only):
「file_write · projects/game/ 配下 · 最長2時間」 / "file_write · under projects/game/ · up to 2 h";
「web_fetch · docs.python.org · 最長2時間」. A `dir_subtree` whose directory is a write root
itself is that whole root; the button text then says so (「ワークスペース全体」 / "the whole
workspace"), so a wide class is never hidden.

**The class is bound to the target that is actually accessed, not to the spelling — and only
where Stira can bind it.** For a write, use compares the **resolved** path (symlinks of its
existing prefix followed) with the resolved `class_key`, and the allowance adds a class policy
to the `policies` that `open_checked` asks again on the opened descriptor's path: a parent
directory swapped for a symlink after the check, pointing to another directory of the same
write root, shows up as a path outside the class and the write is refused at the open. **Path
reads are not offered** (`file_read`, `view_image`, `image_reader`, `lsp_diagnostics`,
`lsp_hover`, `editor view`: `read_unbound`): they open their file in the upstream tool, with no
checked open Stira could add the class to, so "it never reaches another directory" could not
be guaranteed. *Cost:* a person who set a file-read tool to Ask still confirms every read
(new installs make reads Always, so this only affects that choice). For a `host` class the
guarantee is the host of the **requested** URL (bound by the argument hash); where the tool
follows a redirect is the tool's behaviour, the same as under an Always rule, and the button
text says 「このホストへの要求」 / "requests to this host". A protected directory below the class
is still refused by the floor before the approver slot; the class never makes a floor
reachable.

**Not offered (only "Approve once" and "Reject", with a one-line reason under the card:**
「この操作は毎回確認します(<reason>)」 / "This kind of call is confirmed every time (<reason>)"):

| Case | Reason code |
|---|---|
| exec class (`shell`, `batch`, `cron`, `use_agent`, `think`, `swarm`, `graph`, …) | `exec` |
| `PERSON_TOOLS` (`diagram`, `delegate`, `page_check`, `propose_procedure`, `run_checks`) | `person_only` |
| MCP tools and any unclassified tool | `unclassified` |
| a path outside the workspace / write roots, not decidable, or through a symlink below the class directory | `scope_outside` / `scope_unknown` |
| path reads (`file_read`, `view_image`, `image_reader`, `lsp_*`, `editor view`) | `read_unbound` |
| the session already has 20 allowances (the button is shown disabled with 「継続許可は20件までです」 / "Up to 20 allowances") | `list_full` |
| secret-looking arguments (any tool), credential by reference, unnameable host | `secret_in_args` / `credential_by_reference` / `destination_unknown` |
| `http_request` with a method other than `GET` / `HEAD`, `webhook_send`, `oauth_api_call`, `speak`, `memory` (sending data out) | `sends_data` |
| a request raised under `CRITICAL_HALT`, or while the breaker is not NORMAL | `breaker` |
| a restored request (after a restart), a job-origin request, a request of another session | `not_this_session` |
| an **edit** of the arguments (the person changed the call) | approve-once only (an edited call is not "the same") |
| Never tools, floors, `protected_dirs`, ACP delegation | never reach a card at all |

### 4. Linearization: issuance, use, consumption

#### 4.0 Evidence, then compare-and-set

Each step of §4a–§4c has two parts, and only the second runs under the allowance lock:

* **Evidence (outside the lock, I/O allowed).** First take the settings store's save lock
  (`ConfigStore.lock`), read the **snapshot** — `fence`, `breaker_departures`,
  `roots_generation`, the record's `state` / `generation`, the audit writer's health epoch
  (below) — under the allowance lock in one short critical section, read the rules and the
  roots, and release the store lock; the live breaker is read after that (no engine lock under
  the store lock). Then collect what else needs I/O: §3's eligibility function on the call's
  arguments, path resolution, the roots, their digest and `roots_generation`, the tool rule and
  its generation (all read together under the settings store's save lock, below),
  the live breaker level (`fresh_breaker_rank`, engine lock), the clocks' anomaly check, audit
  row writes and receipt verification against the chain. A failed read ends every allowance
  (`read_failed`); a roots digest different from the last published one calls `bump_roots()`
  (§5).
* **Compare-and-set (under the lock, no I/O, no other lock).** Compares only in-memory values
  with the snapshot: `fence`, `breaker_departures`, `roots_generation`, the record's `state` /
  `generation`, the writer's health epoch, the view token's state (§4a), and the record's
  monotonic deadline (a clock read is a system call, not I/O). Then it applies the step's
  effect (activate, spend, count).

This is sound because **everything that could make the evidence stale publishes a bump under
the same lock before it takes effect**, and because **a save and the evidence that reads its
values are serialized by the save's own lock**: a rule or roots save holds the existing settings
store lock (`ConfigStore.lock`, ADR 0010 §4 lock order) from its bump until the file is replaced,
and the evidence reads the rules, the roots, their digest and the counter values it compares
under that same lock (lock order `ConfigStore.lock` → allowance lock, as the save's bump). So an
evidence step sees either (old counter, old file) — and the save's later bump fails the CAS — or
(new counter, new file); never a new counter with the old file. A save stopped between its bump
and its replace keeps the lock: a new evidence step waits (bounded; a timeout is a failed read,
`read_failed`) and never reads the half-saved state. The allowance lock itself is still never
held across I/O. A hand edit of the file takes no lock; it is caught by the digest, whose first
observation bumps (§5). Further: a breaker departure is published inside the engine's critical
section that commits it (Terms); stop, `/new`, containment, revoke and the clock anomaly bump first; the audit writer
turning unhealthy is sticky (`EventStore.healthy`, as YOLO uses it) and advances the health
epoch and the fence when first seen. So "the snapshot values are unchanged at the CAS" means
"nothing the evidence depended on changed in between": either the end was published first and
the CAS fails, or the CAS was first and the end comes after a call that was authorized while
everything still held — the same order a person's approval spent a moment before a breaker trip
has today.

#### 4a. Issuance

1. **Reserve** (CAS) — the press carries the card's **view token** (Terms). Under the lock: the
   token exists, is unused, belongs to this incarnation and session, and its **view snapshot
   equals the current** `fence`, `breaker_departures`, `roots_generation`; the evidence taken
   just before (the stored request's eligibility recomputed with §3's function and the current
   rule generation) gives the **same class** and the **same rule generation** as the view.
   Then the token is spent (single use) and the record is created as `reserved` with the
   view's values. Any mismatch → 409 `stale_view` / `class_changed` / `op_token_used`,
   **nothing is approved**, the card stays pending and the page re-reads its view (new offer,
   new token). A rule save, "Revoke all", `/new` or a breaker departure after the card was
   shown therefore never lets a delayed click create an allowance under the new state.
2. **Record** (evidence) — the card's decision goes through the existing single-use store
   transition (`pending → decided(approved)`, ADR 0006; `audit.approval.decided`), then
   `audit.allow.issued` is written and its `standing` receipt verified.
3. **Commit** (CAS) — the record is still `reserved`, and `fence`, `breaker_departures`,
   `roots_generation` and the health epoch still equal the reserved values, the live breaker
   read in step 2 was NORMAL → `active` (`generation` 1). Anything else → the record is removed,
   nothing becomes active, and `audit.allow.ended` with `end_reason = issue_aborted_<cause>`
   (`fence`, `departure`, `audit`) is written best-effort. **Reader rule:** an `issued` row
   proves that an issuance was recorded, not that it became active; only an automatic call row
   whose `issued_ref` names it proves a use, and an `issued` row followed by `issue_aborted_*`
   — or by nothing — with no such call row was never used.

A stop, `/new` or a breaker departure between steps 1 and 3 bumps first, so step 3 fails: no
stop can be overtaken by a slower issuance. The once-approval recorded in step 2 is then decided
by the existing checks, not by this ADR: a chat stop or `/new` cancels the turn and its card; a
stricter breaker refuses the approved call at the gate (ADR 0024 §3, the live-breaker re-check);
a rule save voids it (generation). The result line says 「今回のみ承認しました —
停止/状態の変化があったため継続許可は作成していません」 / "Approved once — the allowance was not
created because something stopped or changed". This is the **only** case of "approved once, no
allowance" (a changed class or a stale view approves nothing, step 1).

#### 4b. Use (the approver at step 5)

Evidence: same `incarnation` and `session_epoch`; origin = a human chat / Stage turn; same
tool; §3's function on **this** call's arguments gives the same `class_kind`, a target inside
`class_key` and the same `class_version`; the tool's rule is still `ask` at the record's rule
generation; the live breaker is NORMAL and the containment stage is none; the roots digest
equals the record's; no clock anomaly. CAS: the record is `active` at `generation` 1, and
`fence`, `breaker_departures`, `roots_generation`, the health epoch equal the record's; the
monotonic deadline (`issued_at + 2 h` by `max(ΔW, ΔU[, ΔB])`, ADR 0012 §7: sleep counts) has not
passed.

The answer is an `APPROVE` bound like a person's **plus** `allow_id`, the record's
`generation`, the snapshot it was checked against, and the resolved target. **A read that
fails** (an exception, an unknown breaker level, an unreadable clock, a roots digest that cannot
be computed) **ends every allowance** (`read_failed`) and sends this call to a person; it is
never "a card this time, the allowance next time". A plain mismatch (another directory, another
host) only sends this call to a person.

#### 4c. Consumption (the last hook)

1. **Evidence** — re-read the snapshot; write the call's `audit.tool.started` row (`decision =
   auto_approved`, `authorizer_kind = session_allow`, `allow_id`, `issued_ref`) and verify its
   `call` receipt and the record's `standing` receipt; re-run the class check on the resolved
   target. A failed write or receipt → the call is refused and every allowance ends
   (`audit_failed`).
2. **CAS** — the record is `active` at the bound `generation`; `fence`, `breaker_departures`,
   `roots_generation` and the health epoch equal **both** the binding's and the snapshot's; the
   monotonic deadline has not passed → `uses += 1`, the binding is spent (single use), the call
   runs (for a write, through `open_checked` with the class policy, §3).
3. Anything else → the call is not run: `audit.tool.void` best-effort, refusal text
   「継続許可が終了したため実行しませんでした。もう一度確認してください」 / "Not run: the allowance
   ended. Ask again." — the model's retry raises a card. A revocation, `/new`, a departure or an
   expiry published before this CAS therefore always wins.

### 5. When it ends

Ends are narrowings: the fence (or the record's own `generation`) is bumped **first**, under
the allowance lock, without I/O; the `audit.allow.ended` row is written afterwards. Since every
CAS of §4 compares the fence and the counters under the same lock, a lost notification, a
failed `ended` row or a value that later returns to what it was cannot bring an allowance back.

| Event | Effect | Reason code (`audit.allow.ended`) |
|---|---|---|
| `/new` (#350) | fence + 1 | `new_session` |
| restart / crash | nothing exists any more (memory only, incarnation) | no row; a reader derives it from `audit.process.started` |
| the breaker leaves NORMAL (WARNING, COHERENCE, CRITICAL, unknown) — published inside the engine's critical section that commits it (Terms) | departure counter + 1 and fence + 1; no pause / resume | `breaker` |
| a containment stage other than none (ADR 0023) | fence + 1 | `containment` |
| a person's stop: the chat stop (`/api/chat/cancel`), the YOLO badge stop, the allowance list's 「すべて取り消し」 / "Revoke all" (Q5) | fence + 1 | `stop` |
| the person revokes one on the allowance list (names `allow_id`) | that record's `generation` + 1 | `revoked` |
| 2 hours after the press | that record ends at its next check (§4b, §4c) | `time` |
| any tool-rule save (hub, legacy APIs, rollback, preset, RECOVER) | fence + 1 before the new rules become visible (the generation moves too) | `rule_changed` |
| write roots, read roots or `protected_dirs` saved, **or a roots digest observed different from the last published one** (a hand edit, seen by the loader or by any evidence step) | roots counter + 1, fence + 1 — irreversible: changing the file back does not revive anything | `roots_changed` |
| a read that fails in §4, a clock anomaly | fence + 1 | `read_failed` / `clock_anomaly` |
| the audit writer is unhealthy or a row cannot be written (§7) | health epoch + 1, fence + 1; the two buttons are hidden until restart | `audit_failed` |

Never extended: a press while one is active for another class creates a second allowance (at
most **20** per session; at 20 the button is shown disabled, §3 `list_full`). A new press after
an end creates a new allowance with a new 2 hours.

### 6. Memory only — why it is never persisted

* A restart must end it. Kept only in memory and bound to the incarnation, it cannot be
  restored by any read-back, so a full data-directory rollback (#273) cannot replay it, and no
  "was it revoked before the crash?" question exists. The fence and both counters are memory
  too; they only need to be monotonic inside one process, because nothing they protect
  outlives it.
* No new file on the floors, no migration, no lock order with the config store; the only disk
  effect is the audit rows, which are never read back to restore anything.
* Cost accepted: after a restart the person presses again — small for something that lasts at
  most 2 hours.

### 7. Audit rows (ADR 0013 shape) and the audit-failure rule (B5)

Written by the same hash-chained writer as `audit.yolo.*` (events.db, ADR 0009), with the
ADR 0013 §1a common fields. Registry names and fields (ADR 0013 §1b additions):

| Row | `phase` / `delta_class` | `actor_kind` / `surface` | Required fields |
|---|---|---|---|
| `audit.allow.issued` | `decision` / loosening | `human` / `chat` (the card's decision route) | `allow_id`, `source_request_id`, `decision_receipt_ref` (event id of the card's `audit.approval.decided` row — the original approval), `operation` (tool, registry enum), `class_kind`, `class_version`, `target` (`<root label>:<keyed hash>` of the directory, `host:<keyed hash>`, the layer enum, or `none`), `policy` (`rules`, `breaker`), `fence`, `expires_at`, `session_ref` (keyed hash of the session epoch), human op (§2f: `op_token_ref`, `expected` = the card's `request_id` in state `pending` + the class shown). Issues a `standing` receipt bound to `allow_id` + `fence` |
| `audit.tool.started` with `decision = auto_approved` | `intent` | `system` / `chat` | as ADR 0013 `audit.tool.*`, with `authorizer_kind = session_allow`, `allow_id` and `issued_ref` (event id of `audit.allow.issued`) — ADR 0013 change: `window_id` is required only for `authorizer_kind = yolo`; its `committed` / `failed` / `void` outcome as every call |
| `audit.allow.ended` | `transition` / narrowing | `system` or `human` / `chat` or `admin` | `allow_id` (or `all` with the new `fence`), `end_reason` (§5 codes), `uses` |

* **Reference chain:** every automatic call row → `issued_ref` → the `audit.allow.issued` row →
  `decision_receipt_ref` → the person's `audit.approval.decided` row. A contract test walks it
  for every automatic call.
* **Audit failure refuses the effect** (ADR 0013 §4). If a row of §4a or §4c cannot be written,
  or the writer is unhealthy, or a receipt does not verify at the consume point: no allowance
  becomes active, no automatic call runs, every allowance ends — and the once-approval of the
  press runs **only if** its own decision and call rows ("Dependencies": built for presses of
  these two buttons) are written and their receipts verify at its consume point. A card is never a way
  around a failed audit: with an unhealthy writer the card's approval is refused too.
  "Approved once, no allowance" is shown only when the allowance failed at its commit (§4a
  step 3: a stop, `/new` or a departure published in between) while the audit is healthy; a
  changed class or a stale view approves nothing (§4a step 1).
* **No free text.** Enums, counts, random ids and keyed hashes only; the path and the host are
  keyed hashes in rows (as ADR 0012 Open (10) (b)).
* **Masking boundary of the page.** The class text in clear (directory, host) lives only in the
  server's memory record and is sent only to the page (`GET /api/allowances`, the card view).
  The persistent turn events, the chat history and logs carry `allow_id` (short), the tool and
  the `class_kind` only; the page renders the clear text from the live list while the
  allowance exists and 「(終了した継続許可)」 / "(ended allowance)" afterwards. Clear text never
  goes beyond what the card itself already showed.
* **Where a person sees it** (#448's place): an always-visible 「継続許可 N件」 / "N allowances"
  badge next to the YOLO badge (chat header, Stage dialogue edge) opens a list — tool, class,
  remaining time, uses, a 「取り消し」 / "Revoke" button per row and 「すべて取り消し」. Each
  automatic answer adds one log line in the chat (「↻ 継続許可で承認: file_write · projects/game/
  配下」 / "↻ Approved by allowance: file_write · under projects/game/", rendered as above). The
  #448 counters gain the decision `allowed_again` beside `auto_approved` (window) — counts only.

### 8. One loosening judgement for both buttons

* **Issuance is a loosening judged by the one function.** `rules_loosening(before, after,
  context)` (ADR 0010 §4; the function YOLO already extends, ADR 0012 §2) gains the field
  `session_allow` (ordered `none < active`, per tool + class). Issuance (`none → active`) is
  allowed only in the context `card_allow_again` (§4a), with a NORMAL breaker. Every other
  context — hub save, legacy APIs, rollback, RECOVER, YOLO, ADR 0011 grants, `stira
  tool-rules` — treats `session_allow` / `allow*` as a **reserved key** (409
  `allow_not_settable`), and no output contains it. Ending is a narrowing, never refused.
* **Make Always uses the same function** through the hub's existing save, unchanged:
  `settings_hub.tool_rule_change` → `rules_loosening`; a write / external tool needs its name
  typed (`confirm_always`, ADR 0010 §4); the breaker must be NORMAL; `audit.policy.loosened`
  commits before the file; the save is a history entry with the new `source` value
  `approval_card`. No second judgement is added for either button.

### 9. Button 2: Make Always — when the save may start, and undo

* **Ordering with the generation check (measured from the code, Q6).** The save moves the rule
  generation, and an approval asked under the old generation is voided at the gate. Saving
  *before* the approved call ran would cancel the very call the person approved. So: the press
  approves this call once; the save may start only **after the call has reached a terminal
  result**.
* **Server state that allows the save to start.** Not the approval's `consumed` (that marks the
  start of the call, not its end): the save request names the card's `request_id`, and the hub
  refuses it with 409 `call_not_finished` until that request has a **terminal result**: the
  call's outcome row (`audit.tool.committed` / `.failed` / `.void`), or the approval ended
  without running (`expired` / `cancelled` — a stop or `/new` cancelled the turn — or revoked
  by a rule or breaker change).
* **A one-time save token binds the confirmation** (the same token base as the view token, PR 1).
  When the terminal result is there, the server issues a **save token** with the dialog's view:
  in memory under the allowance lock, bound to the incarnation, the card's `request_id`, the
  one change (this tool `ask` → `always`), the hub's saved-values version
  (`settings_hub.version_of`, #195), the rule generation and the **fence** at that moment. The
  hub save accepts a card-origin change only with that token: unused, same incarnation, and
  version, rule generation and fence still equal — checked and spent (single use) under the
  allowance lock inside the save's precommit, before the audit row and the file. A stop, `/new`,
  a breaker departure, a rule or roots save bumps the fence, so a confirmation sent before it
  and arriving after it is refused (409 `stale_confirmation`, nothing saved); a restart changes
  the incarnation, so a request from before the restart is refused too; a second send of the
  same token is refused (`op_token_used`). CSRF still applies to every save (#185) but is not
  what makes the operation fresh. After a refusal the dialog shows the current result again and
  the person may **confirm anew** (a new token).
* **The person confirms after seeing the result.** The press opens the dialog at once with
  「実行後に保存できます」 / "You can save once it has run"; the confirm control (typed name, Q7)
  becomes active only when the terminal result is there, and the dialog shows it:
  「実行されました」 / "It ran", 「実行に失敗しました」 / "It failed", or 「実行されませんでした
  (停止・状態の変化)」 / "It did not run (stopped or something changed)". The dialog says what
  the save means in every case: 「保存するのはこのツールの今後すべての呼び出しへの『常に許可』です。
  この操作が実行されたかどうかとは関係ありません」 / "Saving makes every future call of this tool
  Always; it does not depend on whether this call ran." A person who does not want that after
  a failure or a stop closes the dialog; nothing is saved. If the page is closed first, nothing
  is saved and the next card offers the button again.
* **Undo is a save like any other** (B4). Undo from Settings → History (`/settings#sec-history`)
  is a history rollback, and a rollback is judged by `rules_loosening` **against the current
  rules, not against the history entry** (ADR 0010 §4: one loosening check for every save path,
  rollback and RECOVER included), and advances the generation like every save. Rolling back
  "Ask → Always" while the tool is still Always restores Ask: a tightening, possible during a
  breaker. If the tool was changed since (say to Never), the same rollback would make it Ask —
  a **loosening** from the current state, which needs the confirmation and a NORMAL breaker
  like any loosening. The quickest undo is the tool row's own rule switch (Always → Ask), always
  a tightening; the result line links to it: 「取り消し: 設定 → ツール(または 履歴)」 / "Undo:
  Settings → Tools (or History)".
* Offered under the same eligibility function as button 1 (§3) — one rule for both buttons;
  exec-class tools can never be Always anyway (`always_not_allowed`).
* Every allowance ends with that save (`rule_changed`); the tool is Always from then on.

### 10. Invariants (the "grant of authority" checklist)

| # | Invariant | How this design holds it | Test that pins it (at implementation; fake model, offline) |
|---|---|---|---|
| 1 | A full data-directory rollback does not replay a token or a counter | allowances, view and save tokens, the fence and both counters live in memory only and carry the per-process random `incarnation`; nothing is read back; a view token is spent once at §4a step 1, a save token once in the save's precommit (§9) | restore a copied data directory into a new process → no allowance; an old view or save token is refused; a second press or send with the same token → `op_token_used` |
| 2 | A person's operation is bound to one specific card / stop / generation | allow-again: the view token binds the card's `request_id` in state `pending`, its argument hash, the class shown **and the view snapshot** (fence, departures, roots and rule generation at display); the reserve step requires the current values to equal it, so a rule save, "Revoke all", `/new` or a departure after the card was shown makes a delayed click approve nothing (§4a step 1); a stop during issuance fails the commit (step 3); revoke names one `allow_id` and its `generation`; stop / `/new` act on everything current (a stale page can only revoke more); Make Always: the one-time save token binds the incarnation, the card, the one change, the hub version, the rule generation and the fence at the confirmation (§9) | card shown → rule save (still Ask) → delayed click → `stale_view`, no allowance, no approval; card shown → "Revoke all" → delayed click → same; stop during the `issued` write (fault injection) → no allowance; Make Always confirmation sent → a person's stop → the delayed request → `stale_confirmation`, nothing saved; the same across a restart → refused; a delayed save after another save → refused |
| 3 | A failed revocation never leaves a permission | every end is published by a bump under the **one** allowance lock that also guards every CAS (§4.0); the engine publishes a departure inside the critical section that commits it (lock order `state_lock` → allowance lock); an observed roots digest change bumps irreversibly; a failed read ends every allowance | a WARNING published by another thread between the consume step's evidence and its CAS → not run (the interleaving forced in the test, `on_breaker` suppressed); a breaker round trip → refused; a hand edit A → B observed, then B → A → refused; a raising breaker read → allowance gone at the next NORMAL too; revoke between §4b and §4c → not run |
| 4 | No free-text field is stored unmasked | rows: enums, ids, keyed hashes (§7); arguments never stored; eligibility requires `mask_secrets` to leave the arguments unchanged for every tool; the class in clear stays in memory and the page | contract test over every `audit.allow.*` row and the persisted turn events; a call with a token in a URL or in a memory text → not offered |
| 5 | An old definition does not run after an update | the record holds `class_version`, `rule_generation`, `roots_generation` / `roots_digest` and the incarnation; rule and root saves bump the fence before they take effect; a code update needs a restart | rule save / rollback / root save → next call is a card; a record with another `class_version` never matches |

Plus: the reference chain of §7 (contract test), and the race tests of "Implementation order".

### 11. Relation to other designs (and why they do not conflict)

* **ADR 0012 (YOLO).** A different `kind` (`session_allow`, never `yolo`); it uses its own
  reserved field of `rules_loosening`, never YOLO's `yolo*` fields, never counts against a
  window's cap, never opens, extends or widens a window, and a window never creates an
  allowance. Both are approvers at step 5; YOLO answers first, so nothing is counted twice.
  Both reuse `yolo/tiers.py`'s checks, so an allowance never answers a call a window would
  refuse for a safety reason (secret arguments, scope, exec). The YOLO badge stop also ends
  allowances (§5); a window's end does not.
* **ADR 0024 (approvals beyond the turn).** The boundary is the store: an automatic answer of
  an allowance is made **before a card exists, outside the durable approval store**, like a
  YOLO answer, and is **not** counted as a human approval anywhere (statistics: `allowed_again`,
  never `manual`; the audit's `authorizer_kind = session_allow`, `actor_kind = system`). The
  only human decision is the press on one card, which is an ordinary store decision. ADR
  0024's freeze is unaffected: an allowance ends the moment the breaker leaves NORMAL, so it
  cannot answer during a freeze. Job-origin and restored requests are never answered (§2).
* **ADR 0010 §5 (persistent permissions).** The only persistent permission stays an Always
  rule; Make Always uses the existing path. The time-limited Always of §5 Open is **not** this:
  an allowance is narrower (class), shorter (≤ 2 h, session) and not saved.
* **#174 (delegation grammar).** #174 would auto-approve from learned familiarity. An allowance
  is not learned: it is an explicit, person-issued, time-bounded decision. Its automatic
  answers are not human approvals and must not count as "familiarity" evidence for #174 (no
  self-reinforcement); #174 may later *suggest* pressing allow-again, never press it.
* **#227 (settings hub).** Button 2 is one hub save; history and rollback unchanged (and judged
  against the current state, §9).

### 12. Cost

* **Safety side — what increases:** a permission that lasts (up to 2 h): one in-memory store,
  one fence and two counters, one gate step and one consume step, eleven end triggers with
  tests, a new audit family. "うっかり広い許可": a `dir_subtree` lets the agent write *any* file
  below that directory for up to 2 hours (for example overwrite earlier files); mitigations:
  the class is named on the button (including 「ワークスペース全体」), the badge and list are
  always visible, every answer is a log line, floors still hold, the artifact registry keeps
  earlier versions (ADR 0020). `GET` / `HEAD` can still carry data out in a path or query to
  the allowed host (only secret-*looking* values are caught).
* **Convenience side — what decreases:** every breaker blip, rule or root save, stop or `/new`
  ends the allowances and the person presses again; a restart loses them; `POST` and
  data-sending tools still ask every time; path reads set to Ask still ask every time;
  a delayed click on a card shown before such a change approves nothing and the card offers
  again; Make Always waits for the call to finish.
* **Without it:** approval fatigue on safe repeated calls, and people reach for the six-hour
  YOLO window instead — a wider permission than this one.
* **Residual risk (out of scope):** a program of the server's OS user can press the buttons as
  it can press "Approve once" today (ADR 0012 §8a). Path reads are not offered (§3), so no
  class guarantee depends on a read that Stira cannot bind; a host class guarantees the host of
  the requested URL, not where the tool is redirected (stated on the button).

## Lighter vs heavier (how the review's races are closed without a persisted state machine)

The reviews asked for linearized issuance, use and expiry, for stale clicks to be bound to what
was shown, and for ends that cannot be lost. The heavier way is ADR 0012's: a per-grant state
machine with pause / resume, a durable marker file on the floor and a latch that survives a
restart, with a power-loss write order. Here **one leaf lock** guards one **fence**, two
**counters** and the allowance table; every end publishes a bump under it before it takes effect
(the engine inside its own critical section), every view token carries the values at display,
and every step separates evidence (I/O, outside) from a compare-and-set of those in-memory
integers (inside). That gives the same properties — a stale click or an issuance in flight loses
to a stop or a change, an ended allowance never runs a call, a value that went and came back is
still seen as changed — because nothing here outlives the process: a restart is already the
strongest end, and a persisted latch would only protect state that is never persisted. Where
Stira cannot bind the accessed target (path reads), the lighter answer is to leave those calls
out rather than to build a checked read path. No invariant was weakened to make this lighter.

## Alternatives considered (lighter first)

| Alternative | Why not |
|---|---|
| Do nothing; use the YOLO window | six hours, every Ask in a tier: far wider than one kind of call |
| "Same call again" only (exact arguments) | lightest, but useless for file edits (content differs every time) |
| Tool only, no class | one row less in §3, but `file_write` anywhere in the roots / `web_fetch` any host for 2 h: the "うっかり広い許可" the issue warns about |
| Next N calls (count, no time) | odd to explain; a loop spends it at once; time + session is what other harnesses do |
| Time-limited Always (ADR 0010 §5 Open) | persisted, survives restarts, any argument — needs the marker / latch machinery to be fail-closed |
| A typed word or a glob the person writes on the card | heavier than the risk: the server already computes a narrow class and shows it |
| Pause / resume on the breaker like YOLO | a state machine for a 2-hour convenience; ending and pressing again is simpler and safer |

## Dependencies (ADR 0013 is Proposed and not implemented)

This ADR does not assume ADR 0013 as a whole. What it needs, and where it comes from:

| Needed | State on `main` | Scope |
|---|---|---|
| A hash-chained row with a receipt that can be verified by event id | exists for YOLO: `EventStore.receipt`, `audit_chain`, `yolo/authority.py` `Receipt` | reused; `audit.allow.*` registered in the same allow-list as `audit.yolo.*` (`yolo/audit_schema.py` style), not ADR 0013's general registry |
| Sticky writer health (once failed, unhealthy until restart) | exists: `EventStore.healthy` ("any other database error is sticky"), used by YOLO | reused; this ADR adds the health epoch under the allowance lock |
| A one-time token for a person's operation, bound to what was shown | exists for YOLO requests (`op_token_ref`); **not** for approval cards or hub saves | **built here** (PR 1, one token base): the view token (§4a) and the Make Always save token (§9) |
| A direct `audit.approval.decided` row with a `decision` receipt | not for cards: the approval store has its own chain (ADR 0024 table, row 8) | **built here** for a card decided **with one of the two buttons** only — the row §4a step 2 and the reference chain of §7 name; "Approve once" pressed on its own keeps today's path |
| `audit.tool.started` + outcome rows with a `call` receipt | for YOLO's `auto_approved` calls only | **built here** for (a) every allowance-authorized call (§4c) and (b) the once-approved call of a card pressed with either button — so the press's own call is verified at its consume point (§7) and Make Always has a terminal result to wait for (§9); other calls keep today's path |
| `audit.tool.*` field change (`window_id` only for `authorizer_kind = yolo`) | YOLO's schema | PR 1, in the same schema module |

So the audit conditions of §4a, §4c, §7 and §9 apply to exactly the calls these rows exist for.
If one of these cannot be built in PR 1, the buttons stay off; nothing here falls back to an
unaudited path.

## Implementation order (each PR passes every gate alone)

The PRs stay separate, but **nothing is offered until both buttons and the revoke UI are
complete**: until PR 3, the **server entries refuse** (`allow_again` on the decision route and a
card-origin save return 409 `not_enabled`), not only the page hiding them; PR 2 builds the page
with the buttons hidden, and PR 3 turns both on together (one code constant, not a setting) —
the operator's "both buttons in one UI". The prerequisites of "Dependencies", **both tokens
included**, land in PR 1.

1. Backend: the eligibility function, the record, fence and counters, §4a–§4c, the end
   triggers, the audit rows and their reference chain, the reserved key, and the race tests:
   stop / `/new` during the `issued` write; revoke between use and consumption; a breaker round
   trip with `on_breaker` suppressed; a departure published by another thread between a
   consume step's evidence and its CAS; a hand edit of the roots observed and changed back; a
   card shown, then a rule save or "Revoke all", then a delayed click; a rule save then an Undo
   that is a loosening from the current state; a second and a delayed press with one token; an
   audit writer failing at each write, the rows of a once-approved press included; a Make
   Always confirmation delayed across a stop and across a restart; a roots save paused between
   its bump and its replace while an evidence step runs; the save token checked in the save's
   precommit.
2. Page: the two buttons (hidden), the not-offered reason line, the badge and list with
   revoke, the log line, the #448 counter, i18n (ja / en), a11y (`STIRA_REQUIRE_AXE=1`).
3. Make Always: the card dialog on the hub save, the `approval_card` history source, the
   `call_not_finished` check and the result display of §9 (the save token itself is PR 1); then
   both buttons on.

## Questions (operator decision)

Q1. **Buttons shown by default?** (A) shown by default, no setting — **recommended**. The
argument differs per button: button 1's allowance is narrow (one class) and short (≤ 2 h,
session) — that is what makes it lighter than the Always rule a person can already set on the
settings page by default; button 2 *is* that permanent, any-argument Always, offered from the
card, and keeps the hub's full bar (typed name, NORMAL breaker, audit first, history) — the card
adds a shortcut, not a lower bar. Both need a person's press, and the alpha favours feel; (B) behind a
hub switch, default off (the usual "new setting = off" rule; most people would never find it);
(C) shown, with a hub switch to hide them. Note: (A) adds no setting, but it is a new
permission path on by default — hence this question.

Q2. **File class width.** (A) the subtree of the approved file's directory, capped at its write
root — **recommended**; (B) that directory only, not below (narrower; a project with
subfolders asks again per folder); (C) the whole write root (simplest; widest).

Q3. **`http_request` methods.** (A) `GET` / `HEAD` only; other methods stay a card every time
— **recommended** (matches #547's "an approved POST runs, and only that call"; note that a
`GET` can still carry data in its path or query to that host); (B) the same host with any
method (fewer cards for API work; request bodies can be sent to that host for 2 h).

Q4. **What ends it on a rule save.** (A) any tool-rule save (the one global generation of
ADR 0010 §6) — **recommended**: one rule, already tested; saves are rare; (B) only a save that
changes this tool's rule (needs a per-tool generation: new state, more tests).

Q5. **Does the chat stop end allowances?** (A) yes — every human stop (chat stop, YOLO stop,
"Revoke all") — **recommended** (the issue's "人間の停止"); (B) only "Revoke all" and the
YOLO stop — **difference from the stated requirement:** the issue lists "a person's stop" as an
end condition; (B) narrows that to two explicit controls, so stopping one long answer keeps the
allowances.

Q6. **Make Always ordering.** (A) approve now, save after the call's terminal result, page-
driven, refused by the server until then (§9) — **recommended**: no queued loosening on the
server, the approved call is not cancelled; but if the page is closed first nothing is saved,
and the save is also possible after the call failed or was stopped (the dialog shows that
result and says the rule does not depend on it; the person may close it); (B) save first — the pending call is voided
by the generation check and the model must retry (one extra round trip, looks like a
failure); (C) a server-side deferred save after the call (robust if the page closes, but a
stored, later-consumed loosening — ADR 0013 §2c — to keep fail-closed).

Q7. **Make Always confirmation.** (A) exactly the hub's existing one (typed tool name for write
/ external tools, ADR 0010 §4) — **recommended**: a permanent permission keeps its existing
bar; (B) a checkbox when it starts from a card — **difference from the existing rule:** ADR
0010 §4's typed-name bar for making a write / external tool Always would then have a second,
lower bar on one path (the same change, two bars).
