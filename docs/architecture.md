# StiraCore Architecture

The entry point: what the parts are, how one chat turn and one autonomous goal run through
them, and where each can be stopped. Every section links to the document that holds the
rules; this page does not repeat them. Line numbers are left out on purpose (they move);
module and function names are given so that they can be searched.

## 1. Design principles

1. **Strands as foundation** — Don't reinvent the agent loop. Use Strands' tool/model/lifecycle abstraction.
2. **NXP as the soul layer** — Memory, identity, and coherence detection sit above Strands core.
3. **Stira as the safety layer** — Circuit breakers and drift detection are non-negotiable.
4. **Layer separation** — L0-L2 are optimizable. L3+ (Identity Anchors) are protected.
   (These are the *identity* layers of `StiraEngine.check_layer_boundary()`; they are not the
   package layers of §2.)

Background: [design-philosophy.md](design-philosophy.md). Terms: [glossary.md](glossary.md).

## 2. The whole on one page

A harness on Strands that adds a persona (Neural State, memory) and safety (breaker, tool
gate, approvals); on top of that, ways to run while nobody watches (YOLO, grants, the
scheduler) and the pages that show and record it.

The intended direction of imports is upward only (a layer imports from the layers below
it). The assignment of packages to layers is a judgement, and the code does not keep to it
everywhere (§7). Sizes are rough (measured 2026-11-15) and only show where the weight is.

| Layer | Packages (under `src/stiracore/` unless noted) | Size | What it is |
|---|---|---|---|
| L0 base | root modules: `durable_dir`, `owner_lock`, `identity_paths`, `self_protect`, `labels`, `memory_*`, `procedural_provenance`, `_platform` | ≈ 3k lines | Shared helpers: durable writes, the process lock, protected paths (SOUL and the data directory), trust labels, memory origin / importance / rating |
| L1 core | `core` | ≈ 4k | Neural State (6 dimensions, decay), the breaker (`engine.py`), coherence score, Amygdala scoring, HITL approval types and the durable approval store, the turn lock, containment stages |
| L2 agent | `agent`, `middleware`, `providers` | ≈ 8k / 1k / 2k | `NXPAgent` (`agent/nxp_agent.py`): one guarded turn around a Strands `Agent`. Context assembly, associative recall, child agents (`subagent`, `delegation`, `grant_child`), model routing. `middleware/identity_guard.py` protects SOUL and write paths. `providers` selects models and reads credentials by reference |
| L3 tools and gates | `tools` | ≈ 21k | The tools (memory, files, web, verification: `page_check` / `page_look` / `run_checks`, `delegate`) and their gates (`tool_policy`, `tool_registry`, `tool_consent`, `exec_gate`, `os_protection`, `own_dashboard_gate`, `child_env`) |
| L4 authority and autonomy | `yolo`, `authority`, `grants`, `scheduler`, `progress`, `heartbeat` | ≈ 11k / 5k / 8k / 4k / 1k / 1k | Running without a person answering each step (§5): the YOLO window and allow-again, the authority boundary (permits, epoch, kill flag, latches, watchdog), goal grants, scheduled jobs, progress reports, the model-free heartbeat |
| L5 operations | `control`, `config`, `diagnostics`, `eval`, `integrations` | ≈ 10k / 3k / 2k / 3k / 1k | Memory maintenance CLIs and jobs, layered settings, `stira doctor`, the ablation evaluation, Slack / Discord / webhook / OAuth |
| L6 surface | `observability`, and `dashboard/` (outside `src/`) | ≈ 16k + 5k | Three things under one name: the server's HTTP API (`chat_api`, `config_api`, `settings_hub`), the event and audit stores (`event_store`, `audit_chain`, `turn_events`), and masking / request security (`masking`, `security`). `dashboard/stira_server.py` is the chat server; `dashboard/stage/` is the Stage |

Mainline and experiment: grants and the authority service are **alpha and off by default**
([autonomy.md](autonomy.md)); YOLO, the scheduler, the heartbeat, progress reports and the
integrations are opt-in; the rest is the product's main line. What costs tokens or widens
permissions is opt-in ([CONTRIBUTING.md](../CONTRIBUTING.md), "Design rules").

