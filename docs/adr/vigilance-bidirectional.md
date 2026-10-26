# ADR: Bidirectional Vigilance

**Status:** Accepted

**Decision:** Option B — vigilance rises with threat and falls gradually with reassurance.

**Issue:** #76

## Context

Positive stimuli previously changed other Neural State dimensions but left vigilance
unchanged. That asymmetry cannot represent reassurance from a successful or trusted
interaction. Time-based decay alone expresses a return toward a resting baseline,
not the immediate effect of a reassuring dialogue.

## Decision and coefficients

Keep vigilance as a bidirectional state in `[0.0, 1.0]`. Change only the vigilance
column of the positive-stimulus rows in `EMOTION_MATRIX`:

| Stimulus | Vigilance delta at intensity 1.0 | Rationale |
|---|---:|---|
| joy | -0.02 | Smallest effect: pleasure provides indirect reassurance. |
| achievement | -0.03 | Completing a task provides more concrete reassurance. |
| connection | -0.05 | Largest reassurance effect: trust formation directly reduces vigilance. |
| threat | +0.08 (unchanged) | Threat remains stronger than any single reassurance event. |
| loss | +0.03 (unchanged) | Loss continues to increase vigilance. |
| uncertainty | +0.04 (unchanged) | Uncertainty continues to increase vigilance. |

These are design coefficients, not empirically calibrated psychological constants.
They preserve the ordering `joy < achievement < connection` in reassurance strength.
At equal unit intensity, one connection event only partially offsets one threat
(`+0.08 - 0.05 = +0.03`). All other dimensions and stimulus coefficients remain
unchanged. Actual changes scale with intensity and may be smaller near saturation;
these directional statements assume positive intensity.

## Bounds and decay are separate contracts

- `apply_stimulus` already clamps every dimension with `max(0.0, min(1.0, ...))`;
  no new clamp or API change is needed. Repeated reassurance cannot cross zero.
- The vigilance baseline (`0.1`) is a **decay target, not a stimulus floor**.
  Reassurance can lower vigilance below the baseline, down to zero. Otherwise a
  state at its default baseline could not respond to reassurance.
- `apply_decay` remains unchanged: above baseline it lowers vigilance by `0.015`
  per elapsed hour and stops at baseline. Below baseline it raises vigilance
  toward baseline at half that rate, without overshooting.
- Stimuli do not require elapsed time and do not advance `last_decay_check`.
  Decay requires elapsed time and does not imply a positive stimulus. The two
  mechanisms must not be conflated or counted twice.

## Verification

`tests/test_neural_state.py` records independent expected coefficients for every
stimulus and dimension, checking direction and magnitude at intensities 0.5 and
1.0 away from saturation. Further cases verify repeated reassurance clamps at
zero, below-baseline reassurance, decay returning to (never crossing) baseline,
`threat → connection`, and stimulus/decay separation with an injected clock.

Required gates: `python -m pytest tests/ -x`, `ruff check .`, and
`ruff format --check .` using the project virtual environment.
