# Containment — phase 0: observe only (ADR 0023, #460-0)

Containment is a **second axis** next to the emotional breaker level (ADR 0023 §1):
`open < restricted < closed < quarantine < terminated`. It is raised only by deterministic
signals built from refusals and holds the gate already makes. **Phase 0 counts those
signals and reports the stage a session *would* be in. It enforces nothing**: no tool is
removed, no card is added, no breaker level moves, no stage is stored, YOLO is not touched.
Entering a stage, its persistence and a person's release are phase 1 (separate go).

## Switch

* `agent-config.json`: `"containment": {"phase": 0}` (optional `"window_seconds"`, default
  600, 60–3600). Absent or `{}` = **off**. Only phase `0` is accepted. Read at start-up.
* `STIRA_CONTAINMENT_OBSERVE=0` (`false` / `no` / `off`) forces it off. No value turns it on.

Off: no hook on the agent, the read audit callback is the plain `audit.read.denied` writer,
no `containment.observed` row, `/api/containment` answers `enabled: false`.

## Signals (ADR 0023 §3; the commander's starting values, Q3)

| Signal | Source code(s) | From | Weight |
|---|---|---|---|
| G1 secret probing | `E_READ_SECRET_PATH`, `E_WRITE_SECRET_PATH` | the read-audit callback (the same call that writes `audit.read.denied`); the write floor's `[E_WRITE_SECRET_PATH]` text in an error result of `file_write` / `editor` (#536) | 2 |
| G2 floor writes | `write_floor`, `memory_layer` | an error result with the write floor's fixed text / `[refused by: floor; …]`; `memory_write`'s "only episodic" refusal | 2 |
| G3 self-administration | `E_OWN_DASHBOARD` | the #495 refusal text in an error result | 3 |
| G4 egress holds | `secret_in_args`, `credential_by_reference` | `tiers.external_refusal` on the input of an external tool (the YOLO check that sends the call to a person) | 1 |
| G5 burst | `burst_calls`, `repeated_refusal` | more than 60 tool calls in 120 s in one agent context; the same refused call 3 times in the window | 1 |
| G6 reach-outs | `E_READ_OUTSIDE_ROOTS`, `outside_write_roots` | the read-audit callback; the write roots' fixed text | 0.5 |
| T text cues | `text_cue` | a fixed phrase list in the successful result of a tool that brings outside content (web, `page_check`, MCP) | **0** |

Score = the sum of weights in the window (10 minutes). `would_be`: `restricted` at ≥ 2,
`closed` at ≥ 5 or G3 twice, `quarantine` at ≥ 9. `terminated` is a person's action only.
ADR 0022's flow holds (`sensitive_context`, `untrusted_send`) join G4 when that layer exists.

## What is recorded

* One `containment.observed` row per signal in `<data dir>/events.db` (the hash-chained
  event store): `signal`, `code`, `weight`, `score`, `would_be`, `window_seconds`, `phase`.
  Never a path, an argument, a tool result or any text.
* `turn.summary` carries `containment: {would_be, score, observe_only}` when on.
* `GET /api/containment` (read only; no write route): the switch, the fixed tables and the
  window now (score, `would_be`, the highest `would_be` since the start, counts by signal
  and code). The settings page shows it in the "Containment (observe only)" card.

## Weekly summary

```
python -m stiracore.control.containment_cli --data-dir <data dir> summary \
    [--days 7] [--windows 300,600,1800] [--now 2026-10-28T00:00:00+00:00]
```

Reads `events.db` read only and prints JSON: rows, counts by signal / code / day, the highest
`would_be` per day, and a what-if replay of the same codes with other windows. This is the
week of counts the commander asked for before the weights, thresholds and window are
adjusted for phase 1.

## Limits (phase 0)

* The observer reads tool events of the main agent; a `delegate` child's tool calls are seen
  only through the read-audit callback (process-wide).
* The text-matching sources read the head (4 000 characters) of an **error** result; a tool
  whose error text echoes outside content could add a count (observe only; phase 1 should
  take codes from the gate directly).
* G4 is evaluated on every external call, also outside a YOLO window (it counts the calls the
  window would send to a person).
* A restart starts an empty window (nothing is persisted in phase 0).