Some files worth knowing by name:

| File | Purpose |
|---|---|
| `core/neural_state.py` | 6-dim emotion state, decay, bcWindow |
| `core/engine.py` | The breaker (`pre_turn` / `post_turn`), `request_hitl`, layer protection |
| `core/turn_lock.py` | One turn at a time: the chat turn takes it, and taking it signals a running goal iteration to yield |
| `agent/checkpoint_hooks.py` | The per-call gate of a turn (§3.1) and the checkpoint a restored approval resumes from |
| `tools/workspace_artifacts.py` | Workspace artifacts: what `file_write` / `editor` wrote, as versions ([ADR 0020](adr/0020-workspace-artifacts.md)); a fourth layer beside the three-layer memory |
| `grants/executor.py` | One goal iteration: admission, the child session, permits per effect, the end code |
| `authority/boundary.py` | Permits, `commit_at_epoch`, the kill flag |
| `heartbeat/` | Model-free tick (decay, snapshot, breaker query) and its opt-in daemon |

## 3. One chat turn

From the message to the reply, on the web chat (`POST /api/chat`). "Can stop here" names
the check; a refusal inside the turn goes back to the model as the tool's result, it does
not end the turn.

| # | Step | Module | Can stop here |
|---|---|---|---|
| 1 | The page posts the message; the reply is an SSE stream | `observability/chat_api.py` | — |
| 2 | Host, Origin, CSRF token and the Idempotency-Key are checked | `observability/security.py` | 403; 409 for a repeated or reused key. Nothing runs |
| 3 | The body is checked (text or image, vision capability, `progress_reply`) | `chat_api.py` | 400 |
| 4 | The turn lock is tried without waiting; a running goal iteration is signalled to yield first | `core/turn_lock.py` | SSE `error`, code `turn_running`; the key is released |
| 5 | The turn runs on its own worker thread (`run_turn`); the response only reads its stream, so closing the page does not stop the turn | `chat_api.py` | `POST /api/chat/cancel`, or the server's stop |
| 6 | `turn.started` is recorded | `observability/turn_events.py` | — (an event that cannot be written is dropped) |
| 7 | The thread is marked "a turn a person started": the only origin a YOLO window or an allow-again answers | `yolo/origin.py` | — |
| 8 | The runner re-reads the model selection and saved settings and builds this turn's tool list (`_stira_tools`, `filter_tools_by_config`) | `dashboard/stira_server.py`, `tools/tool_registry.py` | Tools are only removed here by saved switches; the breaker level does not change the list (#259: write calls are refused at the call, ADR 0010 addendum) |
| 9 | `NXPAgent.__call__`: the session is restored (first turn), then `engine.pre_turn()` applies decay, updates the coherence window and fixes the breaker level for the turn | `agent/nxp_agent.py`, `core/engine.py` | The breaker's actions (`BLOCK_WRITES`, `REQUIRE_CONFIRMATION`, `HALT`, `INJECT_IDENTITY`) |
| 10 | Prompt and tools are prepared: SOUL, anchors, state, recalled memory within the budget; `_turn_tools` keeps the named tools at every breaker level (#259: the breaker refuses write calls in `IdentityGuard` instead) | `agent/context_assembler.py`, `nxp_agent.py` | `ProtectedContextOverflow` when the protected context does not fit |
| 11 | A Strands `Agent` is built for the turn with the hooks `checkpoint_hooks`, `identity_guard`, extra and context hooks, `binding_verifier` (last), and the model is called | `nxp_agent.py` | A model error ends the turn with a fixed failure code |
| 12 | Every tool call passes the gate (§3.1), runs, and its result returns to the model | §3.1 | §3.1 |
| 13 | `_finish_turn` runs exactly once, also after a failure: the stimulus classifier, `engine.post_turn()`, then the session is saved | `nxp_agent.py`, `core/engine.py` | — (a classifier failure is an abstention) |
| 14 | On success the turn is summarised into episodic memory (`record_episode`, no model); state is saved and the neural history recorded in any case | `dashboard/stira_server.py`, `tools/episodic_formatter.py` | — (`record_episode` never raises) |
| 15 | The masked reply joins the history; `turn.summary`, `breaker.transition` (if the level moved) and `turn.completed` are written once; the lock is released | `chat_api.py` | — |

Between turns, the opt-in heartbeat (off by default) runs the same lazy decay and a
breaker *query* on a background thread of `stira_server`, under the same
`engine.state_lock`, and never calls a model. Active decay can release WARNING (and
CRITICAL once its cooldown was served by turns) without a turn; elapsed time is not an
approval. See [heartbeat-daemon.md](heartbeat-daemon.md).

Discord / Slack turns use another runner (`integrations/runtime.py`, `ChatSession.runner`).
It calls the same agent; whether every step above is the same there has not been traced.

### 3.1 The gate of one tool call (chat)

Rule → breaker → floor, and the strictest wins
([ADR 0010](adr/0010-tool-policy-gate-stack.md) §2 holds the decision table;
[tool-execution-paths.md](tool-execution-paths.md) the paths). The checks sit in several
modules; this is the order in which a call meets them.

| Order | Check | Where | What it stops |
|---|---|---|---|
| a | The tool list (before the model sees it) | `tool_registry.get_tools`, `_stira_tools`, `NXPAgent._turn_tools` | Exec-class tools are not offered unless `STIRA_ENABLE_EXEC_TOOLS`; write tools are not withheld by level since #259 — the chat server's `_stira_tools` builds one list for every level (only `delegate` stays out at CRITICAL) and IdentityGuard refuses the calls (`breaker_write_refused`; under `halt` each write goes to a person). `NXPAgent._turn_tools` does not remove them either |
| b | The rule | `CheckpointHooks.before_tool` | **Never** is refused. A write to a run-later file (git hooks, CI workflows, editor tasks) is treated as Ask even under Always. A read-only tool that is not Ask passes here |
| c | The breaker | same | A write while the breaker blocks writes is refused and **not** sent for approval (an approval never lifts the breaker) |
| d | The path floor | same (`path_check`) | A protected path is refused and not sent for approval |
| e | Who answers an Ask | same | In this order: an open YOLO window (`auto_approver`), a session allowance (`allow_approver`), a person's card. CRITICAL with `halt`, a resumed call and a run-later file go to the card only |
| f | The card | `engine.request_hitl` → `HITLApprovalBroker.request` (`observability/chat_api.py`), `core/approval_store.py` | No approver, a failed save, expiry or shutdown is a rejection (fail closed). A person may approve, edit, reject or respond |
| g | After the answer | `CheckpointHooks.before_tool` | The rule changed while waiting; the breaker got stricter while waiting; the approval's binding (agent, tool, argument hash, expiry) does not match |
| h | Identity and write paths | `IdentityGuard.on_before_tool_call` (`middleware/identity_guard.py`) | SOUL / identity paths, the write-path policy, the breaker's write block for a call no person approved |
| i | Last hook | `ApprovalBindingVerifier` (`agent/checkpoint_hooks.py`) | The binding is verified again after every other hook and the call is claimed in the approval ledger |
| j | At execution, exec-class tools only | `ExecGatedTool` (`tools/exec_gate.py`) → `tool_consent.claim_gate` | Runs only inside the approval given to this exact call |

Approvals: [hitl-approvals.md](hitl-approvals.md), [ADR 0006](adr/0006-hitl-persistence.md),
[ADR 0024](adr/0024-approvals-beyond-the-turn.md). YOLO and allow-again:
[ADR 0012](adr/0012-yolo-mode.md), [allow-again.md](allow-again.md).

## 4. One autonomous goal (grant)

Alpha, off by default. The rules are in [grants.md](grants.md) and
[authority-service.md](authority-service.md); how to try it is in [autonomy.md](autonomy.md);
the decisions are [ADR 0011](adr/0011-agent-self-scheduling.md) and
[ADR 0028](adr/0028-round-mode-loop.md).

**Issue (a person).**

1. The settings page posts the goal, tools, working directory, budget and TTL with the
   confirmation word (`POST /api/grants/issue`, `grants/routes.py`).
2. `grants/schema.py` `parse_issue` validates the body against allow-lists and ranges;
   refusals are fixed codes.
3. `GrantService.issue` (`grants/service.py`) reads the authority epoch first (the fence),
   then checks the switches the scope needs (heartbeat start, `run_checks`, `page_look`),
   that the breaker is NORMAL and that no other grant is live.
4. Host confirmation: the request returns 428 `grant_confirmation_required`; a person
   confirms the operation on the host (`yolo/host_channel.py`, `yolo/host_ops.py`); the
   page sends the same body again with the operation id.
5. The record is stored first (`grants/store.py`), then published inside the fence
   (`authority/boundary.py` `commit_at_epoch`): if the epoch moved or a kill arrived
   meanwhile, the issue is aborted. A lifecycle event is written.

**One iteration (the agent, inside the grant).**

6. Start: a person's press (`POST /api/grants/<id>/step`), or — for a grant issued with
   `heartbeat_start` — `scheduler/grant_tick.py` `fire`, at most one press per wake and
   only when every clause of its admission holds.
7. Admission (`GoalIterationExecutor.step` → `_admit` / `_admissible`): kill, authority or
   store unhealthy, grant and goal state, breaker not NORMAL, clock, TTL, iteration cap,
   budget, the page-tool axis, a supported provider, an intact working directory.
8. A child session `grant:<id>` is created (`agent/grant_child.py`): writes limited to the
   grant's directory, no delegation, tools wrapped in `GrantTool`, plus `goal_report`.
9. Every model call and every tool call takes and consumes **one permit** from the
   authority boundary (`issue_permit` / `consume`); tokens are reserved before the call and
   settled after it.
10. At each effect boundary `_precheck` looks for a person's turn (the turn lock's signal),
    a timeout and a stop; a person's turn ends the iteration with `yielded_user_turn`.
