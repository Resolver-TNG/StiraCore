# Breaker reasons (#455)

Each breaker evaluation records **why** the level is what it is, as fixed codes and rounded
numbers (`src/stiracore/core/breaker_reasons.py`). The explanation is derived after the
decision from the same values the engine compared; it never takes part in the decision
(regression: `tests/test_breaker_reasons.py::test_decisions_are_unchanged_by_the_reasons`
compares every level at every boundary with the pre-#455 decision).

## Codes

| Code | Level | Inputs |
| --- | --- | --- |
| `stability_below_critical` | CRITICAL | `stability`, `stability_critical` |
| `stability_below_warning` | WARNING | `stability`, `stability_warning` |
| `bc_score_below_coherence` | COHERENCE | `bc_score`, `bc_threshold` |
| `hysteresis_hold` | CRITICAL / WARNING / COHERENCE | the value and the recovery threshold of that level (`stability_critical_exit` / `stability_warning_exit` / `bc_exit_threshold`) |
| `cooldown_active` | CRITICAL | `critical_turns`, `critical_cooldown_turns` |
| `stability_above_critical_exit`, `cooldown_served` | left CRITICAL | `stability`, `stability_critical_exit`, `critical_turns`, `critical_cooldown_turns` |
| `stability_above_warning_exit` | WARNING latch released | `stability`, `stability_warning_exit` |
| `bc_score_above_coherence_exit` | COHERENCE latch released | `bc_score`, `bc_exit_threshold` |
| `within_thresholds` | NORMAL | `stability`, `stability_warning`, `bc_score`, `bc_threshold` |

Numbers are rounded to 4 digits. No conversation text, memory content or identifier is ever
part of a reason; every surface forwards only allow-listed codes and input names
(`breaker_reasons.clean`).

## Where they appear (all additive)

- `engine.post_turn()` summary: `breaker_reasons`, `breaker_inputs`, `breaker_transition`
  (the last level change, `{from, to, reasons, inputs}`); `engine.breaker_explanation()` and
  `engine.last_breaker_transition()`.
- Turn events: `turn.summary` carries `breaker_reasons` / `breaker_inputs` for every turn (held
  turns included); `breaker.transition` keeps `from` / `to` and adds `reasons` / `inputs`.
- `GET /api/gateway/status` `breaker`: `level` and `last_changed` unchanged; adds `reasons` and
  `inputs` (of the current level) and `last_transition`.
- `GET /api/yolo/status` `window`: `pause_reason` unchanged (`breaker_warning` …); adds
  `pause_breaker_reasons` / `pause_breaker_inputs`.
- Screens: chat (breaker card "Reason", turn inspector), `/observability` (the transition row),
  Stage (one backlog line per level change), YOLO badge (the pause line). Codes are worded by
  `dashboard/static/js/breaker_reasons.js` with the `breaker_reason.*` catalog keys; an
  unknown code reads "unknown reason", never the code itself (#279).

Display only: nothing acts on a reason (no automatic action).
