# ADR 0012: YOLO mode — six hours of automatic approval for Ask, in two tiers, started by a button

**Path:** `docs/adr/0012-yolo-mode.md`
**Status:** **Accepted** (revision 8, operator decision of 2026-10-19 12:28: exec is
chosen at the press — §9 **option C**; the breaker pauses and resumes after 10 min of NORMAL
— Open (1) **P**, as implemented; the destination only as a keyed hash — Open (10) **(b)**;
tier external needs a second checkbox — Open (13) **(a)**; **v1 is macOS only** (Linux:
a separate issue, §7); the v1 promise is "**no six-hour wait on approval cards**" — leaving
Stira fully unattended belongs to #299 / phase 8. **Implemented: phases 0–7** (7a: exec in
the backend, "As implemented (phase 7a)" in §9; 7b: the exec choice in the UI, §12; 7c: the
values also from `gateway-config.json` and the settings page, §1). History of
the earlier status: Proposed (revision 7: operator decision of 2026-10-19 00:18 — the browser
confirmation is a **checkbox** (not a typed word), and an approval card's approve / edit is
**one press** also while `STIRA_YOLO_ALLOW` is set; revision 6: operator decision of
2026-10-18 23:16 — the confirmation is
in the **browser** by default, the host confirmation is an opt-in; revision 5: operator decision
of 2026-10-18 17:58 — start by a button; after design review rounds 1–2 and round 1 of
revision 4; needs an operator decision on the points marked **Open** before any implementation
beyond phase 2 — **one provisional exception:** phases 2b / 2c implement the *recommended*,
not yet decided, Open (13) (a) (tier external = browser + one more confirmation), because a
browser channel needs some answer for the external tier; choosing (b) or (c) later is a
change in `confirm_in_browser` only. **Implemented: phases 0–3** (#365, `src/stiracore/yolo/`: the §6b
state machine, the §11 row contract, the start-up allowance and clock check, the own-dashboard
refusal, the reserved `yolo*` keys, the running-tool-call refusal and the §13 boundary skeleton;
phase 2: the confirmed operations of §8b and the YOLO request + its confirmation, which is
recorded and settled as a system cancel because there is no open sequence yet; phase 2b:
the browser channel as the default, the host channels — command (iv) and the macOS dialog
with OS authentication (ii) — behind `STIRA_YOLO_HOST_CONFIRM`, `/new` revoking the
operation store, and the three fixes of the phase-2 review; phase 2c: the checkbox
confirmation, the narrowed set of confirmed operations (§8b table), the review minors of
2b; phase 3: the window's open / close sequences of §6a / §11, the marker, the latch
judgement and unlatch, the clocks of §7, stop and `/new` — with the phase-4 fault
injection at every I/O point after the confirmation, see "As implemented (phase 3)" in
§11); **phase 5a** connects the approver — while a window is `active` the Ask of an
**internal** tool in a human chat / Stage turn is approved automatically (§2 step 5, §6a),
with the cap's used / reserved slots, pause / CAS resume on the breaker and the flapping
end; the external tier is not in effect yet (an external tool keeps its card in every
window) — see "As implemented (phase 5a)" after §6b; **phase 5b** adds the button, the
dialogs, the badge and the log line on the chat and Stage pages — see "As implemented
(phase 5b)" in §12; **phase 6** puts the external tier in effect — in a window of tier
external the listed external tools are answered too, under every mitigation of §10, with
the destination recorded as a keyed hash only (Open (10) (b), provisionally) — see "As
implemented (phase 6)" in §10)
**Issue:** #365. Related: #190 / ADR 0010 (gate stack, `rules_loosening`), #299 / ADR 0011
(grant foundation, authority epoch, incarnation), #277 / ADR 0013 (audit contract, receipts,
operation tokens), #240 / ADR 0006 (durable approvals), #239 / ADR 0008 (scheduler), #362
(exec consent bridge), #243 / #271 / #312 (write roots, data-directory floors), #350 (Stage,
`/new`), #145 (children of exec tools), #167 (exec sandbox), #318 (separate OS user), #254
(prompt-cache prefix), #276 (single pre-execution boundary), #285 (owner lock).
**Decision (accepted, revision 8):** on a server whose operator allowed it at start-up, a person presses
**the YOLO button**, picks **internal only** or **external allowed**, and confirms the
request by ticking a checkbox **in the dialog that press opened** (the browser channel, the
default; revision 6) — or **on the host** (an OS confirmation, or a host command) when the
operator chose the stricter host channel (`STIRA_YOLO_HOST_CONFIRM`, §8b). For **six hours from the
press** — never extended — every call the tool rules answer with **Ask** inside the chosen
tier is approved automatically instead of showing a card. Never, the floors, secret masking,
`IdentityGuard`, budgets and loop limits, the hash-chained audit and the stop controls are
unchanged; only Ask → automatic approval is exempted. Without the start-up allowance there
is no button and nothing changes.

## History (経緯)

Issue #365 proposed a **session grant**: a Stage/chat button with a dialog, 15 min / 1 h /
4 h, a scope per category ("all" only with a typed word), expiry when the breaker leaves
NORMAL, extension as a new issuance, human chat turns only.

Revisions 1–3 followed the operator's first decision of 2026-10-18 (start-up parameter
only). On 2026-10-18 17:58 the operator added: **a window is started by pressing a button on
the screen and choosing internal only / external allowed**; six hours and the exemption are
unchanged; the general destination guard for `web_fetch` / `web_search` is **not** to be
built (Open (7) decided: status quo), while the agent's loopback route to its own dashboard
must be closed as a prerequisite of the button. Requirement 1 below is amended accordingly.
Requirements ("requirement N" below):

1. ~~Enabled at start-up~~ **Started by a button press with the tier chosen there**
   (amended 17:58), valid **6 h from the press**, Ask treated as approved; never extended
   (to continue, press again after the window ended).
2. **Two tiers chosen at start:** internal only, or external allowed.
3. Purpose: **leave it running**; autonomy (heartbeat, jobs, the ADR 0011 goal loop) may be
   covered; a design that adds permission is accepted.
4. Unchanged: Never, the write floor (data directory, SOUL, settings, `protected_dirs`),
   secret masking, `IdentityGuard`, budgets / loop limits, hash-chained audit, stop
   button / command. Only Ask → automatic approval is exempted.
5. Tool-kind scope; one window at a time; ADR 0011's grant foundation shared under its own
   kind; no cards but one log line and an always-visible badge; monotonic clock +
   incarnation; ends on time, restart, human stop, `/new`; a large configurable cap;
   CRITICAL-judged calls never exempt.
