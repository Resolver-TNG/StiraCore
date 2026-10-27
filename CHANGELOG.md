# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/) once it leaves `0.1.0.dev0`.

## [Unreleased]

First public snapshot. Nothing below is a stable API yet. Features marked *opt-in* are off
by default.

### Autonomy — ALPHA (ADR 0011 stage C, #296)

Experimental and **off by default**; it may change without notice. A person issues a
time-, budget- and scope-limited **goal grant** (host-confirmed) for one goal; each
iteration runs in a dedicated session (`grant:<id>`) when the person presses "Run one
iteration" or — opt-in twice (`autonomy.heartbeat_start` and the grant's own flag) — when the
scheduler's model-free check starts one; a permit is taken at every effect; any narrowing
(kill switch, revoke, breaker, `/new`, TTL, restart) or a person's turn stops or pre-empts
it; no approval is answered automatically; only the person confirms completion. Built in
waves W1–W6d (the entries below: authority service, latches / lifecycle events / watchdog,
grants and the control plane, the iteration executor, the Stage board, progress by outcome,
pre-emption by every turn, claim evidence, heartbeat start), then one supervised real-model
smoke (W6e) and its fixes (W6-F). Not yet: YOLO two-binding (W7), taint for iterations (W8),
a day budget pool.
Overview, how to try it and the known limits: docs/autonomy.md.

### Changed

- **Name** — the project is now **StiraCore** (Python package `stiracore`, distribution
  `stiracore`); the CLI stays `stira` and the `STIRA_*` variables are unchanged. It is built
  on the Strands Agents SDK. The chat server is
  `dashboard/stira_server.py` (`scripts/stira_ctl.sh` / `.ps1`), its default data directory
  `.local/stira/` with agent id `stira`, and the sample persona is Resolver
  (`examples/personas/resolver/`). No migration code: with the server stopped, move an
  existing data directory from its previous default name under `.local/` to `.local/stira/`
  (`mv`), and its `memory/agents/<previous id>/` to `memory/agents/stira/` (docs/release.md).

### Added

- **Core engine** — six-dimension Neural State with lazy decay, behavioural-consistency
  score (bcScore), circuit breaker (NORMAL / WARNING / CRITICAL / COHERENCE) with
  transition reasons, SOUL read-only structure, checkpoint persistence.
- **Memory** — episodic / semantic / procedural layers, Amygdala Pass and Harvest
  distillation (rule-only or provider-neutral LLM), staging review, procedure proposals,
  *opt-in* associative recall and emotion-proximity signal, importance tags with reversible
  retention and dry-run forgetting, delete requests that reach every copy.
- **Model providers** — provider abstraction (`ModelSpec`): Bedrock, Ollama, llama.cpp,
  OpenAI, Anthropic and others; credentials by environment-variable name only, capability
  table, usage normalisation, output-limit defaults with a thinking reserve.
- **Safety layers** — identity guard, protected directories, write/read floors, Host /
  Origin / CSRF checks, CSP, idempotency keys and secret masking on every POST, trust labels
  and taint observation (ADR 0022), containment signals (ADR 0023), outbound SSRF checks.
- **Approvals (HITL)** — approvals bound to agent / tool / argument hash / expiry, durable
  hash-chained ledger, restart restore and asynchronous resume (fail-closed), breaker hold,
  in-chat approval cards and queue, Always / Ask / Never tool rules.
- **YOLO window** — *opt-in*, time-boxed auto-approval of workspace tools with browser
  confirmation, caps, an external tier, exec option, per-window statistics and audit.
- **Progress reports** — interim reports and declared checkpoints during long turns;
  10-minute auto-continue inside a YOLO window.
- **Artifacts** — files written by the agent registered as versioned artifacts; artifacts
  page with a sandboxed HTML preview.
- **Verification tools** — `page_check` (browser checks of a localhost page: actions,
  waits, assertions, screenshots) and `run_checks` (fixed check kinds on a throwaway copy).
- **Delegation** — `delegate`, a child agent under the same gate.
- **Scheduler** — *opt-in* cron-style jobs with budgets, notifications and crash-safe
  recovery; gated agent-layer executor; heartbeat daemon.
