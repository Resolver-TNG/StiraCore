# Settings surface (#468)

What the settings page can change, what it cannot and why. One row per setting: every
`GatewayConfig`, `AgentConfig` and `StiraConfig` field and every environment variable of
[configuration.md](configuration.md) §1–§2.

**Source of truth:** [`src/stiracore/config/settings_surface.json`](../src/stiracore/config/settings_surface.json).
The tables below are generated from it (`python -m stiracore.config.settings_surface`
prints them); `tests/test_settings_surface.py` fails when a field or a variable is missing from
the manifest, when a class C row has no reason, or when this page differs from the manifest.
Adding a setting: add its row to the JSON, then paste the output between the markers.

日本語の要約: 設定ごとに「設定画面から変えられるか」「保存した値がいつ効くか」「どの環境変数が勝つか」を
1 行で持つ一覧です。正本は JSON で、この表はそこから生成します。新しい設定を足すと、分類しないかぎり
テストが落ちます。

## Columns

- **Class** — A: on the settings page (done) or planned (the Issue is named); B: on the page with a
  confirmation (a loosening, a cost or an outside destination); C: not on the page, with a reason;
  D: needs a decision.
- **UI** — `yes` / `partial` (part of it, or through another field) / `no`.
- **Applies** — `next_turn`, `restart`, `immediate` (read each time it is used), `reload` (the page
  is opened again), `on_run` (the command or job runs), `per_field` (see its own section),
  `never` (the server decides), `none` (not a server setting).
