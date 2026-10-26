# Coherence score (`bc_score`) — definition (#168)

日本語: [coherence_ja.md](coherence_ja.md). Reference implementation:
`stiracore.core.compute_bc_score` (`src/stiracore/core/coherence.py`), pinned by
`tests/test_coherence_score.py`. Every number on this page was measured on the current code.

## What it measures — and what it does not

`bc_score` measures **how much `stability` has moved over the recent turns**. Nothing else
goes in: not the other five dimensions, not the conversation, not the SOUL, not the identity
anchors. A low score says "the state has been changing fast"; it says nothing about *what* the
agent is. This page defines a calculation, not an identity: the anchors stay text that the
operator writes, and the harness never turns them into numbers.

## Definition

| | |
|---|---|
| Sample | `stability`, read at each `StiraEngine.pre_turn` **after decay and before that turn's stimulus** (so it reflects the stimuli up to the previous turn). |
| When a sample is taken | Once per attempted turn (`pre_turn`), read-only and halted turns included. `breaker_status()` queries, heartbeat ticks and `post_turn` add none. |
| Window | The most recent `bc_window_size` samples, oldest first (`NeuralState.bc_window`). Saved in `neural-state.json` and kept across restarts. |
| Minimum | Fewer than `BC_MIN_SAMPLES` samples: **unmeasured**, the score is `1.0`. |
| Formula | `bc_score = round(max(0, 1 − 5 · σ), 4)`, σ = population standard deviation of the samples present (`sqrt(Σ(x − mean)² / n)`). |
| Range | `[0, 1]`. σ = 0 → 1.0; σ ≥ 0.2 → 0.0. Only the spread counts: the order of the samples and a common offset do not change it. |
| After a reload | Recomputed from the saved window; a saved `bc_score` value is ignored. |

<!-- coherence-defaults:begin -->
| Setting / constant | Default | Where |
|---|---:|---|
| `bc_window_size` | 20 | `StiraConfig` |
| `BC_MIN_SAMPLES` | 5 | `core/coherence.py` |
| `BC_STDDEV_SCALE` | 5.0 | `core/coherence.py` |
| `bc_threshold` (enter COHERENCE below) | 0.6 | `StiraConfig` |
| `bc_exit_threshold` (leave COHERENCE above) | 0.7 | `StiraConfig` |
<!-- coherence-defaults:end -->

In σ terms with the defaults: COHERENCE starts when σ > 0.08 and ends when σ < 0.06.

## Examples (window → `bc_score`)

| Window | σ | `bc_score` | Breaker (from NORMAL) |
|---|---:|---:|---|
| `[0, 1, 0, 1]` (4 samples) | — | 1.0 (unmeasured) | NORMAL |
| `[0, 1, 0, 1, 0]` | 0.49 | 0.0 | COHERENCE |
| `[0.7] × 5` | 0 | 1.0 | NORMAL |
| `[0.7, 0.7, 0.7, 0.7, 0.6]` | 0.04 | 0.8 | NORMAL |
| `[0.7, 0.7, 0.7, 0.7, 0.5]` | 0.08 | 0.6 | NORMAL (entry is strictly below 0.6) |
| `[0.9, 0.9, 0.9, 0.9, 0.69]` | 0.084 | 0.58 | COHERENCE |
| `[0.62] × 10 + [0.78] × 10` | 0.08 | 0.6 | NORMAL |
| `[0.64] × 10 + [0.76] × 10` | 0.06 | 0.7 | stays COHERENCE once in (exit is strictly above 0.7) |
| `0.70, 0.72, … 0.88` (10 samples, +0.02 each) | 0.057 | 0.7128 | NORMAL |
| `0.60, 0.62, … 0.98` (20 samples, +0.02 each) | 0.115 | 0.4234 | COHERENCE |

```python
from stiracore.core import compute_bc_score

compute_bc_score([0.7, 0.7, 0.7, 0.7, 0.5])  # 0.6
compute_bc_score([0.7, 0.7, 0.7, 0.7])  # 1.0 — fewer than 5 samples
```

Another implementation (for example a self-monitor outside Stira) gets the same values by
keeping its own window of stability samples and passing it to `compute_bc_score`. Samples must
be finite numbers in `[0, 1]`; anything else raises `ValueError`.

## Initial fill — the confirmed behaviour (#142)

This is the specified behaviour, measured and pinned by tests; it is not changed here.

