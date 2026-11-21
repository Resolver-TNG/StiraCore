# ADR 0030: An autonomy console — a timeline of what an unattended loop did, and a task board of the work a person gives it

**Path:** `docs/adr/0030-autonomy-timeline-and-task-board.md`
**Status:** Accepted for implementation, with the operator's decisions of 2026-11-15 (see "Operator decisions
(2026-11-15)" at the end). History: Proposed (2026-11-14) — **docs only: nothing in this ADR is built.** Refs #788
(operator request 2026-11-14 17:00: "for autonomous development we want a timeline view and a
task board"), #749 (autonomy tracking). Related: [ADR 0011](0011-agent-self-scheduling.md)
(grants, stage C), [ADR 0014](0014-progress-report-phase.md) (progress reports on the Stage
board), [ADR 0015](0015-verification-browser-tool.md) rev 6 (axis, shot registry R1),
[ADR 0028](0028-round-mode-loop.md) (round mode, round ledger §5, #757 / #760), ADR 0029 /
#785 (the assisted grant brief; **not in this worktree when this ADR was written** — every
statement below about briefs depends on that ADR's record and is marked *(ADR 0029)*),
[grants](../grants.md), [admin-api-permissions](../admin-api-permissions.md).

**In short.** Everything the console needs to *show* is already recorded, as codes and counts,
in two hash-chained, bounded files (the round ledger and the lifecycle log) and in the grant
store. The console is a **read model over those sources** plus a **board of cards whose
buttons call the person-only routes that exist today**, with the confirmations they already
have. It adds no authority, no new way to start work, no model call and no free text. It does
**not** add "start the next queued brief automatically"; that is a new autonomy decision and is
left to its own ADR (§2 "The queue", Q4).

## Context — what exists (read before building; do not reinvent)

| Piece | Where | What it gives the console |
|---|---|---|
| Stage board *Activity* mode | `dashboard/stage/js/activity.js:3`, `:531` (polls `GET /api/activity?after_id=<cursor>`); `src/stiracore/observability/activity.py:1-17` | the state *now*, recent rows by row cursor (`RECENT_LIMIT = 20`, `DIFF_LIMIT = 100`, `activity.py:41-42`), the YOLO window numbers, totals, stall hint; an allow-list builder (no argument / output / preview) |
| The board's grant panel | `activity.py:501-580` (`grant_view`): newest grant only, codes + counters, `cards` = pending approval cards flagged `grant_card` (`:568`), the iteration's `run` / `end` / `detail`; `rounds_view` (`activity.py:466-498`) | the *live* grant, never history |
| `GET /api/activity` | `src/stiracore/observability/chat_api.py:2378-2392` (`after_id` cursor, a cursor from another store restarts at 0) | the cursor pattern to copy |
| Round ledger | `src/stiracore/grants/round_ledger.py:1-39` (allow-list, refuse-don't-truncate, hash chain, two kinds `round` / `approval`), `LEDGER_FILE` / `OLD_LEDGER_FILE` / `ROTATE_BYTES = 1 << 20` (`:75-77`) | one record per round: `start`, `end`, `wall_ms`, `end_code`, `detail`, `progress`, counts, `tools` (`TOOL_KEYS`, `:217`), `files_changed`, tokens, `errors`, `limits`, `stop`, `axis {start,end}` (`:277-299`); approval records with `approved_by` ∈ `{person, none}` (`:313`), `decision`, `reason` (`:314-368`) |
| Rounds route | `src/stiracore/grants/routes.py:195-204` → `GoalIterationExecutor.ledger_view` (`src/stiracore/grants/executor.py:2552-2562`) | **all** records of a grant, oldest first, no cursor, no limit (the `last` argument is not used by the route) |
| Read-only CLI view | `src/stiracore/control/grant_rounds_cli.py:103` (`RoundLedger(..., rotate=False)`) | the offline reader |
| Grant / goal state | `src/stiracore/grants/state.py:109-159` (`GRANT_STATUSES`, `GRANT_REASONS`, `GOAL_STATUSES`, `GOAL_REASONS`, `AGENT_BLOCKED_CODES`); [grants](../grants.md) "States" | the lanes' source (all enums) |
| Lifecycle events of grants | `state.py:682-710` (`transitions`, `all_reason_codes`: `grant_issued`, `grant_resumed`, `paused_user`, `revoked_user`, `revoked_kill_switch`, `halted_breaker_*`, `blocked_<reason>`, `goal_verified`, …); log `authority-lifecycle.jsonl`, `ROTATE_BYTES = 1 << 20` + one old segment (`src/stiracore/authority/lifecycle_events.py:18-27`, `:63-66`); subjects `grant`, `goal`, `grant_iteration`, `grant_tick`, `yolo_window`, `breaker`, `latch` (`:72-88`) | the person's actions, YOLO window boundaries, breaker / latch incidents — codes and 32-hex ids only |
| Heartbeat start | `src/stiracore/scheduler/grant_tick.py:496-516` (`view` with cost rows, `board` without; never waits, `BoardBusy`) | skip reasons and starts of the live grant |
| Grant store | `src/stiracore/grants/store.py:25`, `:44-46` (`MAX_GRANTS = 16`, the oldest ended ones dropped) | which grants the board can list at all |
| Audit chain | `src/stiracore/observability/audit_chain.py:1-21` (identifiers and hashes only, fixed kinds) | approval decisions / tool executions as evidence — not needed for v1 of the console |
| Screenshots | `src/stiracore/tools/page_check.py:1388-1389` (`KEEP_RUNS = 5`, `SHOT_DIR = ".stira-verify"`), `:2233-2251` (`<workspace>/.stira-verify/<run>/<index>.png`, older runs pruned); R1 shot registry: ADR 0015 rev 6 (`0015-verification-browser-tool.md:1149-1154`: memory only, ≤ 64 runs, 30 min, tagged with the grant id, `O_NOFOLLOW` re-read, size + SHA-256) | **R1 is not built yet** (`tools/page_shot_registry.py` does not exist) |
| Report files (#758) | `src/stiracore/grants/schema.py:23-25`, `service.py:1420-1421` (the views carry the **count** only, never a path), `executor.py:1931` (excluded from `files_changed`) | a number per grant; no per-round "report files touched" exists |
| Progress reports (ADR 0014) | `0014-progress-report-phase.md:127-132`: `progress.report` / `.reply` / `.auto_continue` rows in `turns.db`, listed by `/api/activity` | chat / YOLO turns only — a grant iteration has no ADR 0014 checkpoint today |
| Settings "Goal grants" | `dashboard/static/js/settings_grants.js:4-25` (issue with the host-confirmed operation, revoke, pause, resume, step; `createElement` + `textContent`, `:55`) | the only place a person issues a grant today |
| Pages | `dashboard/templates/`: chat, approvals, artifacts, memory, forest, observability, schedules, settings, stage (+ base, index); route table `admin-api-permissions.md:65`; Stage is opt-in (`src/stiracore/observability/stage.py:31`, `STIRA_STAGE`) | where a new page would sit |
| Own-dashboard guard | `src/stiracore/yolo/own_dashboard.py:82` (`E_OWN_DASHBOARD`), `tools/own_dashboard_gate.py:41` (`http_request` / `rss` URLs) | the model's HTTP tools cannot reach this server |

Facts that shape the design:

1. **One live grant** (`grants/service.py:29`: "At most one live grant (v1)"). There is no queue.
2. **No actor identity.** The dashboard has no user authentication (`admin-api-permissions.md:9-24`);
   lifecycle events carry a *reason code*, not a principal. "Who" can only be a class derived from
   the code: `person` (`*_user`, `grant_issued`, `grant_resumed`, `goal_verified`,
   `verification_rejected`), `harness` (`*_breaker_*`, `*_kill_switch`, `*_restart`, `ttl`,
   `blocked_*` of the executor), `agent` (`blocked_needs_decision|out_of_scope|missing_input`,
   `goal_claimed` — a model's *report*, as a code), `heartbeat` (`grant_tick`). For approvals the
   ledger's `approved_by` is the answer; ADR 0028 rev 2 adds `main` / `reviewer` later. The issue
   text's `answered_by` is this field (no new name).
3. **The ledger records less than ADR 0028 §5's table.** The writer's key set (`round_ledger.py:277-299`)
   has no `check`, `check_total`, `review`, `approvals` yet, and `axis` is always `not_run`
   (`:137-140`). There is **no money cost** anywhere: only reported tokens (`tokens_in/out`,
   `cache_read/write`), `model_calls`, and the tick cost rows (numbers). The console shows tokens;
   a price estimate is out of scope (it would need a price table that is not data Stira owns).
4. **Bounded history.** Ledger and lifecycle log: each ≤ 1 MiB current + 1 rotated segment;
   grant store: 16 grants; R1: 30 minutes, memory only; `.stira-verify`: 5 runs per workspace.
   A round record is a few hundred bytes (to be measured, not asserted), so the ledger holds on
   the order of a few thousand rounds across all grants — enough for a 24 h / 100-iteration grant
   (ADR 0028 Q1), not for months.

## Decision (Proposed)

### 1. Timeline (history, not live)

**Sources (one source of truth each, no copy):** round records and approval records from the
round ledger; `grant`, `goal`, `grant_iteration`, `grant_tick`, `yolo_window`, `breaker`,
`latch` events from the lifecycle log; the grant store's view for the grant's own fields. The
live state stays on the Stage board; the timeline never polls faster than the person scrolls.

**Read model** `observability/timeline.py` (pure, no I/O of its own, no model, no lock of the
chat API): `items(ledger_records, lifecycle_events, grant_view) -> list[item]`. An item is an
**allow-listed** dict built field by field, like `activity_item`:

| Item kind | Fields (all enum / int / 32-hex / 12-hex) |
|---|---|
| `round` | `seq`, `grant` (short id), `round`, `of_max`, `origin` (`press` / `tick`), `start`, `end`, `wall_ms`, `end_code`, `detail`, `progress`, `stop`, `axis.start`, `axis.end`, `model_calls`, `writes`, `approval_waits`, `reports`, `files_changed`, `tools` (exactly `TOOL_KEYS`), `tokens_in`, `tokens_out`, `cache_read`, `cache_write`, `errors` (≤ 8 enums), `shots` (§1.3; absent until slice T5) |
| `approval` | `seq`, `round`, `ts`, `tool` (enum), `target_class`, `decision`, `approved_by`, `reason`, `latency_ms` — never `args_digest` on the page (it is a hash, but it identifies arguments; export only, Q3) |
| `lifecycle` | `ts`, `subject` (enum), `from`, `to`, `reason_code` (enum), `actor` (derived class, fact 2) |
| `gap` | `from`, `to`: the ledger or log was rotated out / unusable before this point (§1.4) |

A value outside its enum is shown as `other`, never as the string it had (the `_code` rule of
`activity.py:44`). Nothing reads `goal`, a path, a file name, a URL, an argument, a check output
or model text; the sources cannot hold one (`round_ledger.py:18-19`, `lifecycle_events.py:9-16`).

**Route** `GET /api/autonomy/timeline?grant=<32hex>&after=<seq>&limit=<n>` (observe class):
items of one grant with `seq > after`, oldest first, `limit` ≤ 200 (default 100), plus
`next` (the cursor), `ledger` / `log` state (`ok` or the fixed `round_ledger_*` code), and
`oldest` (the first seq still on disk). Lifecycle events are merged by `ts_wall` into the same
page (their own cursor `after_ev`). A cursor beyond the newest (a reset data directory)
restarts at 0, exactly as `/api/activity` (`chat_api.py:2390-2391`). The existing
`/api/grants/<id>/rounds` stays as it is (its existing readers are unchanged).

**Display:** one row per grant (the 16 the store keeps, newest first), rounds as bars on a
time axis (`start`→`end`), markers for lifecycle events (issue, pause, resume, revoke, halt,
verify, YOLO window open/close, breaker transitions, latches) and for approval records. Each
bar's detail panel lists the fields of the table above as a definition list. Bars carry text
labels and shapes as well as colour (§3).

* **Filters** (client-side over the loaded page, no new query language): end code group
  (`done` / stopped / failed / refused), `origin`, `progress`, has approvals, has errors,
  axis not `pass`/`not_run`, actor class.
* **Zoom**: round / hour / day buckets; a bucket shows counts, never a merged text.
* **"Since I left"**: the client stores the last `seq` it showed in `localStorage` (no server
  state, no new data file). The summary is computed **from codes only** by a pure function
  (`timeline.since(items)`): rounds run, `new` / `repeat` / `none` progress, end codes counted,
  stops, approvals by `decision` × `approved_by`, the current grant/goal state, tokens summed
  (`None` when no round reported, as `round_ledger.summary`, `:582-594`). Rendered from i18n
  keys with numbers. No model, no free text, no "the agent says".

#### 1.2 What the person did

Issue, pause, resume, revoke, verify are lifecycle events with person reason codes (fact 2);
approval answers are ledger `approval` records (`approved_by: person`). The timeline shows them
as markers of actor class `person`. It cannot say *which* person (no authentication) and says so
in the panel ("a person at the dashboard or host"). Approval answers of chat / YOLO turns stay
on the approvals page and the Stage board; they are not grant history.

#### 1.3 Screenshots as thumbnails (slice T5, depends on R1)

The ledger has no field that links a round to a screenshot run. Proposal: R1 (when built)
records `run → [(index, sha256, size)]` tagged with the grant id; the **writer** of the round
record adds `shots: [{run_ref, index, sha12}]` — `run_ref` a 12-hex keyed digest of the run id
(memory-only key, like the `review` digest of ADR 0028 §5), ≤ 4 per round, never the run
directory name or a path. This is a ledger schema change (version stays 1, a new optional key
needs the reader's legacy rule of `round_ledger.py:300-304`), so it is its own slice.

Pixels are served only by `GET /api/autonomy/shots/<grant32>/<run_ref12>/<index>`:

* the server resolves `run_ref` through **R1 of this process** only; unknown, expired (30 min),
  another grant's run, or an R1 entry whose re-read (`O_NOFOLLOW`, size, SHA-256) differs →
  `404 shot_unavailable` (one code for all, nothing about which check failed);
* no path, file name or URL ever comes from the client; the route builds the path from the R1
  entry and the grant's workspace, inside `.stira-verify/`;
* answer `image/png`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`,
  `Content-Disposition: inline`, size ≤ the R1 size; a thumbnail is the same bytes scaled by
  CSS (no server-side image decoding: no decoder attack surface);
* after a restart R1 is empty, so **old rounds show "screenshot no longer available"** — the
  hash in the ledger proves what existed, the pixels are gone by design (R1 is memory only;
  `.stira-verify` keeps 5 runs, `page_check.py:1388`).

#### 1.4 Retention: what the timeline can and cannot show

* It shows rounds and events still in the current + rotated segment of each file, and grants
  still in the store. Anything older is a `gap` item with the oldest `seq` / `ts`; the page says
  "older history was rotated out (the ledger keeps about 2 MiB)". A grant whose rounds were
  rotated out still appears (from the store) with its final state and no bars.
* An unusable ledger or log (`round_ledger_*`, sticky) shows its code and **no** rounds after
  the break — the console never repairs or guesses.
* **Bounded history store (slice T7, optional, decide after measuring):** at a grant's terminal
  transition, append one allow-listed **grant summary** line (`grant`, issued / ended ts, final
  states and reasons, `round_ledger.summary` numbers, counts per end code) to
  `grant-history.jsonl` (`0600`, hash-chained like the ledger, ≤ 1 MiB + 1 segment, ≈ thousands
  of grants). It adds no per-round copy: one source of truth per round stays the ledger. Not
  needed for v1.

### 2. Task board

**Cards** are person-given work items; **lanes** are derived from enums, never stored:

| Lane | A card is here when | Source |
|---|---|---|
| Drafts | a brief saved, never started *(ADR 0029)* | the brief store |
| Queued | a brief the person marked "next" *(ADR 0029)* — **inert data** | the brief store |
| Running | grant `active`, goal `active` | `GrantService.list_view` (`service.py:1441`) |
| Needs you | something only a person can unblock (list below) | grant view + pending cards + `/api/activity` |
| Blocked | grant `paused(goal_blocked)` / goal `blocked(<reason>)` with a reason that needs no answer but a new grant (`max_iterations`, grants.md:56) | grant view |
| Done | grant terminal: `ended(complete)`, `expired`, `revoked(*)`; goal `complete` / `unmet(*)` | grant view |

**"Needs you" aggregates what stops a loop**, each as a code with a link to where it is
answered (the board answers nothing itself):

* a pending approval card of the grant (`grant_view.cards > 0`, `activity.py:568`) → the
  approvals page / Stage board;
* `blocked(<reason>)` the person can resume (`no_progress`, `loop_detected`, `same_blocker`,
  `approval_timeout`, `verification`, `run_checks_unavailable`, `iteration_timeout`,
  `workspace_changed`, and the agent's `needs_decision` / `out_of_scope` / `missing_input`);
* `halted(breaker_*)` (resume only at NORMAL, typed `RESUME`);
* `budget_limited` (a new grant);
* `awaiting_verification` (verify confirm / reject);
* an axis failure (`end_code = page_tools_unavailable`, `detail` an axis code) — once produced;
* report-files review: the grant has report files (count only) and the goal is blocked
  `no_progress` — a hint that the list may be too wide/narrow (ADR 0028 §4), nothing more;
* an ADR 0014 checkpoint: **does not exist for grant iterations today**; a pending
  `progress.report` of a chat / YOLO turn is shown in this lane as a link to the Stage board,
  outside any grant card.

**Moving a card = pressing an existing route**, with the confirmation it already has:

| Move | Route | Confirmation (unchanged) |
|---|---|---|
| Queued/Draft → Running | `POST /api/grants/issue` | typed `ISSUE` + host-confirmed operation (`routes.py:14-19`); the board pre-fills from the brief *(ADR 0029)*, the person still sees and confirms the dialog |
| Running → paused | `POST /api/grants/<id>/pause` | one press (a narrowing) |
| any live → Done | `POST /api/grants/<id>/revoke` | one press, always available |
| Needs you/Blocked → Running | `POST /api/grants/<id>/resume` | operation; a halt also `RESUME` |
| Needs you → Done | `POST /api/grants/<id>/verify` | operation |
| one round | `POST /api/grants/<id>/step` | as today (`stage_c_exec`) |

There is **no drag-and-drop write**. Dragging (or a keyboard "move") only opens the same
button's dialog; a drop with no completed confirmation changes nothing, and the card snaps back.
The board reuses `settings_grants.js`'s issue / operation flow (extract the shared functions;
no second implementation of the host-confirmed operation). Each card shows who/what answered
each step: the lifecycle actor class and the ledger `approved_by` counts.

**The queue.** There is one live grant. *Queued* means "a person wrote it and put it next";
nothing reads the queue to start anything. Starting the next brief automatically when a grant
ends would be a **new autonomy decision** (a grant issued without a person's confirmation of
that grant). This ADR does **not** offer it. If ever offered, it needs its own ADR,
`permission-layer`, built and reviewed by a person, and at least: a hand-edited gateway opt-in
(off, not on the settings page), a per-brief "may start automatically" flag the person set at
the time the brief was confirmed with the host-confirmed operation (the confirmation covers the
exact brief digest), a ceiling (e.g. ≤ 1 chained start per terminal end, ≤ N per day), never
after `revoked(*)` / `halted` / a latch / `unhealthy`, and the same ceilings as a hand-issued
grant (ADR 0028 Q1). Recommended answer today: no (Q4).

**A card = a brief.** The card's title is the brief's title *(ADR 0029)*: the **person's own
text**, length-capped, escaped. The goal text is never on the board (it stays behind
`/api/grants/<id>/goal?confirm=SHOW`, `routes.py:11-13`). Model-written text is never a card
field: the agent's blockers are codes (`AGENT_BLOCKED_CODES`, `state.py:132`).

### 3. Placement and UX

* **A new page `/autonomy`** ("Autonomy" in the side menu), with two tabs: *Timeline* and
  *Board*. Not the Stage board (that is the live view and opt-in by `STIRA_STAGE`; it gets a
  link "history →" from its grant panel) and not the Settings section (configuration, not
  history; it gets a link too and keeps issuing). The page and its routes are registered only
  when `autonomy.stage_c` is on, like the grant routes (`routes.py:3-4`) — **no new setting**.
* Narrow screens: lanes become a single column with a lane selector; the timeline becomes a
  vertical list of rounds. Keyboard: every card and bar is a button in tab order; arrow keys
  move within a lane / along the axis; the "move" action is a menu of the allowed routes.
* `prefers-reduced-motion`: no animated transitions, no auto-scroll. Colour is never the only
  signal: each state has a text label and an icon shape; contrast uses the board tokens.
* i18n en + ja for every label and every code shown (codes map to catalog keys; an unknown
  code shows the code itself, escaped).
* **Export** (read-only): `GET /api/autonomy/timeline/export?grant=<id>&format=json|csv` —
  the same item allow-list as §1, no brief title (Q3), every CSV cell an enum or a non-negative
  number (no cell can start with `=`, `+`, `-`, `@`; a test asserts it), `Content-Disposition:
  attachment`, `Cache-Control: no-store`.

### 4. Safety and privacy

* Every new route is **GET / observe class** (timeline, export, shots, board read). Every state
  change goes through an existing grant route with its existing Host / Origin / CSRF checks and
  confirmations (`admin-api-permissions.md:28-34`, `:90-98`); a GET under `/api/` is
  same-origin only (`:31-32`), so another site cannot read the timeline or the export. The board adds no POST route.
* **Route inventory:** each new route needs a row in `docs/admin-api-permissions.md` (the
  chat-server table) or `tests/test_admin_api_permissions.py:257` fails; the page `/autonomy`
  joins the `page` row (`:65`).
* **The model has no route to these pages:** the own-dashboard guard refuses the agent's HTTP
  tools to this server (`E_OWN_DASHBOARD`, `yolo/own_dashboard.py:82`), and the new routes are
  no model-visible tools. A test asserts that no tool spec names `/api/autonomy`.
* **No secrets, no page text, no argument / output previews**: the sources hold none, and the
  read model builds items field by field (a test feeds records with extra keys and free-text
  values and asserts they never appear).
* **What a hostile page or model could try to inject:** a card title (only the person's brief
  title, never model text), a code (every code is checked against its enum; outside it →
  `other`), a tool name (fixed `TOOL_KEYS`), a file name (none exists in the data), a
  screenshot (served only by R1 hash match, as an image, `nosniff`, never as HTML/SVG). The page
  renders with `createElement` + `textContent` only (no `innerHTML`; a static test greps the new
  JS, as for the Stage board), and the page loads no inline script (the server's CSP header,
  `src/stiracore/observability/security.py:280`).
* **The board never widens a permission:** it has no control that changes scope, tools, rules,
  budget, TTL, report files or approvers; issuing from a brief shows the full issue dialog. A
  person-only route stays person-only; "who answered" is displayed, never selectable.

### 5. Slices (dependency order)

| # | Slice | permission-layer | Tests (new) | Docs to sync |
|---|---|---|---|---|
| T1 | `observability/timeline.py` read model (items, `since`) over ledger + lifecycle records | no | `tests/test_timeline_model.py`: allow-list (extra keys / free text dropped, unknown enum → `other`), merge order, `gap` items, `since` from codes, tokens `None` rule | none |
| T2 | `GET /api/autonomy/timeline` (cursor, limit, `oldest`) under `autonomy.stage_c` | no (observe) | `tests/test_timeline_route.py`: cursor reset, limit cap, unknown grant, no route without stage C; route inventory | `admin-api-permissions.md`, `grants.md` (a pointer) |
| T3 | `/autonomy` page, Timeline tab (template, JS, i18n en/ja), links from Stage and Settings | no | `tests/autonomy_timeline_ui_test.cjs` (jsdom: textContent only, filters, zoom, reduced motion) + axe as `tests/stage_board_audit_test.cjs:333` | page row in `admin-api-permissions.md`, i18n catalogs, `docs/dashboard*.md` if it lists pages |
| T4 | Board tab: lanes from grant views + pending cards; buttons reuse the shared issue / operation code of `settings_grants.js` | no (calls existing routes only) | `tests/autonomy_board_ui_test.cjs`: no write without the existing dialog, drop snaps back, lane mapping per state/reason, axe | i18n |
| T5 | R1 link: `shots` in the round record + `GET /api/autonomy/shots/...` | **yes** (a new file-serving read path; ledger schema) | `tests/test_timeline_shots.py`: unknown / expired / other-grant / changed bytes / symlink → one 404 code; no client path accepted; headers | ADR 0028 §5 table, ADR 0015 R1, `admin-api-permissions.md` |
| T6 | Export JSON/CSV | no | `tests/test_timeline_export.py`: same allow-list as T1, CSV cells enum/number, no title | `admin-api-permissions.md` |
| T7 | (optional) `grant-history.jsonl` bounded summary store | no (observation, but a new data file) | `tests/test_grant_history.py`: allow-list, chain, rotation | data-directory table in `docs/configuration*.md` |
| T8 | Draft / Queued lanes | no | in T4's test | after ADR 0029's brief store exists |

T5 needs R1 (ADR 0015 rev 6) first; T8 needs ADR 0029. Auto-start is not a slice here.

### 6. Connections

* **Assisted brief (ADR 0029 / #785):** a card is a brief; the board is where drafts wait. The
  brief's title is the only person text on the board.
* **ADR 0014:** progress reports are chat / YOLO turn checkpoints (`0014-progress-report-phase.md:127-132`);
  the board links a pending one in *Needs you*. A checkpoint inside grant iterations would be an
  ADR 0014 / 0028 change, not this ADR's.
* **Coffee-talk-style local loops** (a driver outside Stira posting turns to `POST /api/chat`):
  its turns are chat turns in `turns.db`, not grant rounds; they have no grant, no permit and no
  ledger record, and they **must not** be written into the round ledger (its writer is the
  executor only). The timeline may show them as a separate track "chat turns" built from the
  `/api/activity` rows' allow-list (turn start/end, tool counts, approvals, progress reports),
  labelled *not a grant*, so a person never mistakes an outside driver for a supervised grant.
  Recommended for after T3 (Q5).

### Not in scope

Automatic execution of queued briefs; cross-grant dependency graphs; a Jira-like workflow
(assignees, custom states, comments, estimates); a money cost estimate; any model-written
summary; editing a grant's scope from the board; any change to the round ledger's existing
fields or to the confirmation of any route.

## Open questions (with recommendations)

1. **Q1 Placement.** New `/autonomy` page, Stage panel, or Settings? — *Recommend a new page*,
   registered with `autonomy.stage_c`, linked from both.
2. **Q2 Gate.** A new switch for the page? — *Recommend no*: it follows `autonomy.stage_c`
   (no route, no page without it), so nothing new to configure.
3. **Q3 Person text and hashes.** Show the brief title on the board? Put it or `args_digest` in the
   export? — *Recommend title on the board only (escaped, capped), never in the timeline API or
   the export; `args_digest` in the JSON export only, never on the page.*
4. **Q4 Auto-start the next queued brief.** — *Recommend not now*; if wanted, its own
   permission-layer ADR with the opt-in, per-brief confirmation and ceilings of §2.
5. **Q5 Outside drivers and the history store.** Show coffee-talk turns as a separate track, and
   build T7? — *Recommend the track after T3; T7 only after a measured 24 h run shows the
   ledger rotating out history a person still needs.*

## Consequences

The console is a view: a broken console cannot start, stop or approve anything that the
existing routes would not. Its history is as long as the bounded files, and it says so.
Building it needs no change to an authority decision; only T5 touches a permission-relevant
path (serving workspace bytes) and is marked for a person's implementation and review.

## Operator decisions (2026-11-15)

The operator answered "概ね OK" (broadly OK) to Q1–Q5 as recommended, and added two directions
(thread "ADR 0030" of the module map, 2026-11-15 17:01):

* **Q1–Q5 stand as recommended**: a new `/autonomy` page registered with `autonomy.stage_c`; no new switch;
  the brief title on the board only (escaped, capped), never in the timeline API or the export, `args_digest` in the
  JSON export only; no auto-start of the next queued brief; the external-driver track after T3 and T7 only after a
  measured 24 h run.
* **Addition 1 — the Stage is strengthened.** The pixel-art Stage (`dashboard/stage/`) gets a richer view of the
  same material (timeline lane, the board's cards), not only the activity mode it has today.
* **Addition 2 — plain screens as well.** The same read model also drives **ordinary pages** that are easy to read
  at a glance; the Stage is not the only view. **Both views read the one read model this ADR describes and add no authority**
  (the console stays a read model: a broken console starts, stops and approves nothing the existing routes cannot).

Consequence for the slices: the read model comes first; the plain `/autonomy` page and the Stage panel are two
renderers over it. Which renders first is a scheduling choice, not a decision of this ADR.