11. Tools: `IterationGuard`, the child's `CheckpointHooks` and `GrantTool` / `_check_tool`
    (scope, directory, `run_checks` containment). The rules are the parent's
    (`_rule_reader`).
12. An Ask is answered by a person's card only (`_approver`). An iteration started by the
    heartbeat, or one with no approval broker, shows no card and ends with
    `approval_timeout`. Nothing approves itself.
13. The agent reports with `goal_report` (fixed codes). `claim_complete` moves the goal to
    `awaiting_verification`, not to complete.
14. The end: the written files are read again and fingerprinted (`grants/progress.py`);
    progress is judged by outcome (files, check results), not by activity. The iteration is
    appended to the round ledger (`grants/round_ledger.py`; codes and numbers only).

**Verify (a person).**

15. A person confirms or rejects the claim (`POST /api/grants/<id>/verify`, host-confirmed
    like the issue). Only this makes the goal complete; a rejection returns it to active.

**What stops it.** Every narrowing event ends or pauses the work; none of them is undone by
elapsed time.

| Cause | Result (status and reason) | Where |
|---|---|---|
| Kill switch (`POST /api/authority/kill`) | revoked (`kill_switch`); running effects are cancelled | `authority/service.py`, `grants/service.py` |
| A person revokes / pauses | revoked (`user`) / paused (`user`) | `grants/service.py` |
| TTL reached; clock unreadable or moved | expired; revoked (`clock_anomaly`) | `grants/state.py` `clock_check` |
| Breaker leaves NORMAL | halted (`breaker_warning` / `breaker_critical`) | `on_breaker_transition`, `halt_reason_for` |
| Server restart; `/new` in the chat | revoked (`restart`); revoked (`session_reset`) | `grants/service.py` |
| Store or authority unhealthy | revoked (`unhealthy`) | `grants/service.py` |
| A person's turn arrives | the iteration ends `yielded_user_turn`; the grant stays | `core/turn_lock.py`, `_precheck` |
| Ends that block the goal | `approval_timeout`, `iteration_timeout`, `run_checks_unavailable`, `workspace_changed` | `BLOCK_FOR_END` in `grants/executor.py` |
| Counters | `max_iterations`, `no_progress`, `loop_detected`, `same_blocker`; budget used up → goal `budget_limited` | `grants/state.py`, `grants/progress.py` |
| Watchdog pass (no model) | applies the above on a timer, also without a press | `authority/watchdog.py` |

