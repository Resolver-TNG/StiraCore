# Memory autonomy — phase 0 (observe only)

Design: [ADR 0018](adr/0018-autonomous-memory.md) (accepted 2026-10-20; phase 0 only — phases 1
and 2 each need the commander's own go). Issues #251, #252; the forged front-matter point of
#506. Code: `src/stiracore/memory_autonomy.py` (settings, turn trust, the harness side of
`memory_rate`), `src/stiracore/memory_rating.py` (the bound table, the harness-owned rule
score), `src/stiracore/tools/memory_rate_tool.py`, `src/stiracore/control/memory_jobs.py`
(the three job kinds), `src/stiracore/scheduler/` (the `memory` job layer). Japanese:
[memory-autonomy_ja.md](memory-autonomy_ja.md).

Memory stays three genres (episodic / semantic / procedural); emotion values stay raw
([ADR 0017](adr/0017-emotion-proximity-recall.md)). Phase 0 records what the agent would decide
and what the jobs would do, and **changes nothing that is remembered, recalled or forgotten.**

## Switching it on

Off by default. `agent-config.json` (read at start-up):

```json
"memory_autonomy": {
  "phase": 0,
  "in_turn_rating": true,
  "jobs": {"memory_rate_backfill": false, "memory_consolidate": false, "memory_tidy": false},
  "daily_token_budget": 100000,
  "undo_window_days": 30
}
```

* Absent or `{}`: **off** — no `memory_rate` tool is offered, no memory job kind exists,
  nothing is bound, `/api/gateway/status` → `memory_autonomy` is `null`.
* `"phase"` is required and must be `0`. Anything else (1, 2, unknown keys, a budget above
  100,000) is refused by the settings loader.
* `STIRA_MEMORY_AUTONOMY=0` (or `false` / `no` / `off`) forces it off whatever the file says.
  No value turns it on.
* The settings page has no card for it yet (a later issue); the manifest lists it as
  `agent.memory_autonomy` (class A, `ui: no`).

## What exists and what is deliberately inert

