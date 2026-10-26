# Memory maintenance: manual runs, OS timers, and what they cost (#317)

日本語: [memory-maintenance_ja.md](memory-maintenance_ja.md)

**Nothing runs by default.** Stira does not distill, review or forget memories on its own.
The heartbeat tick (#238) and the scheduler (#239) are off by default. The tick never does
memory work (decay, snapshot, breaker query). With the scheduler on, Harvest runs only as a
`memory_harvest` job that a human created (pattern C, #251): rule distiller only, staging
only. Agent self-maintenance (#252) is **not implemented**.

What exists today and can be run:

| Command | What it does | Model calls |
|---|---|---|
| `python -m stiracore.control.distillation_cli --base-dir <memory> --config <agent-config.json>` (`scripts/stira_ctl.sh distill`) | One Harvest sweep (Episodic → Semantic, staged for review by default), a forgetting **preview**, counts of deletion candidates | none with the default `distiller_mode: "rule"` |
| same, `--dry-run` | Counts what a run with the same arguments would do, predicted routes (rule), number and size of model calls (llm); creates and writes nothing (an incomplete store is not opened; `--owner-lock` is only probed) | none |
| same, `--max-model-calls N` | `llm`: score at most N episodes in this run, exit 4 when more were waiting (with `--dry-run`: exit 4 when the preview needs more or cannot tell) | at most N |
| same, `--apply-forgetting` | Compresses the previewed entries after archiving each original (#294) | none |
| same, `--list-forgotten` / `--undo-forgetting <id>` | Lists / restores archived originals | none |
| `python -m stiracore.control.staging_cli --base-dir <memory> list\|show\|approve\|reject\|reindex` (`stira_ctl.sh staging ...`) | Human review of what Harvest staged (#293) | none |
| `stira memory retention\|forget\|restore\|removed\|jobs --data-dir <data directory>` | Retention report and opt-in reversible runs, delete requests that reach every copy ([below](#retention-delete-requests-and-provenance-294)) | none |
| `stira memory tag --data-dir <data directory> --id <layer>/<key> --retention invariant\|derived\|none [--half-life-days N] [--apply]` | A memory's importance tag for forgetting: never forgotten, or a half-life ([below](#importance-tags-retention-invariant--derived-and-a-half-life-173)) | none |

`<memory>` is `<data directory>/memory` (`.local/stira/memory` for the chat server) and
the config is `<data directory>/agent-config.json`; a missing config file means the
defaults (`distiller_mode: "rule"`). Exit codes: `0` done, `1` something failed (an
episode, a refused forgetting step, an unavailable distiller), `2` usage error, `3` the
data directory is owned by a running server, or its `server.lock` is not a plain file of
yours (`--owner-lock`, below; `E_DATA_DIR_OWNED` / `E_LOCK_FILE_UNSAFE`), `4` the
model-call limit (`--max-model-calls`). The example wrapper adds `5` (unsafe log path) and
`6` (log not written).

There are three ways to run it. **A** is the default; **B** is an OS timer; **C** is a
scheduler job inside the server (#251; #252 is not implemented).

## Pattern A — no automation (default)

Run the commands yourself, with the server stopped or idle:

```bash
scripts/stira_ctl.sh distill --dry-run   # what would happen, nothing written
scripts/stira_ctl.sh distill             # Harvest + forgetting preview
scripts/stira_ctl.sh staging list        # what waits for review (no content)
scripts/stira_ctl.sh staging show <key>
scripts/stira_ctl.sh staging approve <key>   # or: reject <key> --reason "..."
```

`stira_ctl.sh distill` passes extra arguments to the CLI; add `--owner-lock .local/stira`
(run from the checkout) to refuse the run while the server owns the data directory (exit 3;
without it, a running server is not excluded). Read `forgetting.items` in the output before
you ever run `distill --apply-forgetting`
([operations §12](operations.md#12-forgetting-is-archived-undo-is-explicit-only-294)).

How often: whenever you are going to review staging anyway — weekly is plenty for a
personal setup. A sweep only scores episodes it has not scored yet, so frequent runs are
cheap, but every staged entry still needs a human; running more often than you review only
grows the queue (`staging_pending` in the output).

`stira_ctl.ps1 distill` (Windows) runs the same sweep but passes no extra arguments; call
the module directly for `--dry-run` and friends.

## Pattern B — an OS timer runs the CLI (rule distiller)

Copy-ready files are in [`examples/memory-maintenance/`](../examples/memory-maintenance/):

| File | For |
|---|---|
| `stira-distill.sh` | The wrapper every timer calls (below) |
| `crontab.txt` | cron (Linux, macOS) |
| `com.example.stira-distill.plist` | launchd (macOS, per-user LaunchAgent) |
| `stira-distill.service` + `stira-distill.timer` | systemd user units (Linux) |

All examples run daily at 04:30 local time and only call the wrapper. Replace the checkout
path (`/home/YOU/StiraCore`, `/Users/YOU/StiraCore`, `%h/StiraCore`).

### What the wrapper does

0. **Settings are checked first.** Numeric settings must be 1–6 decimal digits
   (`STIRA_DISTILL_OWNER_LOCK`: `0` or `1`); anything else exits **2** before anything runs
   — values are never evaluated as shell arithmetic. A log path that is a symlink or not a
   regular file (`/dev/full`, a directory) exits **5** before anything runs. Files are
   created with `umask 077`.
1. **Dry run first, same arguments.** The CLI arguments are built once — including
   whatever you pass to the wrapper, e.g. `--config other.json` — and given unchanged to
   `--dry-run` and then to the run. If the preview needs more than
   `STIRA_DISTILL_MAX_CALLS` model calls (default **0**), or cannot tell, the run is
   refused with exit **4**. With the default rule distiller there are none, so a config
   that was later switched to `"llm"` does not start spending tokens from a timer.
2. **The run has the same limit.** Preview and run are two processes: the config or the
   memory can change in between, so the estimate can be stale. The run therefore gets
   `--max-model-calls` as well: an `llm` run scores at most that many episodes, leaves the
   rest for the next run and exits **4** (`model_call_limit` in its JSON).
3. **One writer.** Both pass `--owner-lock <data directory>`: the run holds the server's
   owner lock (`server.lock`, #285) for its whole duration; the dry run only probes it
   (nothing is created or written). While a server owns the directory both are refused
   with exit **3** and nothing is read or written; a server started during the run refuses
   to start. Schedule the timer when the server is stopped, or set
   `STIRA_DISTILL_OWNER_LOCK=0` knowingly — the server then is not excluded (the CLI and
   the server may write the same memory directory at once).
4. **Run** `distillation_cli` (forgetting is previewed only unless you pass
   `--apply-forgetting`). `--undo-forgetting` / `--list-forgotten` do not go through the
   wrapper (the dry run refuses them, exit 2); run them directly.
5. **Never silent.** Every attempt appends one JSON line (`at`, `phase`, `exit`, and the
   CLI's JSON) to `<data directory>/distill-runs.jsonl`. A non-zero exit also prints one
   line on stderr — cron mails it when `MAILTO` works, launchd writes it to
   `StandardErrorPath`, systemd keeps it in the journal and marks the unit failed — and is
   returned to the timer. If a log line cannot be written (disk full, no permission) the
   wrapper exits **6** and says on stderr what the attempt itself returned. Of Python's own
   stderr only the last `STIRA_DISTILL_ERR_LINES` lines (default 20) are kept, passed
   through the same secret masking as the server's logs, in `distill-runs.err.log`; when
   masking cannot run, nothing of it is kept. Masking is pattern-based: a secret it does
   not recognise can still end up in that file, which is created owner-only.

The log checks happen before the run; a log path replaced by a symlink after that check
is not detected (the data directory's permissions are the boundary, as for `server.lock`).

| Variable | Default | Meaning |
|---|---|---|
| `STIRA_ROOT` | (required) | The checkout |
| `STIRA_DATA_DIR` | `$STIRA_ROOT/.local/stira` | Data directory |
| `STIRA_AGENT_ID` | `stira` | Agent |
| `STIRA_PYTHON` | `$STIRA_ROOT/.venv/bin/python` | Interpreter |
| `STIRA_DISTILL_MAX_CALLS` | `0` | Model calls allowed per run (the dry run's `estimate.model_calls`; also `--max-model-calls` of the run) |
| `STIRA_DISTILL_ERR_LINES` | `20` | Lines of Python's stderr kept per phase (masked); `0` = none |
| `STIRA_DISTILL_OWNER_LOCK` | `1` | `0` = do not hold `server.lock` |
| `STIRA_DISTILL_LOG` / `STIRA_DISTILL_ERR_LOG` | `<data dir>/distill-runs.jsonl` / `.err.log` | Logs (not rotated; one line per run) |

These variables are read by the example script only, not by Stira.

### Turn on, run once, turn off

```bash
# cron
crontab -e                      # add the line from crontab.txt; remove it to stop
# launchd (macOS)
mkdir -p -m 700 /Users/YOU/StiraCore/.local   # StandardErrorPath's directory (launchd won't create it)
cp examples/memory-maintenance/com.example.stira-distill.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.stira-distill.plist
launchctl kickstart gui/$(id -u)/com.example.stira-distill          # run once now
launchctl print gui/$(id -u)/com.example.stira-distill | grep "last exit code"
launchctl bootout gui/$(id -u)/com.example.stira-distill            # stop
# systemd (user units)
cp examples/memory-maintenance/stira-distill.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now stira-distill.timer
systemctl --user start stira-distill.service                        # run once now
systemctl --user disable --now stira-distill.timer                  # stop
```

A run in progress stops with the usual signal of the timer (`systemctl --user stop
stira-distill.service`, `launchctl kill SIGTERM gui/$(id -u)/com.example.stira-distill`, or
`kill <pid>`); an interrupted sweep leaves the episodes it has not marked yet for the next
run (one whose Semantic entry was already written is then recognised as a duplicate). To stop everything for good, remove the
timer *and* check `distill-runs.jsonl` for its last line.

### What was verified (2026-10-16, macOS arm64, Python 3.11)

* Second round (after review): invalid numeric settings including `a[$(touch …)0]` and
  `1/0` exit 2 with nothing evaluated or run; `--config <llm.json>` passed to the wrapper
  hits the same limit (exit 4); symlinked / device / directory log paths exit 5; an
  unwritable log exits 6; only the masked tail of stderr is kept; the cron line runs from
  a checkout path with spaces — all in `tests/test_memory_maintenance_examples.py`.
* The wrapper under a cron-like empty environment (`env -i`, `PATH=/usr/bin:/bin`, AWS
  configuration disabled): rule run exit 0 and one log line; `"llm"` config exit 4 with no
  model call; data directory owned by a running `stira_server` exit 3; missing data
  directory exit 2. The same cases run in `tests/test_memory_maintenance_examples.py`.
* launchd: the plist passes `plutil -lint`; bootstrapped with the paths filled in,
  `kickstart` ran it (last exit code 0, log line written), and with the lock held it
  reported last exit code 3 and the stderr line in `StandardErrorPath`; then `bootout`.
* systemd: both units pass `systemd-analyze --user verify` (systemd 255) and the calendar
  expression normalises to `*-*-* 04:30:00`. **Not run on a Linux host** with the
  repository installed.
* cron: the crontab line's command run by `/bin/sh -c` in a cron-like environment
  (exit 0). Not installed into a real crontab. Windows Task Scheduler: no example, not
  verified.

## Pattern C — a scheduler job runs it inside the server (#251, opt-in, rule only)

The scheduler (#239, [ADR 0008](adr/0008-scheduler.md)) has a Gateway action
`memory_harvest`. Like every Gateway action it never calls a model: it uses the **rule
distiller only**. It is not a timer of its own and the heartbeat does not run it. A human
must do all of the following before anything runs:

1. start the server with `STIRA_SCHEDULER=1` (off by default; only `1`, `true`, `yes`,
   `on` turn it on);
2. keep `distiller_mode: "rule"` (the default) — with `"llm"` or `"jev"` the job cannot be
   created, updated or resumed (`403 scope`, `distiller_not_rule`), and a run whose setting
   was switched afterwards fails before scoring anything;
3. create the job through the scheduler API with `"confirm": true` (`/schedules` lists,
   pauses and resumes jobs but has no create form; with the YOLO allowance on, the create is
   also a confirmed operation, [guide-scheduler-heartbeat.md §2.3](guide-scheduler-heartbeat.md#23-create-the-first-job-an-hourly-health-check-model-free)).
   The API needs the dashboard's same-origin request and CSRF token; Stira has no tool or
   route that creates, widens or resumes a job, a job cannot create one, and neither a chat
   message nor a webhook can (tested).

```json
{"job": {"job_id": "harvest", "name": "nightly harvest", "action": "memory_harvest",
         "schedule": {"type": "cron", "expr": "30 4 * * *", "timezone": "Asia/Tokyo"}},
 "confirm": true, "actor": "you"}
```

**Model scoring (`"llm"`) is not a scheduled job.** Run `distillation_cli` by hand
(pattern A) or from an OS timer (pattern B, with `STIRA_DISTILL_MAX_CALLS` and
`--max-model-calls`), with the server stopped (`--owner-lock`).

What one run does:

* **One writer.** It runs inside the server, which holds `server.lock` (#285); without it
  the run is refused (`data_dir_not_owned`). Within the process every Harvest sweep of the
  same memory directory shares one lock: the job never overlaps another job or an
  in-process manual sweep (it does not wait: `harvest_in_progress`), and in-process manual
  sweeps wait for each other. Another process is excluded only by `server.lock`: the CLI
  with `--owner-lock` is refused while the server runs (exit 3), a second server refuses to
  start. **Limitation:** a CLI run *without* `--owner-lock` is not excluded and may write
  the same memory directory at the same time as the server.
* **Breaker.** A fire at WARNING / COHERENCE is held, at CRITICAL dropped (ADR 0008 §7);
  the run re-checks before it starts and before every episode and stops when the breaker
  is not NORMAL (`cancelled`, the rest left for later).
* **Staging only.** Everything it promotes goes to `semantic/staging/` — also when
  `harvest_stage_auto` is `false` (that setting applies to manual runs only). It never
  approves, rejects or applies forgetting.
* **Limit.** At most `harvest_job_max_episodes` (default 100) episodes per run, newest
  first. Exactly that many waiting: the run completes normally. More: the rest stay
  unmarked for the next run (`deferred`, `stopped_by=episode_limit`, notification
  `harvest_stopped`). The job's `budget.max_runs_per_day` applies as for every job.
  Tokens: always 0.
* **Never silent.** The run summary carries counts and codes only (`mode`, `staged`,
  `discard`, `skipped`, `deferred`, `errors`, `staging_pending`, `episode_limit`,
  `stopped_by`); never episode text, paths or exception text (tested with canaries). A
  failure or a stop raises a notification (`harvest_failed`, `harvest_cancelled`,
  `harvest_stopped`); three failures in a row also raise `job_failing`.
  `/api/gateway/status` → `harvest_job.last_run` shows the newest run and
  `memory.semantic_count` is refreshed right after it.
* **Stop:** pause the job on `/schedules` (always allowed), or unset `STIRA_SCHEDULER`.

Measured (2026-10-17, macOS arm64, a real `stira_server` process with a fake chat model,
three chat turns, no Bedrock): scheduler off → job creation 503, no `harvest_job` status,
nothing marked; scheduler on with `"llm"` → creation `403 scope distiller_not_rule`; with
`"rule"` the job (interval 60 s) ran 60 s later in 49 ms: `staged=2 discard=1`, tokens 0,
`semantic_count` 0 → 3 (2 staged + index); a CLI `distill --owner-lock` meanwhile exited
3; after switching to `"llm"` the next run failed `distiller_not_rule` (notification
`harvest_failed`) and resume was refused. The chat model's tool definitions and the static
system prompt hash the same as on `main`, with the scheduler off and on.

**#252 (self-maintenance)** is not provided: model-based summarising and tidying only
after the ON/OFF evaluation (#143); proposals and staging only, no direct writes, and no
coupling of the amount of maintenance to the agent's internal state.

## Cost: tokens and time (measured)

Measured on 2026-10-16 on an Apple M1 Max laptop, main=cba495c plus this change, with
synthetic episodes written by the same `record_episode` the chat server uses (short
Japanese/English turns, some with headings and code). Times are wall-clock including the
Python start-up (about 1 s). "Not measured" means not measured — no estimate is filled in.

| Mode | Model calls per run | Input / output tokens | Cache read / write | Time |
|---|---|---|---|---|
| `rule` (default) | 0 | 0 / 0 (ran with AWS configuration disabled; no model is built) | 0 / 0 | 100 new episodes: 3.7 s · 1000 new episodes into an empty Semantic layer: 385.9 s · 10 new episodes with 489 Semantic entries: 11.2 s · nothing new: 1.0 s (100) / 3.1 s (1000) · `--dry-run`: 1.0 s (100) / 1.5 s (1000) |
| `llm` (Bedrock, default `distiller_model_id`, ap-northeast-1) | one per episode not yet scored by this model | 2 episodes (prompts 1190 characters): call 1 213 / 185, call 2 266 / 111; 479 / 296 in total | not reported (the distiller sets no cache point, #254) | 2.18 s and 1.71 s per call (model latency 2138 ms / 1704 ms), 4.0 s for the run |
| `llm`, other providers or models | — | not measured | not measured | not measured |
| `jev` | 0 — the distiller is a stub (#106); every episode fails, exit 1 | 0 / 0 | 0 / 0 | not measured |

What the numbers depend on:

* **Episodes, not days.** One chat turn writes one episode; a run scores only episodes not
  yet scored by the same distiller version. `--dry-run` reports `harvest.to_score`, and for
  `llm` `estimate.model_calls`, `estimate.prompt_chars` (the exact text of every call) and
  `estimate.max_output_tokens` (the per-call cap, 1024, times the calls) — the tokens
  themselves are known only after the call. **These are a guide, not a strict bound:**
  `prompt_chars` counts the characters of the user message only, not input tokens (the
  request adds message framing, and characters per token vary by language); `model_calls`
  counts one call per episode, while the SDK may retry a throttled or failed request, so
  the provider can see more calls than that; and the estimate can be stale by the time the
  run starts (see the wrapper, step 2). For a daily figure, multiply your turns per
  day by tokens per call; the two calls above are the only measured calls.
* **Switching modes re-scores everything.** Episodes harvested by `rule` are scored again
  by `llm` (and by another model): measured with `--dry-run`, 20 of 20 already-harvested
  episodes were `to_score` after switching to `"llm"`.
* **Rule runs grow with the Semantic layer.** Each new episode that is not discarded is
  checked for duplicates against the Semantic layer, so a large first run is slow (1000
  episodes: 6.4 minutes) while later runs that only add a few episodes stay short. Run
  the first sweep by hand.
* **One memory per call (fixed by this change).** Before it, the LLM distiller reused one
  conversation for the whole sweep: the n-th call resent every earlier memory and answer
  (offline fake model: 1, 3, 5 messages for three episodes), so input grew with every
  episode and one memory's text could influence the next scores. Now every call sends one
  message (both Bedrock calls above: 1 message each).

## Retention, delete requests and provenance (#294)

Decided in #294. **Default: nothing is ever deleted, and nothing here runs by itself** —
no timer, no scheduler job, no heartbeat work (ADR 0008 keeps scheduled jobs
model-free; a retention job, if ever added, would be a Gateway action a human creates
with `confirm`). Three operator commands; the agent has no tool that runs them. The
report, the dry runs, `jobs` and `removed` only read; `--apply`, `--resume` and
`restore` change data:

```bash
stira memory retention --data-dir .local/stira            # report (read only)
stira memory retention --data-dir .local/stira --apply    # archive, then remove what is over the limits
stira memory forget --data-dir .local/stira --query "a phrase"        # where it is (read only)
stira memory forget --data-dir .local/stira --id episodic/chat-123    # an entry + what was derived from it
stira memory forget --data-dir .local/stira --query "a phrase" --apply   # delete every copy (irreversible)
stira memory forget --data-dir .local/stira --resume <job-id> --query "a phrase"  # finish a failed job (same --id/--query)
stira memory jobs | removed | restore <id> --data-dir .local/stira
```

(`python -m stiracore.control.memory_cli ...` is the same without the `stira`
entry point.) `--apply`, `--resume` and `restore` hold the data directory's owner lock
(`server.lock`, #285): while the server runs they exit **3** and change nothing — stop
the server first. Exit codes: 0 done, 1 refused/failed, 2 usage, 3 owned.
An error no command formats (the pre-apply SQLite recovery refusing a link, #402) prints
a fixed code and the exception type only — `{"refused": {"code": "E_UNSAFE_PATH
(UnsafePathError)"}}` — and exits 1: never its message or a traceback, on stdout or
stderr (a path can hold the very words being deleted).

### Three ways to take text out, on purpose different

| | Forgetting (#320) | Retention run | Delete request |
|---|---|---|---|
| Command | `distill --apply-forgetting` | `memory retention --apply` | `memory forget --apply` |
| What | compresses old Semantic entries | removes what is over *your* limits | removes one memory/phrase everywhere |
| Copy kept | yes, `forgotten/<id>.json` | yes, `forgotten/removed-<id>.json` | **no** — and it removes the matching archived copies too |
| Way back | `--undo-forgetting <id>` (explicit, audited) | `memory restore <id>` (explicit, audited) | none |

Forgetting and retention are about space and noise, so they must be reversible. A
delete request is about the text itself, so leaving an "old value" in an archive or a
history table would defeat it (the failure mode the research in #294 / #342 points at).

### Retention: what each store gets

Limits live in `agent-config.json` under `memory_retention`
([configuration §4](configuration.md#4-agent-configjson-agentconfig--checked)); without it, every limit is unset
and the report lists no candidates — except plan files of memory jobs, which have a default
limit of 90 days (still applied only by `--apply`). Example:

```json
{"memory_retention": {
  "episodic": {"max_age_days": 365, "max_entries": 20000},
  "turns": {"max_age_days": 180},
  "tool_payloads": {"max_age_days": 30},
  "checkpoints": {"max_per_thread": 5},
  "staging": {"warn_after_days": 30},
  "scheduler_log": {"warn_bytes": 50000000}
}}
```

| Store | Decision | `--apply` |
|---|---|---|
| Episodic (`episodic/*.md`, one per turn) | `max_age_days`, `max_entries` (oldest first), `expires_at` in the front matter only with `honour_expires_at: true`. Episodes Harvest has not scored yet are **held** (`keep_unharvested`, default true) | archive, remove, index row and emotion record (#364) too; `restore` brings both back |
| `turns.db` UI events | `turns.max_age_days`: a turn goes with all its events and tool payloads | archive rows, remove, compact |
| `turns.db` tool payloads | own `tool_payloads.max_age_days` (tool observations are kept apart from the conversation and usually expire sooner; payloads without a turn count as old) | archive rows, remove, compact |
| Checkpoints (`checkpoints/`, ≤20 per thread already) | `max_age_days`, `max_per_thread`; **the newest of a thread is never a candidate** (a resume needs it) | archive, remove |
| Plan files of memory jobs (`memory-plans/`, ADR 0018, [memory-autonomy.md](memory-autonomy.md)) | `memory_plans.max_age_days`, **default 90** without any setting (age = file modification time; leftovers of an interrupted write count too) | archive, remove; `restore` brings them back |
| Semantic, `staging/` | report only: forgetting is `--apply-forgetting`; a staged entry waits for a human (`warn_after_days` only counts them) | — |
| Procedural, search index, context stash (`retention_hours`, #258), staging-rejection ledger | report only | — |
| `scheduler-events.jsonl`, `events.db` | report only (`warn_bytes`): hash-chained with a head anchor, so rotation needs a chain-segment design (follow-up) | — |
| Archive (`forgotten/`) | report only (`warn_bytes`); it holds full text — a delete request removes the copies it matches | — |

The report shows, per store, count, bytes, oldest/newest, the limits, candidates and
what `--apply` would do, plus `origin`/`source` counts (below). It opens SQLite files
read-only and creates nothing (on an empty directory it writes no file; with a running
server it reads through the WAL). `--apply` plans again under the lock, writes one
removal record, reads it back, logs `remove.started`, removes, compacts the SQLite files
(`secure_delete`, `VACUUM`, WAL truncated) and logs `remove.applied` in
`forgotten/forgetting-log.jsonl` (ids, counts, digests; never text). Like a delete
request, `--apply` is refused when the plan made under the lock skipped a store (e.g. a
`turns.db` without its tables): a limit is applied only to what was fully seen. The
refusal prints a code and the skipped stores as area labels and digests (the report
shows where they are).
`memory restore <id>` puts everything back byte for byte (rows with their ids, files
with their times) and refuses before changing anything when something *different* now
sits where an item goes. Only an item equal in every column (rows: id, turn, seq, type,
time, payload; payloads: key, text and counters) or byte (files) counts as already
restored; rows are written first in one transaction checked again row by row, so a
conflict found then also changes nothing and the record is kept. A refusal says
`E_CONFLICT: <area> <digest>` (or a fixed code), in the output and in the audit log —
never a key or a path, which can carry a memory's words.

### Delete requests: every copy, as a resumable job

`forget` searches (literal, case-insensitive, also the `\uXXXX` form; at least 3
characters) every place the data directory keeps text and sorts what it finds:

* **reached** — Episodic entries; Semantic entries including `staging/` and every entry
  whose `source_id` is a removed episode; the emotion record of every removed episode
  (`emotion/<digest>.json`, #364: no text, but how that moment felt is part of what the
  person asked to forget; removed before its episode, as `MemoryStore.delete` does);
  their `index.sqlite3` rows and stale rows that match; `semantic/index.md` (regenerated); `staging_rejections.jsonl` lines;
  checkpoints; `context-stash/` files; `turns.db` UI events and tool payloads; plan files
  of memory jobs (`memory-plans/`) that name a forgotten entry's key or hold the text —
  removed whole, also with `--id` alone (a plan is a proposal; the next run writes a new
  one); the forgetting archive records and retention records that hold it (a record that also
  holds other items is rewritten without the match, so `restore` can never bring the
  deleted text back).
* **not reached** (listed with the reason, never edited) — `events.db`,
  `scheduler-events.jsonl`, `hitl-*.db`, `forgetting-log.jsonl` (hash-chained: editing a
  line breaks the chain; they are meant to hold masked summaries, ids and digests),
  `emotion-log.jsonl` (#364's clean-up log: record file names and reasons only — the
  tests check it never holds the deleted text), the
  Procedural layer (`procedural_cli` only), `workspace/` and other files that are yours,
  and everything outside the data directory: server logs, backups and snapshots,
  provider-side prompt caches (they expire on the provider's side), a running process.

`--id <layer>/<key>` alone reaches the entry and what was derived from it; add
`--query` with a phrase to reach the conversation copies (checkpoints, `turns.db`, the
stash). The dry run never prints the query back (only its SHA-256).

What is forgotten is tracked from the request, independently of which files exist
now: an entry that only survives in an archive (a retention record, a #320 forgetting
record) still counts, and so does everything derived from it (`source_id`), including
a compressed live entry whose archived original matches. Archived files (checkpoints,
stash files, emotion records) are stored Base64-encoded in a retention record; they are
decoded before they are searched, so a phrase archived by retention cannot come back
through `restore` after a delete.

`--apply` is refused before anything changes when a store could not be inspected (the
dry run lists it under `skipped`): a directory that cannot be listed (no permission) is
never skipped silently, and a SQLite file with a `-wal` but no `-shm` is not opened
(reading it would create a file, so the dry run does not; `--apply` lets SQLite recover it under
the owner lock first). It writes a job `forgotten/delete-<id>.json` — `pending` →
`running` → `completed` | `failed`, `attempts` — with the inventory (locations and
SHA-256 digests, no text) and runs it step by step, saving the state after every step.
The search sources go first (entry files, then their index rows), so the entry is out
of `memory_search` even if a later step fails. Every step is checked (the file or row
must be gone), not assumed. The derived steps — regenerating `semantic/index.md`
(checked: it must not list a deleted entry) and compacting SQLite — wait while a
removal step has failed and run again on every attempt. After the steps, the request is
planned again over every store; whatever is still found is inventoried and removed in a
second round, and if anything is left the job is `failed` (`left`). Steps only ever
remove: a retry or `--resume` never restores anything, and a completed job is not run
again. In **every** state the job file is written from an allow-list and holds digests,
fixed labels and codes only — the SHA-256 of the query, of each requested id, of each
step's location and of each leftover's path (with a fixed area label such as `emotion`
or `checkpoints`), and an error as a fixed code and exception type
(`E_PERMISSION (PermissionError)`, never its message: it can name a file whose name is
the very words being deleted). The job's command output follows the same rule (counts,
labels, digests); only the dry run shows locations, to the operator who asked. So
`--resume` of an unfinished job needs the same `--id`/`--query` (checked against the
digests; refused otherwise — nothing could be verified) and plans the request again
**only to locate the job's own steps**: a step runs when its location digest is one of
the job's, and removes the item only while its content still has the digest the job's
inventory saw (never re-inventoried with what is there now, #402; a
`staging_rejections.jsonl` line, whose number moves when an earlier line goes, is matched
by its content digest, at most as many identical lines as the job inventoried). A copy the job never
inventoried (one that turned up later) is not removed by a resume: the verification
lists it under `left` and the job stays failed — a new delete request (`--apply`)
removes it. A resume has no second round. A
`--resume` of a completed job only reports it and checks nothing (it is never run
again). The job also keeps the digests of everything the request resolved to, derived
entries included: an entry found only through its source (`source_id`) is still found
by a resume after the source itself is gone. An item that
changed since the inventory is left in place and its step fails (also a derived entry a
legitimate writer rewrote before the resume; the index row of an entry left in place is
kept too). The plan and the
verification look at the same files: dot files in `context-stash/` and `checkpoints/`,
and stray files in the reached stores (an interrupted write's temporary file in
`episodic/`, `semantic/`, `emotion/`, `forgotten/` or the memory directory) are removed
too. The last check runs the search over the raw bytes of **every** file of the data
directory (`residual`): a reached store or the archive that still holds the text — or
that could not be read — fails the job (`left`, exit 1), also on `--resume`; only the
not-reached places may remain (an unreadable one is listed as "not readable").
Audit lines:
`delete.started` / `delete.completed` / `delete.failed` and one `purge.applied` per
archive record removed or rewritten — ids, counts and the query's digest only.

Checkpoints hold the whole conversation of a resume: deleting one means a pending
approval of that batch can no longer be resumed (the dry run says so). Deliberate: the
delete wins.

**Limits (accepted, not fixed here).**
- *The archive is plain text.* Retention records and #320 forgetting records keep the
  full text (Base64 is an encoding, not encryption). They sit in the same protected
  place as the memory itself (`0700`/`0600`, on the file tools' read and write floor) and
  the memory is plain text too, so who can read them does not change — but it is one more
  readable copy until a delete request removes it (a delete always removes the archived
  copies as well). Encryption at rest needs key management: a separate issue.
- *Parent-directory swaps.* Removals check a file (no link at any component, digest)
  and then unlink it by path. A non-cooperating process that can write the data
  directory could swap a parent directory between the check and the unlink; the owner
  lock does not stop a process that ignores it. Descriptor-relative (`dir_fd`) opens and
  unlinks for every memory path are the follow-up in #294.

Measured (2026-10-18, macOS arm64, a real `stira_server` with a fake chat model and
`STIRA_SCHEDULER=1`, data directory under `/tmp`, no Bedrock): four chat turns, one with
a canary phrase. The report ran while the server was up (episodic 4, `turns.db` 4 turns
/ 12 events, checkpoints 4, no file created). `forget --query` found the phrase in 1
episode, 1 index row and **3 checkpoints** (every later checkpoint carries the history);
`--apply` exited 3 while the server ran, and after it stopped completed in one attempt;
`grep -r` over the data directory found the phrase nowhere; the server restarted and
chatted normally and the phrase did not come back. A retention run with
`checkpoints.max_per_thread: 1` removed 2 checkpoints and `restore` put both back. The
tests (`tests/test_memory_retention_forget.py`) build every store with synthetic data
and check the raw bytes of every file after a delete, a failed and resumed job, and a
retention run with its restore.

### Provenance: `origin` and `source`

New entries carry two front-matter fields: **`origin`** — who wrote the text: `person`
(typed by the person), `extracted` (taken from the conversation without rewriting: an
Episodic turn summary), `model` (written by a model: the agent's `memory_write`),
`assistant` (attributed to the assistant's output) — and **`source`** — the channel or
writer (`web-chat`, `memory_write`). Entries written before #294 have neither and count
as `unknown`; nothing rewrites them. The fields label; they grant nothing.

The Episodic front matter alone is not trusted: `episodic/` is a write root of the
agent's file tools, so the agent could rewrite `origin: person` into an episode and keep
its body. The server's writers (`record_episode`, `memory_write`, and Harvest when it
re-marks an episode) therefore also record the value in `index.sqlite3` (on the floor:
the file tools can neither read nor write it), bound to the SHA-256 of the file's whole
text. Only a value bound to the file's current bytes counts: the report counts any other
front-matter claim as `unverified`, and Harvest copies `origin`/`source` to the Semantic
entry only when they are bound (`semantic/` is on the floor, so its front matter is what
Harvest wrote). Restoring a retention record restores the binding; deleting the index
loses all bindings: entries that claim an origin then read `unverified` and the others
`unknown` — never a claimed value as verified. Tested with the
real write policy and parser (`test_a_forged_origin_in_episodic_front_matter_is_not_trusted`). `expires_at` (ISO 8601) is a TTL field any writer may
set; only a retention run with `honour_expires_at: true` acts on it (nothing sets it
yet). It is not a knowledge-validity date (`valid_until`, #173).

### Follow-up (not in this change)

A head anchor for `forgetting-log.jsonl` (a removed last line is not detected);
descriptor-relative (`dir_fd`) no-follow opens for every memory path (this change opens
path-based with an `lstat` walk and `O_NOFOLLOW`, see the limits above); encryption at
rest for the archive; rotation of the hash-chained logs;
search-time filters by `origin`/`source`; never auto-promoting `origin: model` past
staging; a delete request for the Procedural layer.

## Importance tags: `retention: invariant | derived` and a half-life (#173)

**Default: unchanged.** An entry without a tag is `derived` with the reference half-life
of 30 days, which selects exactly what was selected before (30/90/180 days, stale at
365). Nothing sets a tag by itself; no new automatic deletion exists.

Two front-matter fields:

| Field | Values | Effect |
|---|---|---|
| `retention` | `invariant` | Never forgotten by maintenance: not compressed by `--apply-forgetting` (listed under `forgetting.protected` instead), never a duplicate or stale deletion candidate (in a duplicate group the invariant copy is the one kept), and an Episodic one is held by `memory retention --apply` (`held_invariant`). |
| | `derived` (= missing) | Can be regenerated; fades with its half-life. |
| `retention_half_life_days` | integer 1–3650 (default 30) | A `derived` Semantic entry's half-life. |

**From days to an importance score.** An entry not read for `age` days keeps the strength
`2 ** (-age / half_life)` (shown as `strength` with every candidate). The stages are fixed
points on that curve, set by the reference half-life so that untagged entries keep the old
day counts:

| Stage | Strength at or below | Half-lives | Untagged | Half-life 10 | Half-life 60 |
|---|---|---|---|---|---|
| `light` (≤ 4000 chars) | 1/2 | 1 | 30 days | 10 days | 60 days |
| `compact` (≤ 2000) | 1/8 | 3 | 90 days | 30 days | 180 days |
| `archive` (≤ 800) | 1/64 | 6 | 180 days | 60 days | 360 days |
| stale (deletion candidate) | 2^(-365/30) ≈ 0.00022 | ≈ 12.2 | 365 days | 122 days | 730 days |

The comparison is done in integers (`age × 30 ≥ threshold × half_life`), so an untagged
entry crosses a stage on exactly the same day as before. `age` is still counted from
`last_accessed` (above): reading or searching an entry restarts its curve. Associative
recall (#170) reads Episodic rows only and touches no access time, so it does not
reinforce a Semantic entry yet.

Worked examples (`age` = days since `last_accessed`, strength rounded to 4 places as in
the preview):

| Entry | Age | Strength | Integer check | Proposed action |
|---|---|---|---|---|
| untagged (half-life 30) | 45 | 2^(-45/30) = 0.3536 | 45×30 = 1350 ≥ 30×30 = 900, < 90×30 = 2700 | `light` |
| `retention_half_life_days: 90` | 45 | 2^(-45/90) = 0.7071 | 1350 < 30×90 = 2700 | none (not listed) |
| `retention_half_life_days: 10` | 45 | 2^(-45/10) = 0.0442 | 1350 ≥ 90×10 = 900, < 180×10 = 1800 | `compact` |
| untagged | 400 | 2^(-400/30) ≈ 0.0001 | 400×30 = 12000 ≥ 180×30 = 5400; ≥ 365×30 | `archive`, and a stale deletion candidate |
| `retention: invariant` | 400 | — (not scored) | — | none; listed under `forgetting.protected` |

**Reinforcement hook (not wired yet).** `stiracore.memory_importance.touch_recalled(store,
key, at=None)` restarts a Semantic entry's curve because it was recalled: it moves the
index `last_accessed` forward to `at` (default now) — never backwards, so a stale or
replayed time cannot make an entry fade faster — and changes nothing else (same file
bytes, same modification time, the tag untouched; no index row is created for an unknown
key, which returns `False`). It is a host-side function, not an agent tool, and nothing
calls it yet; associative recall (#170) can call it later for the Semantic entries it
actually used. Reinforcement only restarts the clock: it never lengthens a half-life or
grants `invariant` (that stays `stira memory tag`, below).

A value outside the contract (`retention: forever`, a half-life of 0, `"10"`, `true`,
3651…) is ignored: the entry is selected as if untagged and the candidate says
`retention_invalid: true` (`invalid retention tag ignored` in its reason). An unreadable
tag never protects an entry and never makes it fade faster.

**The preview shows the list and the reasons** (`scripts/stira_ctl.sh distill --dry-run`,
or `python -m stiracore.control.distillation_cli --base-dir <memory base> --config
<agent-config.json> --dry-run`; it prints one JSON object, so `… | jq .forgetting` shows
just this part). `distill --dry-run` now carries
`forgetting.items` (stage, age, half-life, strength, reason) and `forgetting.protected`
(`code: retention_invariant` and the reason) without writing anything. Both name an entry
by a fixed area label (`semantic` or `staging`) and `key_sha256`, the SHA-256 of
`semantic/<key>` — **never the key**, which can carry a memory's words; it is the same
digest `stira memory tag --id semantic/<key>` prints, so you can check a key you suspect.
The dry run's items are an allow-list — area, digest, stage names, numbers, the index
time `last_accessed` and a reason built from those — so no front-matter string reaches
it: a `forgetting_stage` that is not a known stage name is shown as `unknown`.
A `distill` run carries `forgetting.protected` in the same form (its `forgetting.items`
predate #173 and still show the key). The retention report counts Episodic tags under
`stores.episodic.retention`: `invariant` (recorded by a human), `held_invariant` (over a
limit, kept), `unverified_held` (recorded, but the file changed since: kept, look at it)
and `unverified` (a front-matter claim nobody recorded: not kept).

**Only a human sets a tag.**

```bash
stira memory tag --data-dir .local/stira --id semantic/knowledge/general/x --retention invariant          # preview
stira memory tag --data-dir .local/stira --id semantic/knowledge/general/x --retention invariant --apply  # write it
stira memory tag --data-dir .local/stira --id semantic/knowledge/general/y --retention derived --half-life-days 90 --apply
stira memory tag --data-dir .local/stira --id episodic/chat-123 --retention invariant --apply
stira memory tag --data-dir .local/stira --id semantic/knowledge/general/x --retention none --apply       # remove it
```

Without `--apply` it shows the current and the new tag and reads only (for an episode
also `recorded`: the human's tag and whether the text is still the one tagged,
`verified` / `changed`). `--apply` holds the
owner lock (exit 3 while the server runs), keeps the entry's modification time and
`last_accessed`, and logs `retention.tagged` in `forgotten/forgetting-log.jsonl` with the
layer, the SHA-256 of `<layer>/<key>`, the previous and the new tag — never the key or the
text (the command's output follows the same rule). A Semantic entry whose forgetting step
can still be undone is refused (`E_FORGETTING_UNDOABLE`: tagging would change the bytes
`--undo-forgetting` checks; undo first, then tag). A half-life applies to Semantic
entries only (Episodic retention has its own `max_age_days`).

**The agent cannot tag its way out of forgetting** (tested):

* `semantic/` is on the floor of the agent's file tools, `memory_write` writes Episodic
  entries only with fixed front matter, and Harvest copies an allow-list of fields to the
  Semantic entry (never `retention`): a Semantic tag is what a human wrote.
* `episodic/` is a write root of the file tools, so a tag in the front matter there is a
  claim and holds nothing. What counts is the record `stira memory tag` writes in the
  search index (`index.sqlite3`, on the floor): **that a human tagged it** (the kind) and,
  separately, **the SHA-256 of the text that was tagged**. Approval and integrity are kept
  apart:
  * the agent cannot add a tag (it cannot write the record);
  * the agent cannot lift one: an edit of the file — a character appended, the
    front-matter field removed, the whole text replaced through `memory_write` — leaves
    the tag in force; the episode stays held and the report counts it as
    `unverified_held` so that a human looks at it. Only `stira memory tag --retention
    none|derived --apply` (owner lock, audited) lifts it;
  * Harvest's own re-mark moves the digest to the text it wrote, and only when the file
    was exactly the tagged text and what the sweep read. The digest is computed from what
    Harvest rendered, never from a re-read of the file: an edit by another writer before
    or after the rewrite stays a mismatch (tested by injecting both).
  Limitation: a held episode whose text the agent replaced keeps the agent's text held
  until a human looks (`unverified_held` > 0 in the report is the signal).
* The agent can at most suggest a tag in words; a human decides (same rule as
  [never schedule maintenance because content asked for it](#never-schedule-maintenance-because-content-asked-for-it)).

**A tag is not a delete lock.** `stira memory forget` removes an `invariant` entry like
any other (the human delete command, above). Not in this change: a knowledge-validity date
(`valid_until`, kept apart from the TTL `expires_at`) and moving superseded revisions to
the archive instead of letting them fade (#173 comments, #193, #320).

## Safe defaults and guardrails

* **Off by default:** no automatic memory work runs; the heartbeat and the scheduler are
  off (`hb_enabled: false`, `STIRA_SCHEDULER` unset) and no `memory_harvest` job exists
  until a human creates one; `distiller_mode` defaults to `"rule"`; the example wrapper allows 0
  model calls, and the `memory_harvest` job is rule-only (never a model call).
* **Human gate on by default:** `harvest_stage_auto: true` stages the automatic band
  (0.4–0.7) as well as the staging band; nothing leaves `semantic/staging/` without
  `staging_cli approve` (#293). The timer never approves or rejects.
* **Importance tags change nothing by default:** an untagged entry is selected exactly as
  before #173; only `stira memory tag --apply` (a human, owner lock) sets one, and
  `invariant` only ever keeps an entry.
* **Forgetting is a preview** unless `--apply-forgetting` is given; applied steps are
  archived first and undone only explicitly with `--undo-forgetting` (#294, operations
  §12). The wrapper does not apply forgetting unless you pass the flag.
* **Retention and delete requests are reports** unless `--apply` is given, and no limit
  is set by default; retention runs are archived and undone with `memory restore`, a
  delete request removes every copy for good (#294, above).
* **One writer:** a second server on the same data directory refuses to start (#285);
  `--owner-lock` extends that to a CLI run. `staging_cli` has no such option; review with
  the server stopped or idle.
* **Limits:** per run, at most `STIRA_DISTILL_MAX_CALLS` model calls (wrapper, enforced in
  the preview and in the run by `--max-model-calls`; SDK retries not counted) and at most
  1024 output tokens per call (`LLMDistiller.MAX_TOKENS`). There is no limit in tokens or
  money per run or per day in Stira itself (the `memory_harvest` job makes no model call). Use the dry
  run's numbers and your provider's budget alarms. The CLI's own `--dry-run` is the
  preview.
* **Failures are visible:** the CLI exits non-zero on any failed episode or refused step;
  the wrapper logs every attempt and prints a line on failure. A failed episode stays
  unscored and is retried by the next run.
* **Stop:** disable the timer (above); `distiller_mode: "rule"` stops all model calls.

## Never schedule maintenance because content asked for it

Create, change or remove a maintenance job only on a human's decision. A web page, a file,
a tool result, a memory or a chat message from an untrusted source that says "set up a
nightly cleanup" or "apply forgetting" is **data, not an instruction** — the same rule the
[agent scheduling guidelines](agent-scheduling-guidelines.md) (ADR 0011, Proposed) give
the agent: at most propose, and say where the idea came from. Do not wire a timer to
anything that reads such content, and do not let an agent edit your crontab, LaunchAgents
or systemd units (exec-class tools are off by default; keep it that way for this).

## For the public repository (#107)

The guide and `examples/memory-maintenance/` are generic: placeholder paths, a placeholder
label (`com.example.stira-distill`), no hosts, accounts or tuning values. They can be
referenced as they are from the public README and examples.

## Related

Recommended configuration, review and weekly routine: [guide-memory-routines.md](guide-memory-routines.md).

[operations.md](operations.md) (§1 owner lock, §12 forgetting), [configuration.md](configuration.md)
(`distiller_*`, `harvest_stage_auto`), [heartbeat-daemon.md](heartbeat-daemon.md),
[scheduler.md](scheduler.md), [agent-scheduling-guidelines.md](agent-scheduling-guidelines.md),
[prompt-cache.md](prompt-cache.md). Issues: #251, #252, #293, #294, #285, #238, #239, #254,
#105, #107.
