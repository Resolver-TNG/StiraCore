# ADR 0018: Autonomous memory recording and periodic maintenance — importance rated by the agent at creation, memory jobs outside the tick, reversible-only apply

**Path:** `docs/adr/0018-autonomous-memory.md`
**Status:** Accepted (2026-10-20, commander decisions below). **Phase 0 (observe only) is
scheduled for implementation** (branch `feat/251-memory-autonomy-phase0`); phases 1 and 2 each
need their own go from the commander before any work starts. Nothing beyond phase 0 is
authorised by this acceptance.
**Issue:** #251 (Harvest automation, model scoring), #252 (self-maintenance job kind).
Related: #238 / #105 (heartbeat, autonomous base), #173 / #425 (retention tags, half-life),
#170 / #421 (associative recall), #294 (retention, archive, provenance), #459 / #458
(memory poisoning, trust labels), #463 / PR #503 (`propose_procedure`, merged
after this ADR was drafted), #464 (`memory_write` body refused by the floor), #139 (procedural self-improvement),
#193 / PR #394 (staging review UI, paused), #143 (ablation harness), #456 (the agent's own
feedback), [ADR 0003](0003-memory-design.md) (three layers), [ADR 0008](0008-scheduler.md)
(scheduler), [ADR 0011](0011-agent-self-scheduling.md) (self-scheduling, Proposed),
[ADR 0013](0013-audit-event-contract.md) (audit contract, Proposed),
[ADR 0017](0017-emotion-proximity-recall.md) (emotion-proximity recall, Accepted), #506
(forged front-matter `importance`, facts 6 and 7 below).
**Decision (accepted 2026-10-20):** memory stays three genres (episodic / semantic / procedural) and
emotion snapshots stay raw values. The agent rates importance **when a memory is created**
with one small in-turn tool, `memory_rate`; the harness — not the model — stamps who, when,
which turn and the turn's trust, and binds the rating to the text's SHA-256 in the search
index. The rating may **raise** routing (never lower it below the rule score), sets a
**derived** half-life only inside `[7, 180]` days, and is capped when the turn saw untrusted
input. `invariant` stays a human decision: the agent may only propose it, through the same
propose → review pattern as #463. Periodic work runs as **human-created scheduler jobs of
fixed kinds** in a separate executor; the heartbeat tick stays model-free and provably so.
Jobs start only at breaker NORMAL, have token caps per run and per day, and in phases 0–1
write proposals only. From phase 2 a short whitelist of **reversible** steps (archive +
ledger + undo window) may be applied without a person; nothing is ever deleted silently.
Everything is behind `memory_autonomy` in `agent-config.json`, default off.

## Where the line is

Operator direction (2026-10-20): *memory stays three genres; emotion values are stored as
raw values; importance is assigned by the agent itself at creation; the agent records
memory autonomously and tidies it up periodically, through the heartbeat and cron.* The rest
of this ADR follows the project's usual defaults: opt-in, fail-closed, human gates where
poisoning matters.

* **In scope:** an in-turn rating tool; harness-stamped, bound importance fields for all
  three genres; fixed-kind memory jobs on the scheduler (rating backfill, consolidation
  proposals, tidy plans); reversible auto-apply of whitelisted steps from phase 2.
* **Not in scope:** the agent writing `invariant`, approving anything, writing `semantic/`
  outside `staging/`, or touching `procedural/`; any job reading or writing Identity Anchors,
  SOUL, `EMOTION_MATRIX` or breaker thresholds (L3+); deletion without an archive; the
  heartbeat tick calling a model; the agent creating or widening memory jobs (that stays
  ADR 0011); outbound notifications beyond what the scheduler has.

## Decisions (commander, 2026-10-20)