1. **Samples 1–4: no COHERENCE.** The score stays `1.0` whatever the samples are (a fresh
   `NeuralState` starts at 1.0, a reload with fewer than five samples resets to 1.0).
   The score alone cannot tell "not measured yet" from "measured, no movement" (both 1.0), so
   the status views say it next to the score (#337, see "Unmeasured or completely stable").
2. **Sample 5 onwards, window not yet full: only the samples present count.** The window is
   not padded, and the score is not extrapolated. Consequences, both measured:
   - a single change weighs `1/n`: one sample 0.1 below the rest scores 0.8 at n = 5,
     0.85 at n = 10, 0.891 at n = 20, and 1.0 again once that sample has left the window;
   - a steady trend weighs *more* as the window fills: for a change of `s` per turn over `n`
     samples, σ = `s · sqrt((n² − 1) / 12)`. With a full window of 20, any steady change
     faster than about 0.014 per turn ends in COHERENCE.
3. **Direction does not matter.** A rise counts like a fall. This is why a run of positive
   stimuli reaches COHERENCE in [breaker-reachability.md](breaker-reachability.md):
   `achievement` at intensity 1.0 raises stability by 0.03 per turn, and COHERENCE starts at
   turn 10 (`joy` / `connection`, +0.02 per turn: turn 14); when stability stops at 1.0 the old
   samples leave the window and the breaker returns to NORMAL. stability itself does move in
   these runs (0.70 → 1.00); the score reports that movement.
4. **Persisted.** The window is part of `neural-state.json`; a restart continues where the
   window was, it does not start a new fill.

## Unmeasured or completely stable (#337)

Both score `1.0`. Status views add three fields **next to** `bc_score`; nothing existing
changes (additive, derived from the saved window — `NeuralState` is read, not changed):

| Field | Meaning |
|---|---|
| `bc_samples` | Stability samples the score rests on (`len(bc_window)`). |
| `bc_measured` | `true` when `bc_samples >= bc_min_samples`. `false` = "not measured yet": the `1.0` is the placeholder, not a result. |
| `bc_min_samples` | `BC_MIN_SAMPLES` (5). |

| `bc_score` | `bc_measured` | Reading |
|---:|---|---|
| 1.0 | `false` | Not measured yet (fewer than 5 samples). |
| 1.0 | `true` | Measured; the five-or-more samples do not differ at all (4 decimal places). |
| < 1.0 | `true` | Measured; the state has moved. |

`stiracore.core.bc_is_measured(n)` / `bc_measurement(n)` compute the fields, so another host
can add them the same way.

A measured 1.0 (or a score near it) over many turns can also mean that the classifier
rarely produces a stimulus, e.g. the rule fallback without Sokudan. The status says which
classifier runs and how many turns changed the state (`classifier.mode`, `emotion_updates`;
[neural-saturation.md §6](neural-saturation.md), #367).

## Thresholds, priority and effect

- Entry: `bc_score < bc_threshold`. Exit: `bc_score > bc_exit_threshold` (latched in between).
  `StiraConfig` requires `bc_exit_threshold > bc_threshold`.
- Priority: CRITICAL > WARNING > COHERENCE. The coherence latch keeps running underneath a
  higher level, so COHERENCE shows again when that level clears and the score is still low.
- Effect: `INJECT_IDENTITY` only — the identity notice (with the configured identity anchors)
  is prepended to the system prompt, ending with "COHERENCE: Identity drift detected. Reaffirm
  these anchors." **Tools are not limited** (ADR 0010 §2).

## Where it shows (channels)

| Channel | What |
|---|---|
| Engine log | On entering COHERENCE: the level message and `bcWindow analysis: samples=… score=… window=[…]` (numbers only). While it persists: a numbers-only summary every 20 turns (#165). |
| `on_breaker` callback | On each level transition (hosts re-apply restrictions from it). |
| `/api/gateway/status` (chat server) | `bc_score`; the breaker level under `breaker`; `bc_samples`, `bc_measured`, `bc_min_samples` (below); `bc_window_clamped` (see the floor of 5). |
| Chat UI | "bcScore" in the status strip (`整合性スコア`): `Not measured (3 / 5 samples)` until the minimum, then the number (`1.000 (no movement)` when it is exactly 1.0). |
| Observer (`dashboard/app.py`, `/api/state`) | The coherence dial against `bc_threshold` and the sample count; the dial shows `—` / "Not measured" until the minimum. Same three fields. COHERENCE is 🟡 HOLD. |
| Settings hub | `bc_threshold` is editable (must stay below `bc_exit_threshold`; below 0.3 is flagged as dangerous). |

## `bc_window_size` has a floor of 5 (#337)

A window of fewer than `BC_MIN_SAMPLES` (5) can never be measured: the score would stay 1.0 and
COHERENCE could never fire, with nothing saying so. A bad `bc_window_size` (below 5, or not an
`int` — `bool`, `5.0`, `"20"` and `null` included) is therefore never used as it is. What
happens depends on where it comes from:

- **Start-up / load** (an `agent-config.json` that already holds a bad value, typically
  hand-edited; `bc_window_size` is not editable in the settings UI; the chat server reads the value, rounded if needed, at start-up and gives it to its engine, #272): `AgentConfig.load`
  **rounds it up to 5 and starts**. It logs one warning per process
  (`bc_window_size in the agent configuration was not an integer >= 5; rounded to 5 …
  [bc_window_clamped]`; the value itself is not logged) and sets `bc_window_clamped: true` on
  `/api/gateway/status`, `/api/config` and the settings hub state (additive; `false`
  otherwise). All three read the file as it is now, so they always agree. The file is not
  rewritten by the load; the next settings save writes the 5, and the flag is `false` in all
  three from then on (no restart needed; tested against the running server). Other invalid values in the same file still stop the load.
- **Direct construction and saving** (`StiraConfig(bc_window_size=3)`,
  `AgentConfig(stira_config_overrides={"bc_window_size": 3})`; every settings save builds the
  config through these): still refused with `ValueError: bc_window_size must be an integer
  >= 5 …`, nothing is written. Nobody edits the value there by hand, so an error is the
  clearer answer than a silent change.

Any non-`int` value is rounded too (`"20"`, `20.0` and `null` become 5, not the default 20): the safe side is the sensitive window, and the flag says it happened. The warning is
logged once per process, not once per file.

Start-up rounds instead of failing so a hand-edited file cannot stop the server, and the
flag, the log line and the rounded value in `/api/config` mean the correction is never silent.

## Known limits (not changed here)

- The heartbeat does not take samples: the score changes only with turns.
