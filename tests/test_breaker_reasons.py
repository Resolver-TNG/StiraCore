"""#455: the breaker's reason codes and numbers, without changing its decisions."""

from __future__ import annotations

import json
import random

import pytest

from stiracore.core import breaker_reasons
from stiracore.core.breaker_sim import FIXED_EPOCH
from stiracore.core.engine import BreakLevel, StiraConfig, StiraEngine
from stiracore.core.neural_state import EMOTION_MATRIX, NeuralState


class _Sink:
    def save(self, _checkpoint):
        return "test"


class _LegacyEngine(StiraEngine):
    """The breaker decision exactly as it was before #455 (a verbatim copy, no reasons)."""

    def _check_breaker(self):
        config = self.config
        stability = self.state.stability
        bc_score = self.state.bc_score
        if self._warning_active:
            if stability > config.stability_warning_exit:
                self._warning_active = False
        elif stability < config.stability_warning:
            self._warning_active = True
        if self._coherence_active:
            if bc_score > config.bc_exit_threshold:
                self._coherence_active = False
        elif bc_score < config.bc_threshold:
            self._coherence_active = True
        if self._active_level == BreakLevel.CRITICAL:
            if (
                self._critical_turns < config.critical_cooldown_turns
                or stability <= config.stability_critical_exit
            ):
                return BreakLevel.CRITICAL
        elif stability < config.stability_critical:
            self._critical_turns = 0
            self._active_level = BreakLevel.CRITICAL
            return self._active_level
        if self._warning_active:
            self._active_level = BreakLevel.WARNING
        elif self._coherence_active:
            self._active_level = BreakLevel.COHERENCE
        else:
            self._active_level = BreakLevel.NORMAL
        self._critical_turns = 0
        return self._active_level


def _engine(cls=StiraEngine, config=None):
    clock = {"t": FIXED_EPOCH}
    state = NeuralState(last_stimulus_time=FIXED_EPOCH, last_decay_check=FIXED_EPOCH)
    engine = cls(state=state, config=config, now_fn=lambda: clock["t"], checkpoint_store=_Sink())
    return engine, clock


def _run(schedule, cls=StiraEngine, config=None):
    """Like breaker_sim.simulate, recording each boundary's level and explanation."""
    engine, clock = _engine(cls, config)
    rows = []
    for stimulus, intensity, hours in schedule:
        clock["t"] += hours * 3600.0
        pre = engine.pre_turn()
        pre_expl = None if cls is _LegacyEngine else engine.breaker_explanation()
        summary = engine.post_turn(stimulus=stimulus, intensity=intensity)
        rows.append(
            {
                "pre": pre.level.value,
                "post": summary["breaker"],
                "stability": summary["stability"],
                "bc": summary["bc_score"],
                "pre_expl": pre_expl,
                "summary": summary,
            }
        )
    return engine, rows


def _schedules():
    rng = random.Random(455)
    names = sorted(EMOTION_MATRIX)
    yield from ([(name, 1.0, hours)] * 60 for name in names for hours in (0.0, 0.5, 1.0))
    for _ in range(40):
        yield [
            (
                rng.choice([*names, None]),
                rng.choice([0.5, 1.0, 2.0]),
                rng.choice([0.0, 0.0, 0.5, 1.0, 6.0]),
            )
            for _ in range(80)
        ]


@pytest.mark.parametrize(
    "config",
    [
        None,
        StiraConfig(critical_cooldown_turns=5),
        StiraConfig(
            stability_warning=0.6,
            stability_warning_exit=0.75,
            bc_threshold=0.7,
            bc_exit_threshold=0.8,
        ),
    ],
)
def test_decisions_are_unchanged_by_the_reasons(config):
    """Regression: every level at every boundary equals the pre-#455 decision."""
    for schedule in _schedules():
        _, new = _run(schedule, StiraEngine, config)
        _, old = _run(schedule, _LegacyEngine, config)
        assert [(r["pre"], r["post"], r["stability"], r["bc"]) for r in new] == [
            (r["pre"], r["post"], r["stability"], r["bc"]) for r in old
        ]


def _first(rows, level):
    for index, row in enumerate(rows):
        for side in ("pre", "post"):
            if row[side] == level:
                return index, side
    raise AssertionError(f"{level} not reached")


def test_warning_and_critical_entries_carry_codes_and_numbers():
    _, rows = _run([("loss", 1.0, 0.0)] * 20)
    index, side = _first(rows, "warning")
    assert side == "post"
    summary = rows[index]["summary"]
    assert summary["breaker_reasons"] == ["stability_below_warning"]
    assert summary["breaker_inputs"] == {
        "stability": round(summary["stability"], 4),
        "stability_warning": 0.5,
    }
    assert summary["stability"] < 0.5
    assert summary["breaker_transition"] == {
        "from": "normal",
        "to": "warning",
        "reasons": ["stability_below_warning"],
        "inputs": summary["breaker_inputs"],
    }

    index, side = _first(rows, "critical")
    summary = rows[index]["summary"]
    assert summary["breaker_transition"]["from"] == "warning"
    assert summary["breaker_transition"]["to"] == "critical"
    assert summary["breaker_reasons"] == ["stability_below_critical"]
    assert summary["breaker_inputs"]["stability_critical"] == 0.3
    assert summary["breaker_inputs"]["stability"] < 0.3