6. Breaker: compare "pause while WARNING/CRITICAL, resume inside the remaining time"
   (operator's recommendation) with "end on the breaker".
7. The agent cannot enable, extend or widen it.
8. Exec: compare "exec only in the external tier" with alternatives (#362). **Decided
   2026-10-19 12:28: option C** (§9) — and with it requirement 4 is **not met for commands**
   in a window whose person chose "Include exec", which the operator accepted explicitly.
9. At implementation: no parameter → unchanged, off by default, no token cost; expiry,
   restart, stop, floors, Never and "agent cannot change it" pinned by measured tests.

| Point | #365 session grant | Revisions 1–3 | Revisions 4–7 (this) |
|---|---|---|---|
| Issued by | dashboard button + dialog | start-up parameter | **button + confirmation** — rev. 4–5: on the host; **rev. 6: typed word in the browser by default, the host as an opt-in; rev. 7: a checkbox instead of the word**; the start-up parameter only allows it (Open (11)) |
| Starts counting | issue | process start | **the press** |
| Duration | 15 min / 1 h / 4 h | 6 h, never extended | 6 h, never extended |
| Scope | per category | two tiers | two tiers, chosen at the press |
| Autonomy | chat turns only | also the goal loop | same |
| Breaker | ends the grant | pauses (Open (1)) | same |
| Exec | after #362 | Open (2) | **revision 8: option C** — chosen at the press, its own checkbox; `shell` / `batch` only (§9) |

## Goals and non-goals

**Goal.** Start Stira and leave it working for up to six hours without answering Ask
cards (measured 2026-10-17: approval waits 8.6–112.6 s, median 29.5 s; turns with
approvals 42–176 s against 1–11 s without), in a window fixed at start and always visible.

**Non-goals.** Changing saved rules (YOLO never writes `tool-config.json`); lifting the
breaker or floor layers, `STIRA_ENABLE_EXEC_TOOLS`, `JOB_READ_ONLY_TOOLS`, envelopes, grant
scopes, budgets or loop limits; answering `CRITICAL_HALT`; issuance without a confirmation (§8b), finer scopes,
several windows; sandboxing (#167); any model call, prompt text or tool of its own (tool
list and static prompt unchanged, #254; no job is made to call a model, ADR 0008).

## Terms

* **Allowance** — `STIRA_YOLO_ALLOW` at start-up: the highest tier a person may pick; it
  never opens a window by itself (§1).
* **YOLO request** — what a button press creates: `request_id` (128-bit random), tier,
  `window_id` (allocated at the press), `t0`, expiry (2 min), state `pending | issuing |
  opened | denied | expired | cancelled | fenced(*) | aborted(issue_failed)` (§6b); at most
  one at a time.
* **Confirmation** — the person's approval of a request through the configured channel
  (§8b): the **browser** (default: a one-time token from the press's own answer and a
  ticked checkbox — two for tier external; revision 7) or the **host** (opt-in,
  `STIRA_YOLO_HOST_CONFIRM`: the host command or the OS
  dialog). Only it opens a window. ("Host confirmation" below, written before revision 6,
  means this confirmation through whichever channel is configured, unless a passage names
  the host channel.)
* **YOLO window** — one per process: `kind = "yolo"` (mandatory, §13), `grant_id` (128-bit
  random), `tier`, `incarnation`, `generation`, `t0` (the press) / `expires_at` (all clocks of
  §7), `cap`, `used`, `reserved`, `state`, `end_reason`.
* **Incarnation** — 128 bits from a CSPRNG at every process start, never read back from
  storage (ADR 0011 §5). Every record and token carries it.
* **Generation** — monotonic per window; incremented by every pause, resume and narrowing.
* **Automatic approval** — the YOLO approver's answer to a HITL request with reason
  `RULE_ASK` for an in-tier tool; single-use, bound like a human approval (agent, tool,
  argument hash, `toolUseId`, rule generation, expiry) plus `(incarnation, grant_id,
  generation)` and one cap reservation.
* **States** — `active`, `paused(breaker_<level> | breaker_unknown)`, terminal
  `expired(time | sleep | cap)`, `revoked(stop | new_session | audit_failed |
  clock_anomaly | flapping)`, `ended(issue_aborted_*)`; each end is *normal* or *abnormal*
  (§6). A restart ends the window (nothing survives).

## Decisions

### 1. How a window starts — Open (11)

| Option | Meets amended requirement 1 (button start) | Issuance paths | Start-up self-issue (§8a) | Needs an opt-in allowance | Button present by default |
|---|---|---|---|---|---|
| (A) both: `STIRA_YOLO` opens a window at start **and** the button | yes | two (each through §2) | the start path keeps it (needs its own confirmation) | yes | no |
| (B) **button primary**: `STIRA_YOLO_ALLOW` only allows the button; it opens nothing | yes | one: request → host confirmation | **a restart cannot open a window** | yes | no |
| (C) start-up parameter only (revisions 1–3) | **no** — kept for comparison only | one | kept | — | — |
| (D) button without an allowance | yes | one | — | **no** | **yes** — rejected (on by default on every install) |

**Recommendation: (B)** (the operator's wish, one issuance path, closes the restart path).
The rest of this ADR assumes (B); with (A) or (C) the start path additionally needs
revision 3's start confirmation (arm token / another OS user; in the git history of this
file).

| Variable | Default | Accepted | Effect |
|---|---|---|---|
| `STIRA_YOLO_ALLOW` | off | `internal`, `external` (exact); empty = unset | shows the button; highest tier that may be picked |
| `STIRA_YOLO_MAX_AUTO_APPROVALS` | `2000` (Open (5)) | integer 1–10000 | cap per window |
| `STIRA_YOLO_MAX_EXTERNAL` | `200` (Open (5)) | integer 0–cap | external sub-cap per window |
| `STIRA_YOLO_MAX_EXEC` | `200` (revision 8) | integer 0–cap | exec sub-cap of a window with "Include exec" (§9); the choice itself needs `STIRA_ENABLE_EXEC_TOOLS` and the allowance `external` — there is no variable that admits exec (revision 3's `STIRA_YOLO_EXEC` is not used) |
| `STIRA_YOLO_HOST_CONFIRM` | unset = `browser` (revision 6) | `browser`, `auto`, `command`, `dialog` | the confirmation channel (§8b): a host value **requires** the host and refuses the browser confirmation |

* **Off by default, no token cost.** Unset: no button, no route accepts a request, nothing is
  written; tool definitions and behaviour as on `main`.
* **Invalid values refuse start-up** (message names the variable, not the value): `on`, `1`,
  `all`, `Internal`, a cap out of range, a cap variable without `STIRA_YOLO_ALLOW`.
* **Read once at start-up**; later `os.environ` changes have no effect. Not a hub field.
  **Phase 7c (operator request 2026-10-19 12:40):** each value may also come from the
  `yolo` section of `gateway-config.json` — `allow`, `offer_exec` (no variable; default
  `true` = as before; `false` takes "Include exec" out of the dialog), `max_auto_approvals`,
  `max_external`, `max_exec`, `host_confirm` — with the precedence **environment > config >
  default** per value. The only writer is `POST /api/yolo/settings`, a person's save on the
  settings page: refused while a chat turn or an agent tool call runs (the agent's tools only
  run inside a turn), while a request / window / latch exists (an open window is never
  changed, not even narrowed), for a value its variable pins, out of the variables' ranges;
  a **loosening** save is a confirmed operation (without the allowance the page's dialog —
  one checkbox, `confirm` + `acknowledged` naming the loosening codes; with it the host gate
  of §8b, kind `yolo_settings`). Every other save path still refuses `yolo*` (§2), and the
  hub's gateway saves carry the section over unchanged. The allowance, `offer_exec` and the
  channel are used at the **next start**; the caps from the **next window**. A hand-edited
  section never refuses start-up: an unknown allowance is `off`, integers are rounded into
  their range, other values are left out — one warning naming the codes. Everything else in
  this ADR (the press and its confirmation, exec's own checkbox, Never / floor / CRITICAL /
  breaker, the window not changing while open) is the same whichever source the value came
  from. A platform without a sleep-including clock (§7) does
  not offer the button.

### 1a. The button flow

1. **Press.** The badge area (Stage dialogue box edge, chat header) shows `YOLO を開始` /
   `Start YOLO` when allowed, not latched, no window and no request exist. The dialog offers
   `内部のみ` / `Internal only` and — only with `STIRA_YOLO_ALLOW=external` — `外部もOK` /
   `External allowed` — and, only with `STIRA_ENABLE_EXEC_TOOLS` as well (revision 8, §9
   option C), `実行系も含める` / `Include exec` (which says it includes external) —, with the
   confirmation text of §12, and sends `POST /api/yolo/request {tier, exec, op_token}` (the dialog's ADR 0013 §2f operation token; expected state: no window,
   no request, latch clear).
2. **Request.** The server checks allowance, tier ≤ allowance, latch (§11), no window, no
   pending request, the rate limits (one pending; after a denial or expiry no new request for
   5 min; at most 3 per hour, Open (5)), reads the clocks — **`t0`** — and writes
   `audit.yolo.requested`. The page continues with step 3 in the configured channel: on the
   browser channel (default) the confirmation dialog of the press's own answer; on the host
   channel (opt-in) "confirm on the host" with the 2-minute countdown. Both have a cancel
   button (a narrowing).
3. **Confirmation** (§8b): shows tier, `request_id` (short), `t0` and the end time in a fixed
   format. **Browser channel (default):** the press's own answer carries a one-time token;
   the same page opens the dialog and the person ticks 「内容を確認しました」 / "I have checked
   this" — and, for tier external, 「外部への送信が可能になることを理解しました」 / "I understand
   that sending to outside services becomes possible" as well (Open (13); revision 7:
   checkboxes, the confirm button stays disabled until every box is ticked) — and, for a
   request with exec, a third: 「コマンドが実行され、Stira の保護(保護された場所・監査)を迂回
   しうることを理解しました」 / "I understand that commands run and can bypass Stira's
   protections (protected locations, audit)" (revision 8) — then the page
   posts the token, `acknowledged: true` (+ `external_acknowledged: true`, +
   `exec_acknowledged: true`) and the instance
   reference to `POST /api/host-operations/<request_id>/confirm`; cancel narrows.
   **Host channel (opt-in):** the person confirms or denies on the host. A request created
   by the agent (an MCP server can reach the route, §8b) is never shown in the person's
   page (only the page that pressed has its token); the confirmation text says to cancel a
   request one did not make. Residual: a person who confirms a request they did not make
   opens a window — the fixed text, the rate limits and the 2-minute expiry are the
   mitigation; a program of the same OS user that confirms its own request is the §8a /
   §8b residual of the browser channel.
4. **Open.** Confirmed in time → the durable open sequence of §11 → `active` (or `paused`).
   `expires_at = t0 + 6 h`: the wait for the confirmation counts. Denied / expired /
   cancelled → nothing opens.
5. During a window a new request is refused (`yolo_active`): no extension. After the end a
   new press starts a new window with a new confirmation.

### 2. Gate-stack position and the one loosening judgement

`CheckpointHooks.before_tool` keeps its order; YOLO is an **approver at the rule layer**:

1. rule Never → refused (YOLO never sees it); 2. replay guard; 3. breaker write block →
refused without asking, CRITICAL `halt` → reason `CRITICAL_HALT` → **a person only**;
4. floor (`path_check`) → refused before asking; 5. `request_hitl(reason=RULE_ASK)` →
**only here** the approver may answer for an in-tier tool while `active`; 6. rule-generation
and breaker-freshness re-checks, `ApprovalBinding.check`, `ApprovalBindingVerifier`,
`IdentityGuard.path_denial`, `ExecGatedTool` — unchanged, plus the YOLO binding (§6a).

Only the rule layer's `prompt` becomes `allow` for that one call; breaker and floor are
untouched, so ADR 0010's monotonicity invariant holds. The settings page shows
`prompt — automatic (YOLO)` for in-tier Ask tools while active.

**One function.** `rules_loosening(before, after, context)` (ADR 0010; moved to the #276
boundary by ADR 0011 §2) gains the window as a field: `yolo` ordered `off < internal <
external`, `cap`, `expires_at`, and `state` (`paused` < `active`). Every transition of the
window is judged by it:

| Transition | Loosens? | Allowed context |
|---|---|---|
| off → tier (issue) | yes | `host_confirmed_request` only: a pending request of this incarnation, tier ≤ allowance, confirmed through the host channel (§8b); with Open (11) (A) also `process_start` |
| `paused(breaker)` → `active` (resume) | yes (`state`) — **not a new issuance, but a loosening** | `breaker_resume` only, and only when grant id, incarnation, tier, `expires_at` and cap are **unchanged** (§6); judged by this same function, not by a resume-specific copy |
| active → paused / ended, cap lower | no | any (narrowing is never refused) |
| anything else (tier up, cap up, `expires_at` later, ended → active) | yes | **none** |

Every other context — hub save, `/api/config/tools`, `/api/tools/config`, history rollback,
completing an interrupted save, `RECOVER`, ADR 0011 grant issue/resume, `stira tool-rules`,
`stira yolo unlatch`, the request route itself — treats `yolo*` as a **reserved key**: an input naming it is refused
(409 `yolo_not_settable`), and no output contains it. There is no narrowing route other
than stop in v1. A contract test runs every save path with a `yolo` key.

### 3. Coverage by origin

YOLO answers Ask only where a HITL route exists today; it creates no route and widens no
caller (envelopes, budgets and stops apply first).

| Origin | Covered | Notes |
|---|---|---|
| Human chat / Stage turn | **yes** | main case |
| Resume of a durable approval (ADR 0006) | approved call: the human approval; other Ask calls of the batch: judged now | the old window ended with the restart |
| Heartbeat tick | n/a | model-free, no tool |
| Scheduler job (ADR 0008) | n/a today | `AgentJobExecutor` runs a plain agent with `JobToolGuard` only (no rules, no HITL gate), and the chat server wires only model-free actions: no Ask to answer |
| ADR 0011 goal loop (#299, not implemented) | **yes**, grant scope ∩ envelope ∩ tier | only the individual-approval step; never issues, extends or resumes a grant; §13 |
| Webhook ingress | **no** (Open (4)) | outside content, unattended by nature |
| Discord / Slack bots, ACP (#203) | **no** | other runtimes / not implemented |
| Sub-agents (`create_nxp_subagent`) | **no** (Open (3)) | children keep only Always tools (#368) |
| Children of exec tools | **no** | no hooks (#145): their exec/file calls are refused; other tools run ungated, as today — which is why `use_agent` / `think` / `swarm` / `graph` stay a person's card in every window (§9) |
| Inner calls of `batch` | **as their own call** | they go through the parent agent's hooks one by one (§9) |
| Jobs a `cron` call registers | **no** | they run later without Stira; `cron` stays a person's card (§9) |

### 4. Tiers and classification

* **Internal** — a positive constant list: local reads (`file_read`, `view_image`,
  `image_reader`, `current_time`, `lsp_diagnostics`, `lsp_hover`, Stira memory reads),
  local writes (`file_write`, `editor`, `journal`, Stira memory writes),
  `stop`, `handoff_to_user` — each only for a path inside the workspace / write roots
  (phase 5a scope). `diagram` (starts processes, writes where its `title` points) is
  answered by a person only, like exec.
* **External** — internal + the "network egress" row of
  [tool-execution-paths.md](../tool-execution-paths.md) (`http_request`, `web_fetch`,
  `web_search`, `webhook_send`, `oauth_api_call`, `rss`, `speak`, `memory`) and
  `perplexity_search`.
* **Unclassified = external**: MCP tools, new tools. A test fails for a registered tool in
  no class (as `test_every_registered_write_tool_is_classified`).
* **Exec class**: never internal; never answered in a window without "Include exec"; in a
  window with it (tier external only, revision 8, §9 option C) `shell` and `batch` are
  answered, `cron` and the child-agent tools never. YOLO never exposes an exec tool (the
  tool list is unchanged; `STIRA_ENABLE_EXEC_TOOLS` decides that).

### 5. Invariants and their limit

Never (step 1), the write/read floors (step 4, `IdentityGuard`, `open_checked`), masking,
`IdentityGuard`, budgets and loop limits, the audit (§11), the stop controls (§6) and
`CRITICAL_HALT` are unchanged for **every call that goes through Stira's gate**. They do
not bind a process that runs commands as the server's OS user (a shell, a process it
leaves behind, any non-Stira program of that user): see §8a and Open (2).

### 6. Expiry, pause, stop

| Event | Result | Back? |
|---|---|---|
| 6 h after the press (§7) | `expired(time)` | press again |
| sleep longer than 5 min detected (§7) | `expired(sleep)` | press again |
| cap reached | `expired(cap)` | press again |
| external sub-cap reached | external tools fall back to Ask | — |
| stop (badge button, `POST /api/yolo/stop`; server stop) — also while a request is pending or being issued (§6b) | `revoked(stop)` / request `fenced(stop)` | press again |
| restart / crash | window gone, never re-opened by a start; an unclean end leaves the marker open (§11) | (unlatch,) press again |
| `/new` (#350) — also while a request is pending or being issued | `revoked(new_session)` / request `fenced(new_session)` | press again |
| breaker not NORMAL, or unknown | `paused(...)` (Open (1)) | `breaker_resume` |
| audit write fails | `revoked(audit_failed)` | unlatch, press again |
| clock anomaly (§7) | `revoked(clock_anomaly)` | unlatch, press again |
| > 3 pauses in 60 min | `revoked(flapping)` | press again |

* **Stop and `/new` act on everything current**: the pending or issuing request and the
  window, through the fence of §6b. They name no id (a stale page's stop can only stop more)
  and are never refused (no confirmation, no breaker check).
* **`/new`.** The agent cannot cause it (turn-lock refusal, measured in #350). Jobs, the
  goal loop, summarization and compaction are not `/new`.
* **End classes.** *Normal* ends: `expired(time | sleep | cap)`, `revoked(stop | new_session |
  flapping)`, `ended(issue_aborted_stop | _new_session | _cancel | _epoch)` and request ends
  `denied / expired / cancelled / fenced(*)`. *Abnormal* ends:
  `revoked(audit_failed | clock_anomaly)`, `aborted(issue_failed)`, any reason code not in
  the normal list, and every end with no end row (crash, kill, power loss). Only a normal
  end closes the marker (§11); an abnormal one stays latched until `unlatch`, **also when its
  end row was written successfully**.
* **Pause / resume (recommended form, Open (1)).** A breaker level that is not NORMAL —
  including an exception, no engine, or an unknown level — pauses. Resume is the
  `breaker_resume` transition of §2 and runs only as a compare-and-set under the authority
  lock on `(grant_id, generation recorded at the pause, state paused(breaker))`: a stop,
  expiry, another pause or anything else in between has changed the generation, so a late
  dwell timer can never turn an ended or newer state back to `active`. The dwell (10 min
  proposed) samples the **live** level (not the turn snapshot); any non-NORMAL or unknown
  sample restarts it; at the CAS the level is read once more. Expiry is checked first.
  A window confirmed while the breaker is not NORMAL opens paused.
* An ADR 0011 grant halted by its §9 rules is not resumed by a YOLO resume.

### 6a. Linearization: what is recorded before what

ADR 0011's authority lock is a leaf held only for compare-and-set, never across I/O. Three
kinds of step, each with its own pre-state and commit condition:

* **Narrowing** (pause, stop, `/new`, cancel, expiry, cap end, revocation): under the lock
  set the state and increment the generation and the authority epoch — and, for stop,
  `/new`, cancel and every end, the fence (§6b) — the linearization point; effective at
  once, never waiting for I/O, never undone. The audit row follows outside the lock; if it
  fails the window is additionally `revoked(audit_failed)` and the open marker (§11) latches.
* **Window transitions** (issue, resume) reserve **no** slot.
  *Issue* — pre-state: no window, a `pending` request of this incarnation within its 2
  minutes (states: §6b). (1) under the lock: the host confirmation's token check (ADR 0013
  §2f) and CAS `pending → issuing`, recording the **fence value `f`**, the **authority epoch
  `e`** and the request's `window_id`; (2) outside the lock: write `audit.yolo.confirmed`,
  then the durable sequence of §11 (marker, `audit.yolo.started`); then ADR 0013 §2b (A):
  read back both rows and check their receipts (event id, seq, hash, binding fields);
  (3) under the lock — **the final linearization point**: request still `issuing` with the
  same `request_id` and `window_id`, fence == `f` (no stop, `/new`, cancel or other fence
  since (1)), authority epoch == `e`, incarnation unchanged, no window, latch clear, the
  ADR 0013 writer's sticky health flag healthy (§2b (B) step 1), the receipt evidence of
  (2) present, clocks before `expires_at` → CAS `issuing → opened` and window `active` (or
  `paused`). If (3) fails after `audit.yolo.started` was written, the window never becomes
  usable: `audit.yolo.ended(issue_aborted_<fence reason>)` is written (a normal end if the
  cause was a stop, `/new`, cancel or a moved epoch; `aborted(issue_failed)`, abnormal, otherwise) and the
  marker follows §11. A failed write anywhere opens nothing and latches.
  *Resume* — pre-state `paused(breaker)` at generation `g`: (1) under the lock check state,
  `g`, clocks and the dwell; (2) write `yolo.resumed(g)`; (3) under the lock CAS
  `paused(g) → active(g+1)` after re-reading the live breaker level; a failed CAS leaves the
  newer state (an outcome row `void` follows best effort); a failed write revokes
  (`audit_failed`).
* **Automatic approval** of one call (state `active`): (1) under the lock check state,
  clocks, generation `g` and reserve one cap slot — plus one sub-cap slot **only if the tool
  is external**; (2) write `yolo.auto_approved`; (3) under the lock re-verify incarnation,
  `g`, `active`, clocks, then hand out the permit. A failed write releases the slot, causes
  no effect and revokes; a changed generation releases the slot and the call goes to a
  person (`permit_stale`, outcome row `void`); a reader treats an intent row without
  `consumed` as not known to have run.
* **Slots.** `used` (consumed) and `reserved` (permits outstanding) are separate. When
  `used + reserved` reaches the cap but `used` has not, the slots are only *temporarily* full:
  this call goes to a person (a card), the window stays `active`. Only `used == cap` ends
  it (`expired(cap)`). The external sub-cap works the same for external tools only and never
  ends the window; with sub-cap 0, or in tier internal, no external slot is ever requested,
  so neither issue nor resume nor an internal approval depends on it.
* **Consume** (ADR 0011 §5) at the execution boundary re-checks incarnation, generation,
  clocks and state and turns `reserved` into `used`: exactly one call takes the last slot;
  a voided permit returns its slot; a permit reserved before a cap was reached stays valid.
* **One decider per request.** YOLO answers only a request it receives at step 5 while
  `active`; it never decides a request already shown to a person. A request shown during a
  pause stays the person's after a resume (the approval store's single-decision rule, ADR
  0006); a late decision on a request already decided, expired or consumed is refused, and
  no approval — human or automatic — is applied twice.

### 6b. Request × window state machine and the fence

One **fence** counter (in memory, monotonic, under the authority lock) covers the request
and the window; every stop, `/new`, cancel and window end increments it (a breaker pause
moves the window generation and the authority epoch, not the fence). A step that started
under fence `f` and epoch `e` commits only if both are unchanged.

| From | Event | To | Fence | Notes |
|---|---|---|---|---|
| no request, no window | press accepted (§1a) | request `pending` | — | `window_id` allocated now (§11 schema) |
| `pending` | host confirmation, CAS first | `issuing` | read `f`, `e` | §6a (1) |
| `pending` | cancel / stop / `/new`, CAS first | `cancelled` / `fenced(stop / new_session)` | +1 | a confirmation arriving later finds no `pending` → refused (`yolo_request_stale`) |
| `pending` | deny / 2 min passed | `denied` / `expired` | +1 | normal |
| `issuing` | (3) succeeds | `opened`; window `active` / `paused` | unchanged | — |
| `issuing` | stop / `/new` / cancel | `fenced(*)` | +1 | (3) then fails; `ended(issue_aborted_*)`, normal |
| `issuing` | authority epoch moved (breaker change, rules save, …) | `fenced(epoch)` | +1 | (3) fails; normal — press again |
| `issuing` | a write or receipt check fails, health latched, clock anomaly | `aborted(issue_failed)` | +1 | abnormal: latched |
| window `active` / `paused` | narrowing (§6) | window ended (`reason`) | +1 | then the close sequence of §11 |
| window ended, normal, close sequence done | — | **no window** | — | a new press is accepted only from here |
| window ended, abnormal, or close sequence not durable | — | **latched** | — | `unlatch` (§11) before any new request |

Cancel and confirm race only through these CASes: whichever linearizes first wins, the other
is refused. A request never outlives its incarnation (a restart leaves no request).

**As implemented (phase 5a).** Backend only (the button, badge and log line in the UI are
phase 5b). `src/stiracore/yolo/tiers.py` is the §4 table as constants (internal list,
the egress row + `perplexity_search` as external, the exec class; anything else
external); `origin.py` marks a human turn — the chat route (`/api/chat`, which the Stage
uses) runs its turn inside the mark, every other origin (webhook, bots, a durable-approval
resume, jobs, sub-agents) leaves it unset and is not covered (§3). The gate
(`CheckpointHooks.before_tool`) asks the approver only at step 5 with reason `RULE_ASK`
(Never, the replay guard, the breaker's write block, `CRITICAL_HALT` and the floor are
decided before it); `YoloRequests.approve` runs §6a — (1) under the lock: the clocks,
writer health and a breaker sample (a level other than NORMAL — the gate's turn snapshot
or the live one — pauses the window and the call goes to a person), one slot reserved
(`used + reserved` at the cap: *temporarily full*, a card, the window stays active);
(2) `audit.yolo.auto_approved` (tool as enum, keyed hashes of the argument hash and the
`toolUseId`, generations, origin `turn`) and its read-back — a failure releases the slot
and revokes (`revoked_audit_failed`, latched); (3) under the lock: same window,
generation, `active`, clocks → a single-use permit from the §13 authority boundary
(`AuthorityBoundary`, kind `yolo`; its epoch moves with every pause / end / rules save).
The answer is an APPROVE bound like a human one (argument hash, the HITL timeout) and goes
through every later check (rule generation, breaker freshness, `ApprovalBinding`); it is
**not** a human approval for `IdentityGuard`'s write block nor for an exec gate mark.
`ApprovalBindingVerifier` consumes the permit right before the ledger claim (incarnation,
generation, epoch, clocks, `active`; once), writes `consumed`, and `used == cap` ends the
window `expired(cap)`; a refused or later-cancelled call voids its permit (slot back once,
`void`). Pause / resume: the breaker is sampled at every look, by the 30 s watchdog and at
every approval; `active` + not NORMAL → `paused(breaker_<level>)` (row `paused`; a failed
row revokes); the 4th pause within 60 min → `revoked(flapping)`; NORMAL at every sample for
10 min → `resumed` row → the CAS `paused(g) → active(g+1)` with the clocks and the live
breaker level read **under the lock** (expiry first; a stop, `/new`, expiry or a new pause
during the row wins, `void` follows). Issue and resume are judged by
`rules_loosening(…, context="host_confirmed_request" / "breaker_resume")`. The chat server's
breaker reader is the engine level made stricter by the live stability. Status adds to
`window`: `used`, `reserved`, `limit`, `auto_approved`, `paused`, `pauses_last_hour`,
`resume_in_sec`, `effective_tier` (`internal` in this phase). Without the allowance the gate
has no approver (`CheckpointHooks.auto_approver is None`) and behaves as on `main`.
**Scope and origin (review of PR #435, M1 / M2).** An internal call is answered only when
what it touches resolves (symlinks of the existing prefix followed) inside the workspace or
a write root (`yolo.tiers.scope_refusal`, the agent's `WritePathPolicy`); anything else —
outside, a missing or non-`str` argument, a path that cannot be resolved, no policy — is a
person's card. Writes outside the roots were already refused before any approval
(`path_denial`, the write roots); the scope adds the reads (`file_read`, `editor view`,
`view_image`, `image_reader`, `lsp_*` — measured: `editor view ~/.ssh/…`, `lsp_*` and a
`journal` `date` of `../…` were approved before), and `journal` (`<cwd>/journal/<date>.md`,
`date` exactly `YYYY-MM-DD`; since #464 any other `date` is refused by `path_denial` before
any card, because `journal`'s body is no longer scanned). `diagram` is no longer internal: it starts processes
(graphviz, the OS `open`) and writes where its `title` points — like exec, never answered.
`lsp_*` stay internal: they start a fixed analyzer (Pyright / Ruff, one-shot) on the
checked bytes of a file inside the scope; the model chooses no command. The human-turn
mark is bound to its turn and its agent: a context copied during the turn and run after
it, a thread, or a gate of another agent id is not covered (`origin.covered`). A status
look may narrow (clocks, a pause) but never resumes: the resume CAS runs only in the
watchdog (≤ 30 s after the dwell).
**External tier (decision for the review):** a window of tier external can still be
opened (phases 2b–3 unchanged), but only internal tools are approved in it until phase 6
(§10's mitigations); the alternative — refusing the external press until then — is in the
PR's cost section. Tests: `tests/test_yolo_auto_approve.py`,
`tests/test_yolo_window_routes.py` (real chat server app: internal Ask approved in a human
turn, every other Ask a card, Never / floor / `CRITICAL_HALT` unchanged, tool specs and
prompt unchanged), `tests/test_yolo_default_unchanged.py` (no approver without the
allowance).

### 7. Clocks

**Measured (macOS 26.5.2 / Apple M1 Max, Python 3.11.2, 2026-10-18; no sleep was
triggered for the test).** `time.get_clock_info("monotonic")` is `mach_absolute_time()`
and equals `CLOCK_UPTIME_RAW`; `CLOCK_MONOTONIC` was 58 996 s ahead of it and equal to
wall-clock time since `kern.boottime`. So on this Mac `time.monotonic` **does not count
sleep** and `CLOCK_MONOTONIC` **does**. **Not measured:** Linux (`CLOCK_MONOTONIC` is
documented to exclude suspend, `CLOCK_BOOTTIME` to include it), Windows, containers and VMs.

Contract:

* Three readings at `t0` (the server receiving the press, §1a) and at every check: wall `W`, sleep-including monotonic `B`
  (macOS `CLOCK_MONOTONIC`, Linux `CLOCK_BOOTTIME`) and sleep-excluding monotonic `U`
  (`time.monotonic`). **Elapsed = max(ΔW, ΔB, ΔU)**; the window ends when elapsed ≥ 6 h.
  No clock can make elapsed smaller than another clock says.
* **Sleep detection.** ΔB − ΔU growing by more than 5 min between two checks is a sleep:
  the window ends `expired(sleep)` (conservative; Open (5) for the value). A watchdog
  re-checks every 30 s, and `consume` checks directly.
* **Anomaly.** W moving backwards beyond 2 s, or ΔW falling behind ΔB beyond 2 s →
  `revoked(clock_anomaly)` + latch. Example (review): 7 h of sleep during which the wall
  clock is set back 6 h gives ΔB ≈ 7 h → expired; ΔW ≈ 1 h < ΔB → anomaly as well.
* **No sleep-including clock** (a platform where `B` is unavailable or not yet measured):
  the button is not offered and a request is refused. Supported platforms are listed by
  measurement.
* A request's 2-minute expiry uses the same clocks; a confirmation after it is refused.
* Rollback cannot replay a window: nothing is read back, and every token carries the
  incarnation.

### 8. The agent cannot open, extend or widen it — through Stira

| Path | Refusal | Test that pins it (fake model, offline) |
|---|---|---|
| Tool call / model text | no tool touches the window; text changes nothing | tool-spec and prompt hashes equal with and without the allowance (`cache_probe`) |
| Request route via a tool (HTTP, MCP), also delayed | a request only makes a confirmation possible; nothing opens without it (§8b); Stira's HTTP tools cannot reach the own dashboard (§8b); on the browser channel an MCP server can confirm its own request (§8a residual) | agent-made request + no confirmation → expires, nothing opens; request spam → rate limits; an earlier request is never shown in the person's page |
| Loosening routes via a tool | confirmed (§8b) | §8b tests |
| Approval routes (approve / edit of a card) via a tool | one press, as without the allowance (revision 7): Stira's HTTP tools cannot reach the own dashboard (§8b premise 2); the running-tool-call refusal applies; a same-user program outside the server can decide a pending card (§8a residual, as without the allowance) | `test_yolo_host_gate.py`, `test_yolo_agent_self_reach.py` |
| Hub, legacy APIs, rollback, `RECOVER` | reserved key; loosening confirmed while allowed (§8b) | every save path with `yolo*` keys |
| Config files | not read for YOLO; on the write floor (#271) | `file_write` refused; a hand-placed key → off, warning |
| `os.environ` in-process / restart with the allowance | read once / the allowance opens nothing | restart with `STIRA_YOLO_ALLOW=external` → no window |
| Sub-agents / exec children | no inheritance; no hooks | child Ask tool → rejected |
| Extension | refused while a window exists | request during a window → `yolo_active` |

Only `GET /api/yolo/status`, `POST /api/yolo/request`, `POST /api/yolo/request/cancel`,
`POST /api/yolo/stop` (phase 3) and — for the confirmation of a request on the browser
channel — `POST /api/host-operations/<id>/confirm` touch the YOLO state (static check of
`app.url_map`); `/new` and stop fence the request (§6).

### 8a. Residual risk: a shell on the host

With Open (11) (B) a restart cannot open a window, and an agent-made request needs a
person's host confirmation. What a process that runs commands as the server's OS user (an
agent with exec, a process it left behind, any program of that user) can still do:

* on the **browser channel (the default since revision 6)**: read `/api/csrf`, create an
  operation or a YOLO request, take the one-time token from its own answer and confirm it
  with `acknowledged: true`, exactly as the page does, and decide a pending approval card in
  one press (as without the allowance) — the browser channel does not hold against
  any program of the server's OS user outside the server process (a stdio or HTTP MCP
  server, a shell, an exec command a person approved), which is why exec stays out of every
  window unless the person chose "Include exec" at the press (§8b premise 1, revision 8:
  in such a window a command YOLO approved is that program — accepted, §9);
* run the host command of §8b (iv) itself, or answer a plain host dialog by UI scripting
  where the OS grants it accessibility rights;
* edit Stira's code or its service definition, which no check inside Stira survives.

An **OS authentication** in the host confirmation (macOS LocalAuthentication: Touch ID or
the account password; §8b (ii)) is not answerable by such a process; a confirmation by
**another OS user** (#318) is not either, if the server's user cannot write Stira's code,
service definition, configuration, audit or data floor. Requirement 3 accepts added
permission; this ADR records the shell residual instead of claiming isolation.

### 8b. Human channel: the button and every human decision

**Why the dashboard alone cannot prove a person** (review round 2). The agent's HTTP tools
and MCP servers reach the loopback dashboard and `GET /api/csrf`
([admin-api-permissions.md](../admin-api-permissions.md)); CSRF stops browsers, not local
clients, and a request sent through a tool can arrive *after* the call ended (delayed
delivery), so a count of running calls proves nothing. Single decision and generation
binding do not stop the *first* forged approval of a still-valid card or a forged press.

**Confirmed operations** (named "host-confirmed" before revision 6). While
`STIRA_YOLO_ALLOW` is set (process start to exit) exactly the operations that **widen what
may run without a person** need a confirmation; revision 7 (operator decision 2026-10-19
00:18) took the approval card out of the set. This is the list the contract "a loosening
POST without a confirmed operation is answered 428" refers to:

| Operation (route) | Confirmed while allowed? | Why |
|---|---|---|
| YOLO request (`POST /api/yolo/request`) | **yes** (kind `yolo_request`) | opens a six-hour window of automatic approvals |
| `RECOVER` (`/api/tools/config`, `/api/config/tools`) | **yes** (`recover`) | resets the tool rules to the defaults, may widen them |
| loosening tool-rule save (`/api/config/tools`, `/api/tools/config`), loosening hub save (`/api/config/hub`), loosening history rollback (`/api/config/history/<id>/rollback`) | **yes** (`rules_loosen`) | a standing loosening: every later call is affected |
| scheduler create / resume / confirmed update (`/api/scheduler/jobs…`) | **yes** (`scheduler_loosen`) | makes the agent act later without a person present |
| loosening YOLO settings save (`POST /api/yolo/settings`, phase 7c) | **yes** (`yolo_settings`); without the allowance the page's own dialog (`confirm` + `acknowledged`) | a higher allowance / cap / sub-cap, `offer_exec` on, or the browser instead of a host channel for the next start or window |
| HITL **approve / edit** of a card (`POST /api/hitl/<id>/decision`), incl. `CRITICAL_HALT`, out-of-tier and durable cards | **no — one press** (revision 7; it was confirmed in phases 2–2b) | one decision for one call, already bound to the card id, its `args_hash` and client id, single-use; nothing standing changes. The running-tool-call refusal (`tool_call_running`) still applies while allowed |
| reject, respond, cancel, stop, `/new`, tightening saves, pause, reads | **no** | they only narrow |

The dashboard *requests*; the configured channel *confirms*, bound to the request id, the
operation, the body digest or form version, the target generation and the incarnation (ADR
0013 §2f token and expected state).

**Revision 6 — two channels: browser (default) and host (opt-in).** Operator decision
2026-10-18 23:16: a host confirmation (an OS dialog with Touch ID or the password, a host
command) is too heavy to be the default; the default is the **browser** — the operation's
one-time token plus a typed word on the screen (revision 7: a checkbox instead of the
word); the host confirmation of phase 2 stays, as an option.

| Channel | Chosen by | What the person does | Holds against the agent's in-process tools (HTTP tools, delayed delivery) | Holds against a program of the same OS user outside the server (MCP server, shell, an approved exec command) |
|---|---|---|---|---|
| **browser** (default) | `STIRA_YOLO_HOST_CONFIRM` unset or `browser` | presses the button; the same page opens the confirmation dialog in a fixed format; ticks 「内容を確認しました」 / "I have checked this" (+ the external box for tier external; revision 7 — revision 6 had typed words) | **yes, by premise 2** (they cannot reach the dashboard; the route refuses while that guard is not armed) | **no** — it can do what the page does (residual, premise 1) |
| **host** (opt-in) | `STIRA_YOLO_HOST_CONFIRM=auto` / `command` / `dialog` | as phase 2: the host command on a TTY, or the macOS dialog with OS authentication; the browser confirmation is **refused** (`host_confirm_on_host`) | yes | command: no (a shell runs it); dialog with OS authentication: yes (§8a) |

**The browser channel cannot prove a person.** A gesture is not visible to the server and a
local client can send any header. The default rests on three premises, each enforced in
code and pinned by a test (`tests/test_yolo_browser_confirm.py`):

1. **Exec stays out of every YOLO window — unless the person chose "Include exec" at the
   press** (revision 8, §9 option C; no setting admits it): the service answers an
   exec-class tool only in a window with the exec flag, and then only `shell` / `batch`
   (`tiers.EXEC_AUTO_TOOLS`, checked in `requests.approve`; every inner call of a `batch`
   passes the gate on its own — measured in `tests/test_yolo_exec_batch_app.py`).
   **In a window with exec this premise is knowingly given up** for six hours:
   a command YOLO approved is a same-user program outside the server and can do what the
   page does; while it runs in its call the running-tool-call refusal holds, a process it
   leaves behind is not held (§9, accepted by the operator). With exec tools enabled the start-up log warns, naming the **effective** channel
   and the setting separately, that a command a person approved runs as the server's user
   and can confirm like the page. Only the OS-authenticated dialog **when it is actually
   selected** is a channel such a command cannot answer: `STIRA_YOLO_HOST_CONFIRM=dialog`
   (or `auto`) falls back to the command where no dialog is available (SSH, no desktop
   session, no OS authentication), and then the residual is the same as the command's.
2. **The agent cannot reach its own dashboard** (the phase-1 process guard, below): the
   confirm route refuses (`host_channel_unavailable`) unless that guard is armed.
3. **A fixed, human-readable confirmation, opened only by the person's own press.** The
   dialog lists what (operation kind), how far (tier internal / external; for a card the
   registry tool name or `mcp:` + digest, a short argument hash, the card id prefix; for a
   save the source and change codes; for a job a digest), for how long (2 min to confirm;
   for a request the six hours from the press) and what is treated as approved afterwards —
   built only from server enums and short codes (validated twice, by `host_ops` and the
   page), **never** free text from the agent. The token is only in the answer of the
   request that created the operation (kept as a digest, never in a status or list), and
   **every new request replaces the open one**: an operation created earlier — by an MCP
   server, say — is never shown and can no longer be confirmed.

**Layers of the browser confirmation** (defence in depth, none is authentication): the
128-bit one-time token; the checkbox (`acknowledged` exactly `true`; revision 7 — a box not
ticked leaves the operation open, `host_not_acknowledged`, it is not a denial); the instance
reference (a digest of the incarnation, exactly 16 lowercase hex characters, checked before
the constant-time compare: another process's page or a malformed value is refused,
`host_operation_incarnation`); the CSRF token and Host / Origin checks of every
mutation, plus `Origin` present and `Sec-Fetch-Site: same-origin` on the confirm route
(`host_browser_refused`); the refusal while an agent tool call runs (`tool_call_running`);
every new press ends the open operation (an older id is no longer confirmable).
**Common to both channels:** a loosening POST without a confirmed operation is answered
428; an operation is single-use (a replay is refused), expires (2 min to confirm, 30 s to
apply), is bound to the target generation (`stale_operation`), is rate-limited (host: one
open and 12 per 10 min; browser: 30 per 10 min, since nothing pops up for the agent to
spam), is forgotten by a restart and ended by `/new` (`HostDesk.revoke`); and the
running-tool-call refusal applies to the request.

**Lighter vs heavier (the design yardstick).** Against the three risks this guards: (a)
*the agent widening its own permission without a person* — the agent's own tools are held
by premise 2 on both channels; a separate program of the same user is held by no channel
except the dialog with OS authentication (when actually selected), and the yardstick puts
"the same user running arbitrary code" out of scope (§8a residual); (b) *mistakes and
inattention* — the fixed format and the deliberate tick are the measure on both channels,
an OS dialog adds nothing to them; (c) *leaks of secrets and personal data* — independent of the channel (masking,
§10). So the host channel buys protection only against the out-of-scope case, at the price
of a terminal or a dialog switch for every confirmation: it stays opt-in.
**Cost.** Security: on the default channel a same-user program outside the server (an MCP
server, a shell) can confirm its own operation — phase 2 held against the MCP case with the
dialog. Convenience: no terminal and no desktop dialog; one dialog and one tick per
confirmed operation while `STIRA_YOLO_ALLOW` is set (the time per confirmation is not
measured).

**Cost of revision 7 (checkbox, approval cards one press).** *Security side, what is lost:*
(1) a typed word was a little harder for a script driving the page (UI automation, a
browser extension) than a tick; neither is a defence against automation — the page's own
protocol was always answerable by a same-user program (premise 1, §8a) — so what is lost is
a speed bump for the out-of-scope case, not a layer against the in-scope ones; (2) an
approval card is no longer confirmed while allowed: a same-user program outside the server
(an MCP server) that reads `/api/csrf` can approve a pending card in one press — exactly as
without the allowance, where this was always possible; the agent's own HTTP tools still
cannot reach the dashboard (premise 2) and the running-tool-call refusal stays. *Kept:*
`Origin` / `Sec-Fetch-Site`, CSRF, the running-tool-call refusal, the one-time token, the
instance reference, single use, expiry, target generation, "a new press ends the older
operation" — every layer except the typed word. *Convenience side, what is gained:* no
typing in any confirmation (Space or a click, then the confirm button), and an approval with
the allowance set is one press as it always is without it (phase 2b made it a dialog per
card, against the 29.5 s median approval wait that motivates YOLO). Inattention is still
addressed by the fixed format, a box that is unticked at every opening and a disabled
confirm button until it is ticked.

**Options for the button's (and the decisions') human authentication — Open (12)** (decided
2026-10-18 23:16: browser by default, (v) as the opt-in host channel; the table is the
comparison of revision 5).

| Option | Forgeable by non-exec agent clients (HTTP tools, MCP, delayed delivery)? | By a shell? | Cost |
|---|---|---|---|
| (i) browser user gesture + one-time server challenge + operation token on a separate origin | **yes**: a gesture is not visible to the server; a separate loopback origin is reachable by MCP; the challenge travels through a readable API | yes | — rejected **as authentication**; revision 6 uses a same-origin form of it (token + typed word — a checkbox since revision 7 —, premises 1–3) as the default channel, stating that it is not authentication |
| (ii) **host confirmation started by the button**: the server shows a native dialog in the user's GUI session — with OS authentication (LocalAuthentication: Touch ID / password) where available | no: the answer returns to the server process, not over HTTP | plain dialog: by UI scripting with accessibility rights; **with OS authentication: no** | GUI session only (not headless / remote); macOS first, others to measure |
| (iii) WebAuthn assertion with user verification over `(request_id, operation, args hash)`, credential enrolled out of band | no | no (key in the authenticator) | enrolment; `localhost` support to measure |
| (iv) host terminal: `stira yolo confirm <request_id> --tier T --confirm YOLO` (and `stira approvals decide …` for decisions), principal `local:<OS account>` | no: none of those clients runs a host command | **yes** | a terminal; works headless / over SSH |
| (v) button = request only, final approval through (ii) or (iv) | as the channel used | as the channel used | combines the above |

**Recommendation of revision 5: (v)** — the button only requests; the final approval is
(ii) **with OS authentication** when the server runs in the person's GUI session, otherwise
(iv); (iii) later as a dashboard-native form for both. **Revision 6 (operator):** the default
is the browser channel above; (v) is the opt-in host channel (`STIRA_YOLO_HOST_CONFIRM`);
(iii) stays a later option. Every channel shows what it approves (tier, end time; card: tool
and short argument hash — never the arguments themselves), refuses after the request's
expiry and is single-use.

**Loopback reach to the own dashboard (prerequisite, operator decision 17:58).** While
`STIRA_YOLO_ALLOW` is set, Stira's HTTP tools (`http_request`, `web_fetch`, `web_search`,
`rss`, `webhook_send`, `oauth_api_call`) refuse a destination that resolves to the server's
own listen address and port (loopback names and literals included; resolved once and pinned,
redirects already refused). This is **not** the general destination guard (private /
link-local), which the operator decided not to build (Open (7)). MCP servers and exec cannot
be held to it; on the host channel only the OS-authenticated dialog (when selected) holds
against them — the host command does not hold against exec —, on the browser channel nothing
does (premise 1, §8a).

**Since #495 (2026-10-20): at every start, not only with the allowance.** `stira_server.main`
arms the guard whatever `STIRA_YOLO_ALLOW` says (a wildcard listener without the allowance is
armed `any_interface`: an address at the dashboard's port is the dashboard when this host can
bind it), and `http_request` (and `rss` when installed) checks its URL before anything is sent
— the host resolved, every redirect hop again — and refuses with `E_OWN_DASHBOARD`
(`tools/own_dashboard_gate.py`); the connect hook stays the backstop for a second DNS answer.
No setting; the YOLO-only refusal of a wildcard listener is unchanged.

**Follow-ups (#499, 2026-10-22).** The server also arms the observer dashboard's fixed port
(`dashboard/app.py`, `own_dashboard.OBSERVER_PORT`; it reads the same config files): at that
port every address of this host is refused (`also_ports`). This host name's addresses are
looked up on first need instead of at every start (an mDNS `.local` name can take seconds)
and then kept. A refusal by the connect hook is logged (`E_OWN_DASHBOARD`, no address), so an
in-process caller that swallows the error stays visible; a test runs a chat turn, requests
served and the stop on a live, armed server and finds no connection to its own port. When
`http_request` is registered but its module is not loaded, the redirect hop check cannot be
installed: that is logged once (the URL check and the connect hook still hold). Every write
route of the server (and the YOLO routes) is listed in `tests/test_breaker_write_paths.py`;
none outside the settings hub writes `stira_config_overrides`. Still a residual: a second
chat server of the same user on a port other than these two is not covered (its port is not known).

**As implemented (phase 2).** `src/stiracore/yolo/host_ops.py` (the operation: binding
digest of kind + route + JSON body, target-state digest, incarnation, single use, 2 min to
confirm and 30 s to apply, one open operation and 12 per 10 min), `requests.py` (the YOLO
request and its rate limits of §1a), `host_channel.py` (the fixed text; the Unix socket for
the command, whose kernel-reported peer must be the server's OS user and neither the server
process nor a descendant of it), `host_dialog.py` (the dialog, then LocalAuthentication; only
the dialog and its timeout path were measured, a person completing Touch ID inside it was
not), `host_cli.py` (`stira yolo pending|confirm|deny`, `stira approvals pending|decide`; the
word is typed at a TTY prompt rather than given as `--confirm YOLO`). A confirmed YOLO
request writes `audit.yolo.confirmed` and is then settled as `fenced(cancel)` →
`issue_aborted_cancel` (`audit.yolo.request_fenced`, system): phase 3 replaces that step
with the open sequence. `STIRA_YOLO_HOST_CONFIRM` (`auto` / `command` / `dialog`) chooses
the channel. Tests: `tests/test_yolo_host_*.py`, `tests/test_yolo_requests.py`,
`tests/host_confirm_test.cjs`.

**As implemented (phase 2b, revision 6).** `STIRA_YOLO_HOST_CONFIRM` unset (or `browser`)
→ the browser channel: no socket and no dialog are created; `host_ops.HostOperations` with
`channel="browser"` returns `confirm_token`, `word` and `instance` only in the 428 that
created the operation and replaces any open one; `confirm_in_browser` checks token, word and
instance (exact built-in types only; the word became a checkbox in 2c, below);
`POST /api/host-operations/<id>/confirm` (`routes.py`)
adds the channel, own-dashboard, `Origin` / `Sec-Fetch-Site` and running-tool-call checks
and serves a YOLO request too (`requests.YoloRequests.confirm_in_browser`: token, `YOLO`, and
`EXTERNAL` for tier external; the `confirmed` row with surface `admin`). The page:
`api.js` opens the shared confirmation dialog (`confirm_dialog.js`) for a browser-channel
428, posts the confirmation and re-sends once; the Stage's approval client does the same
through `StiraHostConfirm`. A host value of `STIRA_YOLO_HOST_CONFIRM` keeps phase 2 exactly
and the confirm route answers `host_confirm_on_host`. Also in 2b (review of phase 2): the
scheduler reads the job, checks the confirmed generation, consumes the operation and applies
the change under its own lock (`Scheduler.exclusive`); the page's cancel is final (a later
`confirmed` poll re-sends nothing); a press publishes its candidate only by a CAS on the
machine's revision, so a latch set while its row was written survives; `/new` ends every
open operation and fences the request (stop is phase 3). Tests:
`tests/test_yolo_browser_confirm.py`, `tests/host_confirm_test.cjs`,
`tests/stage_adapter_test.cjs`.

**As implemented (phase 2c, revision 7).** The browser confirmation sends `acknowledged:
true` (and `external_acknowledged: true` for tier external) instead of words; both are
accepted only as the built-in `True` (`host_ops.is_acknowledged`), a missing tick answers
400 `host_not_acknowledged` and leaves the operation open. The press's answer carries
`confirm: {token, external, instance}`. The instance reference is checked against
`[0-9a-f]{16}` before `compare_digest` in both stores (`host_ops.instance_matches`; a
non-ASCII value raised `TypeError` before). `chat_api` no longer requests an operation for
approve / edit (kind `approval_decide` removed). The shared dialog (`confirm_dialog.js`)
gained `checks`: one box per label, the confirm button disabled until all are ticked, the
first box focused, Space ticks, Enter on a box does nothing, Esc cancels, Tab order box(es)
→ Cancel → confirm; `api.js` uses it for confirmed operations and exposes
`StiraHostConfirm.yolo(answer)` for the YOLO button of phase 3. The start-up exec warning
names the effective channel and the setting (`runtime._warn_exec`). Tests:
`tests/test_yolo_browser_confirm.py`, `tests/test_yolo_host_gate.py`,
`tests/test_yolo_host_channel.py`, `tests/confirm_dialog_test.cjs`,
`tests/host_confirm_test.cjs`, `tests/schedules_page_test.cjs`.

**Required tests (phase 2):** with Always `http_request` and with a fake MCP tool: fetch
`/api/csrf`; post a YOLO request, a `RECOVER`, a hub loosening with `confirm`, a scheduler
resume (revision 7: approve / edit of a card are one press and not in this list); **deliver
each after the tool call ended** (handler held before its check) → no window, nothing
loosened; `http_request` to `127.0.0.1:<port>`, `localhost`, `[::1]` and a name resolving to
loopback → refused; a confirmation is accepted once, a replay, an expired one and one of
another incarnation are refused. **Scope of these guarantees:** "an agent-made request is
never confirmed" holds for the agent's in-process HTTP tools (premise 2) on both channels,
and for MCP servers / delayed delivery only on the host channel with the OS-authenticated
dialog actually selected; on the browser channel (default) and the host command an MCP
server or a shell can confirm what it requested (§8a / §8b residual).

### 9. Exec — **decided: option C** (operator, 2026-10-19 12:28; revision 8)

| Option | Requirement 4 (floors etc.) | Requirement 7 (agent cannot enable) | Note |
|---|---|---|---|
| **A** exec in tier external | **not met** for commands: a command edits the data directory and audit and runs children with ungated tools and no YOLO records (#145) | opening a window: met only with an OS-authenticated confirmation (§8a); with (iv) or the browser channel (revision 6) a command can confirm its own request | #362's "exec mark only after a human approval" becomes "human or YOLO" |
| **B** exec never answered by YOLO | met for everything YOLO approves | same as A for opening: exposed exec can run (iv) whatever YOLO does | every command stays a card (phases 0–6) |
| **C — adopted** A + double confirmation **at the press**: an extra `実行系も含める` / `Include exec` choice (only with `STIRA_ENABLE_EXEC_TOOLS` and allowance `external`), its own checkbox in the confirmation and its own line (and the typed word `EXEC`) in the host confirmation — replaces revision 3's `STIRA_YOLO_EXEC` | as A — **for commands it is not met**, and that is stated, not hidden | as A — the extra confirmation informs, it guarantees nothing | per-tool table below |
| **D** A inside an isolation covering network, data directory, config, audit, service definition, parent process and the host confirmation | met for the isolated process | met for it | not available |

**Decision (revision 8).** The operator chose **C** on 2026-10-19 12:28, explicitly
accepting the requirement-4 residual for commands: **a command that YOLO approves runs as
the server's OS user and can reach what Stira's floors and audit protect** — it can write
the data directory, SOUL, the settings and the audit database, change Stira's code or
service definition, leave a process behind, and (on the browser channel) do what the page
does (§8a, §8b premise 1). Stira does not claim to hold against that; the press-time
choice and its checkbox make sure a person chose it knowingly, for one six-hour window.

**What a window with the exec flag answers** (`tiers.EXEC_AUTO_TOOLS`,
`tiers.exec_refusal`):

| Exec tool | In a window with "Include exec" | Why |
|---|---|---|
| `shell` | **automatic** (Ask only, human turn, no secret-like argument, exec sub-cap) | the command runs inside this one call |
| `batch` | **automatic** (same) | each inner call goes through the parent agent's own hooks (the SDK's direct tool call fires `BeforeToolCallEvent`): an inner Ask call is judged on its own — a card, or an automatic approval with its own slot; measured, `tests/test_headless_exec_tools_e2e.py` ("a shell call inside an approved batch needs its own card") and phase 7's real-app tests: `tests/test_yolo_exec_batch_app.py` (review of #441) runs inner `cron` / `think` / `use_agent` / `swarm` / `graph` (own card, not run), an exec tool on its default Never and a name that is not a tool (refused), an MCP tool (card), `handoff_to_user` (judged as its direct call), `batch{shell}` / `batch{batch{shell}}` outside an exec window or under the breaker (never automatic; with every batch approved by a person the inner `shell` still asks) and a nested batch in an exec window (one slot per call). If the premise ever fails (an inner call without the hook), `batch` leaves `EXEC_AUTO_TOOLS` and stays a card |
| `cron` | **a person's card** (`exec_runs_later`) | it writes a crontab line that runs later as the server's OS user, without YOLO, a card or Stira's gate — also after the window ended |
| `use_agent`, `think`, `swarm`, `graph` | **a person's card** (`exec_child_ungated`) | they start child agents with the parent's tools and none of Stira's hooks (#145): a child's tool runs without a rule, a card, the gate's floor check (measured: `think`'s inner agent writes SOUL, `tests/test_tool_path_contract.py`) or the window's mitigations; `graph` keeps its agents for a later `execute` |

Unchanged in every window: Never is Never (the rule, `blocked_tool_names`, the breaker's
write block, `critical_action`), `CRITICAL_HALT` is a person's, a write the floor refuses is
refused before any approval, exec is hidden unless `STIRA_ENABLE_EXEC_TOOLS`, **never
Always** (YOLO writes no rule), each call is bound to its arguments and consumed once by
`ExecGatedTool` (#362: the verifier issues the single-use exec gate mark only for a human
approval *or* an exec automatic approval of this very tool), only human chat / Stage
turns (jobs, webhooks, bots, durable-approval resumes, sub-agents: a card), arguments that
`mask_secrets` would change are a card — the command string included (`export TOKEN=…`, a
bearer header, a URL key; known patterns only) — and the **exec sub-cap**
(`STIRA_YOLO_MAX_EXEC`, default 200, 0 to the cap): each exec approval holds one slot of the
cap **and** one of the exec sub-cap (not of the external one); used up or temporarily
full → exec tools ask again, the window goes on; `0` opens the window with every command a
card. A window never gains exec later: the flag is fixed at the press (the one loosening
judgement refuses `exec` false → true in every context, and exec with tier internal).

**What a command left behind can still do (residual, accepted).** While a tool call runs,
the loosening routes, the press and the confirmation refuse (`tool_call_running`, §8b), and
a new request is refused while the window is open (`yolo_active`); a process the command
leaves running after its call ended is not held by either — it can, like any same-user
program, confirm loosening saves on the browser channel, approve a pending card, or wait
for the window's end and press again (a person still confirms that press in the page it
did not open: the token is only in the pressing page's answer, but a same-user program can
press and confirm itself — §8a). The OS-authenticated host dialog (`STIRA_YOLO_HOST_CONFIRM`
= `dialog`, when actually selected) is the only channel such a process cannot answer.

**The YOLO settings file is part of the same residual (phase 7c, PR #444 review).** The
`yolo` section of `gateway-config.json` decides the next start's allowance, channel and
caps. The agent's file tools cannot write it, approved or not (the store's files are on the
write floor wherever they are; `tests/test_yolo_config_floor.py`, with and without
`STIRA_DATA_DIR`, through a symlink and `..`). A command auto-approved in an exec window can,
exactly as it can edit the data directory and the tool rules file: a loosening it writes
there is left after the window, like a loosened tool rule. The settings page refuses saves
while a window is open (`yolo_busy`); the next start prints one `INFO` line naming the
fields taken from the file that loosen YOLO (names, never values), and the settings page
and `/api/yolo/status` show each value's source (`config`). **Lighter vs heavier:** a
digest of the last confirmed save, checked at start-up (fail-closed to `off` on a
mismatch), was considered and not built: the same-user shell that can write the file can
recompute the digest (or write the audit chain it would live in), so it adds code and a
start-up failure mode without removing this residual. **Cost:** *security side* — a
window's command can leave a looser YOLO setting for the next start (as it can leave a
looser tool rule), visible only in that start's line and on the page; *convenience side* —
nothing changes for a person. Out of scope as everywhere here: a same-user program or an
OS compromise.

**Lighter vs heavier.** Heavier alternatives — an allow-list of commands, a sandbox (#167),
another OS user (#318), forcing the host dialog for an exec window — either do not exist
yet or turn the feature into the prompt-per-command it replaces. The lighter measures
(the opt-in at start-up, the explicit choice at the press, a checkbox that names the
bypass, the sub-cap, the excluded tools, the secret check and the audit row per call) cost
one more tick per window. **Cost:** *security side* — commands run without a card for up to
six hours, and a command can defeat Stira's floors and audit (accepted); *convenience side*
— `cron` and the child-agent tools still ask (each would carry the permission past the
window), a command with a secret-like string asks, and more than 200 commands per window
ask again.

**As implemented (phase 7a — backend).** `allowance.py` (`STIRA_YOLO_MAX_EXEC`;
`STIRA_ENABLE_EXEC_TOOLS` read once with the YOLO variables; `exec_offered` =
allowance `external` and exec tools exposed), `state.py` (`with_exec` on the request and
the window, refused with a tier other than external), `loosening.py` (`exec` in the window
snapshot), `tiers.py` (`EXEC_AUTO_TOOLS`, `EXEC_PERSON_REASONS`, `exec_refusal`),
`requests.py` (`press(tier, with_exec)` → `yolo_exec_not_allowed` unless offered; the
browser confirmation needs `exec_acknowledged: true`; the exec sub-cap's slots;
`exec_cap_used` / `exec_cap_full`), `checkpoint_hooks.py` (the verifier counts an exec
automatic approval of this exact tool for the exec gate mark), `routes.py`
(`POST /api/yolo/request {tier, exec}`; the press's answer carries `confirm.exec`; the
confirm route reads `exec_acknowledged`), `host_channel.py` / `host_cli.py` (the host text
names exec; the host command asks for `EXEC`). Rows (ADR 0013): `exec` on
`requested` / `confirmed` / `started` (+ `exec_cap`), `exec_used` on `ended`,
`tool_class` (`internal` / `external` / `exec`) on every `auto_approved` row and, for exec,
`exec_slot` / `exec_limit` — the command only as the keyed `args_ref`. Status adds
`exec_offered`, `caps.exec`, the window's `exec` / `exec_used` / `exec_reserved` /
`exec_limit` / `exec_effective`, and `exec_n` / `exec_limit` (tier `exec`) on a log line —
the tool name and `#ref` only. The UI (the third choice, the third checkbox, the badge's
`実行系も含む` / `実行 N/200`, the log line wording) is phase 7b (§12). Tests:
`tests/test_yolo_exec.py`, `tests/test_yolo_exec_app.py`.

### 10. External tier: risk and mitigations

While nobody watches, content the agent reads can tell it to send something out, and the
call is approved automatically. Mitigations (all required for tier external): an external
call whose arguments `mask_secrets` would change is not approved automatically (a card;
known patterns only); Never stays Never (`http_request` is Never on new installs); the
sub-cap and cap; the destination record (§11, Open (10)); the host-confirmed operations (§8b); stop; every
automatic approval audited.

**As implemented (phase 6).** `yolo/tiers.py` (`external_refusal`, `destination`,
`mcp_tool`), `yolo/requests.py` (`approve` / `consume`), the gate
(`CheckpointHooks._auto_decision`). In a window of tier **external**, the Ask of a tool in
`EXTERNAL_TOOLS` — the positive list of §4 (the "network egress" row + `perplexity_search`),
**not** the unclassified rest — is approved in a human chat / Stage turn (origin rules of
phase 5a unchanged). A person's card instead when:

* `mask_secrets` would change the arguments (`secret_in_args`; known patterns only — a
  URL's query or userinfo, a header, a body, a host name with a known credential prefix or
  a long high-entropy label, a secret-named key). Phase 6 also fixed a gap in the masking
  itself: the first query parameter of a URL (`https://h/?api_key=…`) was never examined
  (the key/value rule matched from the scheme over the whole URL); it is now, everywhere
  masking runs;
* the call takes a credential from the host by reference (`credential_by_reference`:
  `http_request` with `auth_env_var` or `aws_auth`);
* its `url` has a host that is not plain ASCII, or none at all, or an empty or over-long
  label (`destination_unknown`). A non-ASCII host is **not** normalized: IDNA 2003 and
  2008 read `faß.de` differently (`fass.de` / `xn--fa-hia.de`), so any normalization here
  could record two different destinations as one; one extra card for such a URL is the
  smaller cost, and an ASCII (Punycode) host is recorded as written;
* it also writes a local file outside the workspace / write roots (`speak`'s
  `output_path`);
* its arguments are not plain JSON values (a subclass, a tuple, `NaN` … — only built-in
  types are evaluated; `args_unknown`);
* an MCP server registered a tool of that name (`mcp_tool`, in every tier: an MCP tool that
  reuses a listed name is still MCP);
* the external sub-cap is used up (`external_cap_used`: external tools ask again, the window
  stays active for internal tools; the badge says so) or temporarily full
  (`external_cap_full`) — each external approval holds one slot of the cap **and** one of
  the sub-cap; sub-cap 0 and tier internal never block issue or resume (§6a).

Never, the floors, the breaker's write block, `CRITICAL_HALT`, exec (outside a window with
"Include exec", §9) and `diagram` are unchanged (decided before the approver is asked, or
`tier_not_covered`). A window of tier
internal keeps every external tool's card. **Destination (Open (10), provisionally (b)):**
`audit.yolo.auto_approved` gains `tier`, `external` and `destination_ref` — the keyed hash
(the service's audit key, `key_id`) of the lower-cased host of the call's `url` argument;
absent for tools without one (search, speech, `memory`: the provider is fixed by the tool).
No row, server log line, status answer or page line carries the host in clear; the log line names the tool and `#ref` only:
`自動承認 (YOLO 外部): web_fetch #b4c1 — 38/2000 · 外部 5/200` /
`Auto-approved (YOLO external): web_fetch #b4c1 — 38/2000 · external 5/200`. `audit.yolo.ended`
adds `external_used` for a window of tier external. Status adds to `window` (only adds):
`external_used`, `external_reserved`, `external_limit` (0 in a window of tier internal);
`effective_tier` is `external` while sub-cap slots are left. The badge shows
`外部 N/200` / `External N/200` and, once external tools ask again (used up, or a sub-cap of
0), `外部ツールは確認に戻りました` / `External tools ask you again`. Tests:
`tests/test_yolo_external_tier.py`, `tests/test_mask_secrets.py` (URL parameters),
`tests/yolo_ui_test.cjs`, `tests/a11y/yolo_axe.cjs`. **Residual:** a secret in a form the
known patterns do not match (a short label, an encoded value, one split over calls) is sent
out without a card — §10's "known patterns only"; a same-user program is out of scope (§8a).

### 11. Audit, window marker and latch

**Rows** (events.db chain, ADR 0009; names and fields per ADR 0013 §1b / §5, order per §6a):
`audit.yolo.requested` (surface `admin`, tier, `request_id`, op-token ref),
`audit.yolo.confirmed` / `.denied` / `.request_expired` / `.request_cancelled` (surface `cli`
for (iv), a host-dialog surface for (ii) — ADR 0013 change), `audit.yolo.started` (tier,
caps, `t0`, end time, keyed hash of the incarnation; the window's ADR 0013 `standing`
receipt, stored with the window, §2c there), `audit.yolo.paused` / `.resumed` (reason,
`generation_from` / `_to`), `audit.yolo.ended` (reason, counts), `audit.yolo.auto_approved`
(tool, keyed argument hash, hash of `toolUseId`, `window_id`, generations, origin kind,
destination) with its `consumed` / `void` outcome, `audit.yolo.unlatched`. Each automatic
approval's permit still needs its own `call` receipt (ADR 0013 §2b). Tool names:
registry tools as an enum; **MCP and other dynamic names** (they come from outside) as
`mcp:` + keyed hash, never the name. Not written to the approval store.

**Fields of every `audit.yolo.*` row** (ADR 0013 §1b requires `window_id`,
`generation_from`, `generation_to`, `authority_epoch`). The `window_id` is allocated at the
press, so request rows have one before a window exists; the generation is 0 until the window
opens.

| Row | `phase` / `delta_class` | `actor_kind` / `surface` | `generation_from → to` | Further required fields |
|---|---|---|---|---|
| `requested` | `intent` / loosening | `human` / `admin` | 0 → 0 | `request_id`, `tier`, human op (§2f: dialog token, expected = no window, no request, latch clear) |
| `confirmed` | `decision` / loosening | `human` / `cli` (or a host-dialog surface, ADR 0013 change) | 0 → 0 | `request_id`, `tier`, human op (expected = request `pending`, fence) |
| `denied`, `request_expired`, `request_cancelled`, `request_fenced` | `transition` / narrowing | `human` or `system` | 0 → 0 | `request_id`, `reason_code` |
| `started` | `decision` / loosening | `system` | 0 → 1 | `request_id`, `tier`, `cap`, `t0`, `confirmed_ref` (event id of `confirmed`); its `standing` receipt is stored with the window |
| `paused`, `resumed` | `transition` / narrowing, loosening | `system` | g → g+1 | `reason_code`; `resumed`: `paused_ref` |
| `ended` | `transition` / narrowing | `system` or `human` | g → g+1 | `end_reason`, `end_class` (`normal` / `abnormal`), counts |
| `auto_approved` + `consumed` / `void` | `decision` + `outcome` | `system` | g → g | ADR 0013 `audit.tool.*` fields with `authorizer_kind = yolo` |
| `unlatched` (+ `unlatch_applied`) | `decision` + `outcome` / narrowing of the latch | `human` / `cli` | g → g | `latch_subject` (`window` / `orphan_marker`), `marker_digest`, `chain_seq_seen`, human op (§2f) |
| `settings_saved` (phase 7c, PR #444) | `decision` / loosening, before the `yolo` section is written (a failed row refuses the save) | `human` / `admin` | 0 → 0, zero `window_id` | `fields` (loosened) and `changed` — field names only, never a value |

**`ended` by the clocks (#596, additive, optional):** an end `revoked_clock_anomaly` also carries `clock_check` (`wall_backwards` / `wall_behind_boot` / `clock_unreadable` / `watchdog_failed`), `d_wall` / `d_boot` (seconds since the press), `last_d_wall` / `last_d_boot` (seconds since the previous check, may be negative) and `clock_slack_sec` — fixed words and numbers only (§7); no other end carries them, and readers ignore keys they do not know.

**What reaches storage — known vs unknown secrets.** Enums, numbers, random ids and keyed
hashes hold no input text. The only input-derived value is the destination (Open (10)):
`mask_secrets` removes **known** secret patterns; an **unknown** secret encoded in a host
name (`<data>.attacker.example`) stays in clear in a hash-chained row that cannot be
edited. Open (10) compares the options; "no free-text field" alone proves nothing.

**Window marker (the latch that cannot be skipped by a failed write).** A file
`yolo-window.json` in the data directory (read and write floor, #271/#312) records
`{window id = keyed hash of grant_id, incarnation hash, state: open | closed}`. It is
`open` from before `audit.yolo.started` until a clean, audited end; every other
end — a failed audit or latch write, a clock anomaly, a crash, a kill, a power loss —
leaves it `open`.

**Durable order** (power loss included, not only a killed process). "Durable" means: file
content flushed, then the rename, then the **directory** holding it flushed (a new
directory entry or a rename is not on disk until its directory is); on macOS with
`F_FULLFSYNC` (plain `fsync` does not empty the drive cache there), and for events.db a
committed transaction in WAL mode with `synchronous=FULL` plus `fullfsync` /
`checkpoint_fullfsync` on macOS.

1. **Open** — write the marker to a temporary file, flush it, rename it, flush the data
   directory; then commit `yolo.started` durably; only then the window may become
   `active`. Any failure → nothing opens, nothing approved.
2. **Close** — narrow in memory (§6a); commit `audit.yolo.ended` (with `end_class`)
   durably; then, **only for a normal end**, replace the marker with `closed` by the same
   temporary-file → flush → rename → directory-flush sequence. An abnormal end leaves the
   marker `open`.

**Latch judgement** — at start-up (to decide whether the button is offered) and again for
every request; without `STIRA_YOLO_ALLOW` nothing is checked or changed. A window id is
*closed* only by `audit.yolo.ended` with `end_class = normal` and a reason in the normal
list (§6), or by a verified `audit.yolo.unlatched` for it. An `ended` row with an abnormal
class or reason, or an unknown reason, **does not close it**. A request is accepted only if
every `started` window in the chain is closed **and** the marker is `closed` or
`closed_by_unlatch` with a verifying receipt. The only automatic repair: a marker still
`open` for a window the chain shows closed *normally* (crash between the end row and its
rename) is rewritten to `closed` by the durable sequence; a window closed by an abnormal
`ended` row is never repaired. Otherwise the button shows `yolo_latched` and requests are
refused: an `open` marker whose window is not closed; a marker with no `started` row for its
id (orphan, below); `started` without a closing row (also with a missing or `closed`
marker); an unreadable, malformed, symlinked, foreign-owned or multiply-linked marker; a
chain that cannot be read or verified. The server itself starts either way; detection
survives a restart even when the latch could not be written at the time.

**Explicit exception — an issuance that fails before anything of the start is durable
leaves no latch after a restart** (decided in the phase-3 review, 2026-10-19). A failed
`audit.yolo.confirmed` write, or a marker write that fails before its rename, latches the
running process (no new request until unlatch or restart) but leaves nothing on disk that
a later judgement could see — and that is accepted: no window was ever usable (the final
CAS was never reached), no permission ever existed, and the next start re-checks the audit
writer's health before anything is offered. *Lighter vs heavier:* a durable "issuance in
progress" record written before `confirmed` would keep this latch across a restart, but it
adds one more durable write to every press and, after a disk fault was repaired, only
forces a person to unlatch something that never opened — a convenience cost with no risk
it reduces. Every failure **after** the marker was replaced (`open` written: a failed
`started`, a receipt mismatch, an unhealthy writer, a clock anomaly) stays latched across
restarts as above (orphan marker or unclosed window).

**Unlatch — closing exactly one latch, append-only (ADR 0013 §2c / §2f).** `stira yolo
unlatch --window <id> --confirm UNLATCH` (host command, typed word), refused while a server
holds the owner lock. Under the marker's lock:

0. **§2f inside the run**: read the marker (`marker_digest` = hash of its bytes) and the
   chain head (`chain_seq_seen`); determine the subject — `window` (a `started` window not
   closed) or `orphan_marker` (an `open` marker whose id has **no** `started` row: a crash
   after the marker and before `started`; its `request_id` hash must match a `confirmed` row
   of that id if one exists); show subject, id, reason and the last rows; take the typed
   word; re-read marker and chain head — any change → refused `stale_operation`.
1. **Decision row**: generate the `operation_id`, commit `audit.yolo.unlatched` (subject,
   id, `marker_digest`, `chain_seq_seen`, human op) durably → receipt. Failure → refused,
   nothing changed.
2. **Target side**: replace the marker with `{state: closed_by_unlatch, window id,
   operation_id, receipt ref}` by the durable sequence.
3. **Outcome row** `audit.yolo.unlatch_applied` (best effort). If step 2 failed, the next
   judgement sees an `open` marker whose id has a verifying `unlatched` row: that one closure
   is honoured and the marker repaired — it is the person's explicit, bound decision.

No audit row is deleted and nothing is ignored wholesale: each latch needs its own unlatch;
a delayed command whose `marker_digest` or `chain_seq_seen` no longer matches changes
nothing. Unlatch never opens a window.

**Tests (phase 4):** a crash injected after every syscall of open, close and unlatch, with a
**power-loss model** that drops every write, rename and directory entry not yet flushed
(an fsync-ordering fault injector around the file and SQLite I/O); after each: the next
request is refused unless the window was closed or unlatched, and a server without the
allowance is unchanged; crash after `yolo.started` → refused → unlatch → request accepted;
crash after the marker and before `started` → refused → `unlatch` (orphan) → accepted; a
clock anomaly whose `ended` row was written → still refused until unlatch; marker write
failure → nothing opens. A physical power cut is **not measured**.

**Residual:** deleting the marker **and** the chain rows together (a full rollback) is not
detected — Open (8).

**As implemented (phase 3).** `src/stiracore/yolo/window.py` (the marker, its durable
write — temporary file, `F_FULLFSYNC` on macOS / `fsync` elsewhere, rename, directory
flush — and the judgement) and `requests.py` (the §6a steps, the close sequence, the clocks,
stop / `/new`, unlatch). Confirmation → `audit.yolo.confirmed` → marker `open` →
`audit.yolo.started` (0 → 1, caps, `t0`, `expires_at`, `confirmed_ref`) → both rows read
back from events.db (`SQLiteEventStore.receipt`: seq, hash, type, request / window id,
incarnation reference) → the final CAS (`state.issue_commit`) with the writer's health, the
receipts, fence, epoch and the clock verdict. The issuance always runs to the final CAS:
a stop / `/new` / cancel / epoch move in between writes `request_fenced` and the window
ends `issue_aborted_*` right after `started` (normal: the marker closes again); a failed
write, a receipt that does not match, an unhealthy writer or a clock anomaly is
`aborted(issue_failed)` — latched, the marker stays `open` (or is an orphan when `started`
failed after it). Every end runs the close sequence (`ended`, then the marker `closed` only
for a normal end whose row was written). The window is memory only; status
(`GET /api/yolo/status`) adds `window`, `last_end`, `latches`, `latch_reason`. The clocks
(§7) are checked at every look (status, press, confirmation) and by a watchdog thread
every 30 s. The judgement runs at start-up and before every press. A rules save moves the
epoch (`advance_epoch`: an issuing request is fenced). **Deviations from the text above,
for the review:** (1) the marker's window id is the chain's `window_id` (a random id,
already in the rows) rather than a keyed hash of it — a keyed hash with a per-process key
could not be matched after a restart; (2) unlatch is **in the server**, `POST
/api/yolo/unlatch {window_id}` as a confirmed operation (kind `yolo_unlatch`: the page's
checkbox by default, `stira approvals decide` with a host channel; its target is the latch,
the marker digest and the chain head, re-read before the `unlatched` row — a change is
`stale_operation`), not an offline `stira yolo unlatch` that needs the server stopped:
the server judges again at every press and holds the marker lock, so the in-server form
has the same §2f binding without a restart; `audit.yolo.unlatched` / `unlatch_applied`
accept surface `admin` too; (3) an unreadable / malformed / symlinked marker or an
unverifiable chain is a latch without an id: it is not unlatched by a row — the files need
a look, and a restart judges again; (4) events.db keeps its own durability settings (WAL,
SQLite's default `synchronous`; no `fullfsync` pragma added) — the marker is the durable
part of the order, and the chain row is read back before the window opens; (5) the
power-loss fault injector (dropping unflushed writes) and the phase-4 items that need the
approver (last slot, cap, pause during a card, late dwell, goal + YOLO) are not in this
phase; pause / resume and the cap stay unconnected (phase 5). Tests:
`tests/test_yolo_window.py` (every I/O point × stop / `/new` / cancel / epoch, every
failure → latched across a restart → unlatch, each final check shown to be what stops the
window, races, clocks, marker, judgement, the real event store),
`tests/test_yolo_window_routes.py` (stop, `/new`, server stop, the unlatch route, status,
**an active window changes no tool call**). **Review round 1:** the final CAS reads clocks, writer health and
breaker under the lock (a seam just before the lock lets the tests inject a writer failure
and a lock wait with time passing); every look ends an open window whose clock cannot be
read or whose writer turned unhealthy; stop / `/new` / cancel narrow before reading any
clock, and `HostDesk.revoke` ends the window before the operation store; the watchdog ends
the window when its own check fails; `F_FULLFSYNC` errors other than ENOTSUP / EOPNOTSUPP /
EINVAL / ENOTTY fail the write; a press runs its cheap refusals before the chain
verification; the unlatch target is bound to the marker and the chain's YOLO rows (not the
whole chain head), so an unrelated event during the confirmation does not make it stale.

### 12. UI and log

* **Button and dialog** (§1a): two choices; confirmation text — internal: 「YOLO を開始します。
  押した時点から6時間、選んだ範囲で『確認』が必要な操作を確認なしで実行します。『しない』の
  操作、保護された場所への書込み、CRITICAL 時の操作は変わりません。ホスト側でもう一度確認し
  ます。いつでも停止できます。」 / "Start YOLO. For six hours from now, operations in the chosen
  range that would ask you run without asking. Never rules, protected locations and operations
  at CRITICAL do not change. You confirm once more on the host. You can stop at any time.";
  (revision 6+: on the browser channel the last sentence before "You can stop" is 「この画面で
  もう一度確認します。」 / "You confirm once more on this screen.")
  external adds 「外部への送信も確認なしで実行されます。読み込んだ内容に誘導されて情報が外に
  出る危険があります。」 / "Sending to outside services also runs without asking; content the
  agent reads could lead it to send information out."
* **Confirmation** (revision 6, 7): browser channel — the shared confirmation dialog right
  after the press, the fixed-format lines of §8b premise 3, the checkbox 「内容を確認しました
  (いま自分が押した操作です)」 / "I have checked this; it is what I just pressed" (+ for tier
  external 「外部への送信が可能になることを理解しました」 / "I understand that sending to
  outside services becomes possible"), the confirm button disabled until every box is
  ticked, cancel (final at once); 390 px wide without horizontal scrolling; host channel —
  "ホストで確認してください" / "Confirm on the host", countdown, cancel.
* **Badge** (always visible, not dismissable) at the edge of the Stage dialogue box and the
  chat header: tier, remaining time, count, `一時停止中 (WARNING)` / `Paused (WARNING)`, a
  **stop button that works in every state** — also while a request waits for the host
  confirmation or is being issued (§6b); a notice 10 min before the end; the end reason
  until acknowledged.
* No card for an automatically approved call; one line in the chat log, Stage backlog and raw
  view: `自動承認 (YOLO 内部): file_write #a3f2 — 37/2000` / `Auto-approved (YOLO internal):
  file_write #a3f2 — 37/2000`.
* `GET /api/yolo/status` is computed on the server; the browser holds no policy. ja/en;
  #350's main-screen rules.
* **Exec (revision 8, phase 7b).** The tier dialog offers a third choice
  `実行系も含める` / `Include exec` only when status says `exec_offered`; it states that it
  includes external and that commands run without asking. The confirmation adds the third
  checkbox of §1a. The badge adds `実行系も含む` / `Includes exec` and `実行 N/200` /
  `Exec N/200`, and once the exec sub-cap is used up `実行系は確認に戻りました` / `Exec tools ask
  you again`. The log line names the tool and `#ref` only, never the command:
  `自動承認 (YOLO 実行系): shell #c0de — 39/2000 · 実行 1/200` /
  `Auto-approved (YOLO exec): shell #c0de — 39/2000 · exec 1/200`.
  **As implemented (phase 7b).** `yolo_ui.js` (`viewOf`: the third tier only with
  `exec_offered` and allowance external; the window's `exec` / `execCap`; the request body
  `{tier: "external", exec: true}`; `lineText` for `tier: "exec"`), `api.js`
  (`StiraHostConfirm.yolo`: the third checkbox and `exec_acknowledged` for `confirm.exec`),
  `ui/yolo.css` (the exec tag and text in the strongest tone), the ja / en catalogs, the
  offline harness (`shell` on a message containing `shell`). Measured in headless Chrome on
  the chat and Stage pages at 1300 and 390 px (ja / en): no horizontal scrolling, the three
  boxes in the confirmation, one real `shell` run → one line. Tests:
  `tests/yolo_ui_test.cjs`, `tests/host_confirm_test.cjs`, `tests/a11y/yolo_axe.cjs` (an exec
  window, its used-up note, the tier dialog with exec chosen).

**As implemented (phase 5b).** `dashboard/static/js/yolo_ui.js` + `ui/yolo.css`, present
only when `yolo.routes.install` ran (the template flag `yolo_ui`): without the allowance
the pages carry no slot, no script and send no request (`tests/test_yolo_ui.py`). One slot
in the chat header and one at the top edge of the Stage dialogue box (inside the box: it
clips what is outside). Status adds `offered` (allowed, clocks supported, a confirmation
channel, not latched, no window, no current request; `not_implemented` is gone; every
other reason is in `unavailable`) and `auto_approvals` — the lines of the latest window
(`n` = used after the call, the tool as a registry name, `ref` = the first 4 hex of the
`auto_approved` row's operation id as in the server log, `limit`, `tier`), recorded when
the permit is **consumed** (a voided approval has no line), last 20, kept after the end
until the next window opens. The page draws only this answer: the tier dialog (the §12
text for the configured channel, the tiers the allowance allows, Cancel the default, Enter
on a choice does nothing), `StiraHostConfirm.yolo` for the browser confirmation (phase
2c's checkboxes), on a host channel "confirm on the host" with the request's countdown
and a cancel; the badge (tier, time left, used / limit, `Paused (…)` with the dwell, the
10-minute notice, External windows say external tools still ask); Stop in every state
where something is current and when the status cannot be read (it posts first and reads
nothing before); the end reason until **OK** (remembered per window end in
`localStorage`, a display preference only); latched → **Unlatch** through the confirmed
operation of phase 3 (a latch without an id: no button, a pointer to
[yolo-quickstart.md](../yolo-quickstart.md)). Polling: once at load, on return to the tab
and after the page's own actions; 1 s while a request is pending / issuing (the
countdown), 3 s while a window is open, widening to 15 s while nothing changes; the chat
and the Stage read it right after a tool call ends; never while idle or hidden. A
re-render changes only the texts that changed. Log lines appear for the approvals made
while the page is open (the first read is the baseline): appended to the chat log, and as
a `stira:yolo-line` event the Stage puts into its backlog (after the turn it belongs to)
and raw view. **Not done here:** lines of approvals made before the page loaded are not
shown again (the badge's count has them); the chat log loses them on reload (the Stage
backlog keeps them until reload too). Tests: `tests/yolo_ui_test.cjs`,
`tests/a11y/yolo_axe.cjs` (every state and the tier dialog, chat and Stage),
`tests/test_yolo_ui.py` (the pages with / without the allowance, `offered`, a scripted
model's `file_write` in a real `/api/chat` turn → one line and no card). Offline harness for
trying it: `tests/stira_yolo_harness.py`.

### 13. Shared grant foundation with ADR 0011

* **`kind` is a mandatory discriminator** (`goal` | `yolo`) of the grant record; a record
  without it is invalid. The goal-grant functions (issue, narrow, resume, revoke as goal)
  accept only `kind = goal`; the YOLO functions only `kind = yolo` — a YOLO window can never
  be issued, resumed or extended as a goal grant, nor the reverse (type check + contract
  test).
* Shared: `grant_id`, generation, incarnation, clocks, `end_reason`, authority lock and
  epoch, `consume`, "every start revokes every grant", lifecycle events.
* **A goal-loop call answered by YOLO** carries both bindings; the final `consume` checks,
  in one step under the authority lock: the goal grant (id, generation, expiry, budget
  reservation), the window (id, generation, expiry, cap reservation), the common authority
  epoch and the incarnation. Any mismatch refuses.
* One window per process, independent of goal grants.

### 14. Fail-closed contract

| Contract | Here |
|---|---|
| One loosening judgement for all fields and paths | §2 table, incl. resume |
| Approvals bound to a monotonic generation | §6a; every narrowing increments it |
| Tampered / rolled back / missing / unreadable → safest, latched, kept across restarts | window never read back; marker `open` until a durable **normal** end; an abnormal end row never closes it; power-loss order; chain cross-check; unreadable = latched (§11); **exception:** an issuance failing before the marker is replaced latches the process only (§11, "Explicit exception") |
| Inputs of a commit read under the lock; narrowings depend on nothing that can fail | the final CAS reads the clocks, the writer's health and the breaker **after** taking the lock; stop / `/new` / cancel narrow first, without reading a clock or writing a row; an unreadable clock ends the window (`revoked_clock_anomaly`, latched); a writer that turned unhealthy ends an open window at the next look (`revoked_audit_failed`); `F_FULLFSYNC` falls back to `fsync` only for "not supported" errors (phase 3, review round 1) |
| State, logs, locks above the agent's write floor | memory; marker and chain on the floors |
| Free text and ids through masking | §11, with the known/unknown distinction |
| Failed revocation or audit write stops the loosening | §6a: narrowing first, never undone; issue / resume / approval only after their row |
| A stop or `/new` during issuance wins | fence + epoch + request CAS at the final linearization point (§6a, §6b) |
| Human decisions come from a person | dashboard requests, the configured channel confirms (§8b): browser (token + checkbox, premises 1–3; not a proof of a person, §8a residual) or host (opt-in); never a count of running calls; approve / edit of a card stays a one-press dashboard decision (revision 7) |
| Ids a full rollback can replay | incarnation on every token (§7) |
| Human operations bound to what they release | stop / `/new` → the fence (everything current, §6b); unlatch → one latch (window or orphan marker), marker digest + chain head, append-only rows; host confirmation → request id + operation + argument hash / version + incarnation; resume → CAS on the pause generation |

## Changes needed elsewhere (at implementation)

* **ADR 0013** — `audit.yolo.requested` / `.confirmed` / `.denied` / `.request_expired` /
  `.request_cancelled` / `.unlatched` in the registry; a host-dialog call surface (or (ii)
  recorded as `cli`); the `startup` surface no longer issues YOLO under Open (11) (B);
  `unlatch --window` (it cites `--latch-id`) with the §11 steps 0–3 as its §2c/§2f flow;
  the per-row `audit.yolo.*` field table of §11 (request rows with `window_id` and
  generation 0, `end_class`, `request_fenced`, `unlatch_applied`); the confirmation as
  the second human operation of §2f for every confirmed operation; **revision 6:** surface
  `admin` for `audit.yolo.confirmed` on the browser channel (the registry of
  `yolo/audit_schema.py` accepts `cli` and `admin`), and the browser confirmation (token +
  checkbox, not an authentication) as a recorded second human operation.
* **ADR 0011** — §2 authority type `yolo` (source: a host-confirmed request; may cause:
  automatic answer to `RULE_ASK`; bound to `(incarnation, grant_id, generation)`), `kind` as
  a mandatory discriminator, the `host_confirmed_request` / `breaker_resume` contexts; §5
  YOLO narrowings as epoch advances and the two-binding `consume`; §9 "no automatic resume"
  stated for grant loops (if Open (1) = P).
* **ADR 0010** — notes in §1 (Ask), §4 (loosening saves host-confirmed while allowed) and §5;
  the #362 addendum only if Open (2) includes exec. **ADR 0006** — pointer from the "nothing
  approves automatically" note (revision 7: approve / edit stay one press while allowed).
  **ADR 0008** — none.
* **#362** / **#299** — exec text per Open (2); `kind` and issuers in the foundation.
* **docs/configuration.md** (+ `_ja`): rows for the three variables; "No environment variable
  … auto-approves anything" stays true (the allowance approves nothing by itself) with a
  pointer here; revision 6: `STIRA_YOLO_HOST_CONFIRM` unset = browser, a host value requires
  the host (done in phase 2b). **admin-api-permissions.md**: confirmed operations, the
  browser confirm route and its residual, and the own-dashboard refusal (done in 2b).
  **hitl-approvals.md** (done in 2b), glossary, operations guide (allowance, button,
  confirmation channels, stop, unlatch).

## Implementation order

Nothing ships to operators before phase 5. The admin defence is never built on a counter of
running calls.

| Phase | Content | Acceptance (fake model, fake clocks) |
|---|---|---|
| 0 | the request × window × latch state machine (§6b) and the `audit.yolo.*` schema (§11) registered with ADR 0013, as tests before any code opens anything | every row of §6b and every schema row validated by the registry |
| 1 | **boundary and admin-API defence**: allowance parsing, invalid values, clock support; own-dashboard refusal in Stira's HTTP tools; common authority boundary (#276: `kind`, lock, epoch, single-use `consume`, ADR 0013 receipts) | invalid values refuse start; no allowance → identical behaviour and tool-spec hash; own-dashboard destinations refused |
| 2 | **human authentication**: host-confirmed operations (§8b, Open (12)) for approve / edit (until 2c) / loosening / `RECOVER`, then the YOLO request + host confirmation (no window yet) | every §8b test incl. delayed delivery; request rate limits; confirmation single-use / expiry / other incarnation refused |
| 2b | **revision 6**: the confirmation channel abstraction — browser (default: one-time token, typed word, fixed-format dialog, instance reference, `Origin` / `Sec-Fetch-Site`, own-dashboard guard armed) and host (opt-in, `STIRA_YOLO_HOST_CONFIRM`, the browser refused); `/new` revokes the operation store; the phase-2 review fixes (scheduler target CAS, final cancel, press CAS on the revision) | each browser layer refuses when it fails (and a test fails when its check is removed); an earlier operation is never confirmable after a new press; tier external needs `EXTERNAL`; a host value refuses the browser; `/new` ends a confirmed, unapplied operation; a change cannot slip between the scheduler's target check and its change; a cancel then a `confirmed` poll re-sends nothing; a latch set during a press survives it |
| 2c | **revision 7**: checkbox instead of the typed word (+ one more box for tier external), approve / edit one press, the §8b table of confirmed operations; the review minors of 2b (instance shape before `compare_digest`, the effective-channel warning, ADR leftovers, the schedules page wording) | the confirm button is disabled until every box is ticked; `acknowledged` other than `true` refused and the operation still open; a malformed instance is a 409, not an exception; approve / edit 202 without an operation on both channels; the warning names the effective channel |
| 3 | §6a steps, marker / durable order / latch judgement / unlatch (§11), clocks from the press (§7), stop, `/new`, pause / CAS resume | rows of §6 and §8; sub-cap 0 and tier internal do not block issue or resume |
| 4 | fault injection and races | as revision 3 (audit / marker failures, power-loss model, crash → unlatch, clocks incl. 7 h + 6 h, last slot, temporarily full vs used-up cap, pause during a card, late dwell, goal + YOLO mismatch) plus: confirmation after the request expired; two requests racing; press, then restart before the confirmation; **stop / `/new` / cancel injected at every I/O point after the confirmation** (before and after `confirmed`, marker, `started`, the receipt read) → no usable window; cancel vs confirm race (one wins); a new request after a clock anomaly whose `ended` row was written → refused until unlatch; crash between marker and `started` → orphan unlatch; these race tests come before any phase that opens a window |
| 5 | **tier internal + button + badge + log line together** (first operator release) | Never and floor writes refused while active; `CRITICAL_HALT` still a card, never answered by YOLO — a person decides it (one press on the card, revision 7); button, dialog and badge on every page state (1300 / 390 px, ja/en, axe) |
| 6 | tier external (§10, Open (10)) | secret in arguments → card; sub-cap → Ask |
| 7 | exec per §9 option C (revision 8): **7a** backend — allowance / sub-cap, the press's `exec`, the third checkbox in the confirmation, `shell` / `batch` answered in a window with the flag, rows, status; **7b** the UI (third choice, third checkbox, badge, log line) | every exec tool × window (exec / not, internal / external); Never, floor, breaker block, `blocked_tool_names`, `CRITICAL_HALT` a person's; secret in a command → card; sub-cap boundaries; `cron` and the child-agent tools a card; non-human turns a card; `STIRA_ENABLE_EXEC_TOOLS` off or allowance internal → no choice, the request refused; tool specs and prompt unchanged; a real `shell` run (fake model) |
| 7c | the values from `gateway-config.json` `yolo` and the settings page (operator request 2026-10-19 12:40): precedence env > config > default, `GET/POST /api/yolo/settings` (with and without the allowance), the YOLO section of the settings page | no section and no variable → as before (no button, no badge, no poll); a loosening save without the confirmation (or the host gate) writes nothing; refused during a turn / tool call / open window, for a pinned value, out of range; caps from the next window, the allowance at the next start; the other save paths still refuse `yolo*`; 390 px / ja / en / axe |
| 8 | goal loop (#299) | two-binding `consume`; halted grants not resumed |

## Open points (operator decision — all decided by 2026-10-19 12:28, kept for the record)

* **(1) Breaker.** (P) pause while WARNING / COHERENCE / CRITICAL / unknown (COHERENCE added
  as ADR 0008 §7 and ADR 0011 §9 treat it like WARNING); CAS resume after a 10 min NORMAL
  dwell inside the remaining time; end after > 3 pauses / 60 min. (E) end on any non-NORMAL
  level (#365). (H) pause on WARNING / COHERENCE, end on CRITICAL. P keeps a passing WARNING
  from ending a six-hour run; the resume is **not a new issuance but a loosening**, allowed
  only in its own context (`breaker_resume`, §2) for the same window (same tier, end time,
  cap), and it happens without a person — which ADR 0011 rejects for grant loops. At WARNING writes are already hidden and at CRITICAL-halt
  each write is a person's card, so P and E differ mainly *after* NORMAL returns.
  **Recommended: P** (operator's proposal); H if a CRITICAL should need a person.
  **Decided 2026-10-19 12:28: P** (as implemented in phase 5a).
* **(2) Exec.** A / B / C / D (§9). Recommended was B for v1, C only with an explicit
  acceptance of the requirement-4/7 residual. **Decided 2026-10-19 12:28: C**, with that
  acceptance stated by the operator (§9).
* **(3) Sub-agents.** Not covered / inherit with a shared count. **Recommended: not
  covered.**
* **(4) Webhook / bot turns.** Not covered / internal only / covered. **Recommended: not
  covered.**
* **(5) Values.** Cap 2000 (≈ one per 11 s for 6 h), external sub-cap 200, exec sub-cap
  200 (revision 8), sleep threshold
  5 min, dwell 10 min, request expiry 2 min, request limits (one pending, 5 min after a
  denial / expiry, 3 per hour). No run-time change of them in v1 (stop is the only narrowing route).
* **(6) `/new`.** Ends the window / keeps it. **Recommended: ends** (requirement 5).
* **(7) HTTP-tool destination refusal.** **Decided 2026-10-18 17:58: the general guard
  (private / link-local destinations in `web_fetch` / `web_search` etc.) is not built
  (status quo).** The own-dashboard refusal of §8b stays a prerequisite of the button.
* **(8) Full rollback of marker + chain.** Accept (as ADR 0010 #273 without anchor) / mirror
  the marker in `STIRA_TOOL_RULES_ANCHOR_DIR` when set. **Recommended: mirror, opt-in.**
* **(9) Shorter windows.** No / `STIRA_YOLO_HOURS` 1–6. **Recommended: no** in v1.
* **(10) Destination record** — one decision with ADR 0013 Open (10), same options: (a)
  full host after masking; (b) keyed hash only; (c) registrable domain after masking; (d) (c)
  + keyed hash of the full host; (e) clear text only for destinations on a published
  allow-list set by a person, keyed hash for everything else. (a), (c) and (d) **store input
  text in clear** — the registrable label or an IP literal can carry an unknown secret or an
  identity — so they keep "no secret in the row" only for domains known in advance; only (b)
  and (e) keep it unconditionally. **Recommended (as ADR 0013):** (e) with (b) for unlisted
  hosts if readability matters, otherwise (b). **Decided 2026-10-19 12:28: (b)** (as
  implemented in phase 6; ADR 0013 Open (10) with it).
* **(11) How a window starts** (§1): (A) both / (B) button primary, allowance only / (C)
  parameter only; (D) rejected. **Recommended: (B).** (Revision 3's start-confirmation
  options apply only to (A) / (C).)
* **(12) Human authentication** (§8b): (i) rejected / (ii) host dialog with OS
  authentication / (iii) WebAuthn / (iv) host command / (v) button requests, (ii) or (iv)
  confirms. Revision 5 recommended (v). **Decided 2026-10-18 23:16:** the browser channel
  (token + typed word, premises 1–3 of §8b — a checkbox since 2026-10-19 00:18) is the
  default; (v) stays as the opt-in host channel (`STIRA_YOLO_HOST_CONFIRM`); (iii) later.
* **(13) Tier external on the browser channel** (revision 6). The external tier lets
  content the agent reads lead it to send data out without a card (§10), so a confirmation
  there carries more weight.

  | Option | Security | Convenience | Note |
  |---|---|---|---|
  | (a) **browser, plus a second confirmation** naming the possibility of sending out (revision 6: the typed word `EXTERNAL`; revision 7: a second checkbox) | as internal against a same-user program (§8a residual); the extra confirmation makes an inattentive confirm of the wider tier less likely | one more tick at the press | no new channel |
  | (b) external requires the host channel (refused on the browser channel; `STIRA_YOLO_ALLOW=external` needs `STIRA_YOLO_HOST_CONFIRM`) | holds against a same-user program only with the dialog's OS authentication; the command does not | a terminal or desktop dialog at each external press; headless needs the command | two channels to explain per tier |
  | (c) browser, same as internal | as (a) | lightest | the tier difference is only in the text |

  **Recommended: (a)** — the yardstick puts a same-user program out of scope, so (b) adds a
  heavy step mainly against the case it does not cover except with the dialog; (a) keeps the
  default light and targets the realistic risk (an inattentive confirm). **Provisionally
  implemented as (a)** in phases 2b / 2c — the one exception to "no implementation beyond
  phase 2 before the Open decisions" (Status) — until the operator decides; (b) is a refusal
  in `confirm_in_browser` if chosen, (c) drops the second box. **Decided 2026-10-19 12:28:
  (a)** (as implemented).
* **(14) Platforms of v1** (revision 8). **Decided 2026-10-19 12:28: macOS only** — the
  only platform whose clocks were measured (§7); Linux (`CLOCK_BOOTTIME`) is a separate
  issue, measured before it is offered.
* **(15) What v1 promises** (revision 8). **Decided 2026-10-19 12:28:** "no six-hour wait
  on approval cards" while a person works with Stira; leaving it fully unattended (the goal
  loop, jobs) is #299 / phase 8.

## Operator decisions

* **2026-10-18 17:58** — a window is started by a button with internal / external chosen
  there (requirement 1 amended; six hours and the exemption unchanged); the general
  destination guard of Open (7) is not built; the own-dashboard loopback refusal stays a
  prerequisite. Everything else stays Proposed.
* **2026-10-18 23:16** — a host confirmation (Touch ID / password dialog, host command) is too
  heavy as the default: the default confirmation is the **browser** (token + typed word on
  the screen, §8b revision 6); the host confirmation (macOS dialog / `stira yolo confirm`,
  phase 2) stays as an **option**. Open (12) decided; Open (13) added with a recommendation.
* **2026-10-19 00:18** — the browser confirmation is a **checkbox** instead of a typed word
  (feel first); only loosening saves, `RECOVER`, the YOLO request and scheduler create /
  resume ask for a confirmation; an approval card's approve / deny stays **one press**, also
  with the allowance (revision 7, §8b table and cost).
* **2026-10-19 12:28** — **revision 8, Status Accepted.** Exec: **option C** — a person may
  choose "Include exec" at the press (only with `STIRA_ENABLE_EXEC_TOOLS` and the allowance
  `external`), confirmed with its own checkbox; the operator explicitly accepts that a
  command YOLO approves can bypass Stira's floors and audit (requirement 4 is not met for
  commands, §9). The other points as recommended: the breaker pauses and resumes after 10
  min of NORMAL (Open (1) P, as implemented); the destination only as a keyed hash (Open
  (10) (b)); tier external with a second checkbox (Open (13) (a)); v1 on macOS only (Linux:
  a separate issue); the v1 promise is no six-hour wait on approval cards (unattended
  operation: #299 / phase 8).
* **2026-10-19 12:40** — the YOLO values may also be changed in a config file and on the
  settings page, not only with environment variables (phase 7c; environment wins; the
  allowance after a restart, the caps from the next window; the reserved-key rule and every
  other defence unchanged).
* **2026-10-18 23:19 / 23:23 (yardstick)** — do not build beyond what a practical harness
  does; weigh security and convenience cost explicitly and ask instead of silently choosing
  the heavier side; heavy mechanisms only after comparing a lighter one; light confirmations
  by default, stricter ones optional; this is an alpha OSS: feel and UI/UX first.

## Alternatives considered

* **#365 session grant** — replaced; a possible later phase on the same record.
* **"Always for 6 h" as a rule value** — rejected: writes `tool-config.json`, changes the
  rule generation, survives restarts.
* **A switch that bypasses the HITL gate** (like `BYPASS_TOOL_CONSENT`) — rejected: it would
  skip the breaker and floor checks before the request.
* **Exempting `CRITICAL_HALT`** — rejected (requirement 5).

## Addendum (#508): the agent is told whether a window is open — **operator confirmed (2026-10-21)**

**Change to a Non-goal.** The Non-goals said "any … prompt text … of its own (tool list
and static prompt unchanged, #254)", and §8 pinned "prompt hashes equal with and without
the allowance". #508 (requirement 2, acceptance 3) asks for the opposite on purpose: the
agent credited a window with calls that ran under Always rules (`44815fb0`: 25 Always,
1 manual approval, 0 automatic), because it could not see which permission applied.

**As implemented (#508).** Only with the allowance, the Tool Availability section
(ADR 0010 Addendum #508) ends with two lines: `YOLO: no window is open.` or
`YOLO: a window is open (<tier>[, exec included]) until <fixed end>; it pauses while the
breaker is not NORMAL and applies only to turns a person's message started.`, and
"YOLO only approves Ask calls automatically; Always calls never needed an approval, so
they are not YOLO's doing." Read through `runtime.window_facts()` (id, tier, exec flag,
`ends_at` only — no count, no pause state, no time left), so the static prompt changes at
a window's start and end only (one cache write each, #254). **Unchanged:** without the
allowance the prompt has no YOLO text at all (`tests/test_yolo_default_unchanged.py` now
pins "off = on minus these two lines"); the tool specs and tool names never depend on the
window (`test_yolo_exec_app.py`, `test_yolo_window_routes.py`); the text gives the agent
no way to open, extend or widen a window (§8's other rows unchanged).

**Decision (operator, 2026-10-21).** Keep the two lines. The Non-goal above is narrowed
by this addendum: with the allowance, the prompt states whether a window is open; without
it, the prompt has no YOLO text.

## Addendum (2026-10-24): the button and the scopes are shown by default — **operator decision (2026-10-24)**

**Change.** The public release follows "show it and let people choose": with neither
`STIRA_YOLO_ALLOW` nor `gateway-config.json` `yolo.allow` set, the allowance is
**`external`** (`stiracore.yolo.allowance.DEFAULT_ALLOW`). The chat header and the Stage
dialogue box show the `Start YOLO` button, the start dialog offers both scopes ("internal
tools only" and "external sends allowed too"), and `GET /api/yolo/status` reports
`sources.allow: "default"`. Reason: the dashboard has many settings, and a feature that
has to be found and switched on first is easily missed; a button that does nothing until it
is pressed shows what exists and leaves the choice to the person.

**Unchanged — no permission is added by the default.** Nothing is approved until a person
presses the button and confirms the request (§8b: the browser checkbox by default,
`STIRA_YOLO_HOST_CONFIRM` for the host). The window still ends six hours after the press;
the caps (`max_auto_approvals` 2000, `max_external` 200, `max_exec` 200), the breaker's
pause, Never, the protected locations (floors), MCP, webhook / bot / job turns and the tool
rules' defaults (Always / Ask / Never) are as before. "Include exec" is still offered only
with `external` **and** `STIRA_ENABLE_EXEC_TOOLS` (not from the page). With the default
allowance the confirmed operations of §8b apply as with the variable: a loosening save,
`RECOVER` and scheduler create / resume need the person's confirmation.

**Turning it off.** `STIRA_YOLO_ALLOW=off` (now an accepted value) or `"yolo": {"allow":
"off"}` (the settings page's "Allowed scope: Off") — precedence as before: environment >
config > default. A wildcard listener (`STIRA_HOST=0.0.0.0` / `::`) does not refuse
start-up because of the default: the default is `off` there (one start-up line says so); an
explicit `internal` / `external` on a wildcard listener still refuses (exit 2). The default
allowance prints one short start-up line instead of the operator's notice. Measured: a new
data directory without config shows the button on `/chat` and `/stage/`;
`STIRA_YOLO_ALLOW=off` removes it.

**Also shown by default: static SVG on the Stage board (#453).** For the same reason
`ui.board_svg` defaults to **true**: a reply's ```` ```svg ```` block is drawn as an image
rebuilt from the allow-list of `dashboard/stage/js/svg_safe.js` (no script, link, outside
resource or animation — unchanged). The file keeps `"board_svg": false` only when a person
turned it off; turning it off needs no confirmation, turning it back on is confirmed as
before (the settings page's one-checkbox dialog, recorded in the settings history).

**Exec is not part of the default.** The default `external` does not offer "Include exec"
even with `STIRA_ENABLE_EXEC_TOOLS` (`YoloSettings.exec_offered` reads the allowance's
source): exec in a window still needs an operator's own `external` (variable or config), as
before this addendum. Measured by `tests/test_yolo_exec.py` and
`tests/test_yolo_showcase_default.py`.

**Contributing rule.** `CONTRIBUTING.md` now reads: behaviour that spends tokens or widens
permissions stays off by default unless an ADR decides otherwise with its cost (as ADR 0026
does for the Memory Map and the handoff note); a feature that only shows something and
approves or runs nothing until a person presses or picks it may be shown by default (this
addendum).