A blocked or paused goal waits for a person; the full state table is in
[grants.md](grants.md).

## 5. Three ways to run unattended

They are not three alternatives but three axes — *when it starts*, *what runs*, *who
answers an Ask* ([ADR 0031](adr/0031-run-frame.md), Proposed; nothing in it is built, and
this table describes today's code).

| | YOLO window | Goal grant | Scheduled job |
|---|---|---|---|
| In one line | Answers Asks for a while, inside turns a person started | Works towards one goal in iterations | Runs a defined job at a time |
| Family (ADR 0031) | approval side: the person is in the loop | autonomy side | autonomy side |
| Trigger | a person's button | issue by a person (host-confirmed); each iteration by a press or, if issued so, the heartbeat | the schedule (cron / interval / once) a person defined |
| What runs | nothing of its own: the chat / Stage turn | a child session `grant:<id>` | a job of layer `gateway`, `agent` or `memory` (`scheduler/jobs.py`) |
| Who answers an Ask | the window (`yolo/requests.py`); allow-again answers one tool and one target class | a person's card; none for a heartbeat start (`approval_timeout`) | `unattended_approval`: `deny` (default) or `wait` for a person |
| Limits | 6 hours (`WINDOW_HOURS`), counts of automatic approvals | TTL, iteration cap, total and per-iteration tokens, tool scope, one directory; one live grant | runs per day, tokens per run and per day |
| Under `authority` (a kill stops it) | yes | yes | only the grant tick; ordinary jobs are not |
| Documents | [yolo-quickstart.md](yolo-quickstart.md), [ADR 0012](adr/0012-yolo-mode.md), [ADR 0025](adr/0025-allow-again-from-the-approval-card.md) | [autonomy.md](autonomy.md), [grants.md](grants.md) | [scheduler.md](scheduler.md), [guide-scheduler-heartbeat.md](guide-scheduler-heartbeat.md), [ADR 0008](adr/0008-scheduler.md), [ADR 0021](adr/0021-scheduler-job-base.md) |

A YOLO window does not answer inside a grant iteration, and no model approves for a
person; changing either needs its own ADR (ADR 0031, "What does not change").
[ADR 0014](adr/0014-progress-report-phase.md) progress reports stop a long YOLO-window run at
a checkpoint for the person to continue or redirect.

## 6. Records

Hash-chained, tamper-evident records exist in several stores, in three families
(the table follows ADR 0031's survey; the formats are not shared yet).

| Family | Store | Holds |
|---|---|---|
| Sidecar head | `observability/event_store.py` (+ `event_chain.py`, `audit_chain.py`) | Events and the audit chain ([ADR 0009](adr/0009-eventstore-chain.md), [ADR 0013](adr/0013-audit-event-contract.md)) |
| Sidecar head | `core/approval_store.py` | Approval requests and decisions ([ADR 0006](adr/0006-hitl-persistence.md)) |
| Sidecar head | `scheduler/event_log.py` | Scheduler runs ([ADR 0008](adr/0008-scheduler.md)) |
| Append | `authority/lifecycle_events.py` | Grant / goal / store transitions |
| Append | `grants/round_ledger.py` | One row per goal iteration: codes and numbers ([ADR 0028](adr/0028-round-mode-loop.md)) |
| Whole replace | `grants/store.py`, `authority/latches.py`, `authority/epoch_store.py` | The grant records, the latches, the epoch |

Beside these, `observability/turn_events.py` keeps the turn events the pages read
(`turns.db`), and memory is Markdown with an SQLite index
([ADR 0003](adr/0003-memory-design.md)). New fields in any record are allow-listed codes and
counts, never free text or secrets.

## 7. Known structural findings

Observations about structure, not bug reports. They are tracked on #795 (numbers are the
maintainer's tracker, see the README); two tests freeze today's state so that it does not
get worse while the refactors wait for their own review.

| Finding | Where it shows | State |
|---|---|---|
| The layers of §2 are an intent the code does not keep. `core` imports upward twice, inside functions (`core/approval_store.py` → `observability.masking`, `core/engine.py` → `yolo.session_allow`); `agent` imports `observability`; `agent` and `tools` import each other | `tests/data/layer_edges.json` | `tests/test_layer_imports.py` fails on a **new** cross-package import; the existing ones are listed, not judged |
| The wait for an approval card (`HITLApprovalBroker`) lives in the API module, although approvals are a `core` concern | `observability/chat_api.py` | Open (#795 S1) |
| The turn is spread over three large files; the tool list and the post-turn episodic write (§3 steps 8 and 14) are in the server file, not the agent | `dashboard/stira_server.py`, `chat_api.py`, `nxp_agent.py` | Open (#795 S2) |
| One tool call is checked in several modules; §3.1 lists the order in prose, and no function enumerates it | §3.1 | Open (#795 S3) |
| Chat and grant gate the same tool by different code | `agent/checkpoint_hooks.py`, `grants/executor.py` | `tests/test_gate_equivalence.py` compares `file_read` / `editor` × Always / Ask / Never × four breaker levels. Where they differ, the grant is the stricter side (no iteration is admitted unless the breaker is NORMAL); those cases are pinned as strict xfails. `page_look` (the grant answers it itself unless the rule is Never) is not in that table yet |
| A heartbeat-started iteration cannot get an Ask answered | §4 step 12 | By design; the answerer is the subject of ADR 0031 and ADR 0028 |
| The chained record is implemented several times | §6 | ADR 0031 step 1a (Proposed) |
| `observability` holds three roles, `tools` five families; ten files hold about a quarter of the code | §2 | Not decided; split only after the layer check |

## 8. Where to look

| To see | Go to |
|---|---|
| The pages (Stage, Activity, goal grant panel, settings sections, approvals) and how to turn each on | [stage.md, "Where do I find it"](stage.md#where-do-i-find-it-809) |
| What a setting does, and which variable wins | [configuration.md](configuration.md), [settings-surface.md](settings-surface.md) |
| Which route needs which permission | [admin-api-permissions.md](admin-api-permissions.md) |
| Recovery, backup, `stira doctor` | [operations.md](operations.md), [diagnostics.md](diagnostics.md) |
| Every document and every ADR with its status | [README.md](README.md) (the ADR index there is the source for statuses) |

## 9. Multi-model strategy

| Role | Model | Use case |
|---|---|---|
| Orchestrator | Opus 5.5 | Complex reasoning, architecture decisions |
| Coder/Spec | GPT-6 Astra | Implementation, spec generation |
| Researcher | Sonnet 5 | Investigation, review, analysis |
| Fast gate | Haiku 4.5 | Binary decisions, routing; also every role in the heartbeat context |

These are the Bedrock defaults in `agent/model_factory.py` (`DEFAULT_MODELS`). Any role
can be routed to another provider with a `ModelSpec`; see [providers.md](providers.md).
Side tasks (summaries and the like) can use another model: [task-models.md](task-models.md).

## 10. Relationship to existing repos

- **Stira** (earlier engine, not published) → core engine algorithms. StiraCore integrates these.
- An earlier OpenClaw adapter (not published) → test patterns reused.
- A decision router prototype (not published) → may become a Strands Tool; it is not implemented.

## 11. Diagrams

Visual references live in [docs/diagrams/](./diagrams/) (SVG, GitHub light/dark aware).
They predate L4 (YOLO, authority, grants, scheduler) and show the core turn only:

- **#63 Neural State freeze** — [sequence (before/after)](./diagrams/63-sequence-before-after.svg), [detector flow](./diagrams/63-stimulus-detector-flow.svg)
- **Circuit Breaker** — [state machine](./diagrams/statemachine-circuit-breaker.svg) (NORMAL/WARNING/CRITICAL/COHERENCE, hysteresis)
- **Turn pipeline** — [pre_turn -> model -> detect -> post_turn](./diagrams/pipeline-turn-processing.svg)
- **Package layout** — [components](./diagrams/architecture-components.svg)

See [docs/diagrams/README.md](./diagrams/README.md) for details.
