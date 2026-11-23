# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/) once it leaves the `0.1.0a` series.

## [Unreleased]

### Added

- **Taint ledger: a signal for text that addresses the agent** (ADR 0022 phase 0, observe only): the `external` tool results of a turn are read for headings aimed at an AI, "ignore your instructions", system-style tags, "no confirmation", requests to send secrets out, and the same hidden in comments or attributes or disguised by spacing, full-width forms or base64 (`middleware/agent_address.py`, Japanese and English, no model). Each `taint.observed` row gets `agent_address` = `{scanned, flagged, codes}` (fixed codes and counts, no text); the weekly CLI sums them. It holds nothing and changes no card or approval. Measured on a hand-written corpus (tuned on) 58/60 caught, 0/54 false alarms; on 5,373 machine-checked samples (not tuned on) about 51% caught, 6.5% false alarms; the signal can be switched off on purpose (stated in the docs). Docs: `docs/taint.md`.
- **CI hardening**: `ci/linux/gate.sh` + `gate_verdict.py` give the Linux gate a machine verdict (PASS only when the run has ended, every Python version's block is green, the coverage guard agrees and the tested commit is the expected one); a `pre-push` hook (`scripts/install_hooks.sh`, `.githooks/`) runs ruff, ruff format and the Stage node tests that match the changed files; `ci.yml` runs every `tests/*_test.cjs` by glob (33 files had never run in CI; one stale assertion in `stage_board_grant_test.cjs` is fixed) and `tests/test_ci_registry.py` keeps it so.
- **Stage: the binder is a sprite stack** (#341): 1–3 binders standing in a row, one per memory / file read running at once (the value of `gadget.folder` is now that count, capped at 3; it was 0/1), swung out of the shelf as `folder.out` slides (`tools/stack_folder.py`, 12 baked frames). Same cause as before, no new event or setting.
- **Stage: the warning beacon is a sprite stack** (#341): a base, a wire-guarded dome and a reflector paddle drawn as layers and baked at 16 angles (`tools/sprite_stack.py`, `tools/stack_beacon.py`). The paddle angle is the existing `beacon.spin` phase, so the turning beams at CRITICAL show the paddle turning inside the dome; WARNING keeps its slow pulse with the paddle at rest. No new event, no setting; the flat sprite is the fallback if the frames are missing.
- **A chat turn nobody watches stops by itself** (#680, ADR 0034, decision 2026-11-16): when the YOLO window the turn ran under has ended, the turn stops at its **next approval wait** (a running tool call is not cancelled; the card is refused); a turn whose approval card expires unanswered (`hitl_timeout_sec`) stops; a turn no page reads stops at fixed bounds (3600 s, 2,000,000 tokens). Fixed reason codes (`yolo_window_ended`, `approval_unanswered`, `unattended_time_limit`, `unattended_token_limit`), no settings. The Stage gets a **■ Stop the turn** button that calls the existing cancel route.
- **Memory page — key, importance, retention** (#652): `GET /api/memory/entries` (read-only, allow-listed rows: key, importance, retention; no body, no raw front matter; an author-written importance in the episodic layer reads as unverified) and a table under the forest page at `/memory` (the procedural review panel stays), linked with the retention screen of Settings.
- **`done_when` for a goal grant** (#683, decision 2026-11-16): an optional closed list of `run_checks` kinds (pytest / unittest / node_test / ruff / compileall) declared at issue and shown in the host confirmation; with `verifier: "checks"` the harness runs them itself when the model claims completion and refuses a claim that does not pass (`not_proven`, the kinds that failed; the iteration goes on), then the person decides as before. A grant without `done_when` is unchanged; the current rule of `run_checks` must be Always, else a claim is `not_proven` (no weaker authority). No form field yet (API only).
- **Repository inspection, read-only** (#671 stage 1): `stiracore.diagnostics.repo_inspect` reports a repository's behind/ahead, uncommitted changes, unpushed commits, stale worktrees and the default branch's lag. A fixed set of git commands (no shell, no network, hooks/fsmonitor/filters off), paths checked against the read roots, allow-listed output, credentials masked, a five-minute cache. No UI and no fetch yet (later stages).
- **Repeated-failure indicator, opt-in** (#151): `NXPAgent(repeated_failure=RepeatedFailureSettings(enabled=True))` counts consecutive tool errors; at the threshold a fixed calm notice is prepended at the next turn's start and `turn.summary` carries `repeated_failure` (`count` / `threshold` / `active` only). Off by default (no field, no prompt change). Eval: `forbidden_after_failure_rate` and `--repeated-failure on|off`. Not wired to `agent-config.json` or the UI yet.
- **Doom-loop detector, report only** (#684): `stiracore.progress.doom_loop` (pure `detect`, a failure classification table, `DoomLoopHook`) reuses the grants thresholds. By default it only reports to a sink and changes nothing; the stop switch is off. Not attached to the chat server yet.
- **Context management — server window and turn record** (#746): with context management on, a local OpenAI-compatible endpoint that reports `meta.n_ctx` on `GET <base_url>/models` gives the window (configured → model → capability table → server → 200,000); `turn.completed` carries a `context` block (mode, reason code, counts, `max_input_tokens`), absent while the feature is off. No prompt text or path.
- **Observability — ATIF export (opt-in CLI)** (#700): `python -m stiracore.control.atif_export_cli --data-dir DIR --out FILE [--turn-id ID] [--include-untrusted]` writes one turn's events as an ATIF-shaped file (allow-list, secrets masked, new file only, 0600). No setting, no network. The output shape is unverified against the ATIF spec; see `docs/atif-export.md`.
- **Memory — episode placeholder language** (#653): `episode_labels.placeholders` (`ja` default / `en`) sets the language of the fixed words in a new episode line. Stored episodes are not rewritten.
- **Round ledger** (ADR 0028 §5, #757): one allow-listed, hash-chained record per grant round in `grant-rounds.jsonl` (`0600`, bounded, one rotated segment); `GET /api/grants/<id>/rounds`, a read-only CLI view (`python -m stiracore.control.grant_rounds_cli`). Codes and counts only; a record outside the allow-list is refused, never truncated. A heartbeat-started grant is skipped (`tick_skipped(ledger_unhealthy)`) while the ledger cannot be written; the ledger never latches the authority service.
- **Report files** (ADR 0028 §4, #758): a grant may list up to 8 files the progress fingerprint ignores, chosen at issue and host-confirmed. A round that only rewrites its notes is `none`, so `no_progress` fires. It can only tighten.
- **`page_check` `explore` and `design_lint`** (#747): a seeded, bounded walk over the visible clickables and model-free layout measurements; counts, pattern ids and booleans only.
- **ADR 0015 addendum rev 6** (#763): split page tools (`page_check` Ask-only, `page_look` observation-only, `page_review` for the reviewer), a repeatable *axis check* (A1–A10) with records that never stop, and a slice plan; operator direction 2026-11-14.
- **Axis check, observation only** (#765, #771): the start-time guard measurement **G** (a fixed canary page; nothing may leave the browser), `axis.check`, a read-only row in diagnostics. In memory only, fail closed; nothing consumes it for `page_check`.
- **Round ledger `axis` key** (#767, #771): written `not_run` until S5 consumes it; `TOOL_KEYS` gain `page_look` / `page_review` / `view_image`.
- **ES modules for workspace file targets** (#766, #771): `page_check` answers in-workspace sub-resource requests of a `file://` page itself (closed type table, read-floor checked after the open, at most 64 MiB per call); a failed script load counts as a page error.
- **`page_look`** (#772, #776): a file-target-only page tool; **conditional Always** (#773): a saved Always is effective only while the axis check passes, in a person's chat turn or a YOLO window, with `file_read` Always, within a per-turn / per-window bound of 12 calls and 3 failures; otherwise it is a card. A per-install target key (`page-look-target.key`, `0600`).
- **`visual_review` task and `page_review`** (#774, #779): `visual_review` unset = off (no tool, no prompt change); the tool sends only Stira's own recorded screenshots (SHA-256 + size) to the configured reviewer, always behind a card that Stira writes.
- **Axis check hardening before S5** (#778): an unfinished guard measurement reads `guard_pending` (never effective, re-evaluated at once) instead of the final `browser_guard_unverified`; a transient measurement failure (`guard_run_failed`) is re-measured with a bounded backoff (5/30/120 s, 3 times) while every confirmed failure — including a probe that died after the page reached the canary — stays final; A7 measures the resolved workspace/floor/protected roots with `st_dev`/`st_ino`; a screenshot run pruned from disk reads `shot_missing` (not `shot_changed`); page-look cap codes are written to the ledger through an explicit map.
- **`page_look` and the narrowed `view_image` in a grant scope** (ADR 0015 rev 6 S5, #784): opt-in with the new `autonomy.page_look_unattended` (default off; `gateway-config.json`, read at start — no settings route writes it). A grant that names `page_look` opens its own workspace page files inside its subtree **without an approval card, also when the tool's saved rule is Ask** (never when it is Never) — but **only while `file_read`'s live rule is Always** (otherwise the round is refused `axis_file_read_not_always` and the goal is blocked, because a page could otherwise read through `fetch` what `file_read` would ask a card for); `view_image` (only together with `page_look`) shows only that grant's own recorded screenshots; `page_check` and `page_review` stay not nameable. The axis check runs at issue (`grant_page_look_setting_off` / `grant_page_look_unavailable` / `grant_page_look_axis`), in full at every round boundary (a failing one refuses the round `axis_<code>`; a sticky code blocks the goal `blocked(axis_failed)`; a non-sticky one is a heartbeat skip `axis_guard_pending` / `axis_breaker_not_normal` / `axis_rules_changed` / `axis_stale`) and lightly at every call, before the permit and after the consume (end `page_tools_unavailable`). Per round at most 12 calls and 3 completed failures; calls refused only because running ones fill the slots read `page_look_busy`. The grant dialog offers the two tools only while the setting is on.
- **Self-development scope for a grant, opt-in** (ADR 0029 B6b, #791): new `autonomy.grant_self_development` (default off; `gateway-config.json`, read at start). With it on, the issue form shows a separate, unticked box ("only `docs/` of a checkout is writable; the code, the dashboard and the approval / evaluator / budget files stay read-only"); only a ticked box sends `self_development: true`, and the host-confirmed summary names it (`changes: loosen:self_development`). Without the setting the box is not shown and the confirmation is refused (`grant_self_development_off`).
- **Never-editable list for a grant's `editor`** (#791, `stiracore/self_protect.py`): inside a Stira checkout the approval, containment, egress / masking, floor, persona-core, evaluator (`tests/`, `ci/`, `scripts/`, `pyproject.toml`, …), audit-record and budget / limit / settings-default files, plus the whole `dashboard/` tree (its JS and templates run in the person's browser tab and can answer approval cards) stay unwritable; a self-development grant may write only `docs/` of a checkout (less `docs/eval`, `docs/adr` and the boundary document), and never a new test. The iteration stops `tool_refused` with the new detail code `protected_path`. Full list: docs/grants.md.
- **Docs / ADRs (Proposed):** ADR 0015 addendum rev 5 (unattended `page_check`, optional visual reviewer), ADR 0028 revision 2 (round-mode loop, ceilings up to 24 h, model approvers).

### Changed

- **The Stage page is on by default** (#350, #477; decision 2026-11-16): `stage_enabled` defaults to on and is written to `gateway-config.json` only when it is off. `/` still opens the chat (`ui.home` is unchanged). Turn it off in Settings → Screens or with `STIRA_STAGE=0` (the variable still wins). Existing configs without the key now get the Stage page and its menu item.
- **`vigilance` is no longer shown** (#137, decision 2026-11-16): it is off the gauges, the sparklines, the turn summary, the ribbon, the Discord embed and the docs' list of dimensions; it is not connected to the Breaker (ADR 0011 §8 now names `stability` and `bc_score`). The value is still computed, stored and decayed; whether it stays at all is #832.
- **The Breaker no longer removes write tools from the model's list** (#259, decision 2026-11-16): the tool list stays fixed across Breaker levels (the prompt cache survives a transition) and a write is refused at execution with the reason `breaker_write_refused`, on every path (standard, MCP, delegated). WARNING and a CRITICAL without halt add one fixed line to the volatile block. A tool whose name cannot be resolved (a `ToolProvider`) is still left out while writes are stopped (fail closed). Refusals only grow; floor, write roots, Never and HITL are untouched.
- **Output cap default (#745):** the automatic cap of a hosted model whose limit is not recorded is **16384** (was 8192); an OpenAI-compatible endpoint on a local address takes the local default (32000); `GET /api/gateway/status` reports `max_output_tokens: {value, source}`; a cut-off tool call is annotated for the model with what to do.
- **Context management (#746):** the offload stash no longer depends on `workspace_dir` (the server's own `<data dir>/context-stash/` is accepted without becoming a write root); a refusal carries a reason code in the log and in `context_management.reason` of the status.

- **`run_checks` never reaches a `delegate` child** (#768, #771): it is now in `NOT_FOR_CHILDREN`.
- **Host channel client** (#774): `ask` tolerates a send-side broken pipe and reads the reply (a refusal stays a refusal).

- **A grant on a Stira checkout is refused by default** (ADR 0029 Q3 / B6, #791): a `scope.workspace_subtree` that holds a Stira checkout, or is inside one anywhere but its data places (`.local/`, `workspace/`), is refused at issue with `grant_subtree_protected` — also when it cannot be judged; the running server's own checkout or package is always refused (`grant_subtree_running_checkout`), with or without the option.
- **Files that run later always need a person** (#791): a write by `editor`, `file_write` or `speak` (its output path) to `.git/**` (hooks, `config`, `info`, the worktree pointer), `.gitattributes`, `.gitmodules`, `.github/**`, `.gitlab-ci.yml`, `.vscode/**`, `.idea/**`, `.devcontainer/**`, `.husky/**`, `.pre-commit-config.yaml`, `.envrc`, `.kiro/**`, `.claude/**`, `.cursor/**` or `*.code-workspace` shows an approval card even when the rule is Always; a YOLO window and a session allowance do not answer it. In a grant the same paths are refused in **any** workspace (matched as path components, as spelled and as resolved).

### Upgrade notes

- **Rolling back across the grant changes:** a grant store written with `report_files` (or `heartbeat_start`) is refused by an older reader as `grant_store_malformed`; to roll back, stop the server and move `grants.json` away (every grant in it then ends).
- **Ledger rows with `axis`:** a build older than #771 refuses ledger rows that carry the `axis` key and reports the ledger unhealthy; to roll back, move **both** ledger files away as above. The record `v` stays 1.
- **Ledger `axis` is measured since S5 (#784):** a round of a grant that names a page tool writes the real results instead of `not_run`, and `stop` can be `axis_failed`. The record `v` is still 1: a build before #778 refuses a row whose `axis` holds `guard_pending`, and every build before S5 refuses a row whose `stop` is `axis_failed`, then reports the ledger unhealthy (fail closed). Before rolling back after such a grant ran, stop the server and move **both** ledger files away. Grants that name no page tool still write `not_run`. (Checked against a simulated older reader, not an older build.)
- **Grants on a Stira checkout (#791):** a grant whose subtree holds, or is inside, a Stira checkout (outside its `.local/` and `workspace/`) can no longer be issued (`grant_subtree_protected`) unless `autonomy.grant_self_development` is on and the separate box is ticked; the running server's own checkout is refused in every case, and a self-development grant writes only `docs/` of a checkout. Grants issued before the upgrade end at the restart, as every grant does.
- **Ledger detail `protected_path` (#791):** the record `v` stays 1; an older build refuses a row with this detail and reports the ledger unhealthy. Before rolling back, move both ledger files away.
- **Ledger recovery:** if the ledger is reported unhealthy, stop the server, move **both** `grant-rounds.jsonl` and `grant-rounds.1.jsonl` away, and restart.

## [0.1.0a1] - 2026-11-12

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

- **Settings — Screens section** (#477, #809): the Stage can be switched on from the settings hub (`gateway-config.json` `stage_enabled`, top-level, written only when on; `STIRA_STAGE=1/0` still overrides; takes effect after a restart) together with `ui.home` and the Stage fps (browser-local). Links in both directions between Settings → Goal grants, the approvals page and the Stage; a "where to find it" table in `docs/stage.md` and `docs/autonomy.md`.
- **Observability — failure digest** (#695): `python -m stiracore.control.failure_digest_cli
  --data-dir DIR [--days 7] [--format md|json]` summarises the last days of tool failures as
  counts and representative event ids — codes only, no model call, no bodies, previews or
  secrets (the output is an allow-list). It opens `turns.db` read-only and writes nothing
  (docs/failure-digest.md).
- **Observability — error classes on tool events** (#696): `tool.completed` / `tool_call.updated`
  carry `error_class` (`unknown_tool`, `invalid_input`, `refused`, `tool_failed`, `exception`)
  and, for a refusal, `deny_code` (`rule_never`, `declared`, `other`). Classified from the
  exception type and the SDK's selection only — never from message text; a value outside the
  fixed sets is dropped.
- **Observability — dropped events are counted** (#705): `TurnEventRecorder.drop_counts()` returns
  in-process counts by reason (`no_active_turn`, `invalid_event`, `record_failed`,
  `no_call_id`; and, since the follow-up, `sink_failed` and `payload_failed`). Logging and behaviour are unchanged.
- **Observability — failure digest `scope` column** (#730): every pattern carries a `scope`
  (`invariant` / `domain` / `unknown`) from fixed code tables (the assignment is a proposal, not yet
  validated against real runs); the Markdown shows it with one note line.
- **Memory — `episode_labels`** (#653): optional `agent-config.json` setting
  `episode_labels: {user, agent}` for the speaker labels written into episodic memory. Unset
  keeps the previous labels and leaves existing episodes alone; an invalid value is ignored
  with a warning that names only the key and code `E_EPISODE_LABEL_IGNORED`. Associative
  recall ignores both the previous and the configured labels.
- **Evaluation — A/B run guards** (#729): `stiracore.eval.ab_guards` — a run fingerprint, a
  check that the declared change is the only difference between two runs, a plan check
  (counts, duplicates) and a leak check for held-out material (fail-closed). Pure functions;
  no existing runner calls them yet (docs/eval/ab-run-guards.md).
- **Docs** — harness-change A/B pre-registration (held-out handling, adoption criteria; #698,
  docs/eval/harness-ab-prereg.md, no A/B has been run); the regression set
  (`tests/regression/`, docs/regression-set.md, #697); which parts a self-improvement proposer
  may never edit (*Proposed*, docs/proposer-edit-boundary.md, #731).

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

- **Stage board — the pin goes with its content** (#623): an explicit switch (the Activity button, "Open on the board") to other content clears the pin, so the next reply of the person's own turn is no longer held by a pin they set on something else.
- **Cost Tracker showed `$0.00` for every turn (#651).** The Observability page only read its
  own empty price table. Turn-end events now carry `estimated_cost_usd`, priced from the same
  `token_prices` as the chat page, from one shared `turn_cost()`; both pages agree. No price
  is exposed, only the number.
- **Secrets in turn-event data (#702).** Only `input_preview` / `output_preview` were
  re-masked; a secret placed in `name`, `exception_type`, `result_code`, a summary
  `category` or `emotion_transcription` reached SSE subscribers, `events()` / `tail()`,
  `/api/turns` and the `turns.db` payload. Every string in `data` is now masked when
  stored — except identifier fields (`tool_use_id`, `request_id`, `supersedes`, `report_id`,
  `call_id`) at the top level and only when the value is one plain token (anything else is
  masked: fail-closed).
- **Goal-grant end codes while a card waits (#704).** A wall-time stop with an approval card
  pending ends `approval_timeout` (was `timeout`); no approver connected ends
  `approval_timeout` at the first ask (was `no_action`, and the goal did not stop); a provider
  raising `CancelledError` ends `failed` / `child_error` (was an uncaught thread exit and
  `no_action`). Only the codes change; nothing refused or blocked is loosened.
- **Stage — mug arm and wrists (#739).** Three in-between sprites make the mug arm's
  lift / sip / put-down smooth; the sleeve cuffs are longer and the bare wrist shorter; the
  character keeps its eyes open through a sip, a pending approval and relief. Known: a dark
  dot beside the nose while sipping, and the closed eyes of three other expressions, remain.
- **Test stability** (no product change): a YOLO settings cache that leaked between tests
  (#714, #737), a race with the stop-button route (#715), a 0.3 s drain-window race on slow
  hosts (#724), and a linearity measurement of the Stage board that is now best-of-7 after a
  warm-up (#741). A duplicate-route test pins that no route is registered twice (#703).

- **Approve / edit of an approval card is no longer refused by a finished sibling of a parallel batch (#668).** The running-tool-call guard (ADR 0012 §8b) counted every call from `BeforeToolCall` to `AfterToolCall`; in a parallel batch a sibling that had already returned stayed counted until the batch ended, and the batch cannot end before the pending card is decided, so Approve answered 409 `tool_call_running` and only Deny worked. Calls of tools that cannot reach the admin API (`file_read`, `file_write`, `editor`, `journal`, `current_time`, `image_reader`) are no longer counted; every other name — `http_request`, `shell`, `batch`, MCP and any tool not listed — still is (fail closed).
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
  Windows pytest steps ("Run tests" and "Provider extras") are informational (`continue-on-error`) until that has its own
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
