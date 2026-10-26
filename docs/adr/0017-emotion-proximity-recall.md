# ADR 0017: Emotion-proximity in associative recall — a bounded re-ranking signal from the stored snapshot, off by default

**Path:** `docs/adr/0017-emotion-proximity-recall.md`
**Status:** Accepted (2026-10-20, operator decisions below). Phase 1 (default off) is
implemented by the follow-up PR for #364 (branch `feat/364-emotion-proximity-recall`). This is the
decision record for the part of #364 that PR #392 left open.
**Issue:** #364 (remaining part: wiring emotion-proximity into recall, incl. "same polarity").
Related: #392 (snapshots, `recall_by_emotion`), #170 / #421 (associative recall,
[associative-recall.md](../associative-recall.md)), #173 / #425 (retention, half-life),
#143 (ablation harness, [prereg](../eval/emotion-ablation-prereg.md)), #186 (`memory.injected`),
#294 (verified origin), [ADR 0001](0001-stimulus-attribution.md) (what moves Neural State),
[ADR 0002](0002-breaker-enforcement.md) (breaker), [ADR 0003](0003-memory-design.md) (memory
layers), [episodic-emotion.md](../episodic-emotion.md).
**Decision:** the stored emotion snapshot of an episode becomes **one re-ranking
signal inside `AssociativeRecall`**, never a source of candidates. Closeness is a 4-dimension
"mood" distance (curiosity, vigilance, stability, satisfaction; trust and attachment stay in the
existing `bond` term), turned into a similarity with a fixed radius. Polarity is a sign of a
state valence index with a dead zone, used **only as a guard and a metric, never as a bonus**.
Proximity shares the existing relational budget (0.25) instead of adding to it, never admits a
candidate on its own, may change at most one used item per turn, is off outside the NORMAL
breaker level, never lifts a negative episode while the current mood is negative, and is skipped
on a deterministic 10 % of turns. Stored values never leave the protected area and never reach
the model or an audit row; the operational recall log in `turns.db` may carry the 3-decimal
proximity score (Q11, changed by the operator). It is a **new signal**: the existing
trust/attachment term is kept as it is (Q6). Behind `associative_recall.emotion_proximity`,
default **off**.

## Decisions (commander, 2026-10-20)

The operator accepted every recommendation of this ADR except Q11, which was changed.
Numbering follows the operator's decision message; the ADR's original question 6 (numeric
score in events) is the one decided under Q11.

