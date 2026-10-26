# Goal grants (ADR 0011 stage C, W4 + W5 + W6a–W6d) — alpha

**Status: alpha — may change without notice; off by default.** Overview, step-by-step trial
and the collected known limits: [autonomy](autonomy.md). This page has the full rules: the
data model, the state machine and the human control plane (W4 of #296), the **goal
iteration executor** (W5) behind its own switch, progress by outcome (W6a), pre-emption by
every turn holder (W6b), claim evidence (W6c) and the heartbeat start (W6d). Issuing a grant
runs nothing by itself. With `autonomy.stage_c_exec` on, a **person's press** of "Run one
iteration" runs exactly **one** iteration; with `autonomy.heartbeat_start` on as well **and**
a grant issued with its own `heartbeat_start` flag, the scheduler's model-free `grant_tick`
may start at most one iteration per wake through the same `step` ([below](#heartbeat-start-w6d)).
There is **no automatic next iteration inside a press, no model-based loop judge (progress
is decided by the harness from what an iteration changed, W6a), and the user verifies every
completion claim.** Everything is **off by default** (gateway `autonomy.stage_c`, then
`autonomy.stage_c_exec`, then `autonomy.heartbeat_start`). Japanese:
[grants_ja.md](grants_ja.md).

## What it is

A **grant** is a time-boxed, budgeted, scoped permission a person issues in the dashboard;
its **goal** is the objective (ADR 0011 §2, §8). Both live in one record:

| Field | Meaning (v1) |
|---|---|
| `grant_id` | 32 random hex; never reused |
| `kind` | always `goal` — the discriminator against YOLO windows (`kind="yolo"`): a goal grant is never handled as a window, nor the reverse |
| `session_ref` | `grant:<id>` — the dedicated session the W5 iteration will run in |
| `goal` | free text, **only in the grant store** (`grants.json`, `0600`). Refused when `mask_secrets` would change it. Everywhere else — logs, lifecycle events, the outbox, list views, errors, audit — the goal is referenced by `grant_id` only |
| `scope.tools` | a non-empty subset of `file_read`, `editor`, `run_checks`, `memory_read`, `memory_search`, `memory_list`, `current_time` |
| `scope.workspace_subtree` | an existing directory inside the agent's write roots, resolved (no symlink escape) and within the write floor |
| `budget` | `total_tokens` (≤ 5 000 000) and `per_tick_tokens` (≤ total) |
| `ttl_sec` | default 7200 (2 h), hard cap 21600 (6 h), never extended |
| `max_iterations` | default 10, hard cap 20 |
| `no_progress_ticks` | default 3 |
| `verifier` | `user` (nothing else in v1). A grant without verification cannot be issued |
| `issued_*` | wall clock + sleep-including + sleep-excluding readings at issue |
| `issuer` | `dashboard` (host-confirmed) |
| `claim_evidence` | W6c: the evidence of the last completion claim — codes and counters the harness measured, never model text ([below](#completion-claims-evidence-and-the-waiting-state-w6c)); `null` until the first claim |

**`run_checks` in the scope** needs the user setting `autonomy.run_checks_unattended`
(default true) **and** a verified `os_sandbox` host (ADR 0019). On a `limits_only` host a
grant whose scope names `run_checks` is refused (`grant_run_checks_not_contained`) whatever
the setting says — a safety floor the setting cannot lift.

At most **one live grant per server** (v1).

## States

The grant and the goal each keep their **actual** state; the dashboard headline is derived
from them (the higher-priority one) and never written back.

| Grant state | Reasons | Who recovers |
|---|---|---|
| `active` | — | — |
| `paused` | `user`; `goal_paused`, `goal_blocked`, `goal_budget_limited` (the goal is not active) | the user (operation token); budget: a new grant; `blocked(max_iterations)`: a new grant (v1) |
| `halted` | `breaker_warning` (WARNING / COHERENCE), `breaker_critical` (CRITICAL or unknown) | the user, typed `RESUME` + operation token, only while the breaker is NORMAL; the next tick re-checks (`recheck`) |
| `revoked` (final) | `user`, `kill_switch`, `restart`, `session_reset`, `clock_anomaly`, `unhealthy` | a new grant |
| `expired` (final) | `ttl` | a new grant |
| `ended` (final) | `complete` (the user confirmed) | — |

| Goal state | Meaning |
|---|---|
| `active` | work may continue (W5) |
| `paused(user)` / `blocked(<reason>)` / `budget_limited` | the grant is `paused(goal_<state>)`; reasons `no_progress`, `loop_detected`, `max_iterations`, `same_blocker`, `approval_timeout`, `verification` |
| `awaiting_verification` | an agent claim (W5) moved it here; only the user's verification may act. The grant stays `active` |
| `complete(verified)` | the user confirmed while the grant was `active` |
| `unmet(<grant end>)` | the grant ended first |

Rules (ADR 0011 §8, §11; each pinned by `tests/test_grants_state.py`):

* a grant pause never overwrites the goal's `blocked` / `budget_limited`;
* terminal grant states are final; a verification after the end is recorded as
  `late_verification` (`pass` / `fail`) and never revives anything;
* `complete` only when the user confirmed while the grant was `active`; an agent claim only
  moves the goal to `awaiting_verification`; a rejection sends it back to `active` and
  counts as a no-progress tick;
* a resume returns to the state before the stop (a halt that interrupted a user pause
  returns to that pause);
* the budget accumulates per grant and never resets; the TTL never extends;
* **automatic stops** (in memory at once from the breaker hook, the kill switch's listener
  and `/new`; otherwise the authority watchdog's pass every 15 s and every request — and
  `check_now(grant_id)`, which the W5 entry points `authority_record` and
  `claim_complete` go through): the kill switch → `revoked(kill_switch)`; an unhealthy
  authority or grant store → `revoked(unhealthy)`; the breaker leaving NORMAL →
  `halted(breaker_*)`; the wall clock going back since the last look, or wall and
  sleep-including clocks disagreeing by more than 5 s (a forward or backward jump) →
  `revoked(clock_anomaly)`; the TTL (the largest of the three clock deltas, so a sleep
  counts) → `expired(ttl)`; `/new` → `revoked(session_reset)`; every start →
  `revoked(restart)`. **Every stop advances the authority epoch** (stop id =
  `(incarnation, epoch)`); NORMAL returning, time passing or a chat message never release a
  halt.

Where a host has no separate sleep-including clock, a sleep looks like a jump and the
grant is revoked (fail closed).

A look reads the clocks **inside** the grant service's leaf-lock hold that applies them
(#627; in-memory `clock_gettime` only, no I/O), so concurrent looks (two `GET /api/grants`,
the watchdog's pass next to `check_now` or the executor's `authority_record`) apply their
readings in the order they were taken. Before, a reading taken outside the lock could
reach the grant after a later one and look like a monotonic clock going back (a false
`revoked(clock_anomaly)`). A real jump is still a `clock_anomaly`: no reading is skipped
or clamped.

## The grant store `grants.json`

In the data directory, `0600`, the authority files' safe-open rules (no symlink, hard
link, FIFO or other owner; atomic replace with flushes; re-read before every replace and
on every watchdog pass). The newest 4 records, each the whole state, chained by `seq` and
the previous record's hash. **A grant is never persisted as active**: a live grant is
written as what the next start makes of it — `revoked(restart)`, its goal `unmet` — with
`restart_from` naming its live state; at start each such entry is recorded as the
lifecycle transition `→ revoked(restart)` and written back. At most 16 grants are kept;
the oldest ended ones are dropped first — their goal text stays in the older of the 4
retained records until those roll out (four more writes). Writes run on one bounded
single-flight writer thread (`grants-persist`, the W2 kill-persistence pattern): a request
waits at most 1 s for it (an issue 5 s), the shutdown step at most its budget (a write
already running on a hung disk is left behind and the step says `pending`); a look writes
only on a state change (clock readings stay in memory). The writer never writes what the
reader would refuse.

An unsafe, unreadable, malformed or changed file, or a failed write, makes the stage
**unhealthy, sticky**: every live grant is revoked (`unhealthy`), nothing is issued, and the
file is never overwritten. Repair: stop the server, keep a copy of `grants.json`, move it
aside, start again.

## Human control plane

Routes (only with `autonomy.stage_c`; behind the server's Host / Origin / CSRF checks; no
model-visible tool; see [admin-api-permissions](admin-api-permissions.md)):

| Route | What |
|---|---|
| `GET /api/grants`, `GET /api/grants/<id>` | structured state only: ids, codes, counters, TTL left, budget left, and (W6c) `claim_evidence` — the last claim's codes and counters plus, while it waits, `waiting_sec` / `stale` / `stale_after_sec` (`null` before a claim). Never the goal text, never the workspace path |
| `GET /api/grants/<id>/goal?confirm=SHOW` + header `X-Stira-Goal-View` | the goal, for the issuer only and **once**: the one-time view token of the issue answer (kept in memory, consumed by the first view; a restart ends it; never in a replayed answer) and the typed word `SHOW`. `Cache-Control: no-store` |
| `POST /api/grants/issue` | typed `confirm: "ISSUE"` **and** a host-confirmed operation: the first request answers 428 `grant_confirmation_required` with the operation and its one-time confirmation token; `POST /api/grants/operations/<op>/confirm {token, acknowledged: true, instance}` (a same-origin browser fetch only) confirms it — or, with `STIRA_YOLO_HOST_CONFIRM=command` / `dialog`, the person confirms on the host (the grants use the YOLO desk's operations and its effective channel; a browser confirmation is then refused, `host_confirm_on_host`; the page polls `GET /api/grants/operations/<op>`); the same request is then sent again with `X-Stira-Host-Operation: <op>`. The operation is bound to the incarnation, the body and the service's generation; single use; a stale target is refused; **the same op id with the same body returns the original result** (marked `replayed: true, historical: true`: the grant as it was then, not now), with another body it is refused. Every operation step is refused while an agent tool call runs (the tool-call tracker is on the agent's hooks whenever stage C is on; no tracker → refused). **Fenced:** the authority epoch is captured first; the grant is written durably (as `revoked(restart)`) before it is published with the W1 `commit_at_epoch`; a breaker departure, `/new`, the kill switch or an unhealthy authority in between aborts it (`grant_issue_aborted_epoch`); a write that fails or does not return in 5 s refuses it (`grant_store_unhealthy` / `grant_store_timeout`) |
| `POST /api/grants/<id>/revoke` | one press, always available — also while the store or the authority is unhealthy, and on a hung disk: the revocation and its epoch advance happen in memory before any store I/O (`persisted: false` when the write did not finish in 1 s) |
| `POST /api/grants/<id>/pause` | one press |
| `POST /api/grants/<id>/resume` | a pause: the operation token; a halt: typed `confirm: "RESUME"` + the token, refused unless the breaker is NORMAL now. The operation binds the grant's generation, state and stop epoch: a resume for an older stop is refused (`stale_operation`) |
| `POST /api/grants/<id>/verify {"decision": "confirm" \| "reject"}` | a host-confirmed operation, like issue; only while the goal is `awaiting_verification` and the grant `active`. Right before it commits, the clocks, kill, health and breaker are read again and the commit is fenced (W1 `commit_at_epoch`); the clocks are read once more **inside** that fenced step, so no gap is left between the TTL check and the commit (W4.5): a grant that expired or stopped since the check takes that stop and the verification is recorded only (never `complete`, never an active goal again); after the end the first one is recorded only (`late_verification`) |
| `POST /api/grants/operations/<op>/cancel` | cancel an open operation |
| `POST /api/authority/kill` (W2) | the kill switch revokes every grant (in memory, at the kill's epoch; the grants' lifecycle events and store write follow the kill's own persistence attempt and cancel kickoff) |

Every grant and goal transition is a W2 `lifecycle_event` (subject `grant` / `goal` with
the grant id, codes only); stop-class transitions are stop notices under the existing
keys (`stop|grant|*|<reason>`), the store's health is subject `grants` / `store`.

**Dashboard:** with stage C on, the settings page has a "Goal grants" section: the
headline, the goal state, TTL left, budget left, iterations, the stop reason; Revoke /
Pause / Resume / Verify per grant, the kill switch and the issue form. No goal text is
rendered except in the issuer-only view. W6c: a claim's evidence next to Verify (below).

## Setting up host confirmation for stage C

Issue, resume and verify are loosenings: they need a person **on the host**, not only a
browser request (every proof of the browser channel is handed to the requesting process,
so a same-user process — an exec child, an MCP server, a detached job — could drive it).
Stage C therefore uses the YOLO desk's host channel: set `STIRA_YOLO_ALLOW` and
`STIRA_YOLO_HOST_CONFIRM=auto` (on macOS the OS-authenticated desktop dialog), `dialog` or
`command` (confirm with `stira approvals decide <short>` on a TTY). Without such a channel
these operations are refused (`host_channel_required`, with the setting to change); the
start-up line says which layer applies (`host confirmation (dialog)` / `(command)`, the
weaker browser layer, or refused). The browser confirmation is accepted only when the user
chooses it explicitly with `"allow_browser_confirm": true` in `autonomy` — the weaker layer,
logged at start. Revoke, pause and the kill switch never need a confirmation. With stage C on,
`/new` always advances the authority epoch (a narrowing: it also voids other holders'
permits issued before it).

## Operator procedure

1. Turn it on: `gateway-config.json` → `{"autonomy": {"stage_c": true}}`, set up the host
   confirmation (above), restart.
2. Issue: settings → Goal grants → fill the form, type `ISSUE`, press Issue, tick the
   confirmation. The grant is `active`; **nothing runs** by itself. With
   `"stage_c_exec": true` as well, press **Run one iteration** for each iteration; answer
   its cards on the approvals page; verify a completion claim with Verify.
3. Stop: Revoke (final), Pause, or the kill switch. Resume a halt with `RESUME`.
4. A restart revokes every grant (`revoked(restart)`): issue a new one.

## Known limits

None of these widens authority; each fails closed. Open items stay tracked in #620.

* **Kill ordering (W4.5, #620 W5-3).** The kill listener only revokes in memory. The
  grants' lifecycle events and the store write request run after the authority's kill
  persistence attempt and the start of its cancellations: a blocking recorder delays
  them, never the kill. A crash in between loses at most those events (a live grant is
  stored as `revoked(restart)` anyway).
* **Clock readings outside the lock (#627).** Only the look's and the verify commit's
  readings are compared with the grant's last reading; both are taken under the leaf
  lock. The other readings outside it — the issue's (the grant is published later, so
  every look reads after it), a view's TTL left, the executor's `_admissible` TTL check
  and the permit's consume clock, the host operations' expiry — are compared only with
  the issue reading or a deadline: a racing reading is older by the race window at most
  (a TTL or an expiry ends that much later, never a stop or an anomaly).
* **Verification decision instant (W4.5, #620 W5-2).** It is the clock reading inside the
  fenced commit. A TTL that passes later (while the write runs) does not undo a
  `complete` (a completed grant grants nothing).
* **Writer failure (W4.5, #620 R1).** An unexpected exception in the store writer latches
  the stage unhealthy (`grant_persist_failed`) and revokes every live grant at once
  (`revoked(unhealthy)`).
* **Replays (W4.5, #620 R2).** A replayed operation answer is `historical: true`: it
  describes the grant when the operation applied; read `GET /api/grants/<id>` for now.
* **Executor residuals (W5).** Cancellation is at tool boundaries: a `run_checks` or
  `editor` call already running finishes; a stop during it (a narrowing, the TTL, a
  person's turn, the wall time) is recorded as `<tool>_completed_after_stop`.
* **A stop during a model call (W6-F, #640).** A stop does not cancel the provider request
  that is running: the iteration waits for it — a 0.5 s grace, and while a model call is
  reading its response up to 5 s more (`STOP_DRAIN_SEC`; none for shutdown or the wall
  time). The whole response is read before the child sees it, so a call that completes
  reports its usage: that is charged (total and per tick; the tick cost row of a
  heartbeat-started iteration carries `tokens_in` / `tokens_out`), the rest of the
  reservation is released once, and the residual is `model_completed_after_stop`. The
  answer itself is discarded — the child never sees it, also after a person's turn (a
  text-only answer does not enter the child's history or turn processing). No effect
  follows: every later boundary refuses. A call still running after the wait is given up:
  its provider request is told to stop and it is abandoned (below) with its whole
  reservation charged. Measured in the W6e smoke (one run, estimate): before this, each such
  stop was charged about 71–74k tokens (the reservation) for a call that used about 2.7k.
* **Abandoned provider call (W5).** Python cannot cancel a blocked socket read. The
  provider call runs on its own thread (`grants-call-<short>`); a stop ends the iteration
  thread promptly (after the grace and the drain above) and the call is **abandoned**: its late answer is
  discarded (every later boundary refuses: no tool, no state change), its whole
  reservation stays charged, the residual code is `abandoned_call`, it is counted
  (`abandoned_calls` in the executor status) and **no new iteration is admitted while it
  lives** (`abandoned_call`). The shutdown step answers `abandoned` then. Its lifetime is
  bounded only by the provider client's own read / connect timeouts: the executor uses
  the chat agent's shared model object and does not change its client.
* **A same-user filesystem actor (W5, out of scope).** Another process of the server's user
  can rename the authorized directory between the editor sandbox's final check and the
  write; the write then lands in the renamed directory. The root is pinned at admission and
  at every consume, but not across that last instant. This is the same assumption as W1, W2
  and W4 (a same-user process can edit anything, `grants.json` and the settings included).
  The agent's own tools cannot do it: none renames, and `run_checks` works in a throwaway
  copy whose sandbox refuses writes outside its run directory (pinned by an xfail test).
* **Debug-log filter (W5).** The filter that drops the provider transport's and the tool
  executor's DEBUG lines is installed with the executor and stays for the process lifetime
  (cheap; it never hides INFO or above).
* **`/step` and same-user processes (W5, by design).** `/step` *exercises* a grant a person
  already issued with a host confirmation; it creates no authority (issue, resume and
  verify keep the host operation). The dashboard's CSRF / Origin boundary does not tell a
  same-user local process (even a cookie-less API client that fetched the CSRF token) from
  the person — for any route, approving a chat card or editing a setting included — and
  the model has no route to the dashboard API (the own-dashboard guard). A step spends only
  the grant's own budget, TTL and scope; it is refused while an agent tool call runs. An abandoned call's
  `run_checks` run still in flight ends at its own timeout (≤ 300 s, in its temp directory).
* **Prompt cache (W5).** The iteration's model call runs **without prompt caching** (the
  wrapped model is a custom model to the child; a directly supplied model configured with
  a cache would cache the static prefix only — the context block is always after it). The
  trusted block costs about 355 tokens per model call by Stira's estimate (355–515 after
  the measured estimate error of English text; not a tokenizer measurement); the goal up
  to about 500 more.
* **Other turns (W5; settled in W6b).** Every holder of the turn lock pre-empts: the lock is
  one `TurnLock` (`src/stiracore/core/turn_lock.py`) and it is the only way to take it, so
  each acquirer — a person's chat message (also a progress report's reply / redirect), a
  webhook turn, the automatic continue, the resume of a restored card, `/new`, a procedure
  approval — raises the signal **before** it tries the lock (W5 left the webhook, the
  automatic continue and the resume without it: an effect could finish during such a turn
  with no residual and the end bookkeeping wrong). The signal moves the counter and sets an
  "arriving" mark until the attempt has ended; the executor reads the mark first, then the
  lock, so no press is admitted from the signal until the lock is released — also while a
  resume waits for the lock, or in the instant between the signal and the lock. A running
  iteration yields at its next boundary (`yielded(user_turn)`): before each model call
  (again right before the request is sent), tool call, approval wait and report; a waiting
  card is withdrawn at once. Residual: a provider call already in flight is not interrupted
  (it yields after the answer; the answer produces no further effect), nor is a tool call
  already running (recorded as `<tool>_completed_after_stop`). An attempt that then finds
  the lock taken also signals (a refused chat message, `/new` or procedure approval with
  `turn_running`): harmless, no iteration passes a boundary while the lock is held.
  With the autonomy switches off the lock has no signal: it is a drop-in for the plain
  lock (same return values and exception types, tested side by side).
* **The enumeration test and its limit (W6b).** A test reads `src/stiracore` and all of
  `dashboard/` (AST) against two allow-lists: every **use** of the turn lock must be a known
  form (`.acquire()` at a listed site, `.release()`, `.locked()`, `with` at a listed site, a
  binding to `TurnLock(…)` / `TurnLock.of(…)` or another name, the `turn_lock=` keyword, a
  truth / `is None` test); a bound method, the lock passed to a helper, `vars()` /
  `getattr()`, any other attribute, and any mention of the private plain lock fail it
  (negative controls pin each shape). Turn starts are matched by the callee's name
  (`agent` / `_agent` / `runner` / `run_turn`, `stream_async` / `invoke_async`,
  `resume_approved_request`, a thread `target=` of those) and each one is classified. The
  check is **syntactic**: a lock reached dynamically (a computed attribute name, `eval`) or
  a turn started through a callable named otherwise is not seen; such code is caught by
  review, not by this test. W6-H: every `TurnLock(…)` construction (the class under any
  imported name) must be one of two listed sites, one each (the server's signalled lock,
  the observer app's) — so a module that stores an unsignalled `TurnLock(threading.Lock())`
  in place of the shared one is reported even when it never acquires it; the unbound
  `TurnLock.acquire(lock)` is an acquire site too (it still signals, tested); and a runtime
  test walks the built server's wiring (extensions, blueprints, view functions and their
  closures) and requires every reachable turn lock to be the one shared, signalled
  instance (and no plain lock under a turn-lock name). **W6d placeholder:** every turn or
  goal-iteration start in a heartbeat / scheduler module must be registered in the test's
  `W6D_STARTS` (its holder an acquirer of the shared lock, or an `iteration:` press);
  an unregistered one fails the test. W6d registered its one caller: the scheduler's
  `GrantTicker._press` (an `iteration:` press of the executor's own `step`).
* **Independent background jobs do not pre-empt (W6b decision).** Scheduler agent jobs
  (`GatedJobExecutor`, ADR 0024, including an approved run's resume), memory jobs and the
  distillers run their **own** child agents — their own session and engine, budget
  and approval gate — and never take the turn lock; they neither pre-empt an iteration nor
  are pre-empted by one. **Decision (W6-H, for W6d):** they stay independent of an
  unattended iteration, too — nothing serialises their file writes with an iteration's,
  and no lock is added. An unattended iteration is confined to its grant's workspace
  subtree (the editor and `run_checks` stay inside it, and the existing path floors —
  write roots, the secret and config floors — apply to every writer); a person must not
  rely on exclusivity inside that subtree (a scheduled job or another process may write
  there at the same time; the fresh read at each end then sees it as a change).
* **Still open:** the UI rebuilds the cards on
  every poll (R3); a wasted confirmation after a generic epoch move and the per-call log
  line (R4); a cumulative wall-vs-boot drift above 5 s — e.g. an NTP step — revokes
  (`clock_anomaly`) (R5); stricter validation of older snapshots and goal text kept in
  them until the window rolls (S1, S2); a `grants.json` from an early branch head does not
  load (S3, dev data); a same-user OS process can edit `autonomy` (C1, documented above).

## The goal iteration executor (W5)

`src/stiracore/grants/executor.py`, built only with gateway `autonomy.stage_c_exec: true`
(it needs `autonomy.stage_c`; off: no executor object, no route, no thread, no file).

**Trigger.** `POST /api/grants/<id>/step` (one press in the panel, behind the server's
Host / Origin / CSRF checks; no typed word and no host operation — the grant was
host-confirmed at issue; never a model-visible tool) starts **one** iteration in a
server-owned thread (`grants-iteration-<short>`) and answers at once. `GET
/api/grants[/<id>]` shows `iteration`: `running` / `yielded` / `idle`, its end code
(`done`, `claimed`, `reported_blocked`, `yielded_user_turn`, `permit_stale`,
`budget_refused`, `token_count_unavailable`, `approval_timeout`,
`run_checks_unavailable`, `timeout`, `model_error`, `tool_refused`, `stopped`,
`shutdown`, `failed`, `workspace_changed`, `no_action`), a detail code, counters (model calls, tool calls, writes, checks,
approval waits, reports), a waiting card (`card: waiting`) and residual codes — never
free text.

**Admission.** At the press the grant record from the service's look
(`authority_record` / `executor_record`: `check_now`) must be `active` **and** its goal
`active` (`awaiting_verification`, `paused`, `blocked`, `budget_limited` admit nothing),
with no kill, a healthy authority and store, the breaker NORMAL, iterations below the
maximum, TTL left on the wall **and** the monotonic clock, budget left, no chat turn
running, no abandoned provider call alive, the subtree still the issue-time directory, the
trusted playbook readable, and no other
iteration running (one per server; v1 has one live grant). A `recheck` left by a halt
resume is satisfied there: the checks above ran now, then it is cleared. Refused:
`409 grant_iteration_refused` with a `reason` code.

**Session.** The iteration runs in a dedicated child built like `delegate`'s
(`agent/grant_child.py`): session `grant:<id>`, a fresh Neural State (no reward or
emotion update reaches the main session), its own `CheckpointHooks` with the server's
live rule reader, the floors, `IdentityGuard` and the binding verifier, the write roots
narrowed to `scope.workspace_subtree`, checkpoints in memory only, no observer hooks.
The chat session, its history and its prompt-cache prefix are untouched.

**Tools** = `scope.tools` ∩ {`file_read`, `editor`, `run_checks`, `memory_read`,
`memory_search`, `memory_list`, `current_time`} ∩ the live rule (Never: absent) — plus the
harness tool `goal_report`. No shell, web, delegate / sub-agent, handoff, allow-again,
integration, scheduler-job or MCP tool (a test enumerates the child's list). An Ask is
answered **only by a person's card** through the server's approval broker (no YOLO, no
allowance); the card names the session `grant:<id>`, belongs to no chat turn, is live
only (a restart cancels it, `grant_stopped`) and is withdrawn when the iteration stops.
An unanswered card ends the iteration and blocks the goal: `blocked(approval_timeout)` —
nothing retries it. The durable approval record and the history of a grant card keep
**codes only**, built from fixed fields (the tool, whether the path is inside the subtree,
digest prefixes of the path and of the arguments, their size, the grant id) — no
model-chosen string: not the path, not a `cwd`, not the content. **The live card** (the
normal pending-approval view of the approvals page and the chat) shows the arguments, path
included, because the person must see what they approve; it has the exposure of every
ordinary approval card (the local dashboard, behind Host / Origin / CSRF; the model has no
access to the dashboard API). It is held in memory only and is gone once the card is
withdrawn (the iteration stopped), decided, or the server restarted. The `editor` is confined to the subtree (resolved, no symlink
escape), checked before the permit and **again after the consume**; at admission and at
every `editor` / `run_checks` consume the subtree itself must still be a real directory
whose resolved path is the one stored at issue (never re-resolved): otherwise the effect is
refused and the goal is `blocked(workspace_changed)`. `run_checks` needs,
at each call, the scope, `autonomy.run_checks_unattended` and a currently verified
`os_sandbox` (and `run_checks` itself confirms the sandbox before the run): any downgrade
refuses the call and blocks the goal, `blocked(run_checks_unavailable)`.

**One permit per effect.** Every model call, every tool call, every approval wait and
every `goal_report` takes a W1 permit bound to (incarnation, epoch, grant id, generation,
the TTL pair; a model call also its reservation id) and consumes it right before the I/O.
Before the consume the look runs again (clocks, TTL, kill, health, breaker, grant and
goal state) and the iteration's own stops are read (a person's chat turn, the wall time,
shutdown). A stop between authorize and consume → `permit_stale`, no effect. After a wait
(a card, a slow model answer) the next effect takes a new permit; a permit never outlives
its epoch. A consumed effect is in the W1 in-flight set: a narrowing (kill, revoke,
breaker, `/new`, …) cancels the child at its next safe point and withdraws a pending card;
an effect that still finished is recorded (`<tool>_completed_after_stop`).

**Budget.** Before each provider request the **final** input (the exact messages, tool
specs and system prompt sent, the context block included — **formatted by the model's own
formatter**, so request overrides such as `additional_args` are part of it) gets a **local,
conservative upper bound**: one token per UTF-8 byte of its JSON form plus fixed overheads
per request, message and tool (every token of a byte-level tokenizer is at least one byte);
the output limit is the one the formatted request carries.
**No provider request is made to count** (the provider's `CountTokens` would send the goal
before any check). `bound + max_tokens` is reserved against the grant total, the per-tick
limit (one tick = one iteration) and any shared pool; a call that does not fit is not sent
(`budget_refused`). No formatter, a request that does not serialize to JSON (raw bytes,
e.g. an inline image or document), or no output limit in it → no call
(`token_count_unavailable`). The iteration uses its **own copy** of the chat agent's model;
for Bedrock the copy's client checks the stops, the turn lock and the grant and takes a
permit right before **every** terminal request (`converse_stream` / `converse`), so a
re-request the SDK makes inside one model call (the tool-result turn recovery) after a stop
sends nothing; botocore's transport retries of the same HTTP request are not separate
requests. Every other provider is refused (`provider_unsupported`), also the Strands
providers with a lazy per-call client (OpenAI, Ollama, LiteLLM, Gemini, Mistral, …): only a
provider whose every terminal request is guarded runs an iteration. Settled with the reported usage
(input + output + cache tokens; the difference is refunded; unknown → the whole
reservation) through `state.charge`; every reservation ends exactly once. This holds for a
call that completes after a stop too (W6-F, #640, see [Known limits](#known-limits)); only a
call given up, or one whose usage is unknown, keeps the whole reservation. The bound is
about 4× Stira's own estimate of the same request (measured: 6,466 vs 1,618 for a first
call with the editor, `file_read`, `current_time`, `run_checks` and `goal_report`): with
the issue form's defaults (operator decision 2026-10-25: total 2,000,000, per-tick 200,000;
the cap stays 5,000,000 and per-tick ≤ total) and `max_tokens` 8,192 an iteration gets
about 25 model calls before `budget_refused` (measured with about 1 KB of new text per
round; larger tool results give fewer; 20,000 per tick gave about 2). The bound is not
loosened; choose the per-tick limit accordingly. The total never resets. Each iteration is counted
(`state.count_iteration`); whether it made progress is decided by its **outcome**, not its
activity (W6a, [below](#progress-by-outcome-w6a)).

**Context.** Each model call's input ends with one block appended to a copy of the latest
user message: the trusted playbook (`src/stiracore/grants/dev_playbook.md`, edited by a
person only, loaded read-only; its SHA-256 is the `playbook_<12 hex>` code of the
iteration's start event) and the goal as the trailing part (#254). The goal is never in
the child's history, the main session, a checkpoint, a lifecycle event, the outbox, a
view, an error or a log line (the providers' DEBUG request dumps are dropped while the
executor exists).

**Reporting.** `goal_report(kind, code)` with fixed enums only: `progress` (`step_done`,
`tests_written`, `checks_run`, `investigating`; kept in memory), `blocked`
(`needs_decision`, `out_of_scope`, `missing_input` → goal `blocked(<code>)`),
`claim_complete` (`goal_met` → goal `awaiting_verification`, **never** `complete`). Both
end the iteration. A reply with no tool call and no report ends it as `no_action`.

**Pre-emption and limits.** Every turn-lock holder (W6b: one `TurnLock`; a person's chat
message, a webhook turn, the automatic continue, a restored card's resume, `/new`, a
procedure approval) signals before the turn lock is tried; the iteration yields at its next
boundary (`yielded(user_turn)`: the running effect is not
interrupted, a waiting card is withdrawn). `/new` revokes (`revoked(session_reset)`) and so
stops it. `autonomy.iteration_wall_sec` (default 600) ends it as
`blocked(iteration_timeout)` (checked at every boundary and in the watchdog's pass). The
shutdown step (`grant_executor`, before the approvals close) stops and joins it within its
budget (`pending`: a thread still in a provider call).

Lifecycle events: subject `grant_iteration` (the grant id): `idle → running` with the
playbook code, `running → idle | yielded` with `iteration_<end>`.

## Progress by outcome (W6a)

W5 counted an iteration as progress when it wrote a file or ran the checks. That counts
activity: an iteration that rewrites the same bytes or reruns the same failing checks
looked productive forever, so `blocked(no_progress)` could never fire. W6a decides from
the **outcome**, deterministically, with no model call, nothing widened, and still the
user verifies every completion claim. There is **no model-based loop judge**.

**The fingerprint.** When an iteration ends, the executor builds one keyed fingerprint
(`src/stiracore/grants/progress.py`): HMAC-SHA256 with a random per-grant key held in
memory only (a grant never survives a restart, nor does its key) over a canonical encoding
of

* the **workspace identity**: a **fresh read**, at the iteration's end, of every file the
  grant has written — an HMAC of its relative path and an HMAC of its current bytes; a file
  that is gone (deleted or renamed by anyone) is an explicit "missing" marker. A file is
  tracked once an iteration changed it (its bytes at the end differ from its bytes before
  that iteration's first write to it), or wrote it without the change being observable
  (interrupted, or unknown) — such a file enters with its bytes **before** that write as its
  last known state, so it moves the identity only when its bytes changed (W6-H: a same-bytes
  rewrite of a new file lost to an interruption is still nothing; a real change is seen at
  the next end), and one whose bytes before the write could not be read is not tracked from
  that end; a rewrite that left the same bytes adds nothing. At most
  **256** paths, the most recently written; older ones leave the identity silently (no
  counter). A stat cache (device, inode, size, mtime, ctime) only saves re-reading an
  unchanged file; a changed one is always read again; an unknown end keeps the entries it
  validated (W6-H), so a tracked set that fits the budget heals after one settled read. So a person's edit, rename or delete
  of a written file between iterations is a change, never a false loop;
* the iteration's check outcomes as **one set of per-check rows** — the union over every
  `run_checks` call of the iteration (no duplicates, sorted: running the same checks again,
  more often, in another order, or split over another number of calls is the same outcome).
  So a check result is bound to the files it ran on.

The agent's `goal_report` is **not** part of it (a label is not an outcome); it only drives
the same-blocker rule.

| Tool | What enters the fingerprint | Stripped (never part of it) |
|---|---|---|
| `editor` | the final bytes of a changed file, with its relative path (as part of the state; over 64 MiB: its size and a 3 MiB sample of its bytes) | mtime, inode, mode, the command used, the bytes of a write that changed nothing (for every size) |
| `run_checks` | per check (≤ 16 per call, ≤ 64 distinct per iteration): kind, status, reason code, exit class (`zero` / `one` / `other` / `none`), counts passed / failed / errors / skipped / findings (clamped to 1,000,000); a result without checks is one row (status, reason) | durations, pids, temp paths, timestamps, ids, failure ids and texts, output, tree changes, limits, the directory, containment, the call's overall status, the order, number and grouping of runs |
| read tools | nothing | — |

Codes outside `[a-z][a-z0-9_]*` (≤ 40) enter as `other`.

**Reading a file's bytes** never waits on a FIFO and never follows a symlink: it is opened
non-blocking without following a link and `fstat`-ed before any read; only a regular file is
read (a FIFO, a device, a directory → unreadable). `O_NONBLOCK` does **not** bound storage
latency; a per-iteration budget does: at most **256 MiB** are digested per iteration
(before-write and end reads together; more → unknown), and files about to leave the identity
are not read. A file over **64 MiB** (e.g. grown by someone else) is identified by its size and a
digest of a bounded sample of its bytes — its first, middle and last 1 MiB (3 MiB charged to
the budget; marked, so it never equals a content digest), **never** its mtime or inode: a
`touch`, a backup that copies the same bytes to a new inode or any other metadata-only
change is no progress; a size change or a change in a sampled region is. Known limit: a
same-size change confined to the unsampled regions of such a file is not seen (the editor
does not write files that large). A digest is cached only for a file whose mtime is at least 2 s old (1-second
timestamps). A torn read is refused:
the open file's device, inode, size, mtime and ctime before and after the read, and the
path's own `lstat` after it, must agree. This catches an ordinary concurrent writer or a file
replaced at the path; it is **no** defence against a same-user process that rewrites a file
and restores its times (the same-user filesystem race of W5's known limits).

**Classes.** `none`: nothing observed — no check, and the fresh workspace identity is the
one of the last known outcome (only reading, only reporting, a rewrite that left the same
bytes, all on an unchanged workspace): no progress, one place in the history. A change of a
tracked file by **anyone** (the agent, a person) is observed, even when the iteration's own
edit was a no-op.
`unknown`: anything needed could not be read (a file digest, a torn read, a FIFO, a
permission error, a check result, more than 256 files written or 64 distinct check rows in
one iteration): **no progress (fail closed), not in the history, no other streak changes**;
the next end reads everything fresh again, so the same unreadable file is unknown again.
`repeat`: the fingerprint is among the last 16 known outcomes; `new`: otherwise. **Only
`new` is progress.**

**Rules** (named constants in `progress.py`; defaults per ADR 0011 §8):

| Rule | Blocks the goal |
|---|---|
| `no_progress_ticks` (default 3) counted iterations in a row that are not `new` | `blocked(no_progress)` |
| a `repeat` of one of the last **6** known outcomes (A-A, A-B-A-B, A-B-C-A; `none` iterations take a place, `unknown` ones do not) | `blocked(loop_detected)` |
| the same `goal_report(blocked, code)` on **3** counted iterations in a row | `blocked(same_blocker)` instead of `blocked(<code>)` |
| **2** `no_action` iterations in a row (since the last resume) | `blocked(no_progress)` |
| `max_iterations` (unchanged) | `blocked(max_iterations)` |

**Why A-A is a loop.** Two counted iterations in a row with the same workspace identity and
the same checks did nothing the second time (the agent reran what it had just run, on the
same files): waiting for three would spend one more model iteration on nothing. The cost:
the first identical rerun blocks — also an iteration that only re-reads and re-runs the
checks to orient itself; the person resumes, and the history is kept, so **after a loop the
agent has to change something first** — rerunning the same checks on the same files blocks
again at once. Re-applying a change a person reverted reaches a state already seen: with
the same checks that is a loop too (no net change since then).

A blocked report still blocks the goal (`blocked(<code>)`), at the iteration's end once its
outcome is committed; the person resumes, and the third identical blocker in a row of
**known, committed** outcomes says so with its own code (an unknown or interrupted blocked
report blocks with its plain code and does not feed this count) — the streak survives that
resume (otherwise it could never reach 3) and restarts on any other counted end. It is a
label, not a lock: a new question with the same code after the person answered is also
`same_blocker`, and it stays resumable. A resume restarts the no-progress streak (W5) but
keeps the history, so a loop that spans a resume is still found. A completion claim is
never blocked by these rules: it goes to the person. A provider error (`model_error`) after
a consumed model call is a counted iteration with no progress (unchanged from W5).

**Interrupted iterations** — a person's chat turn (`yielded_user_turn`, or a turn that
arrived after the last effect), `shutdown`, the kill, a revoke / any narrowing (`stopped`),
`permit_stale`, an abandoned provider call, or the grant no longer in the state the
iteration's own end left it in (revoked, paused, halted, or blocked by someone else, or
any stop of the grant since the iteration started) — never change a streak or the history.
The interruption, including the grant's own state, is read again **at the commit**, after
the digests are read, so a shutdown, a revoke or a pause during those reads counts as one.
The iteration's own end is not an interruption: its claim is checked as such, and its own
block (its blocked report, its own timeout block) is applied **after** this check — so a
person's stop during the reads (even a block with the same reason) is never taken for it.
The validation and the count are **one step** (W6-H): the validation reads the grant's
version (its `generation`, moved by every change of the grant) and the service compares it
inside the same locked step that applies the own block and counts the iteration
(`commit_iteration`) — a person's block, pause or stop that lands after the validation makes
the iteration an interruption there (no history, no streak), and a detector's block (loop,
no action) lands only on the version that count left. They count toward `max_iterations` (they consumed an
effect): five chat messages during a grant leave the goal `active`. The count does not depend
on what stopped the iteration (W6-F, #643): a revoke, `/new`, the TTL or the kill counts it
just as a pause does — on a grant the stop ended, only `iterations` moves (no streak, no state
change); an iteration that consumed no effect is never counted (below), whatever stopped it.

**An iteration that consumed no effect** (no model call, tool call, wait or report — a
setup failure, a budget or token-count refusal before the first call, a permit stale at
the first consume) is **not counted at all**, whatever its end: no iteration, no streak, no
history. It ends with its code (`failed`, `budget_refused`, …) and the person presses again.

**The cost of the loop rule.** A revert-and-retry is blocked as a loop only when the
**current** content of every file the grant wrote (read fresh, not only the ones this
iteration touched) **and** the iteration's check set are identical to one of the last six
known outcomes: that really is no net change, and the person resumes. Changing a file and
then running the same checks with the same failure count is `new` (the identity differs);
reverting one file while another file the grant wrote has changed is `new`; a person's edit
of a written file between iterations is `new`. A cycle over **more than 256 files** is not
guaranteed to be found (older paths leave the identity); `max_iterations` and the token
caps still bound the grant. Change detection is not a
judgement of improvement: an irrelevant edit is still `new`. The hard limits (tokens, TTL,
`max_iterations`) and the person's verification contain that.

**What is kept.** In memory only: per grant, a ring of 16 HMACs (32 bytes each, or `None`),
the paths it wrote (≤ 256, with their stat cache and the digests behind the last identity —
real paths, memory only, never shown or logged) and two small streaks, for at most 16 grants (the least recently used leave first;
a grant that has not ended is never evicted).
No path, file content, goal text, command, output or argument is in any store, lifecycle
event, outbox entry, view, the `/api/activity` grant block, the approvals database, an
exception or a log line (a marker test reads them all). The Stage board and the views show
nothing new (the end code `no_action` has its words).

## Completion claims: evidence and the waiting state (W6c)

In v1 the **person** verifies every completion: the agent only claims
(`goal_report(claim_complete, goal_met)` → goal `awaiting_verification`) and `run_checks` is
evidence only. W6c gives the person better information at that moment **without** changing
who decides and **without** letting the model write text into a store: codes only, measured
by the harness from what it observed (the runtime outcome, never the model's words —
OpenClaw `announce.md`). Nothing acts on it: no automatic reject or confirm, no model call.

**The record** (`claim_evidence` on the grant, in `grants.json` with the rest of the grant;
**one** per grant, replaced — never appended — by the next claim; a reject keeps it until
then; after a confirmation it stays as the audit trail and is shown read-only):

| Field | Taken | Meaning |
|---|---|---|
| `claimed_at` | at the claim | wall seconds of the claim (the clock reading the claim itself takes, under the service's lock; no reading → the claim is refused) |
| `iteration`, `max_iterations` | at the claim | the claiming iteration k (it is counted at its end) of the maximum |
| `used_tokens`, `budget_left` | at the claim | the grant's tokens so far / what is left of its total |
| `ttl_left_sec` | at the claim | the TTL left at that instant |
| `checks` | at the iteration's end | **fail closed** — `pass` only when it is positively established. The verdict of **every** `run_checks` call since the grant's last editor write (combined across iterations while the workspace stays the same): any call `fail` → `fail` (a row failed, errored or hit a limit — a narrow passing call afterwards never hides it); else any `unknown` → `unknown`; `pass` only when every call passed. A call is `unknown` when its result cannot be read or contradicts itself: a runner that raised, no check rows, an unknown status, more than 16 rows, a row marked `consistent: false` (its exit code and its report disagree), a passed row whose counts are missing, not numbers or report a failure, an overall status that is not the worst row's, or `real_tree_changed`. A call that overlaps an editor write does not count. An unread workspace identity never lets a recorded `fail` (or
`unknown`) be forgotten: the calls replace an earlier verdict only when both identities are
known and differ (positively another workspace), otherwise they combine. Then `pass` /
`fail` are shown only when the claim's workspace identity (read by its own end) is the one the checks ran on (read at the end of their iteration): another one → `not_run` (the files changed after the check — by the grant, a person or anyone, between iterations included); either unread (e.g. the check's iteration was interrupted) → `unknown`. `not_run` also when none ran or `run_checks` refused |
| `check_rows` | at the iteration's end | per check (≤ 16, only with `pass` / `fail`): kind (`pytest`, `unittest`, `node_test`, `ruff`, `compileall`, else `other`), status (`passed` / `failed` / `error` / `limited`) and the counts passed / failed / errors / skipped / findings (clamped to 1,000,000). No test id, message, output, path or duration |
| `workspace` | at the iteration's end | `changed` / `unchanged` since the previous claim (the first claim: since the issue), from the W6a workspace identity (a fresh read of every file the grant wrote); `unknown` when this claim's or the previous claim's identity could not be read (an interrupted or unknown outcome). No digest, path, HMAC or file name is stored |
| `progress` | at the iteration's end | the claiming iteration's W6a outcome class: `new`, `repeat`, `none` (nothing observed), `unknown` (interrupted — e.g. a person's turn after the claim — or unreadable) |

The claim and the record's first part are one step of the grant service (the claim is the
one lifecycle event, `goal: active → awaiting_verification`, `goal_claimed`); the measured
part is filled in by the claiming iteration's end, inside its board write — **not** a state
change: no generation, no event, no notification, only the record is written again. It
fills only the record of **its** claim (named by `claimed_at`), only for a **committed** count
of the claiming iteration, and only if **at the publication itself** — in the one step that
**installs** the record, under the service's leaf lock and inside the authority's W1 fence
(`commit_at_epoch`) — the grant is still the version that count left (its `generation`), the
authority epoch read when the iteration **started** has not moved (any narrowing since — also
one after the claim, during the end's reads — or the kill), no person's turn has signalled or
is arriving since the iteration started (the lock-free turn mark and kill flag are read last,
right before the install), and the claim still
waits on an active grant: a revoke, a pause, a kill, a narrowing, a person's turn or the
person's verification in between publishes **nothing** — the measured codes stay `unknown`
(a confirmation before the measurement keeps the audit as the person saw it). It never
changes a state. The checks verdict, the identities it is bound to and the claim's workspace
identity are kept in memory beside the W6a tracker (a grant never survives a restart).

**Hostile models.** The model's only input is `goal_report(kind, code)` with fixed enums: a
marker in any field, an extra field or a non-object input is refused (`report_invalid`) and
nothing is recorded. A second claim in the same turn, or a claim after a blocked report, is
refused at its boundary (the first end wins): one claim, one event. A claim while a
person's turn runs yields before it applies; a turn that arrives right after it leaves the
claim standing with every measured code `unknown` (not committed: nothing is published). An immediate claim (no other
effect) is `checks: not_run`, `workspace: unchanged`, `progress: none`.

**One event, one notice.** The claim is one lifecycle event, and `awaiting_verification`
is a stop-class state, so it is one stop notice under the existing key
(`stop|goal|*|goal_claimed`), unread until the person acknowledges it. The measured codes
and every look add nothing. A new claim (after a reject) is a new event: it joins that same
notice (count + 1, its newest event, unread again) — the existing stop-notice rule, no new
coalescing. The person's verification of the claim (confirm, reject, or one recorded after the
end) acknowledges that claim's notice — by the claim's own event id, read in the
verification's own locked step and only when it belongs to the record being verified (its
`claimed_at`), never "the latest"; the outbox accepts it only while it is the notice's newest,
so a newer claim's occurrence — even one posted after the verification — is never
acknowledged by it.

**The waiting state.** While the goal waits (`awaiting_verification`, grant live), the
views add `waiting_sec` (from `claimed_at`, the view's clock reading), `stale_after_sec`
(gateway `autonomy.claim_stale_sec`, 60–86,400 s, default 1,800; anything else is the default
with the warning `claim_stale_sec_invalid`) and `stale` (`waiting_sec ≥ stale_after_sec`).
The settings panel shows "since <time> (n min)", the evidence rows and, past the threshold,
"still waiting for you"; a claim with `checks: fail` gets a "⚠" notice **in words** (not
colour alone). The Stage board shows the same (below). Display only: the threshold starts
no timer, calls no model and takes no action.

**Verify is unchanged.** The verify route keeps its authority (host-confirmed, `confirm` /
`reject`); a reject still counts as a no-progress tick (the W4/W6a rule); a late
verification after the grant ended is still only recorded; the record never revives a
terminal grant and a new claim is refused on one.

**Stored form.** `grants.json` entries carry `claim_evidence` (`null` or the record, checked
strictly: exact keys, known codes, ≤ 16 rows, a `pass` only when every row passed). An entry
written before W6c has no such key and loads as `null` (so a store from before the upgrade
stays healthy); any other missing or unknown key is still refused.

**Limits.** The identities are W6a's: they cover the files the grant wrote, so an edit of
another file is not seen. Within one iteration, an edit by someone else after a check and
before that iteration's end is read at the end and taken as the files the check ran on (the
identity is read once per end, no extra reads). `not_run` after an editor write is
conservative: a write that left the same bytes also makes it `not_run`. The counts in a row
are the numbers the checked program's report gives (bounded integers, never text). The
publication is linearised at its install: a stop that lands after it — a narrowing, a
person's turn, a kill, a revoke — is a later event, and the record keeps the `pass` / `fail`
that was true of the checked workspace when it was installed (the stop itself is shown by the
grant's state). The lock-free turn mark and kill flag are read immediately before the install;
one set in the last instant between that read and the dictionary store is taken as after it
(the same accepted instant as in W1's fenced commits).

## Heartbeat start (W6d)

**Off by default, twice.** The gateway switch `autonomy.heartbeat_start` (default `false`;
it needs `stage_c_exec`; the chat server's scheduler — `STIRA_SCHEDULER=1` — is what wakes
it) **and** the grant's own flag `heartbeat_start` (default `false`),
chosen at issue: the switch in the issue form ("Let the heartbeat start iterations", shown
only with the gateway switch on) is part of the issue body, so it is bound to the
host-confirmed operation's digest and named in its summary (`source: heartbeat_start`, on
the host dialog / command too). It cannot change afterwards — no route sets it; revoke and
issue a new grant. With either off there is no scheduler work for it, no thread, and the
answers (`/api/grants`, `/api/activity`, the grant store) are as before: the key
`heartbeat_start` and the `tick` block appear only on a grant issued with the flag.

**What runs.** The scheduler's fire kind `grant_tick`
(`stiracore.scheduler.grant_tick.GrantTicker`), evaluated after the jobs on every tick of the
scheduler's own loop (its periodic wake, at most every `max_wait_sec` = 30 s) and on an
*event-driven* wake after each heartbeat tick (the server's after-tick hook only marks the
wake; the scheduler's thread does the work — the heartbeat itself still imports and runs
nothing of it). **No model call on the tick path**: the admission reads the grant service's
memory (`tick_record`: the newest live grant, no look, no I/O), the breaker, the clocks,
the heartbeat health (the scheduler's ADR 0021 §8 test, read fresh) and the executor's
memory (`tick_facts`); it never builds a model or a client and never reads the executor's
parent agent. If admitted it presses through the **same** `GoalIterationExecutor.step` a
person's press uses — the same permits, budget reservation, tool allow-list, pre-emption
and commit rules — carrying the **wake's stamp** (`WakeStamp`, fix rounds 1–2): the marks read at the wake —
the authority epoch, the person's-turn counter (the iteration's `seq0`), the grant's stop
epoch, the scheduler's run token (void from the first instant of its `stop`), the executor
it was taken for — and the switch; single use. **What is checked, where:**

* on `step`'s admission look (with the tick's clauses, before it reads the provider) and
  right after it (`fresh`: the memory marks and the heartbeat read now);
* **inside the publication's authority fence** (`commit_at_epoch(expected=<wake epoch>)`:
  the memory marks and the publication itself, one step);
* the whole record again (`fresh`) right before the iteration's **first permit**, right
  before its **first consume** (whose permit must be of the wake's epoch — `consume` then
  checks that epoch is still current, atomically), right before its **first provider
  request** (with a grant look: breaker, TTL, clocks, state) and in the **first terminal
  request's** own permit step (the client proxy's);
* the memory marks (the switch, the run token, the executor, the kill, a person's turn,
  the authority epoch) at **every** later boundary (`_precheck`).

**What is guaranteed.** Every stop, narrowing, pause and resume, kill, revoke, breaker
change, TTL end, person's turn (even one that came and went), scheduler stop, executor
replacement or switch change that has happened when one of these reads runs — and a
heartbeat loss already visible at the last read before the first request — refuses: a skip
before the publication (`narrowed`, `turn_busy`, `scheduler_stopped`, `executor_unavailable`,
`heartbeat_unhealthy`, …) or, after it, an end `permit_stale(tick_stale)` / `stopped
(tick_stale)` with no provider request and no write. **The accepted residual** (parent
decision, fix round 2): the heartbeat is an external observation, read by sampling — a loss
that lands after the last read before a request (the final instructions before the HTTP
request leaves) is not seen by that request, and after the iteration's first request a
heartbeat loss is no stop (the memory marks still are; the heartbeat is a *start*
condition, ADR 0021 §8). A scheduler stop in that same final window is likewise not seen by
that one request; a real shutdown stops the executor before the scheduler, so it is
refused by the executor's own stop flag. **At most one iteration per wake.**

**Admission** (all required; the first failing clause is the skip reason): the switch, the
executor (not stopping), no kill, the authority and the grant store healthy, a live grant
with the flag, grant `active`, goal `active` — **not `awaiting_verification`**: only the
user verifies, nothing runs while a claim waits —, the breaker NORMAL, the heartbeat
healthy, a readable clock with TTL left on both clocks, iterations below the maximum,
budget left, no iteration running or starting, no abandoned provider call, no person's turn
arriving or holding the turn lock (W6b: every holder raises the signal first), the
**minimum spacing** `autonomy.tick_min_spacing_sec` (default 300 s, floor 30 s) since the
grant's issue (on the grant's clocks: the person who just issued it may still be editing),
since the previous iteration's end (any origin; an unreadable end time is a skip) and since
the grant's last resume (W6-F, #644: on the grant's clocks, measured like the issue's; a
resume whose clock could not be read is a skip) — so it counts from the later of the last end
and the last resume, the **no-action
gate** (two consecutive `iteration_no_action` ends since the last person's press — its own
end counts — or resume — a resume follows a stop, and every stop moves the grant's stop
epoch: ticks stay skipped until a person presses or resumes; time alone never clears it)
and, on an event-driven wake only, the **flood guard** (at most 5 heartbeat starts in 60 s;
none on the periodic path).

**Skips are recorded, never deferred.** `tick_skipped(<reason>)` — `not_enabled`,
`no_grant`, `grant_state`, `goal_state`, `awaiting_verification`, `heartbeat_unhealthy`,
`breaker`, `kill`, `authority_unhealthy`, `store_unhealthy`, `clock`, `ttl`,
`max_iterations`, `budget`, `turn_busy`, `iteration_running`, `abandoned_call`,
`min_spacing`, `no_action`, `flood`, `executor_unavailable`, `narrowed` (an authority
narrowing since the wake), `scheduler_stopped`, `shutdown` (the executor is stopping; no
event), `repeated_failure` (three consecutive `failed` / `model_error` / `timeout` ends since
the last press or resume: like the no-action gate, until a person presses or resumes),
`executor_refused` (anything
else the executor refused) — counted per grant (memory only, the last 4 grants). There is
no queue, no backlog and no replay: a wake after a sleep or a restart is one wake (missed
ones coalesce; ADR 0021 forbids catch-up for model-calling work). A *state-class* skip (one
that lasts until a state changes: every reason above except the busy / transient ones
`not_enabled`, `no_grant`, `turn_busy`, `iteration_running`, `abandoned_call`,
`min_spacing`, `flood`, `executor_refused`) posts **one** lifecycle event `grant_tick`
`active → paused` `tick_skipped_<reason>`; the same reason is posted again only after a
heartbeat start or another state-class reason in between (a busy skip in between is not
enough), at most 32 events per grant in this process; a heartbeat start posts `grant_tick` `idle → running` `tick_started` (bounded
by the grant's `max_iterations`). Nothing of the tick is persisted (no file): after any
start every grant is `revoked(restart)`, so the first tick does nothing (`no_grant`).

**An unattended iteration** (`origin: tick`) never waits for an approval: a call the rules
put on a card ends the iteration `approval_timeout` at once — no card, no self-approval, no
YOLO binding (W7) — and the goal is `blocked(approval_timeout)` (a person resumes it). The
floors are unchanged: `run_checks` still needs a verified `os_sandbox` host and
`run_checks_unattended`; the scope, the tools and the budget are the grant's; the user
verifies a completion claim. A person's turn (any turn-lock holder) yields it exactly as
for a press.

**Cost visibility.** Per heartbeat-started iteration, as numbers only: model calls, the
reported input / output tokens, `cache_read` / `cache_write` when the provider reports them,
and the wall time (`wall_ms`). The grant's view (`/api/grants`: `tick.costs`, the last 5)
and the iteration view (`origin: tick`, `tokens_in`, `tokens_out`, …; a press's view is
unchanged) carry them; the Stage board shows the last one. In the W6e smoke (one supervised
run on a real model, an estimate) no `cache_read` / `cache_write` was ever reported: every
call was a cold prompt. W6-F (#640): the row of an iteration stopped during a model call that
completed carries that call's `tokens_in` / `tokens_out` too.

**Unavailable at start-up.** With the switch on but no running scheduler, the server logs
one structured warning `grants: heartbeat_start unavailable (reason=scheduler_off)`; the
ticker is built but attached to nothing (nothing ever wakes it), and a flagged grant's tick
block carries `unavailable: scheduler_off`. With the scheduler running but the heartbeat off
(or not running at start-up) it logs
`grants: heartbeat_start unavailable (reason=heartbeat_off)` and a flagged grant's tick block
carries `unavailable: heartbeat_off` (every wake is then `heartbeat_unhealthy`; restart with
the heartbeat on). A failing evaluation is logged at most once per kind per 5 min (the
exception's class name only).

**Where you see it.** The settings panel's grant card: "Heartbeat start: on · n started ·
spacing", the last tick, the skips by reason, the last heartbeat iteration's tokens and
seconds. The Stage board (activity mode): the same as lines under the iteration count
(allow-listed codes and counts; never announced; no new timer — the activity's one poll).

**Not done here** (deliberately): no day pool or other budget pool beyond the grant's total
and per-tick limit (after the public release), no YOLO two-binding consume, no model-based
judge or verifier, no persistence of tick bookkeeping (the real-model smoke W6e was one
supervised run, not code: [autonomy](autonomy.md#4-known-limits)).

## On the Stage board (W5b)

The Stage whiteboard's activity mode shows the newest grant issued since the server started,
read-only, from the same `GET /api/activity` (a `grant` block; absent with `autonomy.stage_c`
off, `null` without a grant): the headline, the goal state, iterations n / max and the run
state, TTL and budget left (and this iteration's tokens / the per-tick limit), the last end
code in words, the last five iterations (end codes, durations, the tools that ran), and two cues — a
waiting card (pointing to the approval window and the approvals page; never the card's
content) and "awaiting your verification" — W6c: with the claim's evidence ("claimed at
iteration k/max · checks · workspace · progress", the check rows, the tokens / TTL at the
claim, "since <time>"; amber with a "⚠" line in words when the checks failed; "still waiting
for you" past the threshold; read-only after the confirmation). **No goal text, path,
argument or card content; no control** — Revoke / Pause / Verify / Run one iteration stay in this panel. It reads
`GrantService.board_view()` and `GoalIterationExecutor.board()` (memory only: no look, no
store I/O, no lock waited for: a held one answers `grant_busy`; only known codes). With `autonomy.stage_c_exec` the Stage also watches for an iteration and may lower
the board when one starts (only onto a closed board). Details and the decision table:
[stage.md](stage.md#the-goal-grant-on-the-board-adr-0011-stage-c-w5b-of-296).

## Not built yet (W6 and later)

(The heartbeat / scheduler caller is W6d, above; the scheduler's Agent-layer day pool is
still not shared with it.) A per-run tool-loop detector inside one iteration (deferred: the per-tick tokens and
the wall time bound it), a verifier
other than the user, the two-binding consume with YOLO (W7: no automatic approval in an
iteration), taint and episodic origin (W8), wiring the scheduler's Agent-layer day pool as
a shared pool (the executor accepts pools; the server passes none). The agent's `progress` codes are kept
in memory only in W5. No `halted:*` latch is
set by grants (a grant never survives a restart). See ADR 0011 §13.