- **Operator guides** (docs only, #296) — `docs/guide-scheduler-heartbeat.md` and
  `docs/guide-memory-routines.md` (EN + JA): which background part to switch on and what it
  costs, a first model-free job, how to check and stop it, troubleshooting by code, and the
  recommended memory-organisation profiles (rule distiller, a person reviews staging).
- **Authority service** — one process-wide authority boundary (ADR 0011 §5, #296 W1):
  persisted `authority-epoch` (seq + previous hash, fail-closed when unreadable), lock-free
  kill flag, in-flight cancel set; YOLO uses it by injection. Starts no work.
- **Authority latches, lifecycle events, watchdog** (ADR 0011 §5 / §12, #296 W2): a
  persisted latch store (`kill`, `unhealthy`, `halted:*`; unreadable, missing or rolled back
  = all set, checked against a commitment in the epoch store) cleared only by
  `stira authority unlatch` with the server stopped (`reset-latches` repairs); one codes-only `lifecycle_event`
  log with an unread / ack outbox; a model-free watchdog (heartbeat staleness, gateway
  gaps, outbox repair); `GET /api/authority/status`, `POST /api/authority/kill`, the outbox
  routes; bounded cancel callbacks. Starts no work.
- **Fixes from the real-model smoke** (#296 W6-F, #640–#645; part of the *opt-in* goal
  iteration executor): a stop during a model call (revoke, pause, `/new`, the kill, the TTL)
  lets the call finish — up to 5 s after the 0.5 s grace, not for shutdown or the wall time —
  and charges its reported usage instead of the whole reservation (the smoke measured about
  71–74k tokens charged per such stop for about 2.7k used); only a call given up (or with unknown usage) keeps the
  whole reservation, and its provider request is then told to stop; `run_checks` discovery
  reads a workspace venv's own `site-packages` (and the base site with
  `include-system-site-packages`) also when its `python` is a symlink to the server's base
  interpreter (no process is started); the tools view shows `run_checks` as `os_sandbox` on
  a verified host; a stopped iteration that consumed an effect counts toward
  `max_iterations` whatever stopped it (also when the stop ended the grant); the heartbeat
  start keeps `tick_min_spacing_sec` from a resume too; docs/autonomy.md's recipe gains the
  tool rules for unattended runs, the pytest interpreter, the card timeout and the smoke's
  numbers (one run, an estimate).
- **Hardening before unattended iterations** (#296 W6-H, follow-ups #631 #632; part of the
  *opt-in* goal iteration executor): an iteration's validation and count are one step — the
  grant's `generation` read at the validation is compared inside the service's locked
  count (`commit_iteration`), so a person's block, pause or stop landing in between is an
  interruption (no history, no streak) and a detector's block lands only on the version the
  count left; a file over 64 MiB is identified by its size and a 3 MiB sample of its bytes
  (first / middle / last MiB), never its mtime or inode (a `touch` or a same-bytes copy is no
  progress; a change outside the sample of the same size is not seen — documented); a file
  written by an unknown or interrupted iteration enters the tracked set from its
  before-write bytes (a same-bytes rewrite stays "nothing observed", a real change is seen
  at the next end); an unknown end keeps the stat cache entries it validated. The turn-lock
  enumeration also pins every `TurnLock(...)` construction (one per listed site, any
  alias), the unbound `TurnLock.acquire(lock)`, and a runtime walk of the server's wiring
  requires every reachable turn lock to be the shared one; `/new`'s signal is pinned by a
  withdrawn card; a W6d placeholder fails for an unregistered heartbeat / scheduler start.
  Decision recorded: background jobs and unattended iterations stay independent (no lock;
  the iteration is confined to its subtree, the path floors apply).
- **Pre-emption by every turn** (ADR 0011 stage C, #296 W6b; only with
  `autonomy.stage_c_exec`): the shared turn lock is one `TurnLock`
  (`src/stiracore/core/turn_lock.py`) and the only way to take it; every acquirer — a
  person's chat message or progress reply, a webhook turn, the automatic continue, a
  restored card's resume, `/new`, a procedure approval — raises the pre-emption signal
  before it tries the lock, so a running goal iteration yields (`yielded(user_turn)`) and no
  press is admitted from the signal until the lock is released (W5 signalled the chat route
  only). A syntactic (AST) test checks every use of the lock and every named turn start
  against allow-lists (its limit is documented). Scheduler agent jobs and memory jobs run
  their own agents and do not pre-empt. With the switches off the lock is a drop-in for the
  plain lock (no signal); the chat blueprint's `user_turn` argument is gone (the lock
  carries the signal).
- **Goal grant on the Stage board** (ADR 0011 stage C, #296 W5b; *opt-in* with
  `autonomy.stage_c`): the whiteboard's activity mode shows the newest grant — headline,
  goal state, iteration n / max and run state, TTL and budget meters, the last end in words,
  the last five iterations, "waiting for your approval" / "awaiting your verification" cues
  — from a `grant` block of `GET /api/activity` (an allow-list: codes, counts, tool names;
  never the goal, a path, an argument or a card's content; memory-only reads). Display only
  (the controls stay in Settings). With `autonomy.stage_c_exec` the Stage watches for an
  iteration through the same poll and lowers the board when one starts, only onto a closed
  board (docs/stage.md decision table). Off: `/api/activity` and the page are as before.
  Board audit (docs/board-audit-2026-10.md): one row per id in the activity list, no
  visibility listener left by a stopped poller (`poll.js`), and a server restart re-reads
  the activity list from scratch. Review fixes: the board's read never waits on a lock
  (`grant_busy`), an iteration's row lists the tools that ran, a focused link keeps the
  focus across the 1 s refresh, the live region goes with the grant, only known codes pass
  (server and page), a pinned board is never lowered by an iteration, and the activity
  wiring (`activity.js` `startActivityController`) is tested in jsdom. The grant block is built from one
  consistent version (a busy read instead of a mix after an iteration ends).
- **Heartbeat start of goal iterations** (ADR 0011 §8 / §12, ADR 0021, #296 W6d; *opt-in*
  twice): gateway `autonomy.heartbeat_start` (default off; needs `stage_c_exec` and a running
  scheduler, `STIRA_SCHEDULER=1`) **and** a grant issued with its own `heartbeat_start: true`
  (host-confirmed at issue — the flag is part of the confirmed body and named in the
  operation's summary — and never changed afterwards). The scheduler's new fire kind
  `grant_tick` evaluates, on every scheduler wake (periodic, and event-driven after each
  heartbeat tick), without any model call, whether the one live grant may start an
  iteration, and presses through the same executor `step` as a person (same permits,
  budget, tools, pre-emption and commit rules; its clauses re-checked on `step`'s own look).
  At most one iteration per wake; a skip is recorded `tick_skipped(<reason>)` with counts,
  never deferred, never caught up after a sleep or restart (the first tick after any start
  does nothing: every grant is `revoked(restart)`). Clauses beyond the press: a healthy
  heartbeat, not `awaiting_verification`, `autonomy.tick_min_spacing_sec` since the grant's
  issue and the last end (default 300 s, floor 30 s), a flood guard on event-driven wakes (5 per 60 s), and the
  no-action gate (two `iteration_no_action` ends in a row stop the ticks until a person
  presses or resumes). An unattended iteration never waits for an approval
  (`blocked(approval_timeout)` at once). Per heartbeat-started iteration the tokens in / out,
  the cache counters when reported and the wall time are kept (numbers only) and shown on the
  grant card and the Stage board with the skip counts. State-class skips post one lifecycle
  event (`grant_tick`, `tick_skipped_<reason>`; the same reason again only after a start or
  another reason, at most 32 per grant). A server with the switch on but no scheduler or no
  heartbeat logs one structured warning (`heartbeat_start unavailable (reason=…)`) and a
  flagged grant shows `unavailable: scheduler_off` / `heartbeat_off`. The wake's stamp (authority
  epoch, person's-turn counter, grant stop epoch, scheduler run token) is checked inside the
  publication's authority fence, read whole before the first permit, consume, provider request
  and terminal request (memory marks at every boundary; bound to the executor; single use):
  any change readable before a request refuses it. Accepted residual: a heartbeat loss after
  the last read before a request (a sampled observation; a start condition only). Three consecutive failed ends stop the ticks
  (`repeated_failure`) until a person presses or resumes. Off: no ticker, no
  thread, the answers as before. Not done: budget pools beyond the grant's, the YOLO
  two-binding, a model-based judge, a real-model smoke (W6e). docs/grants.md "Heartbeat start".
- **Completion-claim evidence and a visible waiting state** (ADR 0011 §8 / §12, #296 W6c;
  part of the *opt-in* goal iteration executor): when the agent claims completion the harness
  stores one evidence record on the grant (in `grants.json`, replaced at the next claim, kept
  after a confirmation as the audit trail) — the iteration k / max, the grant's last
  `run_checks` outcome (`pass` / `fail` / `not_run` / `unknown`, never `pass` for what could
  not be read; `not_run` after a later edit) with its per-check rows (kind, status, counts),
  the workspace `changed` / `unchanged` / `unknown` since the previous claim (the W6a
  identity, no digest or path), the claiming iteration's progress code, tokens used / left,
  TTL left and the claim instant. Codes measured by the harness only — no model text, no
  goal. Fail closed: the checks verdict combines every call since the last write (any fail →
  fail, anything unreadable or inconsistent → unknown), and `pass` / `fail` are shown only for
  the workspace the checks ran on; the measured codes are published only for the claiming
  iteration's committed count (a stop or a verification in between leaves them `unknown`). The settings panel and the Stage board show it with "since <time>", amber with a
  "⚠" line in words when the checks failed, and "still waiting for you" past gateway
  `autonomy.claim_stale_sec` (default 1800 s; display only, no timer, no action). `GET
  /api/grants` views carry `claim_evidence`; a `grants.json` entry from before has none and
  loads as `null` (a store written by this version does not load in an older one: the
  strict reader refuses the new key). The claim stays one lifecycle event and one stop notice,
  acknowledged by the person or by their verification of that claim; verify's authority is
  unchanged. docs/grants.md "Completion claims".
- **Progress by outcome** (ADR 0011 §8, #296 W6a; part of the *opt-in* goal iteration
  executor): an iteration is progress only when its keyed outcome fingerprint — a fresh read
  of every file the grant wrote (≤ 256; a missing one an explicit marker) and the
  iteration's normalized per-check `run_checks` rows as one set (durations, pids, temp
  paths, ids, output, order, repetitions and call grouping stripped; the agent's report is
  not part of it) — is new to the grant's last 16; a change by anyone counts; a file over
  64 MiB is identified by its size and a 3 MiB sample of its bytes (W6-H; never its mtime); at most 256 MiB are digested per iteration; anything
  unreadable is "unknown": no progress, not in the history (HMAC with a per-grant
  in-memory key; only HMAC bytes and codes are kept, never a path, content, command or
  output). Rewriting the same bytes, rerunning the same failing checks (any number of times,
  in any order) or only reading is no progress (W5 counted any write or check run); a
  file's bytes are read non-blocking, regular files only, a torn read is refused; `no_progress_ticks` in a row →
  `blocked(no_progress)`, a recurrence within 6 (A-A, A-B-A-B, A-B-C-A) →
  `blocked(loop_detected)`, the same agent blocker 3 times in a row (known, committed outcomes) → `blocked(same_blocker)`,
  two replies with no tool call and no report (`iteration_no_action`, a new end code) →
  `blocked(no_progress)`. A person's chat turn, shutdown, a stop or a stale permit never
  counts as no progress (only toward `max_iterations`; read again at the commit, the grant's own state included), and an
  iteration that consumed no effect is not counted at all. An unreadable outcome is never progress. No model-based loop judge; the user
  still verifies. A blocked report now ends as `reported_blocked` (W5 recorded `stopped`).
  docs/grants.md "Progress by outcome".
- **Goal iteration executor** (ADR 0011 stage C, #296 W5; *opt-in*, gateway
  `autonomy.stage_c_exec`, default off, needs `autonomy.stage_c`): a person's "Run one
  iteration" press (`POST /api/grants/<id>/step`) runs exactly one goal iteration in the
  session `grant:<id>` — **manual trigger only** (no heartbeat, no automatic next
  iteration, no loop detection; the user verifies every claim). Every model call, tool
  call, approval wait and report takes a W1 permit consumed right before the I/O (a stop in
  between → `permit_stale`, no effect); the final input gets a local upper bound (one token
  per UTF-8 byte; no provider request to count) and `bound + max_tokens` is reserved
  against the grant / per-tick budget before each call (no bound → no call); tools = scope ∩ {file_read, editor, run_checks, memory reads,
  current_time} ∩ the live rule plus `goal_report` (fixed codes; a claim only reaches
  `awaiting_verification`); an Ask is a person's card (unanswered →
  `blocked(approval_timeout)`); the editor stays in the subtree and `run_checks` needs a
  verified `os_sandbox` at each call (`blocked(run_checks_unavailable)`); a person's chat
  message pre-empts at the next boundary; `autonomy.iteration_wall_sec` (600) →
  `blocked(iteration_timeout)`; a subtree replaced after issue → `blocked(workspace_changed)`;
  a provider call still blocked after a stop is abandoned (no effect, charged, no new
  iteration while it lives). The goal is only in the model input (a trusted playbook block,
  `dev_playbook.md`, then the goal as the trailing block) and the grant store; a grant
  card's durable record keeps codes only.
- **Goal grants** (ADR 0011 stage C, #296 W4; *opt-in*, gateway `autonomy.stage_c`, default
  off): the grant + goal record (strict schema, `kind="goal"`, TTL ≤ 6 h, ≤ 20 iterations,
  verifier = the user, `run_checks` only on a verified `os_sandbox`), the goal × grant state
  machine as pure functions, the `grants.json` store (`0600`, chained, the goal text only
  there, never persisted as active: every start revokes), the human control plane (issue with
  typed confirmation + host-confirmed operation, revoke / pause / resume / verify), automatic
  stops (kill, unhealthy, breaker, clock anomaly, TTL, `/new`) with an epoch advance and
  codes-only lifecycle events, and a settings-page panel. **No executor: nothing runs.**
- **Observability** — versioned turn events, hash-chained event store, turn inspector,
  tool cards, model call progress, token cost estimates, diagnostics page and
  `stira doctor`.
- **Web UI** — chat, settings hub (presets, change history, undo), memory and observer
  pages, Japanese / English, accessibility checks.
- **Stage** — *opt-in* (`STIRA_STAGE=1`) pixel-art visual-novel view of the agent's state,
  approvals and activity board.
- **Context and cost** — *opt-in* prompt cache, tool-result offload and summarisation,
  max output tokens.
- **Integrations** — Slack / Discord bot scaffolding, webhook, Perplexity search add-on,
  LSP diagnostics.
- **Classifiers** — rule-based, Sokudan-compatible, and an *opt-in* Japanese hybrid classifier.
- **Tests** — offline test suite and an attack regression set (`tests/attack/`).
- **Release** — `LICENSE` (Apache-2.0), `CHANGELOG.md`, release snapshot and link-check
  scripts, AI-agent setup guide.

### Fixed

- **Memory store** — a symbolic link *above* the configured memory root (macOS `/tmp` and
  `/var`, a symlinked home or project folder, `STIRA_CHAT_DATA_DIR` or a `--base-dir` under
  them) no longer refuses the store with `MemorySymlinkError`: the ancestors are the
  operator's choice and are resolved once when the store is built. A link at the root
  itself, at `agents/<id>/memory`, at a layer or `emotion/` directory, at `index.sqlite3`
  or at an entry file is still refused, also when swapped in later; the per-read check now
  starts at the root, which halves the `lstat` calls of a read (#296).
- **Memory store (cost of a read)** — the memory root is resolved once when the store is
  built and the entry enumeration no longer walks every entry's components again: a read
  no longer calls `Path.resolve()` at all (the link walk from the resolved root already
  proves the entry is inside it). Measured on the proximity-recall evaluation: 1,171,359
  `lstat` and 117,124 `resolve` calls become 10,097 and 986 (the whole test 27.6 s → 24.7 s
  on macOS, where both are cheap; Windows pays far more per call). The no-follow open, the
  identity compare and the component walk before and after the open are unchanged, and a
  link swapped in just before the open — on the entry or on a parent — is still refused
  (two new tests) (#662).
- **CI (Windows)** — the full test suite runs to the end on Windows now, but several hundred
  tests still fail there (path semantics, the scheduler, grants progress, timing). The
  Windows "Run tests" step is informational (`continue-on-error`) until that has its own
  round (#662); Ubuntu (3.11, 3.12) and macOS stay blocking, and the smoke and PowerShell
  control-script tests run on Windows as before. Windows is not a verified platform.
- **PDF extraction** — a memory limit of zero or less is no longer taken as an enforced
  limit: Linux accepts a negative `RLIMIT_AS` as "unlimited", so the child parsed where it
  should have refused with `pdf_limits_unavailable` (#662).
- **Dependencies** — `strands-agents-tools` is held below 0.8.10 (`>=0.8.0,<0.8.10`): the
  exec bridge refuses a release it was not audited against (0.8.9), so a fresh install
  got 0.8.10 and no exec-class tools. Deliberate until a newer release is audited (#296).
- **Authority and grant stores** — a delete + recreate of the epoch store, latch store,
  lifecycle log, outbox, watchdog record or grant store is now detected on Linux too: the
  file identity is device, inode, size and `st_ctime_ns` (recorded by the writer after its
  own write and rename), not device and inode alone, which ext4 and tmpfs reuse at once
  (#661). An outside append to the lifecycle log is now caught at the next append
  (`lifecycle_log_replaced`), not only at the watchdog's next pass.

- **Schedules** — Resume on `/schedules` with the default browser confirmation channel opens the
  confirmation (checkbox) dialog after the resume dialog instead of answering "Cancelled; nothing
  was applied": the shared dialog lets the pending request's own host confirmation open
  (`followUp`), and still keeps a second dialog / POST of the page out until it settles (#650).
- **Goal grants** — concurrent looks no longer revoke a live grant as `clock_anomaly`: the look reads its clocks under the grant service's leaf lock (#627).
