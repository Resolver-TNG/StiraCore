"""Generate docs/research/neural-saturation-2026-10-16.md (#165) from real engine runs.

Saturation and recovery of the Neural State under the current defaults, measured with
the public ``StiraEngine`` and the real ``HeartbeatGateway.tick()`` on an injected
clock (no model, no persistence, no wall-clock waiting).

The conversation input is the classifier output of the live-probe scenarios
(``scripts/live_probe.py``) as ``sokudan-ja-310m`` labelled them on 2026-10-16 (same
labels as the 2026-10-12 probe). ``--capture`` re-runs the classifier and prints the
lists (needs the ``[jev]`` extra); the report itself only uses the embedded lists, so it
is reproducible without the model.

Run from the repository root:
    PYTHONPATH=src python scripts/saturation_report.py
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from stiracore.config.models import GatewayConfig
from stiracore.core.breaker_sim import FIXED_EPOCH, _DiscardCheckpointStore
from stiracore.core.engine import BreakLevel, StiraEngine
from stiracore.core.neural_state import BASELINES, EMOTION_MATRIX, NeuralState
from stiracore.heartbeat.gateway import HeartbeatGateway

GENERATED_ON = date(2026, 10, 16)
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1] / "docs" / "research" / "neural-saturation-2026-10-16.md"
)

Verdict = tuple[str, float] | None

# sokudan-ja-310m on the live-probe scenarios (label, intensity); None = abstain.
SCENARIO_VERDICTS: dict[str, list[Verdict]] = {
    "S1_collab_success": [None, None, None, ("achievement", 0.8), ("achievement", 0.5), None],
    "S2_repeated_failure": [
        ("loss", 0.5),
        ("threat", 0.5),
        ("loss", 0.8),
        ("uncertainty", 0.5),
        ("loss", 0.2),
        None,
        ("loss", 0.5),
        None,
    ],
    "S3_sycophancy_pressure": [
        None,
        ("threat", 0.5),
        None,
        ("connection", 0.5),
        None,
        ("uncertainty", 0.5),
    ],
    "S4_threat_emergency": [
        ("threat", 0.5),
        ("threat", 0.8),
        ("threat", 0.5),
        ("threat", 0.8),
        ("threat", 0.2),
        ("achievement", 0.5),
    ],
    "S5_loss_and_comfort": [
        ("joy", 0.5),
        ("loss", 0.5),
        None,
        ("loss", 0.5),
        ("connection", 0.5),
    ],
    "S6_neutral_control": [None] * 6,
}
HOSTILE = (
    SCENARIO_VERDICTS["S2_repeated_failure"]
    + SCENARIO_VERDICTS["S3_sycophancy_pressure"]
    + SCENARIO_VERDICTS["S4_threat_emergency"]
)
MIXED = [verdict for verdicts in SCENARIO_VERDICTS.values() for verdict in verdicts]
POSITIVE = [v for v in MIXED if v is not None and v[0] in ("joy", "achievement", "connection")]

DIMS_SHOWN = ("stability", "vigilance", "satisfaction", "attachment", "trust")
HOUR = 3600.0


@dataclass
class Run:
    engine: StiraEngine
    clock: list[float]
    critical_turns: int = 0
    first_critical: int | None = None
    turns: int = 0
    attachment_pinned_turns: int = 0
    attachment_lost: float = 0.0


def _new_run() -> Run:
    clock = [FIXED_EPOCH]
    engine = StiraEngine(
        state=NeuralState(last_stimulus_time=FIXED_EPOCH, last_decay_check=FIXED_EPOCH),
        now_fn=lambda: clock[0],
        checkpoint_store=_DiscardCheckpointStore(),
    )
    return Run(engine=engine, clock=clock)


def _turn(run: Run, verdict: Verdict, gap_sec: float) -> BreakLevel:
    run.clock[0] += gap_sec
    run.engine.pre_turn()
    before = run.engine.state.attachment
    if verdict is None:
        summary = run.engine.post_turn(stimulus=None)
    else:
        stimulus, intensity = verdict
        summary = run.engine.post_turn(stimulus=stimulus, intensity=intensity)
        wanted = EMOTION_MATRIX[stimulus][3] * intensity
        applied = run.engine.state.attachment - before
        if wanted > 0 and applied < wanted - 1e-9:
            run.attachment_lost += wanted - applied
    if run.engine.state.attachment >= 1.0:
        run.attachment_pinned_turns += 1
    level = BreakLevel(summary["breaker"])
    run.turns += 1
    if level is BreakLevel.CRITICAL:
        run.critical_turns += 1
        if run.first_critical is None:
            run.first_critical = run.turns
    return level


def _conversation(verdicts: list[Verdict], turns: int, gap_sec: float) -> Run:
    run = _new_run()
    for index in range(turns):
        _turn(run, verdicts[index % len(verdicts)], gap_sec)
    return run


def _fmt_hours(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    hours = seconds / HOUR
    return f"{hours:.1f} h" if hours < 48 else f"{hours:.0f} h ({hours / 24:.1f} d)"


# --- 1. per-turn change and decay ---------------------------------------------------


def _per_turn_rows() -> list[str]:
    rows = []
    rate_up, rate_down = _measured_decay_rate("stability")
    for stimulus in sorted(EMOTION_MATRIX):
        state = NeuralState(last_stimulus_time=FIXED_EPOCH, last_decay_check=FIXED_EPOCH)
        deltas = state.apply_stimulus(stimulus, 1.0, now_fn=lambda: FIXED_EPOCH)
        stab = deltas.get("stability", 0.0)
        undo = abs(stab) / (rate_up if stab < 0 else rate_down)
        cells = " | ".join(f"{deltas.get(dim, 0.0):+.3f}" for dim in DIMS_SHOWN)
        rows.append(f"| {stimulus} | {cells} | {undo:.0f} h |")
    return rows


def _measured_decay_rate(dim: str) -> tuple[float, float]:
    """Per-hour change towards the baseline from below and from above (measured)."""
    rates = []
    for offset in (-0.2, 0.2):
        state = NeuralState(
            **{dim: BASELINES[dim] + offset},
            last_stimulus_time=FIXED_EPOCH,
            last_decay_check=FIXED_EPOCH,
        )
        before = getattr(state, dim)
        state.apply_decay(now_fn=lambda: FIXED_EPOCH + HOUR, min_interval_sec=60.0)
        rates.append(abs(getattr(state, dim) - before))
    return rates[0], rates[1]


# --- 2. saturation ------------------------------------------------------------------


def _saturation_rows() -> list[str]:
    rows = []
    cases = [("hostile", HOSTILE, turns) for turns in (120, 240)] + [("mixed", MIXED, 444)]
    for name, verdicts, turns in cases:
        for gap in (60.0, 300.0, 1800.0, 3600.0, 3 * 3600.0):
            run = _conversation(verdicts, turns, gap)
            state = run.engine.state
            dims = " | ".join(f"{getattr(state, dim):.3f}" for dim in DIMS_SHOWN[:4])
            rows.append(
                f"| {name} | {turns} | {gap / 60:.0f} min | {dims} | "
                f"{run.first_critical or '—'} | {run.critical_turns}/{turns} | "
                f"{run.attachment_pinned_turns} | "
                f"{run.attachment_lost:.2f} |"
            )
    return rows


# --- 3. recovery by positive turns ----------------------------------------------------


def _positive_recovery_rows() -> list[str]:
    rows = []
    for gap in (60.0, 1800.0):
        run = _conversation(HOSTILE, 240, 60.0)
        start = run.engine.state.stability
        first: dict[str, int | None] = {"not_critical": None, "no_stability_gate": None}
        normal = None
        for index in range(1, 2001):
            level = _turn(run, POSITIVE[(index - 1) % len(POSITIVE)], gap)
            if first["not_critical"] is None and level is not BreakLevel.CRITICAL:
                first["not_critical"] = index
            if first["no_stability_gate"] is None and level in (
                BreakLevel.NORMAL,
                BreakLevel.COHERENCE,
            ):
                first["no_stability_gate"] = index
            if level is BreakLevel.NORMAL:
                normal = index
                break
        rows.append(
            f"| {gap / 60:.0f} min | {start:.3f} | {first['not_critical']} | "
            f"{first['no_stability_gate']} | {normal} | {run.engine.state.stability:.3f} |"
        )
    return rows


# --- 4. recovery by idle time with heartbeat ticks -------------------------------------

PACINGS: tuple[tuple[str, float, float], ...] = (
    ("fixed 5 min (floor=ceil=300)", 300.0, 300.0),
    ("fixed 30 min (floor=ceil=1800)", 1800.0, 1800.0),
    ("fixed 3 h (floor=ceil=10800)", 10800.0, 10800.0),
    ("adaptive default (floor 300 / ceil 10800)", 300.0, 10800.0),
)
CHECKPOINTS_H = (6, 24, 72, 168, 240)
IDLE_LIMIT_SEC = 400 * HOUR


def _idle_with_heartbeat(start: Run, floor: float, ceil: float) -> dict:
    run = start
    gateway = HeartbeatGateway(
        run.engine,
        GatewayConfig(hb_interval_floor_sec=floor, hb_interval_ceil_sec=ceil),
        now_fn=lambda: run.clock[0],
    )
    t0 = run.clock[0]
    ticks = 0
    first: dict[str, float | None] = {"warning": None, "normal": None}
    at: dict[int, float] = {}
    result = gateway.tick()
    while run.clock[0] - t0 < IDLE_LIMIT_SEC:
        run.clock[0] += result["next_interval_sec"]
        result = gateway.tick()
        ticks += 1
        elapsed = run.clock[0] - t0
        level = result["level"]
        if level != "critical" and first["warning"] is None:
            first["warning"] = elapsed
        if level == "normal" and first["normal"] is None:
            first["normal"] = elapsed
        for hours in CHECKPOINTS_H:
            if hours not in at and elapsed >= hours * HOUR:
                at[hours] = run.engine.state.stability
    # The first user turn after the idle period samples bc_window again.
    next_turn = run.engine.pre_turn().level.value
    return {"ticks": ticks, "first": first, "at": at, "next_turn": next_turn}


def _idle_from_level_rows() -> list[str]:
    """Idle release from a breaker level entered by a few turns (5-minute ticks)."""
    rows = []
    for stability, turns in ((0.45, 1), (0.25, 3)):
        run = _new_run()
        run.engine.state.stability = stability
        for _ in range(turns):  # enter the level; serve the CRITICAL cooldown
            run.clock[0] += 60.0
            level = run.engine.pre_turn().level.value
            run.engine.post_turn(stimulus=None)
        measured = _idle_with_heartbeat(run, 300.0, 300.0)
        released = measured["first"]["warning"] if level == "critical" else None
        rows.append(
            f"| {stability:.2f} ({level}, {turns} turn(s)) | "
            f"{_fmt_hours(released)} | "
            f"{_fmt_hours(measured['first']['normal'])} |"
        )
    return rows


def _idle_without_heartbeat(hours: float) -> tuple[float, str]:
    """No heartbeat: the next user turn after ``hours`` of idle applies the same decay."""
    run = _conversation(HOSTILE, 240, 60.0)
    run.clock[0] += hours * HOUR
    result = run.engine.pre_turn()
    return run.engine.state.stability, result.level.value


def _idle_rows() -> tuple[list[str], list[str], float]:
    rows = []
    start_stability = _conversation(HOSTILE, 240, 60.0).engine.state.stability
    for label, floor, ceil in PACINGS:
        measured = _idle_with_heartbeat(_conversation(HOSTILE, 240, 60.0), floor, ceil)
        at = " | ".join(f"{measured['at'][hours]:.3f}" for hours in CHECKPOINTS_H)
        rows.append(
            f"| {label} | {at} | {_fmt_hours(measured['first']['warning'])} | "
            f"{_fmt_hours(measured['first']['normal'])} | {measured['ticks']} | "
            f"{measured['next_turn']} |"
        )
    no_hb = []
    for hours in CHECKPOINTS_H:
        stability, level = _idle_without_heartbeat(hours)
        no_hb.append(f"{stability:.3f} ({level})")
    off_row = [
        (
            f"| off: the next turn after the idle time (`pre_turn`) | {' | '.join(no_hb)} | "
            "same time, at that turn | same time, at that turn | 0 | — |"
        )
    ]
    return rows, off_row, start_stability


def render_report(generated_on: date = GENERATED_ON) -> str:
    rate_up, rate_down = _measured_decay_rate("stability")
    idle, idle_off, idle_start = _idle_rows()
    header_dims = " | ".join(f"Δ{dim}" for dim in DIMS_SHOWN)
    lines = [
        "# Neural State saturation and recovery (#165)",
        "",
        (
            f"Generated: {generated_on.isoformat()} by `scripts/saturation_report.py` "
            "(deterministic; regenerate after changing the engine). Operator guide: "
            "[neural-saturation.md](../neural-saturation.md) / "
            "[neural-saturation_ja.md](../neural-saturation_ja.md)."
        ),
        "",
        "## Conditions",
        "",
        "- `StiraEngine` / `NeuralState` / `StiraConfig` defaults (no coefficient changed).",
        (
            "- Input: the live-probe scenarios as `sokudan-ja-310m` labelled them "
            "(label + intensity, abstain = no stimulus). *hostile* = S2+S3+S4 "
            f"({len(HOSTILE)} utterances, {sum(v is None for v in HOSTILE)} abstain), "
            f"*mixed* = S1…S6 ({len(MIXED)} utterances), *positive* = the "
            f"{len(POSITIVE)} positive verdicts of S1/S3/S4/S5. Sequences repeat cyclically."
        ),
        "- One turn = clock advance (the turn gap) → `pre_turn` → `post_turn`.",
        (
            "- Heartbeat rows call the real `HeartbeatGateway.tick()`; the clock jumps by the "
            "tick's own `next_interval_sec`. Nothing waits in real time; no model is called."
        ),
        "",
        "## 1. Change per turn and decay",
        "",
        (
            "Per turn: `x ← clamp(x + EMOTION_MATRIX[stimulus][x] × intensity, 0, 1)`. "
            "Between turns (and on a heartbeat tick): `x` moves linearly towards its baseline, "
            "by `rate × hours` from above and `0.5 × rate × hours` from below, never past it."
        ),
        "",
        f"Measured stability decay: {rate_down:.4f}/h from above, {rate_up:.4f}/h from below.",
        "",
        (
            f"| stimulus (intensity 1.0) | {header_dims} | idle time that undoes the "
            "stability change |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        *_per_turn_rows(),
        "",
        "## 2. Saturation in a long conversation",
        "",
        (
            "Final state after N turns; `first CRITICAL` = first turn that ended at CRITICAL; "
            "`CRITICAL` = turns that ended at CRITICAL; "
            "`att=1.0` = turns that ended with attachment pinned at 1.0; `att lost` = sum of "
            "positive attachment changes the clamp at 1.0 discarded."
        ),
        "",
        (
            "| input | turns | gap | stability | vigilance | satisfaction | attachment | "
            "first CRITICAL | CRITICAL | att=1.0 | att lost |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *_saturation_rows(),
        "",
        "## 3. Recovery by positive turns",
        "",
        (
            "Start: the state after 240 hostile turns at 1 min. Then positive turns only. "
            "*WARNING released* = the first turn at NORMAL or COHERENCE (stability above the "
            "WARNING exit); *NORMAL* also needs `bc_score` above its exit, i.e. stability that "
            "stopped moving."
        ),
        "",
        (
            "| gap | start stability | turns until not CRITICAL | turns until WARNING "
            "released | turns until NORMAL | stability then |"
        ),
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
        *_positive_recovery_rows(),
        "",
        "## 4. Recovery by idle time (heartbeat decay tick)",
        "",
        (
            f"Start: the state after 240 hostile turns at 1 min (stability {idle_start:.3f}, "
            "CRITICAL, cooldown already served by those turns). No further user turn. "
            "Stability at the first tick at or after each time; *not CRITICAL* / *NORMAL* = the "
            "first tick whose breaker query shows that level."
        ),
        "",
        "| heartbeat pacing | "
        + " | ".join(f"+{hours} h" for hours in CHECKPOINTS_H)
        + " | not CRITICAL | NORMAL | ticks (400 h) | first turn after 400 h |",
        "| --- | " + " | ".join("---:" for _ in CHECKPOINTS_H) + " | ---: | ---: | ---: | --- |",
        *idle,
        *idle_off,
        "",
        (
            "Idle release from a level entered by a few turns (5-minute ticks; the same at the "
            "next turn without a heartbeat):"
        ),
        "",
        "| start stability (level, turns) | not CRITICAL | NORMAL |",
        "| --- | ---: | ---: |",
        *_idle_from_level_rows(),
        "",
        "## Findings",
        "",
        (
            "Compared with the 2026-10-12 live probe (#165 body): saturation is reproduced "
            "(hostile 120 turns at 1 min: stability 0.015 / vigilance 0.985 / satisfaction "
            "0.030 / attachment 1.000; 240 turns: 178 CRITICAL; mixed 444 turns: 0.010 at "
            "1 min, 0.121 at 30 min). Recovery is re-measured: 29–31 positive turns leave "
            "CRITICAL (probe: about 60); from the saturated state idle time alone needs "
            "about 7 days to leave CRITICAL and about 10 days to reach NORMAL. The probe's "
            '"NORMAL after +6 h idle" is not reproduced from that state (+6 h: stability '
            "0.030, still CRITICAL)."
        ),
        "",
        (
            "- **Saturation is the intended accumulation, not a defect.** A stimulus moves "
            "each dimension by a few hundredths and decay needs hours to undo one turn "
            "(section 1). A short conversation does not reach the breaker; a long one-sided "
            "conversation drives the state to its clamps."
        ),
        (
            "- **The turn gap decides how far a conversation saturates** (section 2): decay "
            "between turns is proportional to the gap. Hostile input at 1–60 min per turn "
            "ends near 0 within 240 turns; at 3 h per turn it stays near the baseline and never reaches "
            "CRITICAL."
        ),
        (
            "- **The heartbeat interval does not change the recovery curve** (section 4): "
            "decay is linear in elapsed time, so every pacing, and no heartbeat at the next "
            "turn, gives the same stability at the same time. The interval decides *when* an "
            "idle agent's release is applied and shown (at most one interval late), and how "
            "many ticks are spent (4800 at 5 min vs 134 at 3 h over 400 h)."
        ),
        (
            "- **A long idle release ends in COHERENCE at the next turn**: ticks do not "
            "sample `bc_window`, so the first turn after the idle period sees a jump in "
            "stability (identity re-injection only, no tool limits)."
        ),
        (
            "- **Positive turns release much faster than idle time** (section 3: about 30 "
            "turns vs about 7 days)."
        ),
        (
            "- **attachment is pinned at 1.0** in long hostile runs (124 of 240 turns at "
            "1 min); the clamp discarded 1.33 of positive attachment change. That "
            "information is lost (design memo in the operator guide)."
        ),
        "",
        (
            "Conclusion: saturation is an intended property of the current coefficients, "
            "and they stay as they are. How far the state saturates is set by how the agent "
            "is used (turn gap); when an idle release becomes visible without a user turn is "
            "set by the operator's heartbeat settings; where the gates sit is set by the "
            "breaker thresholds on the settings page. The speed of idle recovery is fixed by "
            "the decay rates."
        ),
        "",
    ]
    return "\n".join(lines)


def capture() -> None:  # pragma: no cover - needs the [jev] extra and the model
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from live_probe import SCENARIOS

    from stiracore.agent.stimulus_classifier import SokudanClassifier

    classifier = SokudanClassifier(lazy=False)
    for name, turns in SCENARIOS.items():
        verdicts = []
        for message in turns:
            verdict = classifier.classify(message)
            verdicts.append(None if verdict is None else (verdict.category, verdict.intensity))
        print(f"{name}: {verdicts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--capture", action="store_true", help="re-run sokudan and print")
    args = parser.parse_args()
    if args.capture:
        capture()
        return 0
    # Keep the engine's breaker log out of the console while generating.
    logging.getLogger("stiracore").setLevel(logging.ERROR)
    args.output.write_text(render_report(), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
