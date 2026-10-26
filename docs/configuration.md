# Configuration reference

English | [日本語](configuration_ja.md)

Every setting the chat server (`dashboard/stira_server.py`) and the observation console
(`dashboard/app.py`) read: environment variables, `gateway-config.json`,
`agent-config.json`, `tool-config.json`, and the files in the data directory. Defaults are
the values in the code on `main`; `tests/test_docs_sync.py` checks the tables marked
*checked* below against the code. How to operate the server (recovery, backup, `stira
doctor`): [operations.md](operations.md).

**Everything that spends tokens or widens permissions is opt-in and off by default**:
heartbeat (#238), scheduler (#239), prompt cache (#254), context management (#258),
exec-class tools (#163/#167). Features that only show something and approve or run nothing
until a person presses or picks them are shown by default ("show it and let people choose",
[ADR 0012 addendum 2026-10-24](adr/0012-yolo-mode.md)): the YOLO button and the Stage
board's static SVG (`ui.board_svg`). No
environment variable approves, bypasses or auto-approves anything; tests pin that names
such as `STIRA_HITL_AUTO_APPROVE` or `BYPASS_TOOL_CONSENT` have no effect on Stira's gates.
(The YOLO allowance approves nothing by itself: it only shows the YOLO button of ADR 0012
(a person presses and confirms it to open a window). It is `external` unless
`STIRA_YOLO_ALLOW` or `gateway-config.json` `yolo.allow` says otherwise; `off` removes the
button. While it is on, loosening saves, `RECOVER` and scheduler create /
resume additionally need a person's confirmation — a checkbox in the page by default, on the
host with `STIRA_YOLO_HOST_CONFIRM`; approving a card stays one press.)

## Where configuration comes from

Precedence: **environment > `agent-config.json` > `gateway-config.json` > code defaults**
(`config/resolver.py`). The heartbeat, prompt-cache and context-management switches are
read once at server start; the model/provider selection and the tool rules apply from the
next turn after a save in the settings hub.

Locations (`stira_server`):

- Configuration files: `.local/stira/gateway-config.json`, `agent-config.json`,
  `tool-config.json` — **relative to the current working directory**.
- Data directory: `<repository>/.local/stira/` — **relative to the repository**, whatever
  the working directory.
- With `STIRA_DATA_DIR` set (an absolute path): the data directory **and** the three
  configuration files (with their `.head`/`.locked` and `settings-history.jsonl`) are in
  that directory, whatever the working directory. Unset or empty: the two lines above,
  unchanged. Nothing is moved or copied: an existing install keeps its files where they are. To
  move one, stop the server and move the whole directory, every file together (a
  `tool-config.json` without its `.head` locks the rules, [operations.md §8](operations.md)).

Start the server from the repository root (`scripts/stira_ctl.sh` / `.ps1` do) so that
both point at the same directory. Started elsewhere, the server reads configuration from
`./.local/stira/` and data from the repository; a missing `tool-config.json` then loads
the safe first-run rules (#272).
The server then prints `WARNING: the configuration files are not in the data directory`
at start-up (no path) and `/api/gateway/status` reports
`config_location.matches_data_dir: false` (`config_location.data_dir`: `"default"` or
`"STIRA_DATA_DIR"`, never a path). The default locations themselves are unchanged.

Which settings the settings page can change, when each saved value applies and which variable
wins: [settings-surface.md](settings-surface.md) (#468; generated from
`config/settings_surface.json`, every field and variable below is classified there). The
settings page shows the same facts (#469): a banner when saved values wait for a restart, and a
read-only "Where settings live" card with this location check and every variable that pins a
setting — by name, never its value.

## 1. Environment variables (server) — *checked*

Read by `src/` and `dashboard/` (tests not included). "Unset" is the behaviour when the
variable is absent. Boolean switches accept `1` / `true` / `yes` / `on` (case-insensitive);
for `STIRA_HEARTBEAT`, `STIRA_PROMPT_CACHE`, `STIRA_CONTEXT_MANAGEMENT`,
`STIRA_EPISODIC_EMOTION`, `STIRA_ASSOCIATIVE_RECALL`, `STIRA_RECALL_EMOTION_PROXIMITY` and
`STIRA_MEMORY_MAP` any
other non-empty value forces the feature off, and an empty value means "not set".

<!-- env-vars:begin -->
| Variable | Unset / default | Effect | Issue / ADR |
|---|---|---|---|
| `STIRA_PORT` | gateway `port` (5555) | Chat server port; also used by `stira_ctl`. Must be an integer. | — |
| `STIRA_HOST` | `127.0.0.1` | Bind address. A non-loopback address is refused at start-up unless `STIRA_ALLOW_REMOTE=1`. Whatever the address, the agent's tools never reach this dashboard: the server process refuses connections to its own port on loopback, unspecified and its own addresses (with a wildcard bind: every local address), and `http_request` refuses such a URL — after resolving the name, and again for each redirect — with `E_OWN_DASHBOARD` before anything is sent. Always on, with or without YOLO and whatever the tool rule; other ports (a local model server) are not touched. No setting. | #185, #495 |
| `STIRA_ALLOW_REMOTE` | refused | Exactly `1` allows a non-loopback bind (checked before anything starts; a WARNING is logged). The servers have **no authentication and no TLS**; put both in front first. Only `Host` names in the allow-list are served. | #185 |
| `STIRA_ALLOWED_HOSTS` | `localhost`, `127.0.0.1`, `[::1]` | Comma-separated extra `Host` header names accepted (e.g. behind a reverse proxy). | #185 |
| `STIRA_ALLOWED_ORIGINS` | none | Comma-separated full origins (`https://name[:port]`) trusted besides the request's own, e.g. the public HTTPS origin of a reverse proxy. Matched by scheme, hostname and effective port; malformed entries are ignored with a WARNING. | #185 |
| `STIRA_TOOL_RULES_ANCHOR_DIR` | off | Opt-in rollback anchor for the tool rules: a directory **outside** the data directory where the highest rule generation is kept. A restored (older) `tool-config.json` + head, a reset data directory, other rules under the same generation, rules written by another install (an install ID kept in the anchor and the head), a newer generation that is not this install's next non-loosening save, a missing/unsafe/unreadable anchor or a directory inside the data directory then load locked (every tool Never) until a `RECOVER` save. Created only by a person (`stira tool-rules init-anchor`) or a `RECOVER` save, never by a load. Set it in the service environment ([operations.md §8](operations.md#8-backup-and-restore)). | #273, [ADR 0010](adr/0010-tool-policy-gate-stack.md) |
| `STIRA_MODEL` | gateway `model_id` | Model ID. Wins over `agent-config.json` `model_override`. | #205 |
| `AWS_REGION` | gateway `region` (`ap-northeast-1`) | Bedrock region. | — |
| `STIRA_PROVIDER` | gateway `provider` (`bedrock`) | Model provider ([providers.md](providers.md)). Wins over `provider_override`. Empty: the code default `bedrock`, in `stira_server` and the settings API alike (#272). | #204, #205 |
| `STIRA_BASE_URL` | gateway `base_url` (none) | Provider endpoint. Empty clears it. | #205 |
| `STIRA_API_KEY_ENV` | gateway `api_key_env` (none) | The **name** of the variable that holds the API key (never the key). Empty clears it. | #205 |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `MISTRAL_API_KEY`, `WRITER_API_KEY`, `LLAMA_API_KEY` | — | The SDK's default key variables. When no `api_key_env` is configured, Stira only checks that the provider's one is set (start-up error otherwise); the value is never shown or stored. | #205 |
| `AWS_ACCESS_KEY_ID`, `AWS_PROFILE`, `AWS_WEB_IDENTITY_TOKEN_FILE`, `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, `AWS_CONTAINER_CREDENTIALS_FULL_URI` | — | Presence only, for the "credentials configured" hint of Bedrock/SageMaker. The AWS SDK resolves the real credential chain. | #205 |
| `AWS_SHARED_CREDENTIALS_FILE`, `AWS_CONFIG_FILE`, `HOME`, `USERPROFILE` | `~/.aws/credentials`, `~/.aws/config` | Where the same hint looks for AWS files. | #205 |
| `STIRA_HEARTBEAT` | gateway `hb_enabled` (off) | Model-free background tick in `stira_server`. | #238, [heartbeat-daemon.md](heartbeat-daemon.md) |
| `STIRA_SCHEDULER` | off (no config field) | Operator-defined scheduled jobs; on the chat server the Gateway actions `health_check` (model-free) and `memory_harvest` (#251: rule-only Harvest to staging; refused unless `distiller_mode` is `"rule"`). | #239, [ADR 0008](adr/0008-scheduler.md) |
| `STIRA_ACCESS_LOG_POLLING` | off (poll lines hidden) | Exactly `1` logs the access-log lines of successful page polls again (`GET` `200`/`304` of exactly the polling reads: `/api/turns?limit=1`, `/api/turns/<id>/events?after_seq=<n≥1>`, `/api/hitl/pending`, `/api/hitl/history?limit=50`, `/api/gateway/status`, `/api/diagnostics/write-failures`, `/api/scheduler`, `/api/state`, `/api/history`, `/api/state/history?limit=500`). By default those lines are left out so `stira.out.log` keeps the WARNING/ERROR lines readable; a failing poll (any other status) and every other request are always logged. For debugging the pages' polling. | #369 |
| `STIRA_STAGE` | off (no config field) | The Stage page `/stage/` on the chat server (pixel-art view of turns, tools, approvals, Neural State and the breaker, inside the dashboard shell). It writes only through the existing APIs: approvals (`/api/hitl/<id>/decision`) and chat messages (`POST /api/chat`, CSRF + `Idempotency-Key`, the chat page's own logic); no new route or permission. Off: no route, no nav item, and gateway `ui.home: "stage"` falls back to the chat. | #341, #350, [stage.md](stage.md) |
| `STIRA_PROMPT_CACHE` | gateway `prompt_cache` (off) | Prompt caching for eligible Bedrock Claude models. | #254, [prompt-cache.md](prompt-cache.md) |
| `STIRA_PROMPT_CACHE_TTL` | gateway `prompt_cache_ttl` (`5m`) | `5m` or `1h`. | #254 |
| `STIRA_CONTEXT_MANAGEMENT` | gateway `context_management` (off) | Offload large tool results + threshold summarization. | #258, [ADR 0005](adr/0005-context-management.md) |
| `STIRA_MEMORY_MAP` | gateway `memory_map` (on) | Memory Map section (layer totals + the most important entries) in the recalled part of the prompt; `0` turns it off. | [memory-index.md](memory-index.md) |
| `STIRA_CONTEXT_PROFILE` | gateway `context_profile` (`default`) | `default` / `small` / `tiny`: the small-window profile ([small-context.md](small-context.md)). An unknown name is ignored with a warning. Read at start-up. | #368 |
| `STIRA_HANDOFF_INJECT` | gateway `handoff_inject` (on) | Handoff notes: the `handoff_write` / `handoff_read` tools and the newest note in the recalled part (no note = no section). `0` turns both off, `1` on. Read at start-up. | [handoff.md](handoff.md) |
| `STIRA_TASK_MODEL_TOOL_SUMMARY` | gateway `task_models.tool_summary` (`inherit`) | The model of the `tool_summary` task (a long tool result summarized on request, #585): `inherit`, a model id of the chat's provider, or `provider:model`. Unknown model / missing SDK or credentials refuse the start; a failing routed model fails the task (no fallback). Read at start-up. | [task-models.md](task-models.md) |
| `STIRA_TASK_MODEL_CONTEXT_SUMMARY` | gateway `task_models.context_summary` (`inherit`) | The model of the conversation summary of context management (#258, opt-in), same values and rules. | [task-models.md](task-models.md) |
| `STIRA_EPISODIC_EMOTION` | agent `episodic_emotion_snapshot` (on) | Opt-out of the post-turn emotion snapshot of new episodes (#364; on the data-directory floor, never in the prompt). Off: episodes stay `episodic-summary/v1`. Read when each episode is written. | #364, [episodic-emotion.md](episodic-emotion.md) |
| `STIRA_ASSOCIATIVE_RECALL` | agent `associative_recall.enabled` (off) | Opt-in associative recall (#170: a few earlier episodes that fit the message, model-free, records `memory.injected`). Overrides `enabled` only; the limits stay those of `agent-config.json` (or the defaults). Invalid options keep it off. Read at start-up. | #170, [associative-recall.md](associative-recall.md) |
| `STIRA_RECALL_EMOTION_PROXIMITY` | agent `associative_recall.emotion_proximity` (off) | Opt-in emotion-proximity signal of associative recall (#364, ADR 0017): re-ranks already relevant episodes by how close the current mood is to the one stored with the episode; at most one recalled item changes per turn, only at the NORMAL breaker level. Overrides `emotion_proximity` only; needs associative recall on. Read at start-up. | #364, [associative-recall.md](associative-recall.md) |
| `STIRA_MEMORY_AUTONOMY` | agent `memory_autonomy` (off) | `0` / `false` / `no` / `off` forces memory autonomy (ADR 0018) **off**, whatever `agent-config.json` says: no `memory_rate` tool, no memory job kind, nothing bound. No value turns it on. Read at start-up. | #251, [memory-autonomy.md](memory-autonomy.md) |
| `STIRA_CONTAINMENT_OBSERVE` | agent `containment` (off) | `0` / `false` / `no` / `off` forces the containment observation (ADR 0023 phase 0) **off**, whatever `agent-config.json` says: no hook, no `containment.observed` rows. No value turns it on. Read at start-up. | #460, [containment.md](containment.md) |
| `STIRA_TAINT_OBSERVE` | agent `taint` (off) | `0` / `false` / `no` / `off` forces the taint observation (ADR 0022 phase 0) **off**, whatever `agent-config.json` says: no hook, no `taint.observed` rows. No value turns it on. Read at start-up. | #458, [taint.md](taint.md) |
| `STIRA_ENABLE_EXEC_TOOLS` | hidden | Exposes the exec class (`shell`, `batch`, `cron`, `use_agent`, `swarm`, `graph`, `think`). Even then every call asks (Ask rule); they can never be Always. No preset sets it. A call runs only after a human approved that exact call (approval card): Stira's gate issues a single-use mark bound to the call, the upstream terminal prompt (`shell`, `cron`) is answered inside that approval only, and the terminal is never read; `BYPASS_TOOL_CONSENT` and `STRANDS_NON_INTERACTIVE` in the environment are ignored. An unapproved call, a replay, a direct `stream()` or a call from an inner agent without Stira's hooks is refused with the reason; a tool whose consent bridge cannot be confirmed is not registered (#362). The agent's shell then runs as the server's OS user, so CLI approvals (procedural rules, staging) are only as strong as OS permissions: run the agent as a different OS user from the approver (#318, [tool-execution-paths](tool-execution-paths.md)). | #163, #167, #362, [ADR 0010](adr/0010-tool-policy-gate-stack.md) |
| `STIRA_BROWSER_PATH` | unset (known locations, then `PATH`) | The Chrome / Chromium / Edge executable of the verification tool `page_check` (ADR 0015 phases 1–3, Addendums rev 3–4, #449). Unset: `/Applications` and `~/Applications` (macOS), then `google-chrome`, `chromium`, `microsoft-edge`, … on `PATH`. Set but not an executable file: the tool is not registered (no silent fallback). No browser, Windows (phase 1) or running as root: the tool is not registered and `stira doctor` says why (`page_check_*` notes). The browser runs with a fresh profile, every request (loopback included) going to a refusing proxy inside Stira; A browser session runs only after Chrome confirmed its guard (else the run ends, `browser_guard_unverified`); the rule is Ask or Never only in phase 1 (a saved Always is read as Ask); logs keep the target as `@workspace/<relative path>`. Phase 2: up to 20 input actions per call (`click`, `key`, `key_down`, `key_up`, `move`, `wait_ms`, `screenshot`, `text`, and since rev 3 `type` (≤ 200 characters at once), `wait_for` (a selector or a text, ≤ 10 s) and `expect` (`exists`, `visible`, `text_contains`, `text_equals`, `count`; pass / fail decided by Stira, reported as `assertions`); checked before the browser starts, none runs code, assertions count toward the 20), up to 4 screenshots, 4,096 characters of page text; the page's own text reaches the model only inside a `<page-content>` block marked as data. Phase 3: `http://localhost:<port>/…` for a port a person listed on the settings page (`tool-config.json` `page_check_ports`, default none; 1024–65535, never the dashboard's port; adding one needs the port typed in a confirmation) — the proxy forwards only that origin to `127.0.0.1:<port>`; without exec tools the person starts the server, on `127.0.0.1`. See [tool-execution-paths](tool-execution-paths.md#page_check-verification-browser-adr-0015). | #449, [ADR 0015](adr/0015-verification-browser-tool.md) |
| `STIRA_RUN_CHECKS` | unset | `0` / `false` / `no` / `off` forces the verification runner `run_checks` (ADR 0019) **off**, whatever gateway `run_checks` says: the tool is not registered. No value turns it on. Read at start-up. | #167, [ADR 0019](adr/0019-verification-runner.md) |
| `LANG`, `LC_ALL` | set by the OS | Passed on to a `run_checks` run (ADR 0019) when the value looks like a locale name; every other variable is left out of the run. | #167 |
| `STIRA_YOLO_ALLOW` | unset = `external` (shown; [addendum 2026-10-24](adr/0012-yolo-mode.md)) | YOLO mode (ADR 0012, #365) — **Default (ADR 0012 addendum 2026-10-24):** with neither this variable nor `gateway-config.json` `yolo.allow`, the allowance is `external` — the button and both scopes are shown, nothing is approved until a person presses and confirms; `off` (here or in the config) removes the button, `STIRA_HOST` on a wildcard turns the default off (one start-up line). The default never offers "Include exec" (that needs an operator's own `external`). **Phases 1–3, 5a, 5b and 6: the chat header and the Stage dialogue box show a `Start YOLO` button (phase 5b; the badge, Stop and the log line — try it with [yolo-quickstart](yolo-quickstart.md)); a confirmed request opens a six-hour window in which the Ask of an internal tool (local files, Stira memory, `stop`, `handoff_to_user`) in a chat / Stage turn is approved automatically (phase 5a). With `external` and "External allowed" chosen at the press (phase 6), the Ask of an external tool (`web_fetch`, `web_search`, `webhook_send`, …) is approved automatically too when its arguments carry no secret — at most `STIRA_YOLO_MAX_EXTERNAL` per window, the destination recorded only as a keyed hash; a call whose arguments look like they carry a secret, or that names a non-ASCII host, still shows its card. **Phase 7 (§9 option C, operator decision 2026-10-19):** with `external` **and** `STIRA_ENABLE_EXEC_TOOLS`, the press may also choose "Include exec" (its own checkbox in the confirmation): in that window the Ask of `shell` and `batch` is approved automatically too when its arguments (the command included) carry no secret — at most `STIRA_YOLO_MAX_EXEC` per window; `cron` and the child-agent tools (`use_agent`, `think`, `swarm`, `graph`) keep their card. **A command YOLO approves runs as the server's OS user and can bypass Stira's protected locations and audit** — accepted for this choice, see ADR 0012 §9. (Revision 3's `STIRA_YOLO_EXEC` does not exist and is not read.) MCP tools, webhook / bot / job turns, Never, protected locations (the floors), the breaker's write block and CRITICAL always ask a person or stay refused.** The allowance by itself approves nothing: only a window a person confirmed does. Exactly `off`, `internal` or `external` (empty = unset = the default; any other value, e.g. `on`, `1`, `Internal`, refuses start-up with exit 2, naming the variable only). Read once at start-up; not a hub field. **Phase 7c:** every YOLO value can also be set in `gateway-config.json` `yolo` (on the settings page) — this variable wins; see [The `yolo` section](#the-yolo-section-adr-0012-phase-7c). With it: `GET /api/yolo/status` exists (allowance, clock support, `offered` and why not, the window, the automatic-approval log lines; the page draws only this); save routes under `/api/config`, `/api/tools`, `/api/scheduler` refuse a body naming `yolo*` (409 `yolo_not_settable`); approve/edit decisions, `RECOVER` and loosening saves are refused while an agent tool call runs (409 `tool_call_running`, one layer of defence — not a proof that a person sent it); the server process refuses connections to its own dashboard port (loopback, unspecified and its own addresses; also through redirects and names) — since #495 at every start, also without this variable (see `STIRA_HOST`). **Phase 2 — confirmation:** `RECOVER`, loosening tool-rule, hub and rollback saves and scheduler create / resume / confirmed update are answered 428 `host_confirmation_required` and apply only after a person confirmed exactly that request — by default in the page (a confirmation dialog with a checkbox; phase 2c), or on the host when `STIRA_YOLO_HOST_CONFIRM` is set (`stira approvals decide <id>` on a TTY, or the desktop dialog); the page re-sends it once confirmed. Approve / edit of an approval card stay **one press** (phase 2c; only the running-tool-call refusal applies). `POST /api/yolo/request` creates a YOLO request the person confirms the same way (one checkbox, plus a second one for tier external; or `stira yolo confirm <id>`). **Phase 3 — the window:** the confirmation opens it (the marker `yolo-window.json` first, then `audit.yolo.started`, both read back); it ends after six hours from the press (the largest of the wall, sleep-including and sleep-excluding clocks), on a sleep longer than five minutes, on `POST /api/yolo/stop`, the server's stop or `/new`; a restart never brings it back. A clock anomaly, a failed audit / marker write or a crash leaves it **latched** (no new request) until a person unlatches it (`POST /api/yolo/unlatch`, a confirmed operation). **Phase 5a — automatic approval:** each one writes `audit.yolo.auto_approved` (codes and keyed hashes, never the arguments) and is consumed once right before the call runs; an audit write that fails approves nothing and ends the window (latched). The cap counts used and reserved approvals (all reserved: that call asks, the window stays; all used: the window ends). A breaker level other than NORMAL pauses the window (every Ask asks); ten minutes of NORMAL resume it; a fourth pause within an hour ends it. The tool list and the prompt do not change. YOLO is offered only on a platform whose clocks were measured (macOS today; `clock_not_measured` elsewhere). **With YOLO, keep the listener on one local address:** a wildcard `STIRA_HOST` (`0.0.0.0`, `::`) together with an explicit `internal` / `external` (variable or config) refuses start-up (exit 2, a fixed message without the address); with the default or `off` a wildcard bind behaves as before (the default is `off` there). | #365, [ADR 0012](adr/0012-yolo-mode.md), [admin-api-permissions](admin-api-permissions.md) |
| `STIRA_YOLO_MAX_AUTO_APPROVALS` | `2000` (with the YOLO allowance) | Cap of automatic approvals per YOLO window (ADR 0012 Open (5)); integer 1–10000. Set while the allowance is `off`, or out of range: start-up is refused. Counted by the window's automatic approvals (phase 5a; used + reserved). | #365 |
| `STIRA_YOLO_MAX_EXTERNAL` | `200` (at most the cap) | Sub-cap for external tools per window; integer 0 to the cap. Same refusals as above. Each automatic approval of an external tool (tier external, phase 6) uses one; used up, external tools ask again while the window goes on. `0`: external tools always ask. | #365 |
| `STIRA_YOLO_MAX_EXEC` | `200` (at most the cap) | Exec sub-cap per window (ADR 0012 phase 7, §9 option C); integer 0 to the cap. Same refusals as above. Used only in a window whose press chose "Include exec" (needs `STIRA_YOLO_ALLOW=external` and `STIRA_ENABLE_EXEC_TOOLS`): each automatic approval of `shell` / `batch` uses one slot of it and one of the cap; used up, exec tools ask again while the window goes on. `0`: the window opens and every command asks. | #365, [ADR 0012](adr/0012-yolo-mode.md) |
| `STIRA_PROGRESS_REPORTS` | off | Progress reports in a YOLO window (ADR 0014 phase 1, #451): `1` / `0` (also `true` / `false`). The default of the YOLO start dialog's "Report progress every N minutes" tick; the tick decides for the window it opens. At a checkpoint (`STIRA_PROGRESS_INTERVAL_MIN` after the turn began or the person last acted) the next tool call is cancelled, the agent writes a short report and the turn ends (`stop: "checkpoint"`); the page shows the report with Continue / Redirect / Stop here. Only human chat / Stage turns in an open window; nothing changes without `STIRA_YOLO_ALLOW`. Also `gateway-config.json` `progress` ([below](#the-progress-section-adr-0014)); the variable wins. | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_INTERVAL_MIN` | `30` | Minutes from the turn's start or the person's last action (a message, an answer, an approval decision) to a checkpoint; 10–360 (rounded into range, one warning). | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_CONTINUE_AFTER_MIN` | `10` | Minutes without an answer after which the server continues by itself, **once per report**, only while the window is `active`, the breaker NORMAL and nothing latched (otherwise it is dropped and the report waits for a person; operator decision 2026-10-19); 1–120. | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_MAX_AUTO_CONTINUES` | `3` | Automatic continues in a row without a person's answer; then the report waits. `0` = never continue by itself. 0–10. | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_COUNT_TOOLS` | `0` (off) | Phase 4: a checkpoint also after this many tool calls since the turn began or the person last acted (the next call is the one cancelled; `trigger: "count"`, `count: "tools"`). 0–1000. | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_COUNT_TOKENS` | `0` (off) | Phase 4: a checkpoint also after this many model tokens (input + output of the finished model calls) since the turn began or the person last acted (`count: "tokens"`). 0 or 1000–10,000,000 (a smaller positive value is rounded up to 1000). | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_DECLARED` | `0` (off) | Phase 3: the agent may declare a milestone itself with `handoff_to_user(breakout_of_loop: true)` — that call is cancelled before its rule, card and the YOLO approver, its message (masked, code blocks dropped, capped) ends the turn and is the report's note (`trigger: "declared"`). At most one declared report every 10 minutes (a declaration sooner is not run and the turn goes on). While a window with reports on is open, one fixed guidance paragraph is added at the end of the static prompt (the same bytes on every turn; it appears and goes only when such a window opens or ends). `1` / `0`. | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_YOLO_HOST_CONFIRM` | unset = `browser` (with the YOLO allowance) | How a person confirms the confirmed operations (ADR 0012 §8b, Open (12), revisions 6–7). **Unset (or `browser`): in the dashboard** — a confirmation dialog in a fixed format and a checkbox (a second one for tier external); nothing on the host. **Set to a host channel, the host is required** and the browser confirmation is refused (409 `host_confirm_on_host`): `auto` = the native dialog with an OS authentication (Touch ID / account password) when the server runs in that person's macOS desktop session, otherwise the host command; `command` = always the host command (`stira approvals decide`, `stira yolo confirm`; a TTY and a typed word), also the way to never show a dialog; `dialog` = the dialog where available, else the command. With exec tools enabled, only the OS-authenticated dialog **actually selected at start-up** is a channel that a command you approved cannot answer — setting `dialog` (or `auto`) does not guarantee it: where no dialog is available (SSH, no desktop session) the command is used, which such a command can answer. Start-up logs a warning naming the channel in use and the setting whenever exec tools are enabled and that channel is not the dialog. Any other value, or set while the allowance is `off`, refuses start-up. Linux / Windows dialogs are not measured: the command is used there. | #365, [ADR 0012](adr/0012-yolo-mode.md) |
| `SSH_CONNECTION`, `SSH_TTY` | (set by sshd) | Read only with the YOLO allowance (when choosing the host-confirmation channel): either one set means the server is not in the person's desktop session, so the host confirmation uses the command, never the dialog. Never set them for Stira. | #365 |
| `BYPASS_TOOL_CONSENT` | ignored | Read by the upstream `file_write`, `shell`, `cron` and `http_request` tools (`shell` also reads `STRANDS_NON_INTERACTIVE`). Stira replaces those lookups: the real process values are ignored and consent comes only from Stira's approval gate. Setting them changes nothing. An `http_request` POST/PUT/PATCH/DELETE runs only after a person approved that exact call (Ask rule, approval card); without one it is refused with the reason (#547). GET is unchanged. | #242, #246, #362, #547 |
| `STIRA_WEB_SEARCH_ENDPOINT` | not configured | JSON search endpoint for `web_search`. Unset (or blank), `web_search` is **not offered** to any agent (#583): the registry keeps its name and saved rule (a profile's Ask stays), the settings page and `GET /api/config/tools` show it as not configured (`unavailable_reason: "not_configured"`), and the agent's Tool Availability names this variable. No external provider is contacted by default. | #583 |
| `PERPLEXITY_API_KEY` | tool not offered | Enables `perplexity_search` ([tools/perplexity.md](tools/perplexity.md)). | — |
| `STRANDS_KNOWLEDGE_BASE_ID` | tool not offered | Enables the standard Bedrock Knowledge Base `memory` tool. Stira's own memory tools never need it. | #207 |
| `STIRA_CHAT_DATA_DIR` | `.local/chat` (working directory) | Per-conversation state of the Slack/Discord bots. | [chat-integrations.md](chat-integrations.md) |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | bot does not start | Slack Socket Mode bot. | [chat-integrations.md](chat-integrations.md) |
| `DISCORD_BOT_TOKEN` | bot does not start | Discord bot. | [chat-integrations.md](chat-integrations.md) |
| `WEBHOOK_SECRET_GENERIC`, `WEBHOOK_SECRET_GITHUB`, `WEBHOOK_SECRET_SLACK` | that provider answers 503 | HMAC secret per webhook provider (`/webhooks/<provider>`). | [business-integrations.md](business-integrations.md) |
| `STIRA_DATA_DIR` | `<repository>/.local/stira` (configs: working directory) | `stira_server`'s data directory; when set, its configuration files are the ones in it too. Fully qualified path only (on Windows `C:data` and `\data` are refused); symlinks are resolved (the floor is the real directory). Blank = unset. A relative path, a control character, a filesystem root, the home directory or an ancestor, a file or a path below a file, or a directory that cannot be created refuses the start (exit 2, `Stira: …`, no value or path printed). Nothing is migrated. `stira_ctl.sh` (lock, PID file, memory commands), `stira_ctl.ps1` (`distill`) and `stira doctor` follow it; both scripts check it with the server's own validator (`python -m stiracore.config.data_dir_check`), so they accept and refuse the same values; the stand-alone CLIs that take `--file`/`--base-dir` do not. | #272 |
| `STIRA_STATE_PATH` | `/tmp/stiracore_poc_state.json` | **Observation console only**: its state file. It is also folded into the resolved `state_base_path` shown in the settings, which `stira_server` does not use (its data directory is `STIRA_DATA_DIR`). | #272 |
| `STIRA_GATEWAY_CONFIG_PATH`, `STIRA_AGENT_CONFIG_PATH`, `STIRA_TOOL_CONFIG_PATH` | `.local/stira/*.json` (working directory) | **Observation console only**: alternative config files. `stira_server` always uses the defaults. | #272 |
| `TERM_PROGRAM`, `__CFBundleIdentifier`, `XPC_SERVICE_NAME` | set by macOS | Read only, to estimate whose OS permission applies (terminal, app or launchd) in permission cards and `stira doctor`. A launchd label is never shown. | #244, [ADR 0007](adr/0007-write-diagnostics.md) |
<!-- env-vars:end -->

## 2. Environment variables (scripts and tests)

Not read by the servers.

<!-- env-vars-dev:begin -->
| Variable | Used by | Effect |
|---|---|---|
| `STIRA_PYTHON` | `scripts/stira_ctl.sh` | Python interpreter (default `.venv/bin/python`). |
| `STIRA_LIVE_MODEL`, `STIRA_LIVE_BASE_URL`, `STIRA_LIVE_TOOLS` | `tests/test_provider_live.py` (`-m live`) | Opt-in live provider smoke test ([providers.md](providers.md)). |
| `STIRA_REQUIRE_AXE` | `tests/test_a11y_axe.py` | `1` fails instead of skipping when axe-core/jsdom is missing. |
| `STIRA_CONTRACT_REPORT` | `tests/test_tool_path_contract.py` | Writes the measured tool-path matrix as JSON. |
| `STIRA_BROWSER_CHECK`, `STIRA_REQUIRE_BROWSER` | `tests/test_board_svg_browser.py` (`scripts/check_board_svg_browser.mjs`; browser: `CHROME`, else found for the platform) | The real-browser check of SVG on the Stage board (#453) runs when Chrome / Chromium is found, else it is skipped with the reason. `STIRA_BROWSER_CHECK=0` skips it; `STIRA_REQUIRE_BROWSER=1` fails instead of skipping when no browser is found. |
<!-- env-vars-dev:end -->

## 3. `gateway-config.json` (`GatewayConfig`) — *checked*

Shared by every agent behind one server. Unknown keys are refused at load. Secret values
are never stored here — only variable **names**.

Fields marked "stored and shown only" (`state_base_path`, `notify_targets`, and in
`agent-config.json` `hb_session_mode`, `hb_task_policy`, `autonomy_boundary`) are listed
in `GET /api/config` → `stored_only_fields` (#272); a test keeps the list in step with
the code, so a field leaves it when something starts to use it.

<!-- gateway-fields:begin -->
| Field | Default | Effect | Env override | Issue |
|---|---|---|---|---|
| `model_id` | `"global.anthropic.claude-sonnet-5"` | Chat model ID. | `STIRA_MODEL` | — |
| `max_output_tokens` | `null` | Output token cap per model call. `null` = automatic: the selected model's documented output limit (`ModelCapabilities.max_output`), bounded at 32000 — 32000 plus the row's `thinking_reserve` (32000) for models whose thinking counts toward the cap; when the limit is not recorded, the provider's default (`8192` for hosted APIs, `32000` for local llama.cpp/Ollama, #416). A number is used as given, also above the model's limit — set it deliberately. A turn that reaches the cap is cut off and shown as such; its tool call is not run. See [Output cap](#output-cap-and-cut-off-turns). Settings page: section "Output limit and cost" (automatic is the recommended default; a number above the model's documented limit is warned about, not refused; 1–1000000 when changed there — a larger value already in the file is kept and does not block saving other settings). Applied from the next turn. | — | #363, #470, #416 |
| `region` | `"ap-northeast-1"` | Bedrock region. Applied from the next turn: the chat server re-resolves the model selection before every turn and rebuilds the model when the region differs (`AWS_REGION` wins). | `AWS_REGION` | #470 |
| `provider` | `"bedrock"` | Model provider. | `STIRA_PROVIDER` | #204 |
| `base_url` | `null` | Provider endpoint. A non-loopback URL for an API provider can be set only here, not from the settings page. | `STIRA_BASE_URL` | #205 |
| `api_key_env` | `null` | Name of the API-key variable. | `STIRA_API_KEY_ENV` | #205 |
| `organization_env` | `null` | Name of the OpenAI organization variable. | — | #205 |
| `port` | `5555` | Chat server port. | `STIRA_PORT` | — |
| `state_base_path` | `".local/stira/state"` | Shown in the resolved settings; **not used at run time** by either server (`stira_server`'s data directory is the default or `STIRA_DATA_DIR`; the observation console reads `STIRA_STATE_PATH` itself). Listed in `stored_only_fields`. | `STIRA_STATE_PATH` | #272 |
| `hb_interval_floor_sec` | `300.0` | Shortest heartbeat interval (low stability). Settings page: section "Heartbeat", in minutes (1 min–24 h when changed there; the longest interval follows when the shortest passes it; a value already in the file outside that range is kept and does not block other saves). Read at start-up. | — | #238, #472 |
| `hb_interval_ceil_sec` | `10800.0` | Longest heartbeat interval (calm). Settings page, like `hb_interval_floor_sec`; must not be below it (`floor_above_ceil`). A file that already holds floor > ceil (an omitted bound counts as its default; a written `null` or non-number is still refused) still loads: the heartbeat does not start (`HeartbeatIntervalsError`), `/api/config` and the settings hub report `hb_intervals_reset` (a warning in the log), and the page shows the file's bounds. Saving other settings keeps the file's bounds as they are; only saving bounds (the defaults too) ends it. Read at start-up. | — | #238, #472, #512 |
| `hb_enabled` | `false` | Background heartbeat tick (model-free: no token cost, so turning it on needs no confirmation). Settings page: section "Heartbeat", with the running status (read-only). Read at start-up: a saved change waits for a restart. While `STIRA_HEARTBEAT` is set the switch is disabled with the variable's name and a change is refused (`env_pinned`), like every pinned setting ([settings-surface.md](settings-surface.md)). | `STIRA_HEARTBEAT` | #238, #472 |
| `prompt_cache` | `false` | Prompt caching (Bedrock Claude only). Settings page: "Output limit and cost" (shown as unsupported for other models). Read at start-up. While the env variable is set, the switch is disabled on the page with the variable's name (never its value) and a change to it is refused (`env_pinned`). The same rule as every pinned setting ([settings-surface.md](settings-surface.md)): the variable is listed under "Where settings live", and a saved change waits for a restart (the banner counts it). | `STIRA_PROMPT_CACHE` | #254, #470 |
| `prompt_cache_ttl` | `"5m"` | `"5m"` or `"1h"`. Settings page, read at start-up; pinned by the env variable like `prompt_cache`. | `STIRA_PROMPT_CACHE_TTL` | #254, #470 |
| `context_management` | `false` | Tool-result offload + summarization. Settings page, read at start-up; pinned by the env variable like `prompt_cache`. | `STIRA_CONTEXT_MANAGEMENT` | #258, #470 |
| `context_management_options` | `{}` | Threshold overrides ([context-management.md](context-management.md)); unknown names / out-of-range values are refused. `context_window_tokens` is also the window the context gauge shows (status `context_window`, #350), with the feature on or off. Settings page: the "Details (thresholds)" fold of "Context management"; each value is checked on its own field, and only values that differ from the code defaults are written. Read at start-up. | — | #258, #470 |
| `memory_map` | `true` | Memory Map section in the recalled part of the prompt (framed as data, dropped whole when the budget is short; an empty store adds nothing). Not on the settings page; read at start-up. | `STIRA_MEMORY_MAP` | [memory-index.md](memory-index.md) |
| `memory_map_max_tokens` | `300` | Size bound of the Memory Map section (integer 100-1000, the prompt assembler's estimate). Read at start-up. | — | [memory-index.md](memory-index.md) |
| `jev_enabled` | `false` | `false` = classifier "auto" (Sokudan when installed, else rules); `true` = use `jev_classifier`. Applied at start-up. `GET /api/config/jev` shows the choice (`auto` / `rule` / `sokudan`), what it resolves to, the running classifier and `restart_required`; `POST {"classifier": …}` sets the whole choice. | — | #227, #162 |
| `jev_classifier` | `"rule"` | `"rule"` or `"sokudan"`. | — | #227 |
| `notify_targets` | `[]` | Stored and shown only; nothing sends notifications to these targets. | — | #272 |
| `ui` | `{"home": "chat"}` | Dashboard options. `home` (`"chat"` or `"stage"`): the page `/` opens. `"stage"` also lists the Stage first in the menu; it takes effect only with `STIRA_STAGE=1` (otherwise `/` stays the chat and a WARNING is printed at start-up). `/chat` always exists. Read at start-up; not editable from the settings page. `board_svg` (boolean, **default on** since the ADR 0012 addendum of 2026-10-24, written only when off): a reply's ```` ```svg ```` blocks are drawn on the Stage board as an image rebuilt from an allow-list of shapes, colours and text (`dashboard/stage/js/svg_safe.js`; no script, link, outside resource or animation; a refused one stays code with its reason). Set from the settings page (section "Board (Stage)"; turning it off applies directly, turning it back on is confirmed; both are recorded in the settings history); read on each Stage page load. The chat page and the Stage log keep showing code. | — | #350, #453 |
<!-- gateway-fields:end -->

### The `context_profile` keys (#368)

Small-window profile ([small-context.md](small-context.md)). Both keys are written only when they differ from the default, so files without them stay as they were (like the sections below).

| Field | Default | Effect | Env override | Issue |
|---|---|---|---|---|
| `context_profile` | `"default"` | Small-window profile ([small-context.md](small-context.md)): `"default"` (prompt and tool list unchanged), `"small"` or `"tiny"`: default budgets of the system prompt (tokens, recent episodes, procedural rules) and only the usable tool definitions sent. An explicit value given by the caller wins. Shown read-only on the settings page ("Output limit and cost"). Written only when not `"default"`. Read at start-up. | `STIRA_CONTEXT_PROFILE` | #368 |
| `tools_send_allowed_only` | `false` | Send only the tool definitions whose live rule is Always or Ask (Never and unresolvable names are left out). The gate is unchanged: a call is checked as before. `small` / `tiny` turn it on too. Written only when on. Read at start-up; the rules themselves are read each turn. | — | #368 |
| `tools_short_schemas` | `false` | Send shorter property descriptions inside the long upstream tool schemas (`http_request`, `think`, `use_agent`, `swarm`, `graph`, `shell`, `cron`; table in `tools/compact_specs.py`). Property names, types, enums, defaults, `required` and nesting are unchanged (tested); limits, defaults and the token-variable caution are kept. Off: the schemas are the upstream ones, byte for byte. `small` / `tiny` turn it on too. Written only when on. Read at start-up. | — | #368 |

### The `yolo` section (ADR 0012 phase 7c)

`gateway-config.json` may hold a `"yolo"` object (absent by default: the allowance is
`external` — the button is shown, nothing approved before a press; `"allow": "off"` removes
it, ADR 0012 addendum 2026-10-24). Each value
is used only when its environment variable is unset — **environment > config > default**,
per value; `GET /api/yolo/status` → `sources` and the settings page show where each came
from. It is written only by the settings page's YOLO section (`POST /api/yolo/settings`, a
person's save; a loosening is confirmed — see [admin-api-permissions](admin-api-permissions.md));
every other save route still refuses `yolo*`, and other saves of this file keep the section
unchanged. A hand-edited value never refuses start-up: an unknown `allow` is `"off"`,
integers are rounded into their range, anything else is left out (its default applies) —
one start-up warning naming the codes. Unlike the variables, a config sub-cap above the cap
is rounded down to the cap.

| Field | Default | Variable that wins | Used | Accepted |
|---|---|---|---|---|
| `allow` | `"external"` (`"off"` on a wildcard listener) | `STIRA_YOLO_ALLOW` | at the next start | `"off"`, `"internal"`, `"external"` |
| `offer_exec` | `true` | — | at the next start | `true` / `false`; `false` takes "Include exec" out of the dialog (it is offered only with `allow` `external` **and** `STIRA_ENABLE_EXEC_TOOLS`, which the page cannot set) |
| `max_auto_approvals` | `2000` | `STIRA_YOLO_MAX_AUTO_APPROVALS` | from the next window | integer 1–10000 |
| `max_external` | `200` (at most the cap) | `STIRA_YOLO_MAX_EXTERNAL` | from the next window | integer 0 to the cap |
| `max_exec` | `200` (at most the cap) | `STIRA_YOLO_MAX_EXEC` | from the next window | integer 0 to the cap |
| `host_confirm` | `"browser"` | `STIRA_YOLO_HOST_CONFIRM` | at the next start | `"browser"`, `"auto"`, `"command"`, `"dialog"` |

A save is refused while a chat turn or an agent tool call runs, while a YOLO request, window
or latch exists (an open window keeps its values), for a value its variable pins, and — for
`allow` other than `off` — on a wildcard listener (the server would not start; the page reads
the listen address with the same function as the start). A loosening save is recorded as
`audit.yolo.settings_saved` (field names, never values) before the file is written, and is
refused when that record cannot be written. A file that cannot be read is treated as no
section, with one warning. When values taken from the file loosen YOLO, the start prints one
`INFO` line naming those fields (never their values). The agent's file tools cannot write this
file; a command auto-approved in an exec window can, as it can the tool rules (ADR 0012 §9).
Example:

```json
{"yolo": {"allow": "internal", "max_auto_approvals": 500}}
```

### The `progress` section (ADR 0014)

Progress reports in a YOLO window (phases 1, 3 and 4, #451). The first trigger that is due wins
(time, then tool calls, then tokens); never two checkpoints in one turn. Read at start-up, leniently: an integer out
of range is rounded into it, anything not understood is left out (its default applies), one
warning with codes only. A variable wins per value. Nothing in it calls a model by itself;
`enabled` only sets the YOLO dialog's default (the person's tick decides per window).

| Field | Default | Variable | Accepted |
|---|---|---|---|
| `enabled` | `false` | `STIRA_PROGRESS_REPORTS` | bool |
| `interval_min` | `30` | `STIRA_PROGRESS_INTERVAL_MIN` | 10–360 |
| `continue_after_min` | `10` | `STIRA_PROGRESS_CONTINUE_AFTER_MIN` | 1–120 |
| `max_auto_continues` | `3` | `STIRA_PROGRESS_MAX_AUTO_CONTINUES` | 0–10 (`0`: never) |
| `count_tools` | `0` | `STIRA_PROGRESS_COUNT_TOOLS` | 0–1000 (`0`: off) |
| `count_tokens` | `0` | `STIRA_PROGRESS_COUNT_TOKENS` | 0 or 1000–10,000,000 (`0`: off) |
| `declared` | `false` | `STIRA_PROGRESS_DECLARED` | bool (phase 3: the agent's own milestone report) |

The automatic continue (operator decision 2026-10-19) starts one ordinary chat turn
(「続けてください。」 / "Please continue.", in the language of the person's last message) only
when the window is `active`, the report came from a chat / Stage turn, nobody answered, and
fewer than `max_auto_continues` continues ran in a row; the report's Stop, its "Don't continue
by itself", `/new`, the chat Stop of a continuation turn and the window's end cancel it. The
settings page does not edit this section yet. Example:

```json
{"progress": {"enabled": true, "interval_min": 20}}
```

### The `delegation` section (ADR 0016)

The `delegate` tool (sub-agents, ADR 0016 phase 1, #486). **On by default**: without the
section the defaults below apply. Only values that differ from the defaults are written; a
value outside its bounds is refused (the file does not load, like the other checked fields).
Set from the settings page (section "Sub-agents (delegate)", recorded in the settings
history); read at every turn and every `delegate` call. No environment variable.

| Field | Default | Accepted |
|---|---|---|
| `enabled` | `true` | bool — on = the tool is offered; each call is still Ask (a person's card; a YOLO window never answers it before ADR 0016 phase 3) |
| `max_tool_calls` | `40` | 1–100 — tool calls of one child (`limit_tool_calls`) |
| `max_seconds` | `600` | 10–1800 — wall time of one child (`limit_time`) |
| `max_children_per_turn` | `20` | 1–50 — children one parent turn may start (`limit_children`) |

The child uses only tools the parent may use without asking, returns a masked summary of at
most 2,000 characters (inside a `<delegate-output>` block marked as data, not instructions),
runs one at a time, cannot start a child, and stops with the parent (stop button, end of the
YOLO window, server stop). The tool is offered only in a chat / Stage turn a person's
message started: a webhook, bot, approval-resume turn or a window's automatic continue does
not list it (no card nobody watches, no tool
definition tokens); the heartbeat calls no model, and the scheduler only in its memory and
`agent_jobs` executors. Example:

```json
{"delegation": {"enabled": false}}
```

### The `agent_jobs` section (ADR 0021 S2)

Scheduled Agent-layer jobs (#239): an operator's prompt run by the scheduler without a person
present. **Off by default**; read when the chat server starts (`STIRA_SCHEDULER=1` is still
needed, and `STIRA_SCHEDULER=0` still turns the whole scheduler off). Set from the settings
page (section "Scheduled agent jobs", recorded in the settings history; turning it on is a
confirmed change). Only values that differ from the defaults are written.

| Field | Default | Accepted |
|---|---|---|
| `enabled` | `false` | bool — on = a person may create Agent-layer jobs (`confirm: true`) |
| `agent_daily_token_budget` | `50000` | 1–10000000 — one pool for every Agent-layer job of a UTC day (never unlimited) |

A job runs through the same gate as a turn (a child built like `delegate`'s: tool rules,
write / read floors, identity guard), with only `JOB_READ_ONLY_TOOLS`; the server's breaker is
read before every model and tool call. The first call outside the job's tools, or one that a
rule would ask about, stops the run (`approval_required`; waiting for a person is ADR 0024
phase 2, not built). Model-calling runs need a healthy heartbeat, `catch_up` is refused, and
Agent-layer jobs come up paused after every start (a person resumes them); an interrupted run
is never resumed from a checkpoint. The result is stored masked inside a
`<job-output>` frame marked as data, not instructions. Example:

```json
{"agent_jobs": {"enabled": true, "agent_daily_token_budget": 20000}}
```

### The `run_checks` section (ADR 0019)

The verification runner `run_checks` (ADR 0019 phases 0–1, #167). **Off by default**: absent
or `{}` = no tool registered and nothing else changes. `{"phase": 1}` turns it on at the next
start (only phase `1` is accepted; anything else is off). `STIRA_RUN_CHECKS=0` forces it off.
Hand edit only (no settings-page switch); not registered on Windows or as root.

```json
{"run_checks": {"phase": 1}}
```

The model chooses kinds (`pytest`, `unittest`, `node_test`, `ruff`, `compileall`, or `auto`)
and a workspace folder; Stira builds every command, runs it in a throwaway copy of the folder
with a scrubbed environment, no network and limits, and returns counts and the first failures
inside a `<check-output>` block marked as data. On macOS the run is inside a generated
`sandbox-exec` profile after a self-test (`containment: os_sandbox`); on Linux it is
`limits_only` and every card and result says what is not held. The rule is Ask by default; a
saved Always is effective only on a verified `os_sandbox` host (else read as Ask,
`rule_always_capped`). See [tool-execution-paths](tool-execution-paths.md#run_checks-verification-runner-adr-0019).

### The `autonomy` section (ADR 0011 stage C, alpha)

The goal grants and the goal iteration executor ([grants](grants.md); overview and known
limits: [autonomy](autonomy.md)). **Alpha — may change without notice. Off by default**:
absent, `{}` or anything but `"stage_c": true` = no route, no file, no thread; the only
visible change is this setting itself. Read at start-up; hand edit only. An unknown key in
the section is ignored with the warning `autonomy_unknown_key`.

```json
{"autonomy": {"stage_c": true, "run_checks_unattended": true}}
```

| Key | Default | Meaning |
|---|---|---|
| `autonomy.stage_c` | `false` | `true` registers the grant routes and the settings-page panel and creates `grants.json` at the first issue. A grant runs nothing by itself. |
| `autonomy.stage_c_exec` | `false` | W5: exactly `true` **and** `stage_c` on builds the goal iteration executor and its route `POST /api/grants/<id>/step`: a person's "Run one iteration" press runs **one** iteration in the session `grant:<id>` (no automatic next iteration; the user verifies; the heartbeat can start one only with `heartbeat_start` below). Without `stage_c` it stays off (warning `stage_c_exec_needs_stage_c`). Off: no executor, no route, no thread. |
| `autonomy.iteration_wall_sec` | `600` | W5: the longest one iteration may run (60–3600 s); past it the iteration ends and the goal is `blocked(iteration_timeout)`. Anything else is the default (warning `iteration_wall_sec_invalid`). |
| `autonomy.claim_stale_sec` | `1800` | W6c: after how long an unverified completion claim is shown as "still waiting for you" in the settings panel and on the Stage board (60–86400 s). Display only: no timer, no model call, no action. Anything else is the default (warning `claim_stale_sec_invalid`). |
| `autonomy.heartbeat_start` | `false` | W6d: exactly `true` **and** `stage_c_exec` on (and a running scheduler, `STIRA_SCHEDULER=1`) lets a grant **issued with its own `heartbeat_start: true`** (host-confirmed at issue, never changed afterwards) be started by the scheduler's model-free `grant_tick`: at most one iteration per scheduler wake, through the same `step` as a person's press. Without `stage_c_exec` it stays off (warning `heartbeat_start_needs_stage_c_exec`); without a running scheduler nothing wakes it (one warning `heartbeat_start unavailable (reason=scheduler_off)`; a flagged grant shows `unavailable: scheduler_off`); without the heartbeat daemon (`hb_enabled` / `STIRA_HEARTBEAT=1`, off by default) every wake is skipped `heartbeat_unhealthy` (warning `reason=heartbeat_off`, `unavailable: heartbeat_off`). Off: no ticker, nothing visible, the answers as before. See [grants](grants.md) "Heartbeat start". |
| `autonomy.tick_min_spacing_sec` | `300` | W6d: the shortest time from the grant's issue, from an iteration's end (any origin) and (W6-F, #644) from the grant's last resume to a heartbeat-started one (30–86400 s; the floor 30 s cannot be lowered). Anything else is the default (warning `tick_min_spacing_sec_invalid`). |
| `autonomy.allow_browser_confirm` | `false` | Issue / resume / verify need a **host confirmation** (the YOLO desk's `dialog` or `command` channel: `STIRA_YOLO_ALLOW` + `STIRA_YOLO_HOST_CONFIRM=auto` / `dialog` / `command`). Without one they are refused (`host_channel_required`). `true` accepts the browser confirmation when the channel is the browser one (`STIRA_YOLO_HOST_CONFIRM` unset / `browser` with `yolo.host_confirm` `"browser"`, or YOLO off with `STIRA_YOLO_HOST_CONFIRM` and the other `STIRA_YOLO_*` overrides unset — a set one refuses the start, `… is set but the YOLO allowance is off`) — the weaker, user-chosen layer: a same-user process can drive it (logged at start). It never replaces a configured host channel (`auto` / `dialog` / `command` still take precedence). Revoke / pause / kill never need it. |
| `autonomy.run_checks_unattended` | `true` | Whether a grant's scope may name `run_checks`. Effective **only on a verified `os_sandbox` host** (ADR 0019): on `limits_only` such a grant is refused whatever this says. Anything but a boolean is read as `false`. |


### Handoff notes (`handoff_inject`, `handoff_max_tokens`, `handoff_max_age_hours`)

Written to `gateway-config.json` only when they differ from the defaults (so they are not
rows of the table above). `handoff_inject` (`true`): the `handoff_write` / `handoff_read`
tools and the newest note in the recalled part; no note, or an old one, = no section.
`handoff_max_tokens` (`400`, 100–1500): a larger note is left out whole, never cut.
`handoff_max_age_hours` (`72`, 1–720): an older note is not shown. Read at start-up;
`STIRA_HANDOFF_INJECT` wins over `handoff_inject`. See [handoff.md](handoff.md).

### The `task_models` section

Which model each side task uses ([task-models.md](task-models.md)). Absent or `{}` (the
default, not written to the file): every task uses the chat model. Keys are task names
(`tool_summary`, `context_summary`); an unknown one refuses the start. Values: `inherit`,
a model id of the chat's provider (its endpoint, key variable and region are reused), or
`provider:model` for another provider whose credentials are already set up. Hand edit or
`STIRA_TASK_MODEL_<TASK>` (wins per task); read at start-up. The settings page shows the
effective model and its source read-only.

```json
{"task_models": {"tool_summary": "global.anthropic.claude-haiku-4-5-20251001-v1:0"}}
```

### The `token_prices` section (#451)

Token unit prices per model, in **USD per million tokens**, keyed by the model id exactly as
the server uses it (`model_id`, or the settings page's model override). **Stira ships no
price data**: without an entry for the model the activity board keeps 「概算コスト: 単価が未設定」 /
"Estimated cost: no price set" and the chat page's estimated cost stays `—`. `input` and
`output` are required; `cache_read` and `cache_write` are optional — a turn that read or
wrote the prompt cache without that price is not priced (never counted as free). Hand edit
only; read at start-up (a saved change waits for a restart). An entry or a value not
understood (negative, not a number, unknown key) is left out with one warning (codes only).

```json
{"token_prices": {"global.anthropic.claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}}}
```

The numbers above only show the shape — take the prices from your provider's own price
list. The board's totals (since the server started) price **each turn by the model named in
its `turn.completed`**: when some turns cannot be priced (another model without an entry, or
the cache without a cache price), the board shows the sum of the priced turns and says that
the rest is left out; the chat page shows its single number only when every turn is priced.

## 4. `agent-config.json` (`AgentConfig`) — *checked*

Per agent. Unknown keys are refused at load.

<!-- agent-fields:begin -->
| Field | Default | Effect |
|---|---|---|
| `identity_anchors` | three neutral built-in anchors (`DEFAULT_IDENTITY_ANCHORS`) | L3+ constraints injected into the prompt; must be non-empty. The chat server reads them at start-up (#152). |
| `soul_path` | `null` | SOUL file; `null` = the bundled sample SOUL (Resolver). Always read-only to the agent. |
| `read_only_paths` | `["IPD.md", "SOUL.md"]` | Extra write-floor paths; the defaults and the SOUL path are always kept. |
| `workspace_dir` | `null` | The `@workspace` write root; `null` = `<data directory>/workspace/` (#271; the episodic layer and the context stash are then write roots too). |
| `write_roots` | `["@workspace"]` | Where `file_write` / `editor` may write; outside every root is denied (#243). The secret-path floor of `read_roots` applies to writes too, whatever the roots (also `/` with `allow_broad_write_roots`): `E_WRITE_SECRET_PATH` (#536). Adding a root on the settings page is confirmed and recorded. |
| `write_outside_action` | `"deny"` | Only `"deny"` is accepted ("ask" is reserved, not implemented). |
| `allow_broad_write_roots` | `false` | Opt-in for a root that is `/`, a drive root, the home directory or an ancestor of it. |
| `read_roots` | `["@workspace"]` | Where `file_read` / `view_image` / `image_reader` / `editor` `view` / `lsp_*` may read; outside every root is `E_READ_OUTSIDE_ROOTS` (#457). `/` is allowed (the secret-path floor still applies). Adding a root on the settings page is confirmed and recorded. |
| `read_secret_paths` | `[]` | Additions to the secret-path floor (`E_READ_SECRET_PATH`, #457; also `E_WRITE_SECRET_PATH` for writes, #536): a path (`~/vault`, an absolute path) or a name pattern (`*.secret`). The built-in floor (`~/.ssh`, `~/.aws`, `.env`, `*.pem`, `id_*`, keychains, browser profiles, `/etc/shadow` …) cannot be removed. |
| `model_override` | `null` | Per-agent model (loses to `STIRA_MODEL`). |
| `provider_override` | `null` | Per-agent provider; needs `model_override`. |
| `base_url_override` | `null` | Endpoint for `provider_override`. |
| `api_key_env_override` | `null` | Key variable name for `provider_override`. |
| `distiller_mode` | `"rule"` | Harvest distiller: `"rule"` (no model call), `"llm"` (one model call per episode not yet scored by that model; switching re-scores every episode) or `"jev"` (stub, #106). Running it by hand or from an OS timer, and measured cost: [memory-maintenance.md](memory-maintenance.md). |
| `distiller_model_id` | `"global.anthropic.claude-haiku-4-5-20251001-v1:0"` | Model of the LLM distiller. |
| `distiller_provider` | `"bedrock"` | Provider of the LLM distiller (#207). |
| `distiller_base_url` | `null` | Endpoint of the LLM distiller. |
| `distiller_api_key_env` | `null` | Key variable name of the LLM distiller. |
| `harvest_stage_auto` | `true` | Harvest stages the automatic band (0.4–0.7) for human review as well, until the review UI (#193). `false` (not recommended): the auto band is written to `semantic/knowledge/` directly, without human review (#293). |
| `memory_retention` | `{}` | Retention limits per store, read only by `stira memory retention` (#294). Empty = no limits (except `memory_plans`, below); nothing is ever removed without `--apply`, and what `--apply` removes is archived (undo: `stira memory restore <id>`). Keys: `episodic` (`max_age_days`, `max_entries`, `keep_unharvested` = `true`, `honour_expires_at` = `false`), `turns` / `tool_payloads` / `checkpoints` (`max_age_days`; checkpoints also `max_per_thread`), `memory_plans` (`max_age_days`, default `90`: plan files of memory jobs, the one limit set by default), `staging` (`warn_after_days`), `scheduler_log` / `archive` (`warn_bytes`) — the last three are reported only. Integers 1–1000000 (`*_bytes`: up to 10^15). See [memory-maintenance.md](memory-maintenance.md#retention-delete-requests-and-provenance-294). |
| `harvest_job_max_episodes` | `100` | Episodes one scheduled `memory_harvest` run scores (1–100000), newest first; the rest wait for the next run (#251). Rule distiller only (with `"llm"`/`"jev"` the job cannot be created or run; use `distillation_cli`); its output always goes to staging, whatever `harvest_stage_auto` says. |
| `episodic_emotion_snapshot` | `true` | Record the post-turn emotion snapshot (six dimensions, category, intensity, breaker, turn) of each new episode in `memory/…/emotion/`, on the floor: not readable by the agent, never in the prompt or the memory tools (#364, [episodic-emotion.md](episodic-emotion.md)). `false` = opt out: new episodes stay `episodic-summary/v1`; existing records stay until deleted (`emotion_cli orphans --apply` removes only orphans). `STIRA_EPISODIC_EMOTION` overrides it. |
| `associative_recall` | `{}` | Opt-in associative recall (#170): `{"enabled": true}` offers up to `max_items` (3; 1–5) earlier episodes that fit the message, in at most `max_chars` (600; 100–2000) characters, chosen from the newest `window` (200; 1–1000) index rows with Neural State weighting (read-only). No model call; records `memory.injected`. Empty = off. The chat server reads it at start-up. `STIRA_ASSOCIATIVE_RECALL` overrides `enabled`. `"emotion_proximity": true` (default `false`) adds the emotion-proximity signal (#364, ADR 0017; `STIRA_RECALL_EMOTION_PROXIMITY` overrides it). [associative-recall.md](associative-recall.md) |
| `memory_autonomy` | `{}` | ADR 0018 **phase 0 (observe only)**. `{}` / absent = off (no `memory_rate` tool, no memory job kind, nothing bound). `{"phase": 0}` turns it on: `in_turn_rating` (default `true`) offers `memory_rate` (the agent rates its turn's episode; the rating is stored, nothing uses it yet); `jobs` (`memory_rate_backfill` / `memory_consolidate` / `memory_tidy`, each default `false`) lets a person create those scheduler jobs, which write plan files only (the model-calling kinds start only while the heartbeat is healthy, ADR 0021 §8); `daily_token_budget` (100000; 0–100000) caps all memory jobs together; `undo_window_days` (30; stored for phase 2). Only phase `0` is accepted. Read at start-up; `STIRA_MEMORY_AUTONOMY=0` forces it off. [memory-autonomy.md](memory-autonomy.md) |
| `containment` | `{}` | ADR 0023 **phase 0 (observe only)**. `{}` / absent = off. `{"phase": 0}` counts the containment signals (refusals and holds the gate already makes: secret-path reads, floor writes, own-dashboard calls, egress holds, bursts, reach-outs; text cues at weight 0) in a sliding window and reports the stage the session **would** be in (`open` / `restricted` / `closed` / `quarantine`). Nothing is enforced: no tool is removed, no card added, no level moved. `window_seconds` (600; 60–3600). Only phase `0` is accepted. Read at start-up; `STIRA_CONTAINMENT_OBSERVE=0` forces it off. [containment.md](containment.md) |
| `taint` | `{}` | ADR 0022 **phase 0 (observe only)**. `{}` / absent = off. `{"phase": 0}` labels each turn's context (trusted / untrusted, sensitive; counts per source) and counts the egress calls a flow layer **would** hold (F1–F4 codes). Nothing is enforced: no prompt, tool result or gate decision changes. Only phase `0` is accepted. Read at start-up; `STIRA_TAINT_OBSERVE=0` forces it off. [taint.md](taint.md) |
| `tool_config` | from `tool-config.json` | Embedded tool rules; the separate file is the one the server uses (section 5). |
| `hb_session_mode` | `"fresh"` | Stored and shown only (#272). |
| `hb_task_policy` | `{}` | Stored and shown only (#272). |
| `autonomy_boundary` | `require_confirmation` for `pr_create`, `merge_main`, `destructive`, `external_send` | Stored and shown only; **not enforced** at run time (`TODO(#67)` in `core/engine.py`, #272). |
| `stira_config_overrides` | `null` | Overrides of `StiraConfig` fields. **All but two reach the chat server's engine**: see [`stira_config_overrides` and the engine](#stira_config_overrides-and-the-engine-272). |
<!-- agent-fields:end -->

### `stira_config_overrides` and the engine (#272)

Every `StiraConfig` field is validated, saved and shown by `GET /api/config` → `stira_config`.
The chat server (`stira_server`) builds its own engine config; the values a human saved are
the values the engine runs on, except the two fields in the last row. `GET /api/config` →
`stira_config_apply` lists, per field, the saved value, the running engine's (`effective`),
`applies` and a `status`. The delivery table below decides first; equal values never make a
`never` field `applied`. `next_turn` / `restart` fields are `applied` when equal, else
`pending`, or `failed` (with `reason`) when the last threshold apply failed and will keep
failing with these saved values; `never` fields are `not_applied` (set in the overrides) or
`server_fixed` (not set: the server chooses); `saved` when no engine is attached, as on the
observer console.

| Fields | Applies | Notes |
|---|---|---|
| `stability_warning`, `stability_critical`, `bc_threshold`, `stability_warning_exit`, `stability_critical_exit`, `bc_exit_threshold` | next turn | Copied onto the running engine before each turn **as one set** (published as one whole config, so a reader never sees an entry with the old exit; the settings hub edits each entry together with its exit, #227/#471: raising an entry to or past its exit raises the exit with it on the page), so a pair that is valid in the file (an entry above the default exit, saved with its exit) is never half applied. Also set when the server starts. |
| `bc_window_size`, `decay_min_interval_sec`, `critical_cooldown_turns`, `sycophancy_threshold`, `sycophancy_window_size`, `protected_layers`, `hitl_timeout_sec`, `hitl_max_edit_rounds` | restart | Read once at start-up (#272). The settings hub edits all but `protected_layers` under **Advanced** (#471): the page rounds a typed value into the limits below and says so (except `sycophancy_threshold`, whose top end is the dullest detection: shown as an error instead); the server refuses a value outside them. A value that switches the sycophancy alert practically off (`sycophancy_threshold` ≥ 0.95, or `sycophancy_window_size` < 10: the engine scores only with 10 answers in its window) needs the typed confirmation, like the breaker floors. The hub also has upper limits the engine leaves open (#471 review M-1, measured on the engine): `bc_window_size` and `sycophancy_window_size` ≤ 200 (with no cap the whole history is the window, so a swing or a run of agreeing answers is seen later the longer the calm; at 200 a swing still trips the coherence line within 11 turns, the sycophancy alert comes within 82 agreeing answers), `critical_cooldown_turns` ≤ 50, `hitl_max_edit_rounds` ≤ 10. Above them the server refuses (400 `out_of_range` with `max`, also on rollback); a value already saved above a limit is kept and only checked when that field is changed, so other saves still work. `protected_layers`, `critical_action` and `blocked_tool_names` are shown there, read-only (with no running engine to ask: the server's own choice, e.g. `critical_action` `halt`). |
| `identity_anchors` (the file's own key, not an override) | restart | Read at start-up (#152). |
| `critical_action`, `blocked_tool_names` | never | The server pins `critical_action="halt"` and adds every write-capable tool to `blocked_tool_names`: a separate decision, not applied from the file. `status` is `server_fixed` unless the overrides set the field, then `not_applied` (also when the saved value happens to equal the running one). |

The safety valves are the validation every save and every load goes through, unchanged by
delivery: `critical_cooldown_turns` ≥ 3, `bc_window_size` ≥ 5 ([coherence.md](coherence.md)),
each exit threshold above its entry, `critical` below `warning`, `hitl_timeout_sec` > 0 and at most 24 h (86400 s),
`hitl_max_edit_rounds` ≥ 0, `sycophancy_window_size` ≥ 1. **`protected_layers` must keep layers
3, 4 and 5** (the Neural State architecture protection); any other positive layer may be
added, which only protects more. A settings save without 3, 4 or 5 in it is refused. A hand-edited
`agent-config.json` that lacks them does not stop the server: the load **adds the missing
layers**, logs one warning per process (`… did not hold layers 3-5; they were added
[protected_layers_clamped]`, no value), and sets `protected_layers_clamped: true` on
`/api/gateway/status`, `/api/config` and the settings hub state (additive; `false`
otherwise; read from the file, so the next save, which writes the completed list, clears it).
Other invalid values (not a list of positive integers) still stop the load.

**`hitl_timeout_sec`** (how long an approval waits) is bounded before it reaches an approval:
over 24 h it is capped at 86400 s; a hand-edited value that is not a positive finite number
(0, negative, NaN, a string, `null`, `true`) is replaced by the default 120 s. Either way the
server starts, logs one warning per process (`[hitl_timeout_clamped]`, no value) and sets
`hitl_timeout_clamped: true` on `/api/gateway/status`, `/api/config` and the settings hub
state (the file is read for the flag; the next save clears it). `stira_config_apply` and
`stira_config` show the bounded value. A save or direct construction with such a value is
refused.

**Breaker entry/exit pairs** (#512): when the six thresholds of a hand-edited (or
other-version) file break a pair rule (an exit at or under its entry, `critical` at or above
`warning`, the critical exit at or above the warning exit; an omitted value counts as its
default), the settings API no longer answers 503 and the server still starts. The load runs
all six on the defaults, logs one warning per process (`[breaker_pairs_reset]`, no values) and
sets `breaker_pairs_reset: true` on `/api/config` and the settings hub state. The breaker card
shows the file's values (an omitted one at its default) with a note, and its apply status is
`failed` (`reason: "breaker_pairs_reset"`). On each turn the running engine takes, per field,
the stricter (higher) of the value it runs and the default (higher is stricter for all six:
a level is entered below its entry and left only above its exit, for stability and bcScore
alike), so it is never looser than the defaults nor than what ran (after a restart: the
defaults). A save of anything else (the
details, the write roots, the other file, the model) keeps the file's six values as written;
only saving a valid set (moving any threshold checks all six) ends it. A `null`, string,
boolean or a value outside 0–1 is not replaced and still stops the load. A save or direct
construction still refuses an inverted set.

At start-up the server prints
one `WARNING` naming (not valuing) the saved fields in the `never` row.

### Identity anchors (#152)

- **Defaults.** Without an `identity_anchors` key the built-in neutral list
  (`DEFAULT_IDENTITY_ANCHORS`, three items) is used. A key that is present always wins,
  also when it repeats the built-in list. The chat server reads the file **at start-up**;
  an edit applies after a restart. Its start-up banner says `Identity anchors: default (3)`,
  `configured (N)` or `configured (3, same as built-in)`; without the key it also prints a
  one-time warning with the fixed code `identity_anchors_default` (no path).
- **Upgrading from a version before #152.** That version had a different, persona-specific
  built-in list, and an install whose `agent-config.json` has no `identity_anchors` key
  switches to the neutral list. To keep the old anchors, save them **before** upgrading,
  with the old version still installed, and merge them into `agent-config.json`:

  ```sh
  python -c "import json; from stiracore.core import StiraConfig; print(json.dumps({'identity_anchors': StiraConfig().identity_anchors}, ensure_ascii=False, indent=2))"
  ```

  Put the printed `identity_anchors` entry into the chat server's
  `.local/stira/agent-config.json` (create the file if it does not exist; keys you do not
  write keep their defaults) and restart. Already upgraded: the old list is
  `StiraConfig.identity_anchors` in `src/stiracore/core/engine.py` of the version you ran
  before. There is no CHANGELOG file; this section is the change note.
- **What the anchors are.** Text injected into the system prompt at WARNING / CRITICAL /
  COHERENCE. They declare; they enforce nothing. In particular the third default
  ("… change only with human approval") is a statement in the prompt: what keeps the agent's
  tools away from the SOUL file and the configuration files in the chat server is the write
  floor ([soul-read-only.md](soul-read-only.md), ADR 0010), not the anchor.
- The observer (`dashboard/app.py`) shows the anchors of an attached chat agent as running;
  otherwise the saved values, labelled as applying at the next start of the chat server.

## 5. `tool-config.json` (tool rules)

One rule per registry tool — **Always**, **Ask** or **Never** — edited on
`/settings#sec-tools`. A genuinely new install gets safe rules: reads Always, local writes
Ask, external sends and exec tools Never. The file carries `schema_version: 2` and a
`generation`; `tool-config.json.head` holds the last generation, a digest and the full
rules; a file that does not check out loads **locked** (every tool Never) until a human
saves with the recovery word ([operations.md](operations.md#4-tool-rules-are-locked)).
Details: [ADR 0010](adr/0010-tool-policy-gate-stack.md).

**Profiles (#450).** The same page offers three fixed tables — *Minimal* (reads Always,
everything else Never), *Development* (reads Always; `file_write`, `editor`, `journal`,
`lsp_*` and `shell` Ask; `page_check` Ask (when a browser is found; YOLO never approves
it); `propose_procedure` Ask (#463: it stages a procedural rule for the review screen; Ask or
Never only, YOLO never approves it; an install whose saved rules predate it reads it as Never
until it is switched on; approving a proposal on the review screen needs the breaker at
NORMAL, rejecting works at any level); network and the other exec tools Never) and *Research*
(Development plus `web_fetch` / `web_search` / `perplexity_search` Ask). Choosing one
shows which tool becomes Always / Ask / Never (and that `shell` needs
`STIRA_ENABLE_EXEC_TOOLS`) before anything changes; "Put in draft" then Save applies it like
any other change (a loosening needs the confirmation, the save is a `settings-history` row
with `source: "profile:<id>"`). Tools outside the table (MCP …) and the shell allowlist keep
their values. No profile is applied on install or update: an existing `tool-config.json` is
never rewritten. **YOLO approves only Ask tools**: Always never asks in the first place and
Never does not run during YOLO either ([yolo-quickstart.md](yolo-quickstart.md)).

## 6. Data directory (`<repository>/.local/stira/`) — *checked*

With `STIRA_DATA_DIR` set, the same files are in that directory instead (#272).

Created with owner-only permissions where the store sets them (`0700` directories, `0600`
files for the approval store, scheduler log, checkpoints and EventStore). Everything here
can contain **private conversation data**; do not commit or share it. "Floor" = on the
agent's write floor (`file_write` / `editor` refuse it, no approval lifts it). The whole
data directory is on the floor
(#271) — including files
that are not listed here and new files — except three subdirectories: `workspace/` (the
default `@workspace`), `memory/agents/<agent>/memory/episodic/` and `context-stash/`.
"Agent can read" = whether the file tools (`file_read`, `view_image`, `image_reader`,
`editor` `view`, `lsp_*`) may read it: the data directory is on the read floor too
(#312, ADR 0010 addendum),
with the same three exceptions, approved or not; outside it file tools read only inside
`read_roots` (default: the workspace only), never on the secret-path floor (#457).
Stop the server before moving or deleting anything.

<!-- data-files:begin -->
| Path | Written by | Contents | Floor | Agent can read (#312) | Delete / move aside? |
|---|---|---|---|---|---|
| `gateway-config.json` | settings hub, by hand | `GatewayConfig` | yes | no | Deleting = defaults. |
| `agent-config.json` | settings hub, by hand | `AgentConfig` | yes | no | Deleting = defaults. |
| `tool-config.json` | settings hub | tool rules, generation | yes | no | No: deleting it (with a head or history present) locks every tool Never. |
| `tool-config.json.head` | settings hub | last generation, digest, full rules | yes | no | No: missing head = locked. |
| `tool-config.json.locked` | rule loader | lock marker | yes | no | Deleting does not unlock; only a recovery save does. |
| `settings-history.jsonl` | settings hub | change history (field-level rollback) | yes | no | Keep (audit trail, rollback source). |
| `tool-policy-approvals.json` | rule reader | rule generation the open approvals were asked under | yes | no | Keep. A mismatch revokes every open approval. |
| `hitl-approvals.db` | `ApprovalStore` | hash-chained approval requests and decisions (masked arguments; an EDIT replacement without secrets until it is consumed) | yes | no | Only together with `-wal`, `-shm` and `.head`: a fresh store, every pending approval lost. |
| `hitl-approvals.db.head` | `ApprovalStore` | head anchor of that chain | yes | no | Never alone: a missing head makes the store unhealthy (fail-closed). |
| `hitl-operations.db` | `OperationLedger` | approved side effects already performed | yes | no | Never roll back or delete while checkpoints may be replayed. |
| `neural-state.json` | `StatePersistence` | 6-dim state, `last_decay_check` | yes | no | Deleting resets the state to the baselines. |
| `memory/agents/<agent>/memory/` | `MemoryStore` | `episodic/`, `semantic/` (incl. `staging/`), `procedural/` Markdown + `index.sqlite3` | yes, except `episodic/` (also written by `memory_write`) | no, except `episodic/` | Markdown is the source of truth (back it up); the index is rebuildable. |
| `memory/agents/<agent>/memory/forgotten/` | `--apply-forgetting`, `stira memory retention --apply` / `forget --apply` (#294) | archived originals of forgotten Semantic entries and `removed-<id>.json` records of retention runs (full text: may hold secrets), `delete-<id>.json` delete-job states (no text) and the hash-chained `forgetting-log.jsonl` | yes | no | Only to delete a memory for good (also its records, see operations.md §12); otherwise undo is no longer possible. |
| `memory/agents/<agent>/memory/emotion/` | `record_episode` (#364) | one JSON per v2 episode: post-turn Neural State (3 decimals), category, intensity, breaker, turn, UI turn id; bound to the episode by key + body hash. `emotion-log.jsonl` next to it logs orphan deletions | yes | no | Deleting an episode with `MemoryStore.delete` deletes its record; orphans: `python -m stiracore.control.emotion_cli --base-dir <memory> orphans [--apply]`. Deleting all of it only loses the snapshots ([episodic-emotion.md](episodic-emotion.md)). |
| `authority-epoch` | `stira_server` (the authority service, ADR 0011 §5, [authority-service](authority-service.md)) | the authority epoch: the newest 16 records `seq`, `epoch`, hash of the previous record, own hash (no incarnation) — `0600`, written durably (flush, rename, directory flush) | yes | no | Do not edit: an unreadable, unsafe or mismatched file makes the server unhealthy (no permit, YOLO approves nothing automatically). Repair only for a damaged epoch file (never for a latch store: that is `reset-latches`): with the server stopped keep a copy, remove it, run `stira authority reset-latches`, start, unlatch. |
| `authority-latches` | `stira_server` (the authority service, ADR 0011 W2, [authority-service](authority-service.md)) | the stop latches `kill`, `unhealthy`, `halted:<reason>` with their stop ids (incarnation, epoch); the newest 64 records, chained (`seq`, previous hash) — `0600`, written durably | yes | no | Do not edit or remove: a latch is cleared only by `stira authority unlatch` with the server stopped. An unreadable, unsafe, mismatched, missing or rolled-back file (checked against the commitment in `authority-epoch` and the lifecycle log) counts as every latch set (killed, unhealthy); repair: `stira authority reset-latches` (every latch set), then `unlatch`. |
| `authority-lifecycle.jsonl`, `authority-lifecycle.1.jsonl` | the authority service (W2) | hash-chained `lifecycle_event` lines: codes and ids only (subject, from, to, reason code, incarnation, epoch, time) — `0600`, flushed per line; moved to `.1` when an append would pass 1 MiB (the older `.1` goes) | yes | no | Keep (the stop record; it is also checked against the latch store at start). A damaged file makes the server unhealthy; repair: authority-service.md "Repairs". |
| `authority-outbox.json` | the authority service (W2) | the notifications (one per subject + incarnation + epoch + to, at most 256, older unread ones coalesced) with unread / acknowledged, the notified and acknowledged event ids — `0600`, atomic replace | yes | no | Removing it rebuilds the notices from the lifecycle log as unread; a damaged file makes the server unhealthy. |
| `authority-kill-armed`, `authority-kill-fired` | the authority service (W2) | an empty marker made at start and renamed by the kill switch (a second kill record that needs no free blocks) | yes | no | Do not remove `authority-kill-fired`: `stira authority unlatch kill` does. A fired marker without the kill latch makes the next start refuse the latch store. |
| `authority-watchdog.json` | the authority watchdog (W2) | its last pass time (the gap since is recorded as `gateway_down` at start) — `0600` | yes | no | Removing it only skips the next gap record; a damaged file makes the server unhealthy. |
| `grants.json` | `stira_server` (only with gateway `autonomy.stage_c`, ADR 0011 W4, [grants](grants.md)) | the goal grants: the newest 4 chained records (`seq`, previous hash), each the whole state; the **goal text lives only here**; a live grant is always written as `revoked(restart)` (never as active) — `0600`, written durably | yes | no | Do not edit: an unsafe, unreadable, malformed or changed file makes the grant stage unhealthy (every grant revoked, nothing issued; the file is never overwritten). Repair: with the server stopped keep a copy, move it aside, start (every grant is gone; issue a new one). |
| `checkpoints/` | `FileCheckpointStore` | per batch: Neural State, conversation, pending tool calls, engine state (20 per thread) | yes | no | Deleting one makes a resume that needs it refuse and cancel the approval. |
| `context-stash/` | context management (#258) | masked originals of offloaded tool results | no (it must be inside the write roots, ADR 0005) | yes | Yes; references become unreadable. Old entries go after `retention_hours` (24). |
| `artifacts/` | `WorkspaceArtifacts` (#454, ADR 0020) | `index.db` (per version: relative path, turn id, create/update, size, SHA-256, kind, time; a person's labels) and `blobs/` (content-addressed snapshots of small versions) | yes | no | Yes; the artifact list, its versions and labels are lost (workspace files are not touched). |
| `turns.db` | `TurnEventStore` | UI turn events (masked previews); the source of `/observability` too (#286) | yes | no | Yes; the turn history in the UI and `/observability` is lost. |
| `events.db` | `SQLiteEventStore` | hash-chained audit events (the chat server writes `/new` only) | yes | no | Only together with `-wal`, `-shm` and `.head`. |
| `events.db.head` | `SQLiteEventStore` | head anchor | yes | no | Never alone (unhealthy). |
| `scheduler-events.jsonl` | scheduler (`STIRA_SCHEDULER=1`) | hash-chained jobs, runs, notifications (masked) | yes | no | Only together with `.head`: no jobs afterwards. |
| `scheduler-events.jsonl.head` | scheduler | head anchor | yes | no | Never alone (log unavailable). |
| `server.lock` | `stira_server` (#285) | OS owner lock of the whole data directory (PID note); a second server refuses to start | yes | no | Harmless when stopped; the kernel holds the lock. |
| `scheduler.lock` | scheduler | OS owner lock (PID note) | yes | no | Harmless when stopped; the kernel holds the lock. |
| `heartbeat.lock` | heartbeat (`hb_enabled`) | OS owner lock (PID note) | yes | no | Harmless when stopped. |
| `stira_server.pid` | `stira_ctl` | PID of the started server | yes | no | Yes, when stopped. |
| `yolo-host-channel.json` | `stira_server` (only with the YOLO allowance, ADR 0012 §8b) | where the host command finds the server's host-confirmation socket (path, PID; the socket itself is in a `0700` temporary directory) | yes | no | Removed at a clean stop; harmless when stopped. |
| `yolo-window.json` | `stira_server` (only with the YOLO allowance, ADR 0012 §11) | the YOLO window marker: window id (random), incarnation reference, `open` / `closed` / `closed_by_unlatch` — written durably (flush, rename, directory flush) | yes | no | Do not delete or edit: an `open` marker is what keeps an unclean end latched across a restart. A malformed or symlinked one latches too. |
| `workspace/` | the agent's file tools | the default `@workspace` (#271): files the agent writes with relative paths | no | yes | Yours: back it up like any work folder. |
<!-- data-files:end -->

Outside the data directory: `.local/stira.out.log` / `.local/stira.err.log`
(`stira_ctl.sh` output) and `STIRA_CHAT_DATA_DIR` (chat bots).

## Output cap and cut-off turns

`max_output_tokens` in `gateway-config.json` is the per-call output cap (`max_tokens` for the
provider). There is no environment override.

- **Automatic (`null`, the default).** The cap is `min(32000, the model's documented output limit)`
  taken from the capability table (`providers/capabilities.py`, `max_output`); the 5.x Claude
  families document 128K, the 4.x rows carry the lowest limit the family shipped with. The
  OpenAI rows `gpt-5` / `gpt-5-mini` / `gpt-5-nano` (and dated snapshots) record 128K,
  `o3` / `o3-mini` / `o4-mini` 100K, `gpt-4.1` / `gpt-4.1-mini` 32,768; Gemini
  `gemini-2.5-pro` / `gemini-2.5-flash` 65,536 (model pages, 2026-10). Only exact IDs: a family
  glob would also match variants with a lower limit (`gpt-5-chat-latest`: 16,384).
- **Thinking shares the cap (#416).** Claude extended/adaptive thinking and OpenAI/Gemini
  reasoning tokens count toward the same `max_tokens` (measured in #396: a Sonnet 5.5 call cut
  at the cap held a reasoning block only; Opus 5.5 on default effort thought for ~12 minutes
  and hit 64000 with no reply). Bedrock's adaptive thinking has no separate budget
  (`thinking.budget_tokens` is refused there), so the automatic cap gives such rows a
  `thinking_reserve` (32000) on top of the reply's 32000: `min(64000, limit)` — Claude 5.x and
  4.x Sonnet/Haiku 64000, Opus 4/4.1 32000 (its limit). Rows without thinking keep 32000.
- **No recorded limit (#416).** The automatic cap is the provider's default
  (`unknown_limit_max_output`): `8192` for hosted APIs (Bedrock, Anthropic, OpenAI and
  OpenAI-compatible servers, Gemini, LiteLLM, ...), because they refuse a request whose cap is
  above the model's own limit — a larger guess would turn every turn into an API error; `32000`
  for local `llamacpp` / `ollama`, whose servers accept a large cap (generation ends at the
  context window) and do not bill. The settings page says so in automatic mode ("Reasoning
  models can use this up on thinking alone. If you know the model's limit, set a number").
  A model switched in the settings page takes the cap resolved for the new model. To pin the cap for a deployment, set `max_output_tokens` itself (settings page, "Output limit and cost", or the file; applied from the next turn — the cut-off note in the chat links there);
  `options.capabilities.max_output` in a `ModelSpec` only affects code that builds the spec
  and calls `resolve_max_output_tokens` (library use) — there is no config-file path for it.
- **A number** is used as given. It is a ceiling you set on purpose: Stira does not check it
  against the model's limit, and a provider rejects a request above its own limit.
- **Cost.** The cap is a ceiling, not a purchase: output is billed for the tokens actually
  produced, and a cap does not enter the prompt, so prompt-cache prefixes (tools → system →
  messages) are unchanged by it. Two things do change with a higher cap: a runaway answer can
  now cost up to the cap, and Bedrock counts `max_tokens` against the account's tokens-per-minute
  quota when a request starts, so a very high cap lowers how many requests fit in the quota.
  With the thinking reserve, a Claude 5.x call reserves input + 64000 at its start (it was
  input + 32000); unused tokens are returned when it ends, and only produced tokens are billed.
- **A turn that reaches the cap** ends with `stop: "max_tokens"` and is shown in the chat and the
  Stage with the reason ("The output reached its limit and was cut off. Raise the limit or ask
  in smaller parts."). The Strands event loop raises before it runs any tool, so a tool call
  cut off mid-arguments (a half-written `file_write`) is **not executed**, even when the rule
  for the tool is Always; the partial message stays in the history with the tool call replaced
  by a note, and the next message works normally. When the cut call had started neither a reply
  nor a tool call (its message holds a reasoning block only, or nothing), the reason says so:
  "The output limit (N tokens) was reached before the reply began. With models whose thinking
  counts toward the limit, thinking alone can use it up. Raise the limit." — the SSE `error`
  carries `cut: "before_reply"` and `limit`, `turn.completed` carries `cut` (#416). (`tests/test_max_output_tokens.py`,
  `tests/test_headless_file_tools_e2e.py`.)
- **Usage of a failed call.** Bedrock bills a call that ends in `max_tokens`. Its
  `metadata.usage` is recorded in `turn.completed.usage` and the session totals. When no count
  exists, the turn carries `usage_reason` instead of a bare `null`: `E_USAGE_NOT_REPORTED`
  (the provider failed before reporting), `E_USAGE_CANCELLED` (the turn was stopped with
  **■ Stop** / `POST /api/chat/cancel` first) or `E_USAGE_INTERRUPTED` (the server stopped or
  restarted first). `GET /api/turns` lists `stop` and `usage_reason` next to `usage`.

## A turn outlives its page (#452)

A chat or Stage message runs on its own worker thread in the server; the page's SSE stream
(`POST /api/chat`) is only a view of it. Closing the tab, reloading, opening another page or
losing the connection **does not stop the turn**: it runs to its end, its reply is kept in
`/api/chat/history` and its events in turns.db (`GET /api/turns/<id>/events?after_seq=N`
continues where the page left off, without a gap). An idle stream sends an SSE comment every
15 s. A page that stops reading is dropped after 10,000 waiting frames (memory stays bounded;
the turn goes on).

How a turn ends (`turn.completed.stop`):

| `stop` | When | `error` |
|---|---|---|
| `completed` | The turn finished — with or without a page reading it | `false` |
| `cancelled` | **Only** an explicit stop: the chat page's **■ Stop** (`POST /api/chat/cancel`). The SDK stops at its next safe point (during the model's stream, before a tool, after a tool); a card of that turn waiting for a person is rejected — also one that appears after the press, until the turn has ended — so the stop never waits for `hitl_timeout_sec`. What the turn produced so far is kept in the history (`stop: "cancelled"`) | `true` |
| `interrupted` | The server stopped while the turn ran (`reason: "server_stopped"`), or a turn was found without an end at the next start — a crash or a kill (`reason: "server_restarted"`). The reply of such a turn is lost (resuming across a restart is #240) | `true` |
| `error` / `max_tokens` | As before | `true` |

- **Approvals wait for their deadline, not for a page.** A card shown while no page is open
  stays pending until `hitl_timeout_sec` (default 120 s, at most 24 h); approving it on any
  page (or `/approvals`) after reconnecting lets the turn go on. Expiry and the server's stop
  are still rejections (fail closed).
- **One turn at a time**, as before (the turn lock, held from the send to the turn's end and
  released on every path, also when the turn fails). A second send while one runs gets the
  stream's `error` event "処理中です / Agent is busy" with `code: "turn_running"`; `/new` is
  refused with `turn_running`. The YOLO stop, `/new` and the running-tool-call refusal behave as
  before; the YOLO stop ends the window (later tool calls go to cards) but does not stop the turn
  itself — **■ Stop** does.
- The Stage's **■ Stop showing** only stops that page's reading (as its label says); the turn
  goes on.
- **Coming back.** A chat page opened (or reloaded) while a turn still runs says so ("An earlier
  turn is still running on the server…"), offers **■ Stop** for that turn (named by its id, so
  a stop never lands on a turn started meanwhile) and reads the history when it has ended.
  `GET /api/turns` marks the running chat turn `stoppable: true`; a turn the chat does not run
  (one resumed after an approval) is shown as running, without **■ Stop**.