| Piece | Exists in phase 0 | Inert in phase 0 |
|---|---|---|
| `memory_rate(importance, reason, half_life_days?)` | offered when `in_turn_rating` is on; the rating is bound to the turn's episode | **nothing reads it**: no routing, no recall weight, no half-life, no retention |
| Bound table `memory_ratings` in `index.sqlite3` | rows with harness stamps, bound to the episode's SHA-256 | `effect_of` (routing `max(rule, agent)`, trust cap 0.5) is the phase-1 formula; nothing calls it |
| Forged front-matter `importance` (#506) | ignored by Harvest and associative recall | — |
| Memory job kinds | created by a person, run in their own executor, write plan files | no plan is applied; nothing is bound, staged, compressed, archived or deleted |
| `harvest_stage_auto: false` | unchanged | removed in phase 1 (commander decision Q6) |

## `memory_rate`

The model supplies at most three values: `importance` (0..1), `reason` (one code of
`decision`, `error_fix`, `preference`, `fact`, `procedure_hint`, `routine`, `other`) and an
optional `half_life_days`. The harness stamps the rest: `rated_by: agent_turn`, `rated_at`,
the server's `turn_id`, the chat `model_ref`, the turn's `trust` and `capped`. The tool has
no text, key, turn or retention argument; it writes no memory text and no file.

* **Binding.** The rating waits until the server writes the turn's episode
  (`record_episode`), then is bound to that file's exact text (SHA-256 of the whole file). A
  later edit — by the agent's file tools, by hand, by `memory_write` — makes the row absent.
  Harvest's own mark moves a row that verified before the rewrite (as it does for `origin`).
* **Bounds.** Non-finite, boolean or non-numeric values are refused
  (`E_AGENT_IMPORTANCE_INVALID`); numbers outside [0, 1] and half-lives outside [7, 180] days
  are clamped and recorded as `capped: clamped`. Without `half_life_days` the half-life is
  derived (< 0.2 → 7, < 0.4 → 14, < 0.6 → 30, < 0.8 → 60, else 120 days).
* **Never `invariant`.** `reason: "invariant"` is refused (`E_INVARIANT_NOT_ALLOWED`);
  `invariant` stays a person's decision (Q2) and phase 0 has no proposal path for it.
* **Rate limits.** One rating per turn (`E_ONE_PER_TURN`). At most 10 ratings `>= 0.8` per
  UTC day; further ones are stored as 0.79 with `capped: quota`.
* **Breaker.** Refused outside NORMAL (`E_BREAKER_NOT_NORMAL`); refused outside a turn
  (`E_NO_TURN`).
* **Trust** (Q8, until #458). `trusted` only when a person's message started the turn and the
  conversation in the context holds no tool result other than `current_time`, `stop`,
  `handoff_to_user`, `memory_rate`, `memory_write`, `memory_list`, `journal`,
  `propose_procedure`. A web, file, LSP, MCP or child-agent result, a memory read-back, any
  unknown tool, a webhook / bot / automatic-continue turn → `untrusted`, `capped: untrusted`.
  The rating is stored as given; the 0.5 cap applies to its *effect* from phase 1 on.
* **Tool rules** (Q3). Listed in the tool rules with the **read** category; default **Always**
  on a fresh install and in the `dev` / `research` profiles; `minimal` sets it Never (the
  smallest surface). An install whose saved rules predate it has no entry for it, so it is
  **Never** there until a person switches it on (like every tool added later). Never = no
  in-turn rating. YOLO class **internal** (pathless): unlike `propose_procedure` it puts no
  text in front of a person, unlike `cron` it starts nothing later, unlike `delegate` it runs
  no child.
* **Status.** `/api/gateway/status` → `memory_autonomy.ratings`: `total`, `today`,
  `high_today`, `untrusted_today` (counts only).

## Forged front-matter `importance` (#506)

The episodic layer is a write root of the agent's file tools, so an episode's front matter is
a claim. Harvest (`RuleBasedDistiller`'s floor, the `distillation_cli` preview) and
associative recall now use `importance` only while the episode is byte-for-byte what the
server's episode writer recorded (verified origin `extracted`, the SHA-256 of the whole file
in `index.sqlite3` on the data-directory floor). Otherwise Harvest re-scores the stored text
(the default rule score) and recall uses the neutral 0.5. `importance_error` is still honoured
(it can only send a turn to review).

Reproduced and fixed: a turn whose text scores 0.25, its front matter rewritten to
`importance: 0.95`, went to staging before; it is now discarded on its own text score
(`tests/test_memory_autonomy_phase0.py::test_forged_importance_*`). Unchanged: every episode
the server recorded and nobody edited reaches the same decision **byte for byte** — a replay of
recorded cases (`tests/harvest_replay_cases.py`, fixture recorded on the previous main).
Behaviour change: episodes without a verified origin (written before #294, edited by hand)
lose their recorded floor and are scored on their stored text.

## Memory jobs (plan files only)

Three fixed kinds on the scheduler (`STIRA_SCHEDULER=1`), each enabled by
`memory_autonomy.jobs`:

| Kind | Model | Per run | Plan file holds |
|---|---|---|---|
| `memory_rate_backfill` | 1 call per episode without a bound rating, ≤ 20 | ≤ 1,500 input chars, ≤ 128 output tokens per call; `max_tokens_per_run` ≤ 15,000; ≤ 6 runs and 60,000 tokens per day | proposed ratings (`rated_by: agent_job`, `trust: unknown`) or a refusal code per episode |
| `memory_consolidate` | clustering model-free (term overlap ≥ 0.5 between Semantic entries); 1 call per cluster, ≤ 5 | ≤ 12,000 input chars, ≤ 800 output tokens per call; ≤ 30,000 tokens per run and per day; 1 run per day | proposed summaries with their sources (keys + SHA-256) |
| `memory_tidy` | none | — | forgetting / deletion candidates (dry-run), staging duplicate groups, backlog |

Create one like any job (`POST /api/scheduler/jobs`, `"confirm": true`, a person, NORMAL
breaker):

```json
{"job": {"job_id": "nightly-tidy", "name": "tidy plan", "layer": "memory",
         "action": "memory_tidy", "schedule": {"type": "cron", "expr": "0 3 * * *"}},
 "confirm": true}
```

A memory job has no prompt and no tools (the kind fixes both); a budget above the kind's
ceiling is refused, a missing one takes the ceiling. The agent has no tool that creates or
widens a job, and a running job cannot change the schedule.

* **Plan files only:** `<memory_dir>/memory-plans/<kind>/<run_id>.json` (`0600`, on the
  data-directory floor; `applied: false`). Every memory layer, the search index, the emotion
  records and the forgetting archive are hashed around each run in the tests and stay
  identical.
* **Plan files follow deletes and expire:** a plan names entries by key (and a
  `memory_consolidate` plan holds a summary derived from them). A delete request
  (`stira memory forget`, also `--id` alone) removes every plan file that names a forgotten
  entry or holds the text, and the archived copies of such files; the tests check that no
  plan file names a deleted entry afterwards. `stira memory retention` lists plan files
  older than `memory_retention.memory_plans.max_age_days` (**default 90**); only `--apply`
  removes them, archived first (`stira memory restore <id>` brings them back). Nothing
  removes a plan file by itself ([memory-maintenance.md](memory-maintenance.md#retention-delete-requests-and-provenance-294)).
* **Own executor:** a run is dispatched by the scheduler's job worker into the memory
  executor's single worker thread (`stira-memory-job`). The heartbeat tick never reaches it.
* **Only `MODEL_CALLING_KINDS`** (`memory_rate_backfill`, `memory_consolidate`) may build a
  model; the executor refuses any other kind, and the scheduler fails a model-free kind that
  reports tokens (`model_call_not_allowed`). The model is the distiller's small model
  (`distiller_model_id`, Q5); the text sent is the stored body masked by `mask_secrets`; the
  answer must be exactly one JSON object of the kind's schema (`E_OUTPUT_SCHEMA` per item).
* **Tokens:** before each call the worst case (prompt characters + output cap + 64) must fit
  in the run's `max_tokens_per_run`, otherwise the run stops (`stopped_by: token_cap`). A
  reported usage above the cap fails the run (`budget_exceeded`) and pauses the job. The
  scheduler reserves `max_tokens_per_run` at the start against the job's daily cap and against
  `daily_token_budget` — one total of 100,000 per UTC day for all memory jobs; a start that
  would pass it is refused and the job paused until a person resumes it.
* **Breaker NORMAL only**, checked at the start and before every item; leaving NORMAL stops at
  the item boundary (`stopped_by: breaker`).
* **Heartbeat** ([ADR 0021](adr/0021-scheduler-job-base.md) §8, S1): a model-calling kind
  starts only while the heartbeat (#238) is healthy — enabled, running, not degraded, last
  tick within `max(3 × interval, 120 s)`; otherwise the fire is recorded
  `run_skipped(heartbeat)` and `heartbeat_required` is raised once. **With the heartbeat off,
  these kinds do not run**; a job may opt out with `"requires_heartbeat": false`.
  `memory_tidy` ignores it. `missed_policy: "catch_up"` is refused for the model-calling kinds.
* **Restart** (Q4): model-calling jobs come up **paused** (`by: system`, "restart: …") and run
  again only after a person resumes them (`confirm: true`); one `jobs_paused_at_start`
  notification gives the count. `memory_tidy` keeps the normal
  missed-run behaviour. A server without memory autonomy (or without the kind enabled) pauses every such stored memory job.
* **One owner:** `server.lock`, the per-store Harvest lock and a memory-job mutex (one memory
  job at a time per store), all without waiting (`data_dir_not_owned`, `harvest_in_progress`,
  `memory_job_in_progress`).
* **Audit:** `audit.memory.planned` / `.refused` / `.rated` rows carry the kind, job / run /
  turn ids, a keyed hash of an entry key, a rating rounded to 0.05, a reason code and integer
  counts — never text, a key in clear or a path. Status: `memory_autonomy.jobs` (newest run per
  kind, counts) and `memory_autonomy.plans` (plan files per kind).

Not yet (ADR 0018 §2e): the `idle` trigger, "3 failed runs → paused by system", the per-item
retry ledger (`held_for_review`) and the provider-throttling back-off. The scheduler's existing
failure alert (`job_failing`) applies.

## The tick stays model-free (provable)

The heartbeat tick (decay, snapshot, breaker query) never calls a model and never enqueues a
job. Checked by the existing tests (`test_tick_has_no_model_or_agent_dependency`,
`test_a_tick_never_calls_a_model`, `test_stira_a_tick_reaches_no_model_classifier_agent_tool_or_job`),
plus:

* an import-graph test: the module-level import closure of `stiracore.heartbeat` reaches
  no `strands`, `stiracore.providers`, `stiracore.agent`, `stiracore.scheduler`,
  `stiracore.control` (memory jobs), `memory_autonomy` or `memory_rating` — with a
  mutation check that one added import is found;
* a tick of the chat server with memory autonomy, every job kind, the scheduler and the
  heartbeat on, with tripwires on the memory executor, its model factory, Strands `Agent` and
  the model;
* the fixed `MODEL_CALLING_KINDS` list (exactly the two kinds above).
