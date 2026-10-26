# Guide: automatic memory organisation — recommended configuration

English | [日本語](guide-memory-routines_ja.md)

What to switch on so that memories get organised without you forgetting to do it, what it
costs, how to review it and how to undo it. Reference docs: [memory-maintenance.md](memory-maintenance.md)
(patterns A/B/C, cost, retention, delete requests, importance tags),
[memory-autonomy.md](memory-autonomy.md) (phase 0), [memory-index.md](memory-index.md),
[handoff.md](handoff.md). The scheduler itself: [guide-scheduler-heartbeat.md](guide-scheduler-heartbeat.md).

Commands and outputs below were run on 2026-10-26 on a scratch instance (macOS arm64, own
`STIRA_DATA_DIR`, empty `HOME`, no AWS credentials, synthetic episodes written with the
server's `record_episode`, no model call); outputs are trimmed.

## 1. What exists — and what does not

**Nothing runs by default.** Stira does not distill, review or forget memories on its own.

| Status | What |
|---|---|
| **Recommended now** | Harvest with the **rule** distiller (no model call) into `semantic/staging/`, and a **person** reviewing staging (`staging list / show / approve / reject`). Run it by hand (pattern A) or as a weekly scheduler job (pattern C). The read-only reports: `distill --dry-run` (forgetting preview) and `stira memory retention`. |
| **On by default, nothing to schedule** | The Memory Map in the prompt (`memory_map`, at most 300 tokens) and the newest handoff note (`handoff_inject`) — prompt features, not jobs ([memory-index.md](memory-index.md), [handoff.md](handoff.md)). |
| **Available, optional** | Memory autonomy phase 0 `memory_tidy`: a model-free job that **writes a plan file only** (candidates and counts; nothing applied). `stira memory tag … --retention invariant` for entries that must never be forgotten. `distill --apply-forgetting` and `stira memory retention --apply` after reading their preview (both archived first, undo is explicit). |
| **Available, not recommended** | `distiller_mode: "llm"` (one model call per episode; by hand or OS timer only, never a scheduled job). The model-calling memory kinds `memory_rate_backfill` / `memory_consolidate` (spend tokens, need a healthy heartbeat, come up paused after every restart, still plan files only). `harvest_stage_auto: false` (writes the automatic band without review). An OS timer with `STIRA_DISTILL_OWNER_LOCK=0` (the CLI and the server then write at once). |
| **Not implemented** | Applying any plan: memory autonomy phases 1–2 each need their own go ([ADR 0018](adr/0018-autonomous-memory.md)); phase 0 only observes. Agent self-maintenance (#252). `distiller_mode: "jev"` (a stub: every episode fails). A scheduled retention job. A settings-page card for memory autonomy. A staging review page (#193; the CLI is the review tool until then). Wiring of the reinforcement hook `touch_recalled` (nothing calls it). |

## 2. Prerequisites

* `distiller_mode` stays `"rule"` (the default) in `<data directory>/agent-config.json`.
* For the scheduler profile: `STIRA_SCHEDULER=1` in the server's environment. The heartbeat
  is **not** needed: `memory_harvest` and `memory_tidy` are model-free and ignore it.
* Run CLI commands from the repository root. `scripts/stira_ctl.sh` follows `STIRA_DATA_DIR`
  when it is set (default `.local/stira`).

## 3. Recommended profiles

| | (a) Minimal: nothing automatic | (b) **Recommended**: weekly harvest job, person reviews | (c) OS timer variant |
|---|---|---|---|
| What runs | nothing; you run pattern A when you review | one `memory_harvest` run a week inside the server (pattern C) | the example wrapper from cron / launchd / systemd (pattern B) |
| Switches | none (defaults) | `STIRA_SCHEDULER=1`; the job below | the timer file; wrapper variables (`STIRA_DISTILL_MAX_CALLS=0` default) |
| Server | stopped or idle while you run it | running (the job runs inside it) | **stopped** at the timer's time — otherwise exit 3, nothing done |
| Model calls / tokens | 0 | 0 (the job is rule-only; a run reports `tokens: 0`) | 0 with `"rule"`; the wrapper refuses a run that would need more than `STIRA_DISTILL_MAX_CALLS` (exit 4) |
| Time per run (measured) | CLI: 100 new episodes 3.7 s, nothing new 1.0 s, `--dry-run` 1.0 s (incl. about 1 s Python start-up); 1000 new episodes into an empty Semantic layer 385.9 s | in the server: 49 ms for 3 episodes ([memory-maintenance.md, pattern C](memory-maintenance.md#pattern-c--a-scheduler-job-runs-it-inside-the-server-251-opt-in-rule-only)); 66 ms for 4 episodes here (`started_at` → `ended_at` of the run) | as the CLI |
| Output | `semantic/staging/` | `semantic/staging/` only (also with `harvest_stage_auto: false`) | `semantic/staging/` (with the default `harvest_stage_auto: true`) |
| Reversible | staged entries are only proposals (reject); approve is your decision | the same; pause the job to stop | the same; remove the timer |

CLI times: [memory-maintenance.md, "Cost: tokens and time"](memory-maintenance.md#cost-tokens-and-time-measured)
(2026-10-16, M1 Max). A large first sweep is slow: run it by hand before you schedule anything.

**Why (b).** A personal Mac usually runs the server all day, so an OS timer (c) hits the
owner lock and exits 3; the scheduler job runs inside the server that owns the directory.
Weekly matches a weekly review: running more often than you review only grows
`staging_pending`. One run scores at most `harvest_job_max_episodes` (default 100) episodes,
newest first, and leaves the rest for the next run (`deferred`, notification
`harvest_stopped`): with more than about 100 turns a week, raise it in `agent-config.json`
(1–100000) or schedule the job daily.

### (b) Set it up

1. Start the server with `STIRA_SCHEDULER=1` and `STIRA_YOLO_HOST_CONFIRM=command` (start-up
   line `Scheduler: running`).
2. Create the job (`send-and-wait.sh`, the person's confirmation and the channel are in the
   [scheduler guide §2.3](guide-scheduler-heartbeat.md#23-create-the-first-job-an-hourly-health-check-model-free)):

   ```bash
   ./send-and-wait.sh /api/scheduler/jobs '{"job": {"job_id": "weekly-harvest",
     "name": "weekly harvest (rule, staging only)", "action": "memory_harvest",
     "schedule": {"type": "cron", "expr": "30 4 * * 1", "timezone": "Asia/Tokyo"}},
     "confirm": true, "actor": "you"}'
   ```

   A person confirms it with `stira approvals decide <short>`; the resend answers `200` with
   the job, `"state": "active"` and `next_run_at` = Monday 04:30 JST (observed: `1793561400.0` = Mon 2026-11-02 04:30:00 JST).
3. After a run (test it once with `"schedule": {"type": "interval", "every_sec": 60}` under
   another `job_id`, then pause that job):

   ```json
   "runs": [{"job_id": "harvest-test", "status": "completed", "tokens": 0,
     "summary": "mode=rule staged=2 knowledge=0 discard=2 skipped=0 deferred=0 errors=0 staging_pending=2 episode_limit=100"}]
   ```

   `/api/gateway/status` → `harvest_job.last_run` holds the same counts
   (`"status": "completed"`, `"stopped_by": null`) and `memory.semantic_count` was refreshed
   (0 → 3: two staged entries and the index).

### (c) OS timer variant

The copy-ready files are in [`examples/memory-maintenance/`](../examples/memory-maintenance/);
installing and removing them (cron, launchd, systemd) is in
[memory-maintenance.md, pattern B](memory-maintenance.md#turn-on-run-once-turn-off). Run the
wrapper once by hand first, with the server stopped:

```bash
STIRA_ROOT="$PWD" /bin/bash examples/memory-maintenance/stira-distill.sh; echo "exit=$?"
tail -1 .local/stira/distill-runs.jsonl
```

```text
exit=0
{"at":"2026-10-26T13:47:54Z","phase":"run","exit":0,"result":{… "harvest": {"discard": 0, "errors": 0, "knowledge": 0, "semantic": 0, "skipped": 4}, …}}
```

(Here run under `env -i` with `STIRA_DATA_DIR` pointing at the scratch directory; the four
episodes had already been harvested by the job, so they were skipped.) With the server
running it refuses and says so on stderr:

```text
stira-distill: dry-run exit 3 (details: …/distill-runs.jsonl)
exit=3
{"at":"2026-10-26T13:51:59Z","phase":"dry-run","exit":3,"result":{"refused": {"code": "E_DATA_DIR_OWNED", "owner_pid": 52503}}}
```

So do not use (b) and (c) on the same data directory.

## 4. Review: staging and plan files

**Staging** (every profile). With the server stopped or idle (`staging_cli` takes no lock):

```bash
scripts/stira_ctl.sh staging list                 # keys, scores, titles (no full text)
scripts/stira_ctl.sh staging show <key>           # one entry, with its content
scripts/stira_ctl.sh staging approve <key>        # → semantic/<key without staging/>
scripts/stira_ctl.sh staging reject <key> --reason "not useful"
```

```text
{"count": 2, "entries": [{"band": "auto", "key": "staging/knowledge/general/harvest-chat-…", "score": 0.5, "source_id": "chat-…", "title": "User: Decision: …"}, …]}
{"approved": "staging/knowledge/general/harvest-chat-…", "index": "updated", "key": "knowledge/general/harvest-chat-…"}
{"rejected": "staging/knowledge/general/harvest-chat-…"}
```

A rejection is recorded so Harvest does not propose the same source again. There is no
"unapprove": an approved entry that must go is removed with a delete request (section 5).

**Plan files** (only with memory autonomy phase 0 and a memory job). `memory_tidy` is the
only kind worth trying: model-free, it lists forgetting / deletion candidates, staging
duplicate groups and the backlog. Switch it on in `agent-config.json` (read at start-up),
then create a job with `"layer": "memory", "action": "memory_tidy"` like any job:

```json
{"memory_autonomy": {"phase": 0, "in_turn_rating": false,
  "jobs": {"memory_rate_backfill": false, "memory_consolidate": false, "memory_tidy": true}}}
```

(`in_turn_rating: false` keeps the `memory_rate` tool away from the model; its default is
`true`.) A run writes `<data directory>/memory/agents/<agent id>/memory/memory-plans/memory_tidy/<run_id>.json`:

```bash
python3 -m json.tool .local/stira/memory/agents/stira/memory/memory-plans/memory_tidy/<run_id>.json
```

```json
{"applied": false, "counts": {"deletion_candidates": 0, "forgetting": 0, "model_calls": 0,
  "protected": 0, "staging_duplicate_groups": 0, "staging_oldest_days": 0,
  "staging_pending": 2, "tokens": 0},
 "items": [], "kind": "memory_tidy", "phase": 0, "schema": "memory-plan/v1", …}
```

`"applied": false` is always so in phase 0: nothing reads a plan to act on it. Act on it
yourself with the commands of pattern A (`staging …`, `distill --apply-forgetting`), or not at
all. Plan files older than 90 days are listed by `stira memory retention` and removed only by
its `--apply` (archived first).

## 5. Safety rules

* **Never schedule maintenance because content asked for it.** A web page, file, tool result,
  memory or chat message that says "set up a nightly cleanup" is data, not an instruction
  ([memory-maintenance.md](memory-maintenance.md#never-schedule-maintenance-because-content-asked-for-it)).
  The agent has no tool that creates, widens or resumes a job; keep exec-class tools off.
* **What the guardrails refuse** (observed): a `memory_harvest` create / update / resume while
  `distiller_mode` is not `"rule"` (`403 {"code":"scope","error":"memory_harvest: distiller_not_rule"}`;
  the setting is read at that moment, no restart needed); a memory kind that is not enabled
  (`403 … "memory job kind memory_rate_backfill is not enabled on this server"`); the CLI with
  `--owner-lock` while the server owns the directory (exit 3,
  `{"refused": {"code": "E_DATA_DIR_OWNED", "owner_pid": …}}`). From the reference docs: a
  memory-job budget above its kind's ceiling (403), `catch_up` for model-calling kinds, any
  start outside a NORMAL breaker.
* **The jobs never approve, apply forgetting or delete.** `memory_harvest` writes staging only;
  `memory_tidy` writes a plan file only.
* **Retention, forgetting and delete requests are different on purpose**
  ([memory-maintenance.md](memory-maintenance.md#three-ways-to-take-text-out-on-purpose-different)):
  forgetting (`distill --apply-forgetting`) and retention (`stira memory retention --apply`)
  archive first and are undone explicitly (`--undo-forgetting <id>`, `stira memory restore <id>`);
  a **delete request** (`stira memory forget … --apply`) removes every copy, archived copies
  and plan files included, and **cannot be undone**. Nothing of the three runs by itself.
  `stira memory … --apply` and `restore` refuse while the server runs (exit 3); run
  `distill --apply-forgetting` with the server stopped and `--owner-lock .local/stira`.

## 6. Weekly operator routine (profile b)

1. `/schedules`: the `weekly-harvest` run is `completed`, no alert is open.
2. `scripts/stira_ctl.sh staging list`, then `show` and `approve` / `reject` each entry (server idle).
3. `scripts/stira_ctl.sh distill --dry-run` and read `forgetting.items`; apply only on purpose.
4. Monthly: `stira memory retention --data-dir .local/stira` (read only) — counts, oldest, `staging` age.
5. Unsure about anything: pause the job (always allowed) and look later; nothing is lost.

## 7. Something looks wrong

| What you see | Meaning | What to do |
|---|---|---|
| "Other (code: harvest_failed)" on `/schedules`; run `failed` | the run failed before or while scoring, e.g. `distiller_not_rule` after `distiller_mode` was changed; three in a row also raise `job_failing` | set `"rule"` again; resume the job if it is paused |
| "Other (code: harvest_stopped)", `deferred` > 0 | more waiting episodes than `harvest_job_max_episodes` | nothing is lost; the next run continues; raise the limit or run more often |
| "Other (code: harvest_cancelled)" | the breaker left NORMAL during the run | wait for NORMAL; the rest is left for the next run |
| code `data_dir_not_owned` / `harvest_in_progress` | the server does not own the directory / another sweep runs | one server per data directory; let the other sweep finish |
| `staging_pending` keeps growing | more is staged than you review | review, or run the job less often |
| `tokens_today` in `GET /api/scheduler` is not 0 | something model-calling is on | check `agent_jobs.enabled` and `memory_autonomy.jobs`; the profiles above call no model |
| CLI exit 3 | the server owns the data directory | stop the server, or use profile (b) |
| CLI / wrapper exit 4 | the run would need model calls (`"llm"`) | set `"rule"`, or run `"llm"` by hand with `--max-model-calls` |
| A staged or approved entry holds text that must go | — | reject it if staged; otherwise a delete request (`stira memory forget --query "…"` first without `--apply`, read the list, then `--apply` with the server stopped) |