def test_coherence_entry_carries_the_bc_score():
    _, rows = _run([("achievement", 1.0, 0.0)] * 12)
    index, side = _first(rows, "coherence")
    assert (index + 1, side) == (10, "pre")  # docs/breaker-reachability.md: turn 10
    expl = rows[index]["pre_expl"]
    assert expl["level"] == "coherence"
    assert expl["reasons"] == ["bc_score_below_coherence"]
    assert expl["inputs"]["bc_threshold"] == 0.6
    assert expl["inputs"]["bc_score"] < 0.6
    assert rows[index]["summary"]["breaker_transition"]["to"] == "coherence"


def test_cooldown_and_hysteresis_holds_are_recorded():
    # Into CRITICAL, then recover by idle hours: the protected turns and the recovery
    # threshold hold the level on turns where the entry condition no longer holds.
    schedule = [("loss", 1.0, 0.0)] * 15 + [(None, 0.0, 6.0)] * 80
    _, rows = _run(schedule)
    codes = [tuple(r["pre_expl"]["reasons"]) for r in rows]
    levels = [r["pre"] for r in rows]
    critical_holds = [c for c, lv in zip(codes, levels, strict=True) if lv == "critical"]
    assert any("cooldown_active" in c for c in critical_holds)
    assert any("hysteresis_hold" in c for c in critical_holds)
    for row in rows:
        expl = row["pre_expl"]
        if "cooldown_active" in expl["reasons"]:
            assert expl["inputs"]["critical_turns"] < expl["inputs"]["critical_cooldown_turns"]
        if row["pre"] == "critical" and "hysteresis_hold" in expl["reasons"]:
            assert 0.3 <= expl["inputs"]["stability"] <= expl["inputs"]["stability_critical_exit"]
        if row["pre"] == "warning" and "hysteresis_hold" in expl["reasons"]:
            assert 0.5 <= expl["inputs"]["stability"] <= expl["inputs"]["stability_warning_exit"]
    assert "hysteresis_hold" in {
        c for lv, cs in zip(levels, codes, strict=True) if lv == "warning" for c in cs
    }
    # Leaving CRITICAL names the recovery and the served cooldown.
    left = [
        r
        for r in rows
        if (r["pre_expl"]["reasons"][:2] == ["stability_above_critical_exit", "cooldown_served"])
    ]
    assert left
    assert left[0]["pre_expl"]["inputs"]["critical_turns"] >= 3
    # Back at NORMAL: the warning latch release, then "within thresholds".
    assert rows[-1]["pre"] == "normal"
    assert rows[-1]["pre_expl"]["reasons"] == ["within_thresholds"]
    released = [r for r in rows if "stability_above_warning_exit" in r["pre_expl"]["reasons"]]
    assert released and released[0]["pre"] in ("normal", "coherence")


def test_coherence_hysteresis_hold():
    _, rows = _run([("achievement", 1.0, 0.0)] * 30)
    holds = [
        r["pre_expl"]
        for r in rows
        if r["pre"] == "coherence" and r["pre_expl"]["reasons"] == ["hysteresis_hold"]
    ]
    assert holds  # bc_score back above 0.6 but not yet above 0.7
    for expl in holds:
        assert 0.6 <= expl["inputs"]["bc_score"] <= expl["inputs"]["bc_exit_threshold"]
    released = [r for r in rows if "bc_score_above_coherence_exit" in r["pre_expl"]["reasons"]]
    assert released and released[0]["pre"] == "normal"


def test_reasons_hold_codes_and_numbers_only():
    schedule = [("loss", 1.0, 0.0)] * 15 + [(None, 0.0, 6.0)] * 30
    engine, rows = _run(schedule)
    for row in rows:
        for expl in (
            row["pre_expl"],
            {
                "reasons": row["summary"]["breaker_reasons"],
                "inputs": row["summary"]["breaker_inputs"],
            },
        ):
            assert set(expl["reasons"]) <= set(breaker_reasons.REASON_CODES)
            assert set(expl["inputs"]) <= set(breaker_reasons.INPUT_KEYS)
            assert all(type(v) in (int, float) for v in expl["inputs"].values())
    text = json.dumps([engine.breaker_explanation(), engine.last_breaker_transition()])
    assert engine.agent_id not in text
    assert "transcription" not in text


def test_clean_drops_unknown_codes_text_and_non_numbers():
    codes, values = breaker_reasons.clean(
        ["stability_below_warning", "user said hello", "stability_below_warning", 3],
        {
            "stability": 0.312345,
            "note": "secret",
            "bc_score": "0.2",
            "critical_turns": 2,
            "stability_warning": float("nan"),
            "critical_cooldown_turns": True,
        },
    )
    assert codes == ["stability_below_warning"]
    assert values == {"stability": 0.3123, "critical_turns": 2}
    assert breaker_reasons.clean("stability_below_warning", None) == ([], {})
    assert breaker_reasons.clean_transition({"from": "x", "to": "warning"}) is None


def test_queries_do_not_change_the_explanation_of_a_held_level():
    engine, _ = _run([("loss", 1.0, 0.0)] * 8)
    assert engine.breaker_status().level == BreakLevel.WARNING
    first = engine.breaker_explanation()
    engine.breaker_status()
    assert engine.breaker_explanation() == first
    assert engine.breaker_explanation()["reasons"] == ["stability_below_warning"]