| # | Question | Resolution |
|---|---|---|
| Q1 | What is a "close emotional state"? | **As recommended:** RMS over the 4 mood dimensions (curiosity, vigilance, stability, satisfaction), equal weights, raw [0, 1] scale, `s = max(0, 1 − d / 0.2)` (§1). |
| Q2 | What is "same polarity", and is it a bonus? | **As recommended:** the state valence index with dead zone 0.05 (§2), used **only as a safety guard (M4) and a metric**, never as a bonus. |
| Q3 | Negative moods and the size of the effect | **As recommended:** no negative-to-negative lift (M4); proximity off outside NORMAL (M5); it shares the existing 0.25 relational budget (M2); at most **one** recalled item may change per turn because of it (M3, k = 1). Mood repair is not part of this decision. |
| Q4 | The shipped `affect_of` mapping "saved under a breaker ⇒ self-protective" | **As recommended: removed.** Protective stays Identity-Anchor based only. |
| Q5 | Holdout | **As recommended:** ε = 0.1, fixed and deterministic by the engine turn number (M6). |
| Q6 | Is proximity a new signal or a replacement of the trust/attachment term? | **As recommended: a new signal.** The existing `bond` term (current trust/attachment pull × the rule classifier's relational score) is **not replaced**; proximity is added next to it inside the shared 0.25 budget. Consequence: in proximity mode `bond` keeps its current inputs (the stored trust/attachment do not feed it), so "proximity off" is exactly today's ranking and M3 compares against it (§3a). |
| **Q11 (changed)** | Precision of stored values and what the recall log may carry | **Changed from the recommendation** ("rank + codes, no numeric score"). The operator: *emotion values go in as raw values.* The six snapshot dimensions are stored as captured (no rounding, bucketing or other discretisation of what is stored or used in scoring), and the `memory.injected` turn event in `turns.db` may carry the 3-decimal proximity score per item (and the existing 3-decimal total score) instead of rank + code only. Reason: debuggability and evaluation of the signal; the bucketing protected only the operational log, which is already on the data-directory floor. |

**Hard limits that still hold (not changed by Q11):**

- Stored snapshots stay in the protected area (`<memory_dir>/emotion/`, data-directory floor,
  `0600`, read only through `read_emotion_snapshot` with key + body binding); the agent's file
  and memory tools cannot read them.
- **No emotion value, distance, polarity or proximity score is ever written into the audit /
  hash-chained `events.db`** (ADR 0013). The proximity score lives only in the operational
  `memory.injected` turn event in `turns.db`.
- **Nothing reaches the model:** no value, score, polarity or "you felt …" text in the prompt or
  in any tool result; the `## Associated Memory` frame is byte-identical.
- Neural State, `EMOTION_MATRIX`, breaker thresholds and Identity Anchors stay **read-only**,
  with tests.

**Data model (operator direction, context for this and later ADRs).** Memory stays three genres
— episodic, semantic, procedural — and no new memory layer is added for emotion; the snapshot is
a protected side record of an episodic entry. Importance is assigned by the agent itself when a
memory is created; that is the subject of **ADR 0018 (planned)** and is not designed here
(proximity reads the stored `importance` as today).

## Context (checked on `main` = `af849f4`; #502 merged since as `9c3f0fd`)

**Two different emotion inputs exist and must not be confused.**

- **Current state (#170, merged as #421).** `AssociativeRecall` reads the live Neural State under
  `engine.state_lock` (the chat server's `_associative_recall`) and scores each
  candidate that literally matched the message:

  ```
  score = 0.5·rel + 0.15·imp + 0.1·rec + 0.25·pull·bond·(1 − g) + 0.35·g·prot
  pull  = (trust + attachment)/2 (current)    g = clamp((0.5 − stability)/(0.5 − 0.3), 0, 1)
  ```

  `bond` comes from the rule classifier's category of the episode text; `prot` from shared
  Identity-Anchor terms. `MIN_SCORE = 0.3`. Off by default; no model call.
- **Stored snapshot (#364, merged as #392).** Each v2 episode has a record in
  `<memory_dir>/emotion/` (data-directory floor, `0600`, bound to key + body SHA-256) with the six
  dimensions after `post_turn`, category, intensity, breaker level, turn. `read_emotion_snapshot`
  returns `status` `stored` / `v1` / `missing` / `mismatch`; only `stored` with all six values is
  "complete".
- **Already shipped but unwired:** `recall_by_emotion` (ranks the newest episodes by 6-dim RMS
  distance alone) and `AssociativeRecall(affect_of=...)`, which, when given a reader, replaces
  `bond` by the stored trust/attachment (`(mean − 0.5)·2`) and sets `prot = 1` for an episode
  saved under WARNING/CRITICAL/COHERENCE. The chat server passes no reader.
- **Since #502 (`9c3f0fd`):** the score is the sum of a pluggable list of `Signal` objects
  (`DEFAULT_SIGNALS` = relevance, importance, recency, bond, protective) and `memory.injected`
  is checked against an allow-list (`observability/memory_injected.py`). Proximity is one more
  `Signal` there (§3e).

Facts that shape the design: trust/attachment decay 0.001–0.002 per hour, vigilance 0.015,
satisfaction 0.010 (`DECAY_RATES`), so the slow pair mostly marks the relationship phase, the
fast four the mood. In long one-sided conversations dimensions saturate (attachment 1.0,
stability near 0; [neural-saturation.md](../neural-saturation.md)). Neural State is moved only
by `post_turn` (a turn's attributed stimulus, ADR 0001) and by decay; recall only reads it.

## Goals and non-goals

Goals: let "how I was when this was saved" reorder memories that are **already relevant**;
keep relevance in charge; make the mood-congruence loop bounded by construction; keep values
in the protected area; be measurable before any default changes.
Non-goals: a recall path driven by emotion alone; changing `NeuralState`, `EMOTION_MATRIX`,
breaker thresholds or Identity Anchors; mood repair; emotion in the prompt text or in agent tools.

## Decisions (each: options, trade-off, recommendation)

### 1. "Close emotional state" — **recommended: (b) 4-dim mood distance, equal weights, raw scale, radius 0.2**

| Option | For | Against |
|---|---|---|
| (a) shipped 6-dim RMS, equal weights | exists, tested (tolerance 0.0005) | stored trust/attachment would count twice (already in `bond`); slow dims act as a second recency signal |
| **(b) RMS over curiosity, vigilance, stability, satisfaction** | no overlap with `bond`; only mood dims; same proof of tolerance (weighted RMS is a norm) | a new function next to the shipped one |
| (c) tuned per-dimension weights | could fit data | no data yet; adds free parameters before evaluation |
| (d) z-scored by corpus statistics | equalises spread | distance changes as the corpus changes (non-stationary, hard to test) |
| (e) cosine | — | states are not directions (baselines are not 0) |

```
d(c, e) = sqrt( Σ_{i ∈ M} (c_i − e_i)² / 4 ),   M = {curiosity, vigilance, stability, satisfaction}
s(c, e) = max(0, 1 − d / R),   R = 0.2        (similarity in [0, 1]; 0 beyond the radius)
```

`c` = current state (live, read under the lock), `e` = stored snapshot. All values are in
[0, 1], so no further normalisation. With R = 0.2 a difference of 0.25 in one dimension alone
gives d = 0.125, s = 0.375; 0.4 in one dimension gives s = 0. Stored values are raw (Q11):
records written from the implementation on keep the captured floats; records written by #392
before carry 3 decimals, which moves d by at most 0.0005, s by at most 0.0025.

**Missing data.** `v1`, `missing`, `mismatch` or any null dimension → **no proximity
contribution** (s treated as 0, code `unknown`); the candidate keeps every other signal and is
never excluded. No partial distance over fewer dimensions (changes the scale) and no imputation
(the snapshot rules say "null, never a guess"). Cost: v2 episodes can be lifted and v1 ones not,
a step of at most 0.15 (the weight below) that favours newer episodes; the evaluation measures the
v1 share of used items (§5).

Known offset: the current state is read before this turn's stimulus, stored states after their
turn's; at intensity 1 one stimulus moves d by at most 0.044 (the `threat` row), s by up to 0.22.

### 2. "Same polarity" — **recommended: state valence index with a dead zone; guard and metric only, no bonus**

Options: (a) by the stored **category** (joy/achievement/connection positive, threat/loss
negative, uncertainty undecided) — directly tied to what moves NS, but null whenever the
classifier abstained, and needs a decision about `uncertainty`; **(b) by the stored state**;
(c) both. And separately: is polarity a **bonus** (lift same-polarity episodes) or a **guard**?

```
v(x) = ((satisfaction − 0.5) + (stability − 0.7) − (vigilance − 0.1)) / 3      (deviation from BASELINES)
pol(x) = negative if v < −0.05,  positive if v > +0.05,  else neutral;  unknown if incomplete
```

Why these three: the difference between the mean positive row (joy, achievement, connection)
and the mean negative row (threat, loss) of `EMOTION_MATRIX` is, per dimension, vigilance −0.088,
satisfaction +0.082, stability +0.048, trust +0.030, curiosity +0.025, attachment +0.005. The
index keeps the three largest, equally weighted, so it does not depend on exact coefficients and
needs no decision about `uncertainty` (its row scores −0.02, slightly negative, under the index).
Examples: the baseline state gives v = 0 (neutral); stability 0.52, vigilance 0.40, satisfaction
0.38 gives v = (−0.12 − 0.18 − 0.30)/3 = −0.20 (negative).

**No polarity bonus.** Proximity over stability/vigilance/satisfaction already encodes valence; a
same-polarity bonus would count it twice and is exactly the mood-congruence amplifier §3 bounds.
Polarity is used by rule M4 and by the metrics; the stored category is recorded in fixtures as a
secondary label only.

### 3. Combining stored proximity with #170's current-state weighting — **recommended: one mood budget, bounded, never a candidate source**

**3a. Score with proximity on** (only stage 2, where the ≤ 20 candidate files are read):

```
base  = 0.5·rel + 0.15·imp + 0.1·rec
mood  = (1 − g) · min(0.25, 0.25·pull·bond + 0.15·s')       (W_P = 0.15, mood cap = today's W_BOND)
prot  = 0.35 · g · anchor_share                              (unchanged, anchors only)
score = base + mood + prot
eligible only if score_off ≥ MIN_SCORE (0.3),  score_off = the same with s' = 0 (today's score)
s'    = s, set to 0 by rules M4–M6 below
```

`bond` keeps today's inputs (Q6: the trust/attachment term is not replaced): the current
trust/attachment pull times the rule classifier's relational score of the episode text; the
stored trust/attachment do not feed it. So `score_off` is exactly the proximity-off score and
the floor M1 is "the score without proximity", which keeps M3's comparison exact. The shipped
mapping "saved under a breaker ⇒ `prot = 1`" is **removed** (Q4): an episode recorded during CRITICAL is a
distressing episode, not a self-protective one, and it would make low stability lift
low-stability memories twice (proximity + protective). Protective stays Identity-Anchor based.
`recall_by_emotion` stays unwired; it ranks by emotion alone and must not become a source.

**3b. Worked example** (NORMAL; current c: trust .60, curiosity .55, vigilance .12,
attachment .62, stability .72, satisfaction .58; pull = .61, v(c) = +.027, neutral):

| Cand. | rel / imp / rec | stored mood (cur, vig, stab, sat) | d / s | base | without prox | with prox |
|---|---|---|---|---|---|---|
| A | .6 / .5 / .8, bond .2 | .52, .15, .70, .55 | .028 / .861 | .455 | .455 + .031 = **.486** | .455 + min(.25, .031 + .129) = **.615** |
| B | .8 / .5 / .3, bond 0 | .50, .40, .45, .35 | .227 / 0 | .505 | **.505** | **.505** |
| X | .2 / .5 / .9 (association only) | as A | .028 / .861 | .265 | .296 → dropped | **not eligible** (base < .3; without M1 it would score .425 and be admitted) |

A overtakes B (relevance .6 vs .8): proximity reorders relevant candidates; it cannot bring X in.
Bound: proximity alone adds ≤ 0.15, so it can overturn a relevance gap of at most 0.3
(0.15 / 0.5); the whole mood term (≤ 0.25) at most 0.5.

Same episodes in a **negative mood** (c': trust .50, curiosity .50, vigilance .40,
attachment .50, stability .52, satisfaction .38; v = −.20, still NORMAL): B has v = −.233
(negative) and d = .038, s = .81, but M4 sets s' = 0; A (v = 0, neutral) has d = .176, s = .12,
lift .018. In a negative mood proximity does almost nothing — intended for phase 1.

**3c. The feedback-loop risk.** Mood-congruent recall can close three loops:
L1 (prompt): low mood → congruent negative memories in the prompt → reply tone → the next
turns' attributed stimuli (the person's reaction, outcomes) → lower mood. L2 (corpus): recalled
topics are discussed again and saved with similar snapshots → more congruent candidates next
time. L3 (durability): if recall ever refreshed retention (#173), congruent memories would also
live longer. Recall cannot move NS directly, so L1's gain goes through the model's replies and
the next turns and is **unmeasured**; the mitigations below are bounds that hold whatever the
gain is. The classic failure — low
stability locking the agent into recalling only negative memories — is closed by M4 + M5; M1–M3
bound everything else.

**3d. Mitigations (all in phase 1).**

- **M1 Affect never admits.** Eligibility uses `score_off` (the score without proximity).
- **M2 One budget.** Proximity takes room from the existing 0.25 relational budget.
- **M3 Swap budget k = 1.** Compute the used set U0 with s' = 0 and U1 with proximity. Walking
  U1's order, an item not in U0 is admitted only while fewer than k such items were admitted;
  further ones are dropped with `D_EMOTION_BUDGET`. At least `max_items − 1` used items are
  exactly those of proximity-off.
- **M4 No negative-to-negative lift.** s' = 0 if pol(current) = negative and pol(episode) =
  negative. Negative episodes are still recalled on relevance.
- **M5 Off outside NORMAL.** s' = 0 while `engine.breaker_status().level` ≠ NORMAL (WARNING,
  CRITICAL, COHERENCE). WARNING's exit hysteresis (enter < 0.5, leave > 0.6) keeps it off until
  stability is back above 0.6; the anchor-based protective term takes over in the band, as today.
- **M6 Deterministic holdout ε = 0.1.** s' = 0 for the whole turn when
  `int(sha256(str(turn).encode()).hexdigest()[:8], 16) % 10 == 0` (engine turn counter).
  Breaks any stable pattern and gives an in-situ proximity-off sample; reproducible in tests.
- **M7 No reinforcement.** Recall touches no access time (as today). If #173 ever reinforces on
  recall, only membership of U0 counts; an emotion swap is never reinforced (closes L3).

**3e. Interface.** One signal, pure and read-only:

```python
class RecallSignal(Protocol):
    name: str  # "emotion_proximity"
    group: Literal["relevance", "mood", "protective"]

    def value(self, candidate, ctx) -> float:
        """Return a value in [0, 1].

        ctx is a frozen copy of the current dims, breaker level, turn and snapshot reader.
        """
        ...
```

The combiner, not the signal, applies weights, the group cap, M1, M3 and the gates M4–M6. #502
made the score a list of `Signal` objects, so proximity is one more `Signal`
(`emotion_proximity`) appended to `DEFAULT_SIGNALS` in proximity mode; the turn gates (M5, M6)
reach it through the read-only `SignalContext`. The invariants of this section are what is
decided, not the class layout.

### 4. Safety and privacy

- **Protected area.** Values are read only through `read_emotion_snapshot` (status `stored`,
  key + body binding), held in memory for the ranking of one query and dropped at
  `take_result`. The file tools' floor, `memory_read` / `memory_search` and the index are
  unchanged; the agent still cannot read or write `emotion/`.
- **Nothing to the model.** No value, distance, polarity or "you felt …" text in the prompt; the
  `## Associated Memory` frame stays byte-identical. Only the choice of bodies changes (a weak
  side channel: accepted, bounded by M3).
- **Events (`memory.injected`, schema 3 in proximity mode; Q11).** With the switch off the
  event is byte-identical to today (schema 2). In proximity mode: per item the existing 3-decimal
  `score` and reason codes, plus `proximity` = s' rounded to 3 decimals (`null` when the item has
  no complete snapshot or was not read), `R_EMOTION_PROXIMITY` when it lifted the item,
  `R_EMOTION_SWAP` when the item is in U1 but not U0 (M3) and `D_EMOTION_BUDGET` for a swap
  refused by M3; turn-level `proximity_gate` (`on` / `holdout` / `breaker` / `error`) and
  `proximity_swaps` (0 or 1). No raw dimension value, distance, polarity or category. A 0 next to
  a positive score can hint at a polarity pair or a far state; accepted by the operator (Q11).
  `turns.db` is on the data-directory floor (the agent cannot read it); the server's
  observability views show it to the person running the server. **Never in `events.db`.**
- **Logs.** Fixed codes and exception types only (`E_EMOTION_READ`, `E_EMOTION_INVALID`, new
  `E_EMOTION_PROXIMITY`); a failure means "proximity off for this turn", the recall goes on.
- **Breaker.** M5; recall stays read-only, so CRITICAL's read-only policy and `halt` are
  unaffected.
- **L3+ and NS untouchable.** Recall reads `engine.state` under the lock, never calls
  `apply_stimulus` or `apply_decay`, never writes `EMOTION_MATRIX`, thresholds, SOUL or Identity
  Anchors (read for terms only). Tests compare the NS dict, the state file, anchors and file
  mtimes before and after a recall, with proximity on.
- **Manipulation.** A person who steers the mood can at most swap one item among relevant,
  origin-verified candidates (M1, M3, `D_UNVERIFIED_ORIGIN`); the agent cannot forge a snapshot
  (rewritten body ⇒ `mismatch` ⇒ `unknown`).
- **Opt-out and deletion** keep working: snapshot capture off ⇒ new episodes are v1 ⇒ `unknown`;
  deleting an episode deletes its record.

### 5. Evaluation — offline fixtures now, #143 later, criteria fixed in advance

**Pre-registration.** `docs/eval/emotion-proximity-prereg.md` is committed in the implementation
PR **before** the first run: conditions, metrics, margins, intervals, exclusions. Results that
miss a criterion are recorded as "criteria not met", not "no effect"; met criteria are reported
as "met on this fixture set / model / setting". Not used as evidence: the agent's statements
about feeling, remembering or relevance; Neural State movement itself (it is the mechanism, not
the outcome).

**Phase 1 (offline, CI, no model).** A fixture store of about 60 synthetic episodes (Japanese and
English) with stored snapshots from five archetypes (neutral, positive, negative-NORMAL,
WARNING-band, CRITICAL-band), 12 queries with relevant sets fixed by construction, and 10 current
states. Fixtures can show *no harm* and *bounded bias*, not benefit (labels are the designers').

| Kind | Check | Pass |
|---|---|---|
| Mechanism (always) | M1–M7 hold; off ⇒ prompt and event byte-identical to today; non-NORMAL ⇒ same used set as off; NS / anchors / mtimes unchanged; event has no value or score | every case |
| Relevance | precision@3 vs proximity-off | Δ ≥ −0.05 |
| Intrusion | share of used items outside the relevant set | Δ ≤ +0.05 |
| Congruence | share of negative-polarity used items when pol(current) = negative | Δ ≤ 0 |
| Diversity | distinct keys used over 100 turns (20 seeds of message order) | Δ ≥ −10 % |
| v1 bias | share of v1 items among used items, mixed v1/v2 store | Δ ≥ −0.10 |
| Loop stress | toy closed loop: each used negative item lowers the next stability by an **assumed** γ = 0.01 (one `uncertainty` stimulus per item, on top of the scripted stimuli); 200 turns, 20 seeds | median turns to WARNING with proximity ≥ without |
| Cost | added time at window 200 (median of 10) | ≤ 5 ms; ≤ 20 extra record reads |

The loop stress test checks that the bounds hold under a pessimistic assumed gain; it does not
measure the real gain.

**Phase 2 (the #143 harness, `--runner nxp`, when available).** Conditions: recall off / recall
on, proximity off / proximity on / proximity with **shuffled snapshots** (records permuted across
episodes: a placebo with the same distribution). Primary outcome: hit rate of related memories,
"related" labelled blind by annotators who see the scenario but not the condition. Secondary:
unrelated intrusion, breaker transitions and time at WARNING/CRITICAL in the hostile scenarios,
task success, cost, latency; the M6 holdout turns give an in-situ comparison. Intervals as in the
#143 prereg (bootstrap over scenarios × seeds). **Criteria for proposing default-on** (all):
primary non-inferior to proximity-off (margin 0.05) **and** better than the shuffled placebo
(interval above 0); intrusion not worse by more than 0.05; time at CRITICAL in hostile
scenarios not worse by more than 2 percentage points (upper bound); no mechanism failure.

### 6. Rollout

- **Switch:** `agent-config.json` → `"associative_recall": {"enabled": true, "emotion_proximity":
  true}`; default **false**; needs `enabled`; read at start-up (restart); unreadable config = off;
  banner adds `emotion proximity on`. Like `STIRA_ASSOCIATIVE_RECALL` (added by #502), a
  non-empty `STIRA_RECALL_EMOTION_PROXIMITY` overrides `emotion_proximity` only. Snapshot capture
  (`episodic_emotion_snapshot`) stays as is; with it off proximity has nothing to read.
- **Constants fixed in phase 1:** W_P 0.15, mood cap 0.25, R 0.2, dead zone 0.05, k 1, ε 0.1.
  Tuning is a later, pre-registered change.
- **Follow-up implementation PR contains:** the mood distance, similarity and valence index
  (`tools/episodic_emotion.py`); the signal and combiner rules in `agent/associative_recall.py`;
  removal of the breaker ⇒ protective mapping; server wiring of the snapshot reader
  (`read_emotion_snapshot` only) and of the breaker level / turn number only when the switch is
  on; event schema 3 in proximity mode; `clean_options` for the new key; raw stored values (Q11); fixtures and tests; the prereg; docs:
  `associative-recall.md`, `episodic-emotion.md`, `configuration.md` (each with its `_ja` pair).
  Review: the emotion-change review required by #364.
- **Acceptance:** switch off ⇒ prompt and `memory.injected` byte-identical to `main` (test;
  the only change with the switch off is the Q4 removal, which only affects a host that passes
  `affect_of` — the chat server does not); every mechanism check of §5 is a test; the phase-1
  fixture criteria pass and the numbers are written in the PR; no stored value in the prompt,
  `events.db` or logs, and no proximity score outside the `turns.db` recall event (tests grep
  for the fixture values); `recall_by_emotion` still has no caller (existing test);
  latency within §5.
- **Out of scope:** default on; mood repair (lifting positive memories in a negative mood);
  per-dimension tuning or learned weights; emotion filters in `memory_search` or any agent tool;
  Semantic/Procedural layers; the Discord/Slack runtime (it has no associative recall);
  back-filling v1 episodes; retention reinforcement (#173); any change to NS, `EMOTION_MATRIX`,
  thresholds or anchors.

## Consequences

Proximity can only reorder a few relevant candidates and change at most one used item; in a
negative mood or outside NORMAL it is inert, so its possible benefit is limited to neutral and
positive moods. That is the price of the bounds; if phase 2 shows no benefit there, the feature
stays off and costs nothing. The shipped `affect_of` mapping changes (breaker ⇒ protective
dropped) before anyone has used it.

## Residual risk

- L1's real gain is unknown; the bounds assume nothing about it, but the toy loop's γ is a guess.
- The valence index is a design choice, not a calibrated measure; polarity near the dead zone
  flips with small changes.
- Selection itself leaks a little about past moods to the model (bounded by M3).
- The pre-turn vs post-turn offset (§1) shifts d by up to 0.044 in any comparison.
- Whoever can write the data directory can change snapshots (as today).

## Questions for the operator (指揮官への質問) — answered 2026-10-20, see "Decisions"

Kept as asked. Under Q11 the operator changed question 6 below (the numeric score is kept).

1. **(Q1) Distance:** (a) shipped 6-dim / **(b) 4 mood dims, equal weights, R = 0.2
   (recommended: no double count with `bond`)** / (c) tuned weights after data.
2. **(Q2) Polarity:** **state valence index with dead zone 0.05, used as guard and metric only
   (recommended)** / by stored category (decide `uncertainty`) / a same-polarity bonus (not
   recommended: double count, amplifies congruence).
3. **(Q3) Negative mood:** **no negative-to-negative lift (M4, recommended)** / cap only /
   mood repair, i.e. lift positive memories (not recommended here: an affect intervention that
   deserves its own decision).
4. **(Q4) Shipped `affect_of`:** **drop "saved under a breaker ⇒ protective" (recommended)** /
   keep it.
5. **(Q5) Holdout:** **ε = 0.1 deterministic by turn (recommended)** / none / only during
   evaluation runs.
6. **(Q6) Events in proximity mode:** **rank + codes, no numeric score (recommended)** / keep the
   3-decimal score (more debuggable, leaks the stored terms).