- **Variable that wins** — while it is set, the settings page shows the field as pinned (the
  variable's **name**, never its value) and disables its input.
- **Reasons (C)** — `bootstrap` (needed before the page exists), `exposure` (widens who can reach
  the server), `secret`, `layer3` (Layer 3+, a person's hand edit only), `server_fixed`,
  `integrity` (rollback protection of the tool rules, ADR 0010), `loosening` (the widest
  loosenings stay a hand edit), `adr_0012` (YOLO variables; their config twins are under
  `gateway.yolo`), `stored_only` (nothing uses it yet), `single_choice`, `os_provided`,
  `upstream_only`, `dev`.

## What the settings page shows (#469)

`GET /api/config/hub` → `surface`:

- `fields["<kind>.<name>"]` — `class`, `ui`, `applies`, `group`, `issue`, `env` (the variable
  names that win), `pinned_by` (the one that is set, or `null`), `source` (`env` / `agent` /
  `gateway` / `default`) and `pending_restart`. `source` follows `resolve_config`: a variable that
  overrides the agent field itself (`STIRA_PROVIDER`, `STIRA_MODEL`) wins; otherwise a set agent
  override wins — `provider_override` brings `base_url` and `api_key_env` with it (even when they
  are empty), so `STIRA_BASE_URL` / `STIRA_API_KEY_ENV` count only without it; then the gateway.
- `pinned` — the variables that are set and pin a setting: `name`, `fields`, `applies`. The
  `STIRA_YOLO_*` variables (reason `adr_0012`) are left out of the hub: with the allowance set,
  the existing read APIs stay byte-identical (ADR 0012); the YOLO section shows its own pins.
- `restart_pending` / `restart_pending_groups` — restart fields saved since the server started
  (compared with the saved values it started with; a field a variable pins never counts),
  one entry per change in the groups list. The page shows "N changes wait for a restart". A
  `per_field` row can name the keys read at start-up (`restart_keys`: `gateway.ui` → `home`;
  `board_svg` is read on each Stage load and never waits).
- `location` — the configuration file names (no path), whether they sit in one directory and in
  the data directory (`STIRA_DATA_DIR` or the default), whether the tool-rule anchor
  (`STIRA_TOOL_RULES_ANCHOR_DIR`) is set and whether the tool rules are locked.
- `available` — `false` when these facts could not be built (a broken manifest, an unreadable
  file): the rest is empty, the hub still answers and the page still saves; the server log has one
  warning with the error type only.

No environment value is returned or shown: a value is read only to decide whether the variable
is set. A field the page edits can carry `data-surface-field="<kind>.<name>"`: the page then
disables it while a variable pins it and says which one.

## Tables

<!-- settings-surface:begin -->
### Environment variables (server)

| Setting | Class | UI | Applies | Overrides | Reason / note |
|---|---|---|---|---|---|
| `STIRA_PORT` | C | no | restart | `gateway.port` | `bootstrap` |
| `STIRA_HOST` | C | no | restart | — | `exposure` |
| `STIRA_ALLOW_REMOTE` | C | no | restart | — | `exposure` |
| `STIRA_ALLOWED_HOSTS` | C | no | restart | — | `exposure` |
| `STIRA_ALLOWED_ORIGINS` | C | no | restart | — | `exposure` |
| `STIRA_TOOL_RULES_ANCHOR_DIR` | C | no | restart | — | `integrity` State shown read-only in the location card. |
| `STIRA_MODEL` | A | partial | restart | `gateway.model_id`, `agent.model_override` | The page sets the same value through model_override; while set, the model field is pinned. |
| `STIRA_PROVIDER` | A | partial | restart | `gateway.provider`, `agent.provider_override` | — |
| `STIRA_BASE_URL` | A | partial | restart | `gateway.base_url` | — |
| `STIRA_API_KEY_ENV` | A | partial | restart | `gateway.api_key_env` | — |
| `AWS_REGION` | A | no | restart | `gateway.region` | #476 |
| `ANTHROPIC_API_KEY` | C | no | next_turn | — | `secret` |
| `OPENAI_API_KEY` | C | no | next_turn | — | `secret` |
| `GEMINI_API_KEY` | C | no | next_turn | — | `secret` |
| `GOOGLE_API_KEY` | C | no | next_turn | — | `secret` |
| `MISTRAL_API_KEY` | C | no | next_turn | — | `secret` |
| `WRITER_API_KEY` | C | no | next_turn | — | `secret` |
| `LLAMA_API_KEY` | C | no | next_turn | — | `secret` |
| `AWS_ACCESS_KEY_ID` | C | no | none | — | `secret` |
| `AWS_PROFILE` | C | no | none | — | `secret` |
| `AWS_WEB_IDENTITY_TOKEN_FILE` | C | no | none | — | `secret` |
| `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` | C | no | none | — | `secret` |
| `AWS_CONTAINER_CREDENTIALS_FULL_URI` | C | no | none | — | `secret` |
| `AWS_SHARED_CREDENTIALS_FILE` | C | no | none | — | `secret` |
| `AWS_CONFIG_FILE` | C | no | none | — | `secret` |
| `HOME` | C | no | none | — | `os_provided` |
| `LANG` | C | no | none | — | `os_provided` #167 Passed on to run_checks runs (ADR 0019) when it looks like a locale name. |
| `LC_ALL` | C | no | none | — | `os_provided` #167 Passed on to run_checks runs (ADR 0019) when it looks like a locale name. |
| `USERPROFILE` | C | no | none | — | `os_provided` |
| `STIRA_HEARTBEAT` | A | no | restart | `gateway.hb_enabled` | #472 |
| `STIRA_SCHEDULER` | B | no | restart | — | #475 No config field yet; #475 adds one and keeps the variable as an override. |
| `STIRA_ACCESS_LOG_POLLING` | C | no | restart | — | `dev` |
| `STIRA_STAGE` | A | no | restart | — | #477 No config field yet; #477 adds one and keeps the variable as an override. |
| `STIRA_PROMPT_CACHE` | A | no | restart | `gateway.prompt_cache` | #470 |
| `STIRA_PROMPT_CACHE_TTL` | A | no | restart | `gateway.prompt_cache_ttl` | #470 |
| `STIRA_CONTEXT_MANAGEMENT` | A | no | restart | `gateway.context_management` | #470 |
| `STIRA_MEMORY_MAP` | A | no | restart | `gateway.memory_map` | Memory Map (docs/memory-index.md); 0 turns it off. |
| `STIRA_CONTEXT_PROFILE` | A | no | restart | `gateway.context_profile` | #368 An unknown name is ignored (the file's profile is used). |
| `STIRA_HANDOFF_INJECT` | A | no | restart | `gateway.handoff_inject` | Handoff notes (docs/handoff.md): 0 / false / no / off turns the tools and the section off; 1 turns them on. |
| `STIRA_TASK_MODEL_TOOL_SUMMARY` | A | partial | restart | `gateway.task_models` | #585 Model per task (docs/task-models.md): shown read-only under Output limit and cost. |
| `STIRA_TASK_MODEL_CONTEXT_SUMMARY` | A | partial | restart | `gateway.task_models` | #258 Model per task (docs/task-models.md): shown read-only under Output limit and cost. |
| `STIRA_EPISODIC_EMOTION` | A | no | immediate | `agent.episodic_emotion_snapshot` | #473 |
| `STIRA_ASSOCIATIVE_RECALL` | A | no | restart | `agent.associative_recall` | #473 |
| `STIRA_RECALL_EMOTION_PROXIMITY` | A | no | restart | `agent.associative_recall` | #364 Overrides associative_recall.emotion_proximity only (ADR 0017); needs associative recall on. |
| `STIRA_MEMORY_AUTONOMY` | A | no | restart | `agent.memory_autonomy` | #251 0 / false / no / off forces memory autonomy off (ADR 0018); no value turns it on. |
| `STIRA_CONTAINMENT_OBSERVE` | A | no | restart | `agent.containment` | #460 0 / false / no / off forces the containment observation off (ADR 0023 phase 0); no value turns it on. |
| `STIRA_TAINT_OBSERVE` | A | no | restart | `agent.taint` | #458 0 / false / no / off forces the taint observation off (ADR 0022 phase 0); no value turns it on. |
| `STIRA_RUN_CHECKS` | C | no | restart | `gateway.run_checks` | `loosening` #167 0 / false / no / off forces run_checks off whatever gateway.run_checks says (ADR 0019); no value turns it on. |
| `STIRA_ENABLE_EXEC_TOOLS` | C | no | restart | — | `loosening` The page shows the state and how to turn it on. |
| `STIRA_BROWSER_PATH` | C | no | restart | — | `bootstrap` #449 The executable the page_check tool starts (read when the tools are registered at start-up); a hand edit only, so the page cannot choose a program the server runs. |
| `STIRA_YOLO_ALLOW` | A | partial | restart | `gateway.yolo` | `adr_0012` The config twin gateway.yolo.allow is on the settings page (#444); the variable wins. Unset (and no config value): external, the button shown, nothing approved before a press (ADR 0012 addendum 2026-10-24); off hides it. |
| `STIRA_YOLO_MAX_AUTO_APPROVALS` | A | partial | per_field | `gateway.yolo` | `adr_0012` |
| `STIRA_YOLO_MAX_EXTERNAL` | A | partial | per_field | `gateway.yolo` | `adr_0012` |
| `STIRA_YOLO_MAX_EXEC` | A | partial | per_field | `gateway.yolo` | `adr_0012` |
| `STIRA_YOLO_HOST_CONFIRM` | A | partial | restart | `gateway.yolo` | `adr_0012` |
| `STIRA_PROGRESS_REPORTS` | B | no | restart | `gateway.progress` | Default of the YOLO dialog's progress tick (ADR 0014). |
| `STIRA_PROGRESS_INTERVAL_MIN` | B | no | restart | `gateway.progress` | — |
| `STIRA_PROGRESS_CONTINUE_AFTER_MIN` | B | no | restart | `gateway.progress` | — |
| `STIRA_PROGRESS_MAX_AUTO_CONTINUES` | B | no | restart | `gateway.progress` | — |
| `STIRA_PROGRESS_COUNT_TOOLS` | B | no | restart | `gateway.progress` | — |
| `STIRA_PROGRESS_COUNT_TOKENS` | B | no | restart | `gateway.progress` | — |
| `STIRA_PROGRESS_DECLARED` | B | no | restart | `gateway.progress` | — |
| `SSH_CONNECTION` | C | no | none | — | `os_provided` |
| `SSH_TTY` | C | no | none | — | `os_provided` |
| `BYPASS_TOOL_CONSENT` | C | no | none | — | `upstream_only` |
| `STIRA_WEB_SEARCH_ENDPOINT` | B | no | restart | — | #478 An outside destination: confirmed; a URL holding credentials is refused. |
| `PERPLEXITY_API_KEY` | C | no | restart | — | `secret` |
| `STRANDS_KNOWLEDGE_BASE_ID` | D | no | restart | — | #478 Opens an outside resource; rarely used. Recommended: later (C-leaning). |
| `STIRA_CHAT_DATA_DIR` | C | no | restart | — | `bootstrap` |
| `SLACK_BOT_TOKEN` | C | no | restart | — | `secret` |
| `SLACK_APP_TOKEN` | C | no | restart | — | `secret` |
| `DISCORD_BOT_TOKEN` | C | no | restart | — | `secret` |
| `WEBHOOK_SECRET_GENERIC` | C | no | restart | — | `secret` |
| `WEBHOOK_SECRET_GITHUB` | C | no | restart | — | `secret` |
| `WEBHOOK_SECRET_SLACK` | C | no | restart | — | `secret` |
| `STIRA_DATA_DIR` | C | no | restart | — | `bootstrap` Where the configuration files live; shown in the location card (name only). |
| `STIRA_STATE_PATH` | C | no | restart | `gateway.state_base_path` | `bootstrap` Observation console only. |
| `STIRA_GATEWAY_CONFIG_PATH` | C | no | restart | — | `bootstrap` Observation console only. |
| `STIRA_AGENT_CONFIG_PATH` | C | no | restart | — | `bootstrap` Observation console only. |
| `STIRA_TOOL_CONFIG_PATH` | C | no | restart | — | `bootstrap` Observation console only. |
| `TERM_PROGRAM` | C | no | none | — | `os_provided` |
| `__CFBundleIdentifier` | C | no | none | — | `os_provided` |
| `XPC_SERVICE_NAME` | C | no | none | — | `os_provided` |

### Environment variables (scripts and tests)

| Setting | Class | UI | Applies | Overrides | Reason / note |
|---|---|---|---|---|---|
| `STIRA_PYTHON` | C | no | none | — | `dev` |
| `STIRA_LIVE_MODEL` | C | no | none | — | `dev` |
| `STIRA_LIVE_BASE_URL` | C | no | none | — | `dev` |
| `STIRA_LIVE_TOOLS` | C | no | none | — | `dev` |
| `STIRA_REQUIRE_AXE` | C | no | none | — | `dev` |
| `STIRA_CONTRACT_REPORT` | C | no | none | — | `dev` |
| `STIRA_BROWSER_CHECK` | C | no | none | — | `dev` |
| `STIRA_REQUIRE_BROWSER` | C | no | none | — | `dev` |

### `gateway-config.json` (`GatewayConfig`)

| Setting | Class | UI | Applies | Variable that wins | Reason / note |
|---|---|---|---|---|---|
| `model_id` | A | partial | next_turn | `STIRA_MODEL` | Changed through agent.model_override on the page. |
| `max_output_tokens` | A | yes | next_turn | — | #470 Output limit and cost section: automatic (recommended) or a number. Resolved again before each turn with the model selection; a new cap rebuilds the model. |
| `region` | A | no | next_turn | `AWS_REGION` | #476 Part of the model selection taken before each turn. |
| `provider` | A | partial | next_turn | `STIRA_PROVIDER` | — |
| `base_url` | A | partial | next_turn | `STIRA_BASE_URL` | Loopback only from the page; a remote API endpoint is a hand edit (it would send the key elsewhere). |
| `api_key_env` | A | partial | next_turn | `STIRA_API_KEY_ENV` | — |
| `organization_env` | A | no | next_turn | — | #476 |
| `port` | C | no | restart | `STIRA_PORT` | `bootstrap` |
| `state_base_path` | C | no | never | `STIRA_STATE_PATH` | `stored_only` |
| `hb_interval_floor_sec` | A | yes | restart | — | #472 Heartbeat section, in minutes (1 min to 24 h when changed there; floor <= ceil). |
| `hb_interval_ceil_sec` | A | yes | restart | — | #472 |
| `hb_enabled` | A | yes | restart | `STIRA_HEARTBEAT` | #472 Heartbeat section, with the running status. A tick never calls a model (no token cost). |
| `prompt_cache` | A | yes | restart | `STIRA_PROMPT_CACHE` | #470 |
| `prompt_cache_ttl` | A | yes | restart | `STIRA_PROMPT_CACHE_TTL` | #470 |
| `context_management` | A | yes | restart | `STIRA_CONTEXT_MANAGEMENT` | #470 |
| `context_management_options` | A | yes | restart | — | #470 In the details fold; only values that differ from the code defaults are written. |
| `memory_map` | A | no | restart | `STIRA_MEMORY_MAP` | Memory Map in the recalled part of the prompt (docs/memory-index.md). On by default; an empty store adds nothing. |
| `memory_map_max_tokens` | A | no | restart | — | Size bound of the Memory Map section (100-1000, default 300; docs/memory-index.md). |
| `context_profile` | A | partial | restart | `STIRA_CONTEXT_PROFILE` | #368 Shown read-only under Output limit and cost; changed in the file or with the variable. |
| `tools_send_allowed_only` | A | partial | restart | — | #368 Shown read-only with the profile (small and tiny turn it on too). |
| `tools_short_schemas` | A | partial | restart | — | #368 Shown read-only with the profile (small and tiny turn it on too). |
| `jev_enabled` | A | yes | restart | — | — |
| `jev_classifier` | A | yes | restart | — | — |
| `notify_targets` | C | no | never | — | `stored_only` |
| `ui` | A | partial | per_field | — | #477 board_svg is on the page (read on each Stage load; on by default, written only when off); home is #477 (read at start-up, so a saved home waits for a restart). |
| `yolo` | A | yes | per_field | `STIRA_YOLO_ALLOW`, `STIRA_YOLO_MAX_AUTO_APPROVALS`, `STIRA_YOLO_MAX_EXTERNAL`, `STIRA_YOLO_MAX_EXEC`, `STIRA_YOLO_HOST_CONFIRM` | The YOLO section (#444); each value says when it applies and which variable wins. |
| `progress` | B | no | restart | `STIRA_PROGRESS_REPORTS`, `STIRA_PROGRESS_INTERVAL_MIN`, `STIRA_PROGRESS_CONTINUE_AFTER_MIN`, `STIRA_PROGRESS_MAX_AUTO_CONTINUES`, `STIRA_PROGRESS_COUNT_TOOLS`, `STIRA_PROGRESS_COUNT_TOKENS`, `STIRA_PROGRESS_DECLARED` | Progress reports in a YOLO window (ADR 0014, #451); no Issue yet. B: an automatic continue starts a model turn (cost), so raising it is confirmed. |
| `run_checks` | C | no | restart | `STIRA_RUN_CHECKS` | `loosening` #167 The verification runner, ADR 0019 phases 0-1: absent or {} = off (no tool registered); {"phase": 1} turns it on at the next start. Its rule is Ask by default; a saved Always is effective only on a verified macOS sandbox host (else read as Ask). stira doctor says how it would be contained. |
| `autonomy` | C | no | restart | — | `loosening` #296 ADR 0011 stage C (W4): {"stage_c": true} registers the goal-grant control plane at the next start (default off: no route, no file, no thread). run_checks_unattended (default true) counts only on a verified os_sandbox host. allow_browser_confirm (default false) accepts the weaker browser confirmation; otherwise issue / resume / verify need STIRA_YOLO_HOST_CONFIRM (dialog / command). W5: stage_c_exec (default false; needs stage_c) adds the goal iteration executor: a person's Run one iteration press runs exactly one iteration in the session grant:<id> (permits per model / tool call, budget reservations, the user verifies); iteration_wall_sec (60-3600, default 600) bounds one iteration. W6c: claim_stale_sec (60-86400, default 1800) is after how long an unverified completion claim is shown as still waiting for you (display only: no timer, no action). W6d: heartbeat_start (default false; needs stage_c_exec and a running scheduler) lets a grant issued with its own heartbeat_start flag (host-confirmed at issue) be started by the scheduler's model-free grant_tick, at most one iteration per wake through the same step as a press; tick_min_spacing_sec (30-86400, default 300) is the shortest time from the grant's issue, from an iteration's end and from the grant's last resume to a heartbeat-started one. Off: no executor, no route, no thread. |
| `handoff_inject` | A | no | restart | `STIRA_HANDOFF_INJECT` | Handoff notes (docs/handoff.md): the handoff_write / handoff_read tools and the newest note in the recalled part; on by default, no note = no section. The settings page does not edit it yet. |
| `handoff_max_tokens` | A | no | restart | — | The largest note shown (100-1500, default 400, a conservative estimate); a larger one is left out whole. |
| `handoff_max_age_hours` | A | no | restart | — | An older note is not shown (1-720, default 72). |
| `task_models` | A | partial | restart | `STIRA_TASK_MODEL_TOOL_SUMMARY`, `STIRA_TASK_MODEL_CONTEXT_SUMMARY` | Model per task (docs/task-models.md): shown read-only under Output limit and cost; changed in the file or with STIRA_TASK_MODEL_<TASK>. Empty = every task on the chat model. |
| `token_prices` | A | no | restart | — | #451 Token unit prices per model id (USD per million tokens; input and output required, cache_read / cache_write optional). Read at start-up; only a model with an entry gets an estimated cost on the activity board and the chat page. Stira ships no price data. The settings page does not edit it yet. |
| `delegation` | A | yes | next_turn | — | Sub-agents (delegate), ADR 0016 phase 1 (#486): on / off and three caps; read at every turn and every call. Each call is still a person's card. |
| `agent_jobs` | A | yes | restart | — | Scheduled agent jobs, ADR 0021 S2 (#239): on / off (default off) and the daily token pool agent_daily_token_budget (50,000). Read at start; jobs come up paused after every start. |

### `agent-config.json` (`AgentConfig`)

| Setting | Class | UI | Applies | Variable that wins | Reason / note |
|---|---|---|---|---|---|
| `identity_anchors` | C | no | restart | — | `layer3` |
| `soul_path` | C | partial | restart | — | `layer3` Preview only. |
| `read_only_paths` | B | no | restart | — | #479 |
| `workspace_dir` | B | no | restart | — | #479 |
| `write_roots` | B | yes | restart | — | Where file_write / editor may write (#243); default @workspace only. Adding a root is confirmed and recorded (#536); the secret-path floor applies whatever the roots. |
| `write_outside_action` | C | no | restart | — | `single_choice` |
| `allow_broad_write_roots` | C | no | restart | — | `loosening` |
| `read_roots` | B | yes | restart | — | Where file tools may read (#457); default @workspace only. Adding a root is confirmed and recorded; the secret-path floor applies whatever the roots. |
| `read_secret_paths` | A | no | restart | — | #457 Additions to the secret-path floor (a path, or a name pattern); the built-in floor cannot be removed. |
| `model_override` | A | yes | next_turn | `STIRA_MODEL` | — |
| `provider_override` | A | yes | next_turn | `STIRA_PROVIDER` | — |
| `base_url_override` | A | yes | next_turn | — | — |
| `api_key_env_override` | A | yes | next_turn | — | — |
| `distiller_mode` | B | no | on_run | — | #473 |
| `distiller_model_id` | A | no | on_run | — | #473 |
| `distiller_provider` | A | no | on_run | — | #473 |
| `distiller_base_url` | A | no | on_run | — | #473 |
| `distiller_api_key_env` | A | no | on_run | — | #473 |
| `harvest_stage_auto` | B | no | on_run | — | #473 |
| `harvest_job_max_episodes` | A | no | on_run | — | #473 |
| `memory_retention` | A | no | on_run | — | #474 Values and the dry run only; --apply stays on the command line. |
| `episodic_emotion_snapshot` | A | no | immediate | `STIRA_EPISODIC_EMOTION` | #473 |
| `associative_recall` | A | no | restart | `STIRA_ASSOCIATIVE_RECALL`, `STIRA_RECALL_EMOTION_PROXIMITY` | #473 |
| `memory_autonomy` | A | no | restart | `STIRA_MEMORY_AUTONOMY` | #251 ADR 0018 phase 0 (observe only); the settings card is a later issue. |
| `containment` | A | no | restart | `STIRA_CONTAINMENT_OBSERVE` | #460 ADR 0023 phase 0 (observe only); the settings page shows the observation read only. |
| `taint` | A | no | restart | `STIRA_TAINT_OBSERVE` | #458 ADR 0022 phase 0 (observe only); the settings page shows the observation read only. |
| `tool_config` | A | yes | next_turn | — | The tool rules (tool-config.json). |
| `hb_session_mode` | C | no | never | — | `stored_only` |
| `hb_task_policy` | C | no | never | — | `stored_only` |
| `autonomy_boundary` | C | no | never | — | `stored_only` |
| `stira_config_overrides` | A | partial | per_field | — | Each key is listed under stira.*. |

### `stira_config_overrides` (`StiraConfig`)

| Setting | Class | UI | Applies | Variable that wins | Reason / note |
|---|---|---|---|---|---|
| `stability_warning` | A | yes | next_turn | — | — |
| `stability_critical` | A | yes | next_turn | — | — |
| `bc_threshold` | A | yes | next_turn | — | — |
| `stability_warning_exit` | A | yes | next_turn | — | #471 |
| `stability_critical_exit` | A | yes | next_turn | — | #471 |
| `bc_exit_threshold` | A | yes | next_turn | — | #471 |
| `bc_window_size` | A | yes | restart | — | #471 |
| `decay_min_interval_sec` | A | yes | restart | — | #471 |
| `critical_cooldown_turns` | A | yes | restart | — | #471 |
| `sycophancy_threshold` | A | yes | restart | — | #471 |
| `sycophancy_window_size` | A | yes | restart | — | #471 |
| `hitl_timeout_sec` | A | yes | restart | — | #471 |
| `hitl_max_edit_rounds` | A | yes | restart | — | #471 |
| `protected_layers` | C | no | restart | — | `layer3` |
| `identity_anchors` | C | no | restart | — | `layer3` Not an override: agent.identity_anchors. |
| `critical_action` | C | no | never | — | `server_fixed` |
| `blocked_tool_names` | C | no | never | — | `server_fixed` |
<!-- settings-surface:end -->
