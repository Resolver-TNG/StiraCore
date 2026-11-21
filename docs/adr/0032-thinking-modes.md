# ADR 0032: Thinking modes — chain (converge) and diverge, chosen per turn or job

- Status: Proposed (2026-11-15; docs only, no code)
- Issue: #430
- Related: #170 (associative recall), #143 (ablation framework), #365 / ADR 0012 (autonomous goal loop), #364 / ADR 0017 (emotion proximity), ADR 0008 (scheduled jobs call no model), ADR 0009 / ADR 0013 (EventStore / audit contract), ADR 0028 (round-mode loop)

## Context

Stira today only decides whether to *use* a model's reasoning feature (`providers/capabilities.py`
thinking support, and how thinking tokens are billed). Nothing controls *how* a task is thought
through. The nearest existing pieces are not thinking modes:

- `associative_recall` (#170, default off, no extra LLM call) widens what *memory* returns. It is
  divergence on the recall side, not on the task.
- "Chain" in #171 / #277 means the audit hash chain, unrelated.

Two modes are proposed, both selectable per turn or per job:

1. **Chain (converge)**: one task is pressed through plan → execute → verify → fix.
2. **Diverge**: several candidates or viewpoints are widened first, then narrowed to one.

## Non-goals

- No change to authority, grants, approvals, tool policy, containment, the breaker, masking or egress.
  A thinking mode never adds a tool, a permission, a budget or a time window.
- No change to NeuralState structure, version, `EMOTION_MATRIX` or Layer 3. They are read only.
- No code in this ADR. Implementation is split in "Implementation split" below.

## Invariants (hold for every option)

- **Default off; existing behaviour unchanged; zero tokens spent** unless a mode is switched on.
  With the mode `off`, no extra prompt, call or event is produced (byte-identical turns).
- A mode only shapes the *prompt and loop of a turn the person was already running*. It cannot widen
  what the turn is allowed to do.
- Scheduled jobs (ADR 0008) keep calling no model. A mode on a job applies only to jobs that already
  start an agent turn through the existing gated path (ADR 0011 / 0021 / 0028), never to the tick.
- Recorded fields are an allow-list of counts, enums and ids. No free text, no model output, no secrets.

## Decision points

### D1. Who picks the mode

| Option | Description | Risk |
|---|---|---|
| A | Person's setting only (global default `off`, per-job field) | Coarse, but safe |
| B | A | plus an explicit per-turn choice by the person in the composer |
| C | The agent picks its own mode | Moves control to the agent; a path to spend more tokens by itself |

**Recommended: A + B.** The person sets the default and may override per turn or per job brief
(ADR 0029 brief gets one more allow-listed enum field). The agent may *suggest* a mode in text, but
a suggestion has no effect until the person confirms it on the host channel. C is rejected: the
agent must not widen its own budget (the Issue's rule).

### D2. Token budget and caps

- Mode `off` is the default and costs nothing.
- When on, each mode has a hard per-turn cap, set by the operator, and the cap is counted *inside*
  the existing turn/round budgets, not on top. Reaching either stops the mode and the turn continues
  as plain (mode `off`) or ends, by the existing rule.
- Chain: `max_cycles` (plan→execute→verify→fix passes; proposed default 3, ceiling 5).
- Diverge: `max_branches` (proposed default 3, ceiling 5) and one narrowing step.
- Thinking-token billing stays as today; a mode adds no new billing path.
- Fail closed: an unreadable or out-of-range setting means `off`.

**Recommended:** caps as above, set in settings (new keys need the docs settings table and i18n in
the implementation PR, not here).

### D3. Emotion / NeuralState

| Option | Description |
|---|---|
| A | No link; the person's choice only |
| B | Read-only *hint* from a stored snapshot: high uncertainty suggests diverge; threat or breaker warning forces chain or off |
| C | Hint chooses the mode automatically |

**Recommended: A in phase 1, B later as a hint shown to the person (same shape as ADR 0017's bounded,
read-only signal).** A hint may only *narrow* (breaker warning/critical ⇒ diverge is not offered;
exploration stops). It never switches a mode on by itself (so C is rejected) and never loosens a gate.
NeuralState is read only through its existing snapshot accessor.

### D4. Evaluation (#143 framework)

Each mode must be comparable on/off with the existing ablation harness, with scripted models, no network.

- Chain: completion rate and number of redo cycles; tokens per completed task.
- Diverge: diversity of candidates (distinct-by-id count and a deterministic overlap measure) and the
  irrelevant-mix rate (candidates dropped by the narrowing step as off-topic).
- Diverge exploration slot: compare (i) reuse of #170 associative recall as the candidate source and
  (ii) an ε-greedy slot like NXP's 15%. **Recommended:** start with (i) (no extra model call for the
  widening, already default-off and measured in #143), and add (ii) only if #143 shows no gain.
- A mode ships on-by-person only after #143 shows a gain; the default stays off regardless.

### D5. Trajectory record (ADR 0009 / 0013)

Record what was widened and what was dropped as **ids and counts only**, in the existing audit
event contract, as one new allow-listed event type per mode, e.g. `thinking.mode`:

- `mode` (enum `chain|diverge`), `source` (enum `setting|turn|job`), `cycles` / `branches` (ints),
  `kept` / `dropped` (counts), `candidate_ids` (opaque ids already known to the store), `stop_reason`
  (enum `done|cap|breaker|error`).
- No candidate text, no plans, no model output. The text stays in the normal transcript under its
  existing masking.
- Events use the hash chain unchanged (no schema or chain-format change). The new event type is added
  to the ADR 0013 catalogue and the docs sync tables in the implementation PR.

**Recommended:** exactly this field set; a larger record is rejected because free text leaks.

## Connection to the goal loop

Chain is designed to sit inside the grant-scoped round loop (ADR 0012 / 0028) as the *inner* cycle of
one round: the round ledger already bounds rounds, so chain cycles do not add rounds. This ADR does
not start a goal loop and does not change #365.

## Implementation split (each its own Issue, after approval)

1. **Mode setting and cap plumbing**: enum `off|chain|diverge`, caps, fail-closed parse, docs/i18n. No behaviour.
2. **Chain mode**: prompt scaffold and bounded cycle in a person-started turn, scripted-model tests.
3. **Diverge mode**: candidate source from #170 recall, one narrowing step, bounded branches.
4. **Trajectory event** `thinking.mode`: allow-listed fields, audit catalogue entry, chain tests.
5. **#143 ablation cases** for both modes.
6. **Per-turn composer choice and brief field** (D1 B), host-confirmed.
7. **Read-only emotion hint** (D3 B), only after 1–5 and #364 evidence.

## Questions for the commander (with recommendation)

- Q1. Who chooses? **Recommend A + B** (person only; agent may suggest).
- Q2. Default caps 3 cycles / 3 branches, ceilings 5? **Recommend yes.**
- Q3. Emotion link in phase 1? **Recommend no**; hint-only later.
- Q4. Exploration source: #170 first, ε-greedy only if needed? **Recommend yes.**
- Q5. Order: ship chain before diverge (chain needs no #170 result)? **Recommend yes.**

## Acceptance check (Issue #430, up to the ADR)

- Five decision points with options and a recommendation: D1–D5.
- Default off, existing behaviour unchanged, no token spend: "Invariants".
- Split into implementation Issues: "Implementation split".