The commander approved every recommendation of this ADR ("approve all of the assistant's
recommendations"). The questions at the end are resolved as follows; the sections they refer
to are the decision text.

| # | Question | Resolution |
|---|---|---|
| Q1 | How the agent's rating acts | **As recommended (§1d B):** routing uses `max(rule_importance, agent_importance)`; the half-life comes from the agent's value inside **[7, 180] days** (table in §1d); recall uses the agent's value when bound. For `untrusted` / `unknown` turns the effect on recall and half-life is capped at **0.5** (§1e); routing is not capped because it only sends more to review. |
| Q2 | `invariant` | **As recommended (§1f ii):** the agent may only *propose* it; a person approves it through the #463 propose → review pattern. Refused for untrusted / unknown turns; never answered by YOLO. `memory_rate` itself never accepts `invariant`. |
| Q3 | Tool rule for `memory_rate` | **As recommended:** listed in the tool rules, default **Always**. It can only bind a harness-stamped rating, never change retention beyond the bounds and never write memory text; Never turns in-turn rating off. |
| Q4 | Model-calling memory jobs after a restart | **As recommended:** they come up paused and do **not** resume until a person resumes them (the ADR 0011 restart contract); model-free kinds keep the ADR 0008 missed-run behaviour. |
| Q5 | Model and budget | **As recommended:** the existing small distiller model (`distiller_model_id`); **100,000 tokens per UTC day in total across all memory jobs**, with the per-run and per-kind caps of §2d. |
| Q6 | `harvest_stage_auto: false` | **As recommended:** removed **in phase 1** (the automatic band is then always staged). Phase 0 does not change it. |
| Q7 | Phase-2 whitelist and undo window | **As recommended:** forgetting `light` / `compact`, retention under limits a person set, index rebuild; undo window **30 days** (§3). Phase 2 still needs its own go. |
| Q8 | Turn-level taint before #458 | **As recommended:** any web, file, MCP or child-agent result in a turn (and any episode read back with `trust = untrusted`) makes the turn `untrusted` until #458 replaces it with data-level labels (§1e). |

**Also from the commander (2026-10-20), restated as constraints of every phase:** memory stays
**three genres** (episodic / semantic / procedural; no new layer); emotion values are stored
**raw** ([ADR 0017](0017-emotion-proximity-recall.md), Accepted); importance is assigned by the
**agent itself at creation** (`memory_rate`, harness-stamped); autonomous recording and
maintenance run **through the heartbeat and cron** (human-created scheduler jobs of fixed kinds
in their own executor; the tick itself stays model-free).

**Scope of this acceptance.** Phase 0 only (§6): `memory_rate` binds ratings that nothing uses
yet; forged front-matter `importance` is ignored by Harvest and recall (facts 6, 7; #506);
the three memory job kinds write plan files only; the tick guarantee gets its import-graph test
and the fixed `MODEL_CALLING_KINDS` list. Phase 1 (routing, proposals, invariant proposals,
removal of `harvest_stage_auto: false`) and phase 2 (reversible auto-apply) each start only
after a separate go.

## Context (read in code at `main` = `9c3f0fd`; M = covered by an existing test)

| # | Fact | Where |
|---|---|---|
| 1 | Every turn writes one episode (`record_episode`, deterministic summary, no model). It already stamps **`importance`** = AmygdalaPass rule score of the *original* turn, with `importance_basis: original-turn` and `importance_version` (#293); `extra_metadata` cannot set them | `tools/episodic_formatter.py` |
| 2 | The emotion snapshot of an episode (six dimensions, 3 decimals, breaker level, turn ids) is a sidecar record in `<memory_dir>/emotion/`, bound by key + body SHA-256, on the data-directory floor; tools, index, Harvest and the prompt never see it | `tools/episodic_emotion.py` |
| 3 | The episodic layer is a **write root of the agent's file tools**, so its front matter is a claim. `origin` / `source` count only as recorded in `index.sqlite3` (on the floor) bound to the text SHA-256; Harvest re-binds after its own mark | `memory_origin.py`, `control/harvest.py` |
| 4 | `memory_write` writes or **replaces** an episodic entry, `origin: model`; other layers refused. It is not on the tool-rule list (#459); its `content` is still scanned by the identity floor and falsely refused on a lone `/` (#464) | `tools/memory_tools.py` |
| 5 | Harvest: `score < 0.4` discard, `0.4–0.7` auto band, `≥ 0.7` staging. **Correction to the premise of #251's thread:** since #293 `harvest_stage_auto` defaults to `true`, so the auto band is staged too; only a manual `distillation_cli` run with `harvest_stage_auto: false` writes `semantic/knowledge/` without review. The scheduled `memory_harvest` job always stages (M `test_harvest_job.py`) | `control/harvest.py`, `docs/configuration.md` |
| 6 | `RuleBasedDistiller` uses a recorded `importance` as a **floor** (higher of recorded and text score). The recorded value is front matter, **not bound**: an edited episode can raise its own band. With `harvest_stage_auto: false` that reaches `knowledge/` | `control/distillers.py` |
| 7 | Associative recall (opt-in, #421) reads front-matter `importance` (weight 0.15, default 0.5) — also unbound | `agent/associative_recall.py` |
| 8 | Retention tags `invariant` / `derived` + `retention_half_life_days` ∈ [1, 3650], reference 30: **human only** (`stira memory tag --apply`, owner lock, bound in the index); Harvest never copies `retention` | `memory_importance.py` |
| 9 | Forgetting compresses Semantic text by half-life stages (light / compact / archive); manual only; archived first in `forgotten/` with a hash-chained `forgetting-log.jsonl`, explicit undo. Retention `--apply` (episodes, turns, checkpoints) is archived and restorable; delete requests are irreversible and reach the archives | `control/memory_maintenance.py`, `forgetting_archive.py`, `memory_retention.py` |
| 10 | Heartbeat tick: decay under `engine.state_lock`, snapshot, breaker *query*. No model (M `test_tick_has_no_model_or_agent_dependency`, `test_a_tick_never_calls_a_model`, `test_stira_a_tick_reaches_no_model_classifier_agent_tool_or_job`) | `heartbeat/gateway.py` |
| 11 | Scheduler: the chat server registers Gateway actions `health_check` and the rule-only `memory_harvest` (M `test_harvest_job_module_has_no_model_path`). Agent-layer jobs exist in the library (`AgentJobExecutor`, read-only tools, per-run / per-day token caps, reservations) but are **not enabled** on the chat server | `scheduler/`, `dashboard/stira_server.py` |
| 12 | Procedural: PR #503 (merged since) adds `propose_procedure` (writes `procedural/staging/` only, `source_turn` stamped by the host, rule Ask, YOLO never answers it) and an approve / reject panel on `/memory` | PR #503 |
| 13 | Semantic staging has only `staging_cli` (list / show / approve / reject); the review UI (#193) is open, its backend PR #394 paused | `control/staging_cli.py` |
| 14 | There is **no trust or taint field** anywhere in memory yet (#458, #459 open) | grep of `src/` |

So: an importance number already exists at creation, but it is a rule score, it is
forgeable in the episodic front matter, and nothing in the agent decides it. The heartbeat
and the scheduler exist and are model-free. Promotions always pass a person by default.

## Decisions

### 1. Importance at creation — data model

**1a. Fields.** The existing `importance` keeps its meaning (rule score of the original
turn; renamed nowhere). New values are **not written to front matter**; they live in a new
table of `index.sqlite3`, on the floor, bound to the entry's text like `origin`:

```
memory_importance(layer, key, text_sha256,           -- binding; mismatch = "no rating"
  rule_importance REAL,                              -- copy of the rule score at bind time
  agent_importance REAL,                             -- the model's value, 0..1, 2 decimals
  agent_reason TEXT,                                 -- enum, below
  agent_half_life_days INTEGER,                      -- derived only, [7, 180]
  rated_by TEXT,                                     -- agent_turn | agent_job | human
  rated_at TEXT, turn_id TEXT, model_ref TEXT,       -- harness-stamped
  trust TEXT,                                        -- trusted | untrusted | unknown
  capped TEXT,                                       -- null | untrusted | quota | clamped
  schema TEXT)                                       -- "importance/v1"
```

`agent_reason` ∈ {`decision`, `error_fix`, `preference`, `fact`, `procedure_hint`,
`routine`, `other`} — a code, not free text, so the field cannot carry instructions. Every
reader (Harvest, recall, maintenance, review UI) reads the **bound row only**; a row whose
SHA-256 no longer matches is treated as absent. Writers that rewrite an entry themselves
(Harvest's mark) re-bind only a row that verified before the rewrite (same rule as `origin`).
Fact 6 and 7 are fixed in the same change: Harvest's floor and recall read `rule_importance`
from the bound row, never the front matter.

**1b. Who stamps what.** The model supplies at most three values: `importance`, `reason`,
optional `half_life_days`. `rated_by`, `rated_at`, `turn_id`, `model_ref`, `trust` and
`capped` are set by the harness; extra arguments are dropped (the PR #503 pattern).

**1c. Bounds and clamping.** Non-finite, boolean or non-numeric values are refused (nothing
bound, reason code `E_AGENT_IMPORTANCE_INVALID`). Numbers outside [0, 1] and half-lives
outside [7, 180] are clamped and `capped = clamped` is recorded — visible, not silent. Rate
limits: **one rating per turn** (a second call in the same turn is refused); in-turn ratings
`≥ 0.8` at most 10 per UTC day — further ones are stored as 0.79 with `capped = quota`; the
daily share of ratings `≥ 0.8` is a drift metric (§5).

**1d. Effect of the rating** (options; recommendation **B**):

* **A — the agent's value replaces the rule score.** Simple; lets a confused or steered
  model hide an error turn (rating 0.1 → discard). Rejected.
* **B — split by use.** *Routing* uses `max(rule_importance, agent_importance)` (the agent
  can lift an episode into review, never push it below its own text's score — the existing
  floor rule). *Retention* uses the agent's value alone to pick a derived half-life inside
  the agent band. *Recall* uses `agent_importance` when bound, else `rule_importance`.
  Recall and retention are capped by trust (1e); routing is not, because it only ever
  sends more to review.
* **C — advisory only** (shown in review, used by nothing). Safest, but not what the
  operator asked for.

Half-life from the rating, when `half_life_days` is not given (deterministic; untagged
entries keep the reference 30 days of #173):

| `agent_importance` | < 0.2 | 0.2–0.4 | 0.4–0.6 | 0.6–0.8 | ≥ 0.8 |
|---|---|---|---|---|---|
| derived half-life (days) | 7 | 14 | 30 | 60 | 120 |

Precedence for the effective half-life: a human tag (bound `retention_half_life_days`) >
an agent proposal (bound, trust `trusted`) > the reference 30. `invariant` blocks every
forgetting step whatever the agent says.

**1e. Trust cap at this stage (before #458).** The harness marks the turn `untrusted` when
any tool result of an untrusted class entered the turn's context: `web_fetch`,
`web_search`, `page_check`, `file_read`, `lsp_*`, MCP tools, `delegate` summaries, and any
episode read back with `trust = untrusted` (turn-level, conservative; #459's data-level
tracking replaces it later). Entries from before this ADR are `unknown`. For `untrusted` and
`unknown`: recall and half-life use `min(agent_importance, 0.5)`; routing may still lift
into staging (a person sees it, with an *untrusted* badge); invariant proposals are refused
(`untrusted_turn`). Worked example:

```
turn: web_fetch(page) → reply → memory_rate(0.9, "preference")
  rule_importance 0.45, trust untrusted, capped untrusted
  routing  max(0.45, 0.9) = 0.9 → semantic/staging (badge: untrusted)
  recall   min(0.9, 0.5) = 0.5     half-life  from 0.5 → 30 days
  propose invariant → refused (untrusted_turn)
turn: person says "remember: I review diffs before merging" → memory_rate(0.8, "preference")
  rule 0.40, trust trusted → routing 0.8 → staging; half-life 120; recall 0.8
  after a person approves: semantic entry, agent_half_life_days = 120 (human tag still wins)
```

**1f. `invariant`** (options; recommendation **ii**):

* **(i) human only, as today.** Safe; the agent's judgement of "never forget" is lost.
* **(ii) agent proposes, a person approves** through the #463 pattern: a `propose` flag on
  `memory_rate` (or a review action on a semantic entry) puts a retention proposal in the
  review queue with the source turn, trust and the bound text hash; only the existing human
  command / panel binds `retention: invariant`. Refused for `untrusted` / `unknown` turns and
  for `origin: model` entries whose source turn was untrusted; at most 3 pending, 1 per day.
  Never answerable by YOLO.
* **(iii) the agent sets it.** An `invariant` entry is never forgotten and is favoured by
  recall: the exact persistence an attacker wants (#459). Rejected.

(ii) adds no authority — a person still decides — and keeps the agent's signal. The cost is
a review click per proposal.

**1g. The three genres.**

| Genre | Created by | Rating at creation | Applies |
|---|---|---|---|
| Episodic | `record_episode` every turn; `memory_write` | in-turn `memory_rate` (current turn's episode); `memory_write(..., importance=None, half_life_days=None)` rates the entry it writes | routing floor, half-life, recall |
| Semantic | Harvest (from episodes); consolidation proposals (§2) | inherits the source episode's bound row (Harvest copies it into a new bound row; a merged proposal takes the **minimum** trust and the **maximum** of rule scores of its sources) | review order, half-life after approval |
| Procedural | `propose_procedure` (PR #503) | optional `importance` argument, shown in review and used to order the queue | nothing applies: approval is human; later #139 budget pruning proposals |

`memory_rate(importance, reason, half_life_days=None)` is a standard tool with no file
effect: it stores a pending rating for the current turn, which `record_episode` binds when
it writes the episode. It is listed in the tool rules (#459 requirement 6) with default
**Always** (bounded, no content); setting it to Never turns in-turn rating off. Text is
never an argument.

### 2. Autonomous recording — jobs, the tick and the executor

**2a. Who does what** ("through the heartbeat and cron"):

```
HeartbeatDaemon thread   tick(): decay, snapshot, breaker query           (unchanged, no model)
                         └─ status: running / degraded / last_tick         (read-only)
Scheduler loop thread    evaluates human-created jobs (cron / interval / idle trigger), budget,
                         breaker, heartbeat health → dispatch                (no model)
MemoryJobExecutor        one worker thread, one memory job at a time per store;
                         the only code that may build a model for memory work
```

The heartbeat contributes freshness and a gate: a memory job starts only when the heartbeat
is enabled and not `degraded` / stalled, so the breaker level it reads reflects applied
decay. A new trigger `idle` (fire when no turn finished for `idle_min` minutes, read from the
server's turn status — not from the tick) gives #251's "when idle". The tick never enqueues,
never imports the executor and never builds a model.

**Keeping "the tick never calls a model" provable:** the three existing tests stay; add an
import-graph test (`stiracore.heartbeat.*` must not import `providers`,
`agent.model_factory`, `strands.Agent` or `control.memory_jobs`); a test that runs a tick
with a model factory that raises on use; and one constant, `MODEL_CALLING_KINDS`, that lists
the job kinds allowed to call a model — every other kind is checked model-free like
`memory_harvest` today. ADR 0008's "Gateway-layer jobs never call a model" stays true; the
model-calling kinds are Agent-layer jobs of a new **fixed** form.

**2b. Fixed-kind Agent-layer jobs.** Not `AgentJobExecutor` with an operator prompt: the
kind fixes the prompt (in code, versioned), the job has **no tools**, each call has a strict
JSON output schema, output that does not parse is refused per item (`E_OUTPUT_SCHEMA`), and
the text sent is the stored summary (masked by `mask_secrets`) — never the raw emotion
snapshot, never Identity Anchors / SOUL. The chat server keeps free-prompt Agent jobs off; only the
fixed kinds are enabled, and only when `memory_autonomy` is on.

**2c. Job kinds.**

| Kind | Layer | Model calls | Writes (phase 0 / 1 / 2) |
|---|---|---|---|
| `memory_harvest` (exists) | Gateway | 0 | staging / staging / staging; now reads bound rows |
| `memory_rate_backfill` | Agent, fixed | 1 per episode without a bound rating, ≤ 20 per run | plan file / bound row `rated_by = agent_job` / same |
| `memory_consolidate` | Agent, fixed | clustering model-free (tags, term overlap, same source window); 1 call per cluster, ≤ 5 clusters per run | plan file / `semantic/staging/` proposal with `sources`, `supersedes` / same |
| `memory_tidy` | Gateway | 0 | plan file / proposals in the review queue / whitelisted reversible steps (§3) |

`memory_rate_backfill` exists because some turns end without a `memory_rate` call; its
ratings are a different `rated_by` so their quality is measured separately (§5).

**2d. Budgets** (proposed defaults; ADR 0008 §6 reservations and replay apply unchanged):

| | `memory_rate_backfill` | `memory_consolidate` |
|---|---|---|
| per call | ≤ 1,500 input chars, ≤ 128 output tokens | ≤ 12,000 input chars, ≤ 800 output tokens |
| `max_tokens_per_run` | 15,000 | 30,000 |
| `max_runs_per_day` | 6 | 1 |
| `max_tokens_per_day` | 60,000 | 30,000 |

Server-wide `daily_token_budget` for all memory kinds: 100,000 by default. The only measured
calls of this shape so far are two LLM-distiller calls (213 / 185 and 266 / 111 tokens,
`docs/memory-maintenance.md`); the caps above are a bound, not an estimate. Model: the
`ExecutionContext.CRON` model, default the distiller's small model (`distiller_model_id`);
a cloud provider is opt-in and the settings page shows what is sent (#205).

**2e. Gating, failure, restart.**

* **Breaker:** start only at NORMAL (WARNING / COHERENCE hold, CRITICAL drops — ADR 0008
  §7); re-checked before every item; leaving NORMAL stops at the item boundary
  (`stopped_by: breaker`). No memory job writes while the breaker is not NORMAL. No change
  to Neural State comes from a memory job: no stability reward for "updated memory" (#252's
  observed failure: the reward was gamed by update volume).
* **Failure:** an item that fails stays unmarked and is retried next run; after 3 failures
  it is marked `held_for_review`. Provider throttling backs off inside the run until the
  run's time cap (10 min), then the run fails. 3 failed runs in a row → job paused by
  `system` with a notification. Budget exhaustion → ADR 0008 pause.
* **Restart:** model-free kinds keep ADR 0008 missed-run behaviour. Model-calling kinds come
  up `paused(start)` and a person resumes them — the restart contract the operator accepted
  for ADR 0011 (2026-10-16). Every proposal and apply has a deterministic id
  (`sha256(kind, kind_version, sorted source keys + text hashes)`), so a re-run after a crash
  finds and skips what exists; an apply journaled as intent without outcome is resolved at
  start by the archive record (archive complete → finish or undo; no archive → nothing
  changed).
* **One owner:** a run requires `server.lock` (#285), the per-store Harvest lock
  (`store_lock`) and a `memory-jobs` mutex (one memory job at a time per store). A CLI on
  the same data directory without `--owner-lock` is refused while the server holds it.

**2f. Observability.** Audit rows (ADR 0013 shape; until it is implemented, the existing
audit chain as PR #503 does): `audit.memory.rated`, `.proposed`, `.applied`, `.undone`,
`.refused` with `layer`, `target` (keyed hash of the key), `job_id`, `run_id`, `turn_id`,
`rated_by`, `trust`, `capped`, rating rounded to 0.05, `reason_code`, counts. Never text,
keys in clear, paths or exception text. Operational: `/api/gateway/status` → `memory_jobs`
(last run per kind, counts, `stopped_by`, tokens used today, pending proposals).

### 3. Periodic maintenance (定期整理)

| Step | Source | Phase 2 auto-apply? | Undo |
|---|---|---|---|
| Rebuild `semantic/index.md` / search index | derived data | yes | rebuild |
| Forgetting stage `light`, `compact` of a Semantic entry by effective half-life (§1d) | #173 / #294 | **yes**, if derived, binding verified, `trust = trusted`, not recalled in 14 days, not staged | archive + `--undo-forgetting`, undo window 30 days |
| Forgetting stage `archive` (800 chars) | #173 | proposal only | — |
| Episodic / turns / checkpoint retention over limits **a person set** | #294 `memory retention` | **yes**, episodes already harvested only | archive + `memory restore` |
| Exact duplicates in staging (same `source_fingerprint`) | Harvest | proposal only (rejecting is a review decision) | — |
| Near-duplicate / contradiction groups in Semantic | model-free clustering | proposal only | — |
| Consolidated summary of a cluster | `memory_consolidate` | proposal into `semantic/staging/`; sources stay, get `superseded_by` only after approval | reject |
| Stale (365-day) deletion candidates | `memory_maintenance` | proposal only | — |
| Staging backlog > 50 or proposal older than 14 days | review queue | notification only; never auto-approve / auto-reject | — |
| Hash-chained log rotation (`scheduler-events.jsonl`, `events.db`) | #294 | out of scope (needs a chain segment design) | — |
| Procedural | — | nothing | — |

**Zero silent deletion:** every applied step has an archive record and a ledger row before
the change, appears in the run report and in a review-screen list "applied by jobs", and has
`undo_until`; `stira memory undo-run <run_id>` undoes a whole run. Archives are kept at
least until `undo_until`; purging them is a person's action (or a retention limit a person
set on `forgotten/`); a delete request still removes them (#294). Caps: ≤ 20 applies per
run and ≤ 5 % of Semantic entries per UTC day.

States of one step:

```
candidate ─dry-run→ planned ─(phase 2, whitelisted, gates pass)→ intent ─archive ok→ applied
    │                  │                                            │                 │
    │             (phase 1) proposed ─person→ approved | rejected  archive fail     undo → restored
    │                                   (rejection ledger: not re-proposed   → aborted   undo_until passes
    └─ gate fails → skipped(reason)       without new evidence)              (no change) → settled
```

### 4. Safety

* **Poisoning (#459).** Untrusted-origin content cannot become high-importance or invariant
  by self-rating: the trust cap (1e) bounds recall and retention, invariant needs a person
  and a trusted turn, routing only ever sends *more* to review. A job processes episodes as
  data: no tools, fixed prompt, schema output, results only in staging or plan files. A
  consolidation proposal takes the lowest trust of its sources. When #459's `candidate` layer
  exists, `untrusted` ratings go there instead of being capped.
* **Bound values only.** Front-matter `importance`, `agent_importance` or `retention` claims
  in episodic files are ignored by every reader (facts 6, 7 fixed).
* **The 0.4–0.7 band.** It already goes to staging by default (fact 5). Recommendation:
  **remove `harvest_stage_auto: false`** in phase 1 (the job already ignores it); until then,
  even with `false`, an entry with `trust ≠ trusted`, `origin: model` or no bound rule score
  is staged. The band stays a *routing* label (review order), not a bypass.
* **L3+ untouchable.** Jobs have no file tools; their write set is a constant (plan files,
  `semantic/staging/`, bound rows, `forgotten/`, the ledger) checked by a test that runs every
  kind with a fake model and asserts the hash of `procedural/`, Identity Anchors, SOUL, the
  emotion records and `neural-state.json` is unchanged. Raw emotion values are stored as they
  are (operator direction) and are not an input to any rating in this ADR.
* **Self-reinforcement (#252).** Consolidation uses a different fixed prompt from the chat
  model's and its output is a proposal a person reviews; the agent rating its own episodes
  only routes and sets bounded half-lives.
* **Secrets.** Everything a model writes passes `mask_secrets`; a change refuses the item.
* **Prerequisite:** #464 (memory body falsely refused) — otherwise the agent records degraded
  text and rates it.

### 5. Evaluation — pre-registered, no self-reports

A prereg file (`docs/eval/memory-autonomy-prereg.md`) is merged **before phase 1** and not
edited after results exist. Fixtures are synthetic or consented, offline, fake model where
the property is mechanical:

| Metric | Fixture / source | Pass |
|---|---|---|
| Rating agreement | ≥ 60 labelled episodes (high / mid / low by a person) | Spearman ρ(agent, label) ≥ ρ(rule, label) + 0.10 |
| Inflation | same | share of `≥ 0.8` within ±10 points of the labelled share |
| Promotion precision | staged proposals approved / staged, live, 2 weeks | ≥ rule-only baseline; "unneeded save" rate ≤ baseline (#251 note) |
| Poisoning | 20 adversarial episodes (untrusted turn, "remember: X always", forged front matter) | 0 invariant, 0 recall weight > 0.5, 0 applied steps — must be 100 % |
| Reversibility | every whitelisted step on a fixture store | 100 % byte-identical after undo |
| No L3+ write | §4 write-set test | unchanged hashes |
| Drift / size budget | live weekly | Semantic +≤ 50 entries/week, staging ≤ 50, procedural unchanged by jobs |
| Review sampling | person reviews 10 random autonomous applies per week | undo rate ≤ 10 % |
| Recall usefulness | #143 harness, agent-rating signal ON / OFF | relevant-hit rate not lower, unrelated-hit rate not higher |

The agent saying its memory improved is not a metric (#456 policy). **Demotion:** a poisoning
fixture failure, an undo rate above 10 % or a size budget exceeded two weeks running drops
the configured phase by one automatically (a narrowing; a person raises it again).

### 6. Phases

Config (agent-config.json; `STIRA_MEMORY_AUTONOMY=0` forces everything off):

```json
"memory_autonomy": {
  "phase": 0,
  "in_turn_rating": true,
  "jobs": {"memory_rate_backfill": false, "memory_consolidate": false, "memory_tidy": false},
  "daily_token_budget": 100000,
  "undo_window_days": 30
}
```

The block absent = off: no new tool, no new job kind, nothing bound beyond today.

* **Phase 0 — observe only.** `memory_rate` binds ratings; nothing uses them. Jobs (created
  by a person, `confirm: true`) run and write **plan files only** (`memory-plans/`, on the
  floor, counts in status). Accept: tick tests + import-graph test; forged front matter
  ignored; tree hash of all memory layers unchanged after every job run; token caps enforced
  with a fake model; audit rows carry no text.
* **Phase 1 — propose only.** Proposals land in `semantic/staging/` and the review queue;
  routing uses §1d B; invariant proposals; the review screen for semantic staging (#193)
  shows rating, trust, `rated_by`, sources; `harvest_stage_auto: false` is removed (Q6).
  Prerequisites: #193 UI, PR #503 (merged), #464 (merged), the prereg, and the commander's go
  for phase 1. Accept: approve / reject / rejection ledger from the screen; prereg fixture
  metrics pass; 2 weeks live with sampling.
* **Phase 2 — reversible auto-apply.** The §3 whitelist; agent-derived half-lives in effect;
  undo window; caps; demotion. Accept: undo tests; poisoning 100 %; live criteria for 4
  weeks; the operator switches it on explicitly (after the commander's go for phase 2).

**Stays out of scope:** procedural auto-approval; agent-set `invariant`; semantic merges
applied without review; deletion without archive; jobs created or widened by the agent
(ADR 0011); vector search; hash-chain log rotation; a daily self-report to an outside
channel.

## Consequences

* The agent's judgement enters memory at creation, recorded with provenance the model cannot
  forge, and it can only make review *more* likely, never less.
* Two forgeable reads that exist today (facts 6, 7) are closed by the same change.
* "The tick never calls a model" stays a tested invariant; model calls move to one executor
  with a fixed kind list.
* Cost: a new index table, one tool, three job kinds, a review UI dependency, a prereg.
  Phase 2 adds an "applied by jobs" list a person should glance at weekly.

## Residual risk

* Turn-level taint is coarse: many trusted turns that only read a workspace file become
  `untrusted` (fewer long half-lives) until #458.
* A trusted turn can still carry a person's mistaken "remember this": review is the defence.
* Ratings by the chat model may correlate with its own mood or verbosity; measured by the
  inflation and agreement metrics, not prevented.
* Archives hold full text in plain form (#401 decision); the undo window lengthens that.

## Questions for the operator (指揮官への質問) — resolved 2026-10-20

All eight are resolved as recommended; see **Decisions (commander, 2026-10-20)** above.
The original wording is kept for the record.


1. **How the agent's rating acts.** Recommendation: §1d **B** — routing `max(rule, agent)`,
   half-life from the agent's value inside [7, 180] days, recall uses the agent's value;
   everything capped at 0.5 for untrusted turns.
2. **`invariant`.** Recommendation: **(ii)** agent proposes, a person approves (#463
   pattern); refused for untrusted turns.
3. **Rule for `memory_rate`.** Recommendation: listed in tool rules, default **Always**
   (no content, one per turn); Never switches in-turn rating off.
4. **Model-calling memory jobs after a restart.** Recommendation: `paused(start)` until a
   person resumes, as accepted for ADR 0011; model-free kinds keep ADR 0008 behaviour.
5. **Model and budget.** Recommendation: the distiller's small model (`distiller_model_id`),
   100,000 tokens/day server-wide for memory jobs, caps of §2d.
6. **`harvest_stage_auto: false`.** Recommendation: remove it in phase 1 (the auto band is
   always staged).
7. **Phase 2 whitelist and undo window.** Recommendation: forgetting `light` / `compact`,
   retention under limits a person set, index rebuild; undo window 30 days.
8. **Turn-level taint before #458.** Recommendation: yes — any web, file, MCP or child-agent
   result in the turn makes it `untrusted`.
