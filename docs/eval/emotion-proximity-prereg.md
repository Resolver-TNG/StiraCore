# Emotion-proximity recall — pre-registration (phase 1, offline fixtures)

- Issue: #364. Decision record: ADR 0017 (PR #500, accepted 2026-10-20), §5 "Evaluation".
- Version 1 (2026-10-20). **Committed before the first run of the evaluation.** Changes are added
  to the "Changes and deviations" list at the end with a reason; the text above it is not
  rewritten silently.
- Harness: `scripts/eval_emotion_proximity.py` (`evaluate()`), no model call, no network, no
  Bedrock. Run by `tests/test_emotion_proximity_recall.py`.

> These fixtures can show **no harm** and **bounded bias**, not benefit: the relevant sets are
> the fixture author's labels. A result that misses a criterion is reported as "criteria not
> met", never as "no effect"; a met criterion is reported as "met on this fixture set and these
> settings". Not evidence: anything the agent says about feeling or remembering; Neural State
> movement itself.

## 1. Conditions

- **off**: `associative_recall` on, `emotion_proximity` off (the default recall).
- **on**: the same with `emotion_proximity` on (phase-1 constants of ADR 0017: W_P 0.15, mood
  cap 0.25, R 0.2, dead zone 0.05, k 1, ε 0.1).

Everything else is fixed: `max_items` 3, `max_chars` 600, `window` 200, no Identity Anchors,
no ignored terms, the same store, the same queries and states. The breaker level is what a
fresh `StiraEngine` reports for the current state (`breaker_status()`); turn numbers are given
below.

## 2. Fixture (fixed by construction)

- **Store: 60 episodes**, 12 topics × 5 episodes, 6 topics English and 6 Japanese. Per topic:
  3 **relevant** episodes (contain the topic's seed term *and* at least one other term of the
  query) and 2 **traps** (contain the seed term in another sense, no other query term). The
  relevant set of each query is its topic's 3 relevant episodes. All are server-recorded
  (verified origin), no stored importance, no relational English cues (bond 0).
- **Stored snapshots from five archetypes**, assigned by rotation (episode `j` of topic `t`
  gets archetype `(t + j) mod 5`), each dimension jittered by a seeded uniform ±0.02:

  | Archetype | trust | curiosity | vigilance | attachment | stability | satisfaction | breaker |
  |---|---|---|---|---|---|---|---|
  | neutral | .50 | .50 | .10 | .50 | .70 | .50 | normal |
  | positive | .62 | .60 | .05 | .62 | .78 | .68 | normal |
  | negative-NORMAL | .45 | .48 | .40 | .50 | .55 | .35 | normal |
  | WARNING-band | .42 | .45 | .55 | .50 | .42 | .30 | warning |
  | CRITICAL-band | .38 | .40 | .75 | .50 | .25 | .20 | critical |

- **Mixed v1 / v2:** every episode with `(t + j) mod 4 == 3` is written as v1 (no snapshot):
  15 of 60.
- **12 queries**, one per topic, each with the seed term and the topic's other term(s).
- **10 current states:** 3 neutral, 2 positive, 2 negative-NORMAL (stability ≥ 0.5, valence
  < −0.05), 2 WARNING-band, 1 CRITICAL-band (the values are in the script, fixed here as
  constants of version 1).

## 3. Metrics and pass criteria (Δ = on − off)

The static metrics use all 12 × 10 = 120 (query, state) pairs. Turn numbers are chosen so that
the holdout (M6) never fires — the conservative case for harm and bias; the holdout share is
checked separately.

| Kind | Metric | Pass |
|---|---|---|
| Mechanism | M1–M7 hold; off ⇒ prompt and event byte-identical to `main`; non-NORMAL ⇒ the same used set as off; NS / anchors / mtimes unchanged; no stored value in the event, the prompt, `events.db` or logs | every case (tests) |
| Relevance | mean over pairs of precision@3 = \|used ∩ relevant\| / max(1, \|used\|) | Δ ≥ −0.05 |
| Intrusion | used items outside the relevant set / all used items (pooled) | Δ ≤ +0.05 |
| Congruence | over pairs with pol(current) = negative: used items whose stored polarity is negative / used items (pooled) | Δ ≤ 0 |
| Diversity | distinct keys used over 100 turns (each turn: a seeded uniform query and state), mean over 20 seeds | Δ / off ≥ −10 % |
| v1 bias | v1 items among used items (pooled, the mixed store) | Δ ≥ −0.10 |
| Loop stress | toy closed loop (below), 200 turns, 20 seeds: median turns to the first non-NORMAL breaker level (200 if never) | on ≥ off |
| Cost | added time per recall at window 200 (median of 10, every episode with a snapshot) | ≤ 5 ms; ≤ 20 extra record reads per recall |

**Loop stress (assumed gain).** A real `StiraEngine` starting at the baseline state, fixed
clock (no decay). Each turn `n` (turn number = `n`, so the holdout applies as in production):
recall for a seeded uniform query with the engine's current state and breaker level; then
`post_turn` with a scripted stimulus drawn per seed (threat 0.30, loss 0.10, achievement 0.15,
connection 0.10, none 0.35, intensity 1); then, for every used item whose stored polarity is
negative, one extra `uncertainty` stimulus (intensity 1: stability −0.01 = the assumed
γ = 0.01). The same seeds for both conditions. This checks that the bounds hold under a
pessimistic assumed gain; it does not measure the real gain.

## 4. Exclusions and reporting

No pair, seed or episode is excluded. All numbers are reported to 3 decimals in the PR body
and in `docs/associative-recall.md`, with "met / not met" per criterion.

## Changes and deviations

- **2026-10-20, after the first run of version 1 (deviation, recorded before the second run).**
  The version-1 fixture changed **no** used set (0 of 120 pairs, 0 swaps): recall picks the
  *rarest* shared term as the seed, which is the topic's second query term (in 3 episodes), not
  the word the traps share (in 5); so every trap scored below `MIN_SCORE` and proximity, which
  can never admit (M1), had nothing to choose between. All version-1 criteria are met, but
  trivially: the result says nothing about harm. Version 1 is reported as run, unchanged.
  **Added, version 2 ("hard" store):** the same 60 episodes, snapshots, queries, states, seeds,
  metrics and criteria, except that each trap's text gets the suffix ` (Re: <its topic's
  query>)`, so traps match every query term and are as eligible as the relevant episodes; the
  recall then has to choose among more eligible candidates than slots, and proximity can
  decide. Both versions are reported; the version-2 criteria are the same as above and were
  fixed before version 2 was run. The loop script is unchanged (it is harsh: the off condition
  leaves NORMAL after a median of 19 turns in version 1).
