"""#168: bc_score (coherence) — the pure reference function and the pinned behaviour.

Every expected value below was measured on the current code, not derived by hand.
``docs/coherence.md`` quotes the same numbers.
"""

from __future__ import annotations

import itertools
import math
import random
import re
from pathlib import Path

import pytest

from stiracore.core import (
    BC_MIN_SAMPLES,
    BreakAction,
    BreakLevel,
    NeuralState,
    StiraConfig,
    compute_bc_score,
)
from stiracore.core.coherence import BC_DECIMALS, BC_STDDEV_SCALE, BC_UNMEASURED

# --- The function: boundaries -------------------------------------------------------


def test_constants_are_the_documented_definition() -> None:
    assert (BC_MIN_SAMPLES, BC_UNMEASURED, BC_STDDEV_SCALE, BC_DECIMALS) == (5, 1.0, 5.0, 4)


@pytest.mark.parametrize("size", [0, 1, 4])
def test_fewer_than_five_samples_are_unmeasured_whatever_the_spread(size: int) -> None:
    window = [0.0, 1.0, 0.0, 1.0][:size]
    assert compute_bc_score(window) == 1.0


def test_four_and_five_samples_boundary() -> None:
    assert compute_bc_score([0.0, 1.0, 0.0, 1.0]) == 1.0
    assert compute_bc_score([0.0, 1.0, 0.0, 1.0, 0.0]) == 0.0


@pytest.mark.parametrize("value", [0.0, 0.37, 0.7, 1.0])
@pytest.mark.parametrize("size", [5, 20, 50])
def test_zero_variance_scores_one(value: float, size: int) -> None:
    assert compute_bc_score([value] * size) == 1.0


@pytest.mark.parametrize(
    "window",
    [[0.0, 1.0] * 10, [0.5, 0.9] * 10, [0.0] * 10 + [1.0] * 10, [0.3, 0.7] * 3],
)
def test_extreme_spread_is_clamped_to_zero(window: list[float]) -> None:
    # A standard deviation of 0.2 or more: 1 - 5 * sd <= 0, never negative.
    assert compute_bc_score(window) == 0.0


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ([0.7, 0.7, 0.7, 0.7, 0.6], 0.8),
        ([0.7, 0.7, 0.7, 0.7, 0.5], 0.6),
        ([0.7, 0.7, 0.7, 0.7, 0.45], 0.5),
        ([0.62] * 10 + [0.78] * 10, 0.6),
        ([0.63] * 10 + [0.77] * 10, 0.65),
        ([0.64] * 10 + [0.76] * 10, 0.7),
        ([round(0.70 + 0.02 * i, 4) for i in range(10)], 0.7128),
        ([round(0.60 + 0.02 * i, 4) for i in range(20)], 0.4234),
    ],
)
def test_documented_examples(window: list[float], expected: float) -> None:
    assert compute_bc_score(window) == expected


# --- The function: invariants -------------------------------------------------------


def _windows(seed: int, count: int = 200):
    rng = random.Random(seed)
    for _ in range(count):
        size = rng.randint(0, 25)
        yield [round(rng.random(), rng.choice((2, 4, 6))) for _ in range(size)]


def test_range_and_rounding() -> None:
    for window in _windows(1):
        score = compute_bc_score(window)
        assert 0.0 <= score <= 1.0
        assert round(score, BC_DECIMALS) == score


def test_matches_the_closed_form() -> None:
    for window in _windows(2):
        if len(window) < BC_MIN_SAMPLES:
            continue
        mean = math.fsum(window) / len(window)
        sd = math.sqrt(math.fsum((x - mean) ** 2 for x in window) / len(window))
        assert compute_bc_score(window) == pytest.approx(max(0.0, 1.0 - 5.0 * sd), abs=1e-4)


def test_order_does_not_matter() -> None:
    # Up to the last rounded digit: summation order can move a value across it.
    window = [0.7, 0.64, 0.81, 0.7, 0.55, 0.9]
    reference = compute_bc_score(window)
    for permutation in itertools.permutations(window):
        assert compute_bc_score(permutation) == pytest.approx(reference, abs=1e-4)


def test_shifting_every_sample_does_not_change_the_score() -> None:
    window = [0.30, 0.34, 0.29, 0.41, 0.33, 0.30]
    for shift in (0.1, 0.3, 0.55):
        shifted = [x + shift for x in window]
        assert compute_bc_score(shifted) == pytest.approx(compute_bc_score(window), abs=1e-4)


def test_a_wider_spread_never_scores_higher() -> None:
    base = [0.7] * 15
    scores = [compute_bc_score([*base, 0.7 - d]) for d in (0.0, 0.05, 0.1, 0.2, 0.4, 0.7)]
    assert scores == sorted(scores, reverse=True)


def test_accepts_any_iterable_and_ints() -> None:
    assert compute_bc_score(iter([1, 1, 1, 1, 1])) == 1.0
    assert compute_bc_score((0.7, 0.7, 0.7, 0.7, 0.5)) == 0.6
    assert compute_bc_score(x for x in [0, 1, 0, 1, 0]) == 0.0


@pytest.mark.parametrize(
    "bad", [math.nan, math.inf, -math.inf, -0.01, 1.01, True, False, "0.5", None]
)
def test_rejects_a_sample_that_is_not_a_finite_number_in_range(bad: object) -> None:
    with pytest.raises(ValueError, match="bc window samples"):
        compute_bc_score([0.7, 0.7, 0.7, 0.7, bad])
    # Validation does not depend on the window being long enough to measure.
    with pytest.raises(ValueError, match="bc window samples"):
        compute_bc_score([bad])


def test_does_not_modify_the_window() -> None:
    window = [0.7, 0.6, 0.5, 0.6, 0.7]
    copy = list(window)
    compute_bc_score(window)
    assert window == copy


# --- Same value as NeuralState (live update and reload) ----------------------------


@pytest.mark.parametrize("window_size", [5, 7, 20])
def test_neural_state_update_computes_the_same_score(window_size: int) -> None:
    rng = random.Random(window_size)
    state = NeuralState()
    for _ in range(80):
        state.stability = rng.choice((rng.random(), state.stability, round(rng.random(), 2)))
        score = state.update_bc_window(window_size=window_size)
        assert score == compute_bc_score(state.bc_window)
        assert score == state.bc_score


def test_reload_derives_the_same_score_from_the_window(tmp_path) -> None:
    rng = random.Random(7)
    path = tmp_path / "neural-state.json"
    for size in (0, 4, 5, 6, 20):
        state = NeuralState(bc_window=[rng.random() for _ in range(size)])
        state.save(path)
        assert NeuralState.load(path).bc_score == compute_bc_score(state.bc_window)


# --- The pinned behaviour of the window (initial fill, #142) ------------------------


def test_one_sample_per_pre_turn_and_none_per_policy_query(make_engine) -> None:
    engine = make_engine()
    for turn in range(1, 4):
        engine.pre_turn()
        engine.breaker_status()
        engine.breaker_status()
        assert len(engine.state.bc_window) == turn


def test_no_stimulus_never_leaves_normal(make_engine) -> None:
    engine = make_engine()
    levels = {engine.pre_turn().level for _ in range(25)}
    assert levels == {BreakLevel.NORMAL}
    assert engine.state.bc_score == 1.0
    assert len(engine.state.bc_window) == 20


def test_the_first_four_turns_cannot_enter_coherence(make_engine) -> None:
    # Stability swings between 1.0 and 0.6 (no stability breaker): a 0.2 sd.
    engine = make_engine()
    levels = []
    for turn in range(6):
        engine.state.stability = 1.0 if turn % 2 == 0 else 0.6
        levels.append(engine.pre_turn().level)
    assert levels[:4] == [BreakLevel.NORMAL] * 4
    assert levels[4] == BreakLevel.COHERENCE
    assert engine.state.bc_score == 0.0


def test_during_fill_the_score_uses_only_the_samples_present(make_engine) -> None:
    # One sample 0.1 below the rest: it weighs 1/n, so the score rises as the window fills,
    # and is back at 1.0 once the sample has left the 20-sample window.
    engine = make_engine()
    scores = {}
    for turn in range(1, 23):
        engine.state.stability = 0.7 if turn == 1 else 0.6
        engine.pre_turn()
        scores[turn] = engine.state.bc_score
    assert [scores[n] for n in (4, 5, 10, 20, 21)] == [1.0, 0.8, 0.85, 0.891, 1.0]


def test_a_steady_rise_enters_coherence_as_the_window_fills(make_engine) -> None:
    # #142 / docs/breaker-reachability.md: achievement at intensity 1.0, no idle time.
    # stability rises by 0.03 per turn (0.70 → 1.00); the score is direction-blind.
    engine = make_engine()
    first = None
    exit_turn = None
    for turn in range(1, 61):
        level = engine.pre_turn().level
        if level == BreakLevel.COHERENCE and first is None:
            first = turn
        if first is not None and exit_turn is None and level == BreakLevel.NORMAL:
            exit_turn = turn
        engine.post_turn("achievement", 1.0)
    assert first == 10
    assert exit_turn is not None and exit_turn > first
    assert engine.state.stability == 1.0
    assert engine.state.bc_score == 1.0


def test_restart_keeps_the_window(tmp_path, make_engine) -> None:
    engine = make_engine()
    for _ in range(7):
        engine.pre_turn()
        engine.post_turn("achievement", 1.0)
    path = tmp_path / "neural-state.json"
    engine.state.save(path)
    restored = NeuralState.load(path)
    assert restored.bc_window == engine.state.bc_window
    assert restored.bc_score == engine.state.bc_score < 1.0


@pytest.mark.parametrize("size", [-1, 0, 1, 4])
def test_a_window_size_below_five_is_refused(size: int) -> None:
    # #337: such a window never reaches five samples, so COHERENCE could never fire and
    # nothing said so. The config refuses it, with the field and the minimum in the message.
    with pytest.raises(ValueError, match=r"bc_window_size.*>= 5"):
        StiraConfig(bc_window_size=size)


@pytest.mark.parametrize("size", [True, 5.0, "20", None])
def test_a_window_size_must_be_an_integer(size) -> None:
    with pytest.raises(ValueError, match="bc_window_size"):
        StiraConfig(bc_window_size=size)


def test_the_smallest_window_size_measures_and_can_fire(make_engine) -> None:
    assert StiraConfig(bc_window_size=BC_MIN_SAMPLES).bc_window_size == 5
    engine = make_engine(config=StiraConfig(bc_window_size=BC_MIN_SAMPLES))
    levels = []
    for turn in range(8):
        engine.state.stability = 0.9 if turn % 2 else 0.1
        levels.append(engine.pre_turn().level)
    assert len(engine.state.bc_window) == 5
    assert engine.state.bc_score < 0.6
    assert BreakLevel.COHERENCE in levels


def test_the_save_path_refuses_a_small_window(tmp_path) -> None:
    from stiracore.config.models import AgentConfig

    with pytest.raises(ValueError, match="bc_window_size"):
        AgentConfig(stira_config_overrides={"bc_window_size": 3})
    AgentConfig(stira_config_overrides={"bc_window_size": 5})


def _write_agent_config(tmp_path, value):
    import json

    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps({"stira_config_overrides": {"bc_window_size": value}}))
    return path


@pytest.mark.parametrize("value", [-1, 0, 1, 4, True, 5.0, 20.0, "20", None])
def test_the_start_up_path_rounds_a_bad_window_to_five_and_says_so(
    tmp_path, monkeypatch, caplog, value
) -> None:
    # #337 follow-up: a hand-edited agent-config.json must not stop the start (nor leave
    # COHERENCE silently off): the value is rounded to the minimum, once-logged and flagged.
    from stiracore.config import models
    from stiracore.config.models import AgentConfig
    from stiracore.config.resolver import resolve_config

    monkeypatch.setattr(models, "_BC_CLAMP_WARNED", False)
    path = _write_agent_config(tmp_path, value)
    with caplog.at_level("WARNING"):
        agent = AgentConfig.load(path, tool_config_path=tmp_path / "tools.json")
    assert agent.stira_config_overrides["bc_window_size"] == BC_MIN_SAMPLES
    assert agent.bc_window_clamped is True
    resolved = resolve_config(agent=agent)
    assert resolved.stira_config.bc_window_size == BC_MIN_SAMPLES
    assert resolved.bc_window_clamped is True
    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(messages) == 1
    assert "bc_window_size" in messages[0]
    # The value never reaches the log: the format arguments are the minimum, twice, and
    # nothing else (a `%r` of the value would add it here).
    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert record.args == (BC_MIN_SAMPLES, BC_MIN_SAMPLES)
    assert "%r" not in record.msg and "%s" not in record.msg


def test_the_clamp_warning_is_logged_once_per_process(tmp_path, monkeypatch, caplog) -> None:
    from stiracore.config import models
    from stiracore.config.models import AgentConfig

    monkeypatch.setattr(models, "_BC_CLAMP_WARNED", False)
    path = _write_agent_config(tmp_path, 3)
    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert AgentConfig.load(path, tool_config_path=tmp_path / "t.json").bc_window_clamped
    assert len([r for r in caplog.records if "bc_window_size" in r.getMessage()]) == 1


@pytest.mark.parametrize("value", [5, 6, 20])
def test_a_valid_window_is_not_touched(tmp_path, monkeypatch, caplog, value) -> None:
    from stiracore.config import models
    from stiracore.config.models import AgentConfig

    monkeypatch.setattr(models, "_BC_CLAMP_WARNED", False)
    path = _write_agent_config(tmp_path, value)
    with caplog.at_level("WARNING"):
        agent = AgentConfig.load(path, tool_config_path=tmp_path / "tools.json")
    assert agent.stira_config_overrides["bc_window_size"] == value
    assert agent.bc_window_clamped is False
    assert not [r for r in caplog.records if "bc_window_size" in r.getMessage()]


def test_saving_after_the_rounding_writes_the_clean_value(tmp_path, monkeypatch) -> None:
    import json

    from stiracore.config import models
    from stiracore.config.models import AgentConfig

    monkeypatch.setattr(models, "_BC_CLAMP_WARNED", True)
    path = _write_agent_config(tmp_path, 3)
    tools = tmp_path / "tools.json"
    agent = AgentConfig.load(path, tool_config_path=tools)
    agent.save()
    assert json.loads(path.read_text())["stira_config_overrides"]["bc_window_size"] == 5
    assert AgentConfig.load(path, tool_config_path=tools).bc_window_clamped is False


def test_the_file_look_agrees_with_the_load(tmp_path) -> None:
    import json

    from stiracore.config.models import bc_window_clamped_in

    path = tmp_path / "agent-config.json"
    assert bc_window_clamped_in(path) is False  # absent
    path.write_text("not json")
    assert bc_window_clamped_in(path) is False
    path.write_text("[]")
    assert bc_window_clamped_in(path) is False
    for value, expected in ((3, True), (None, True), (True, True), (5, False), (20, False)):
        path.write_text(json.dumps({"stira_config_overrides": {"bc_window_size": value}}))
        assert bc_window_clamped_in(path) is expected, value
    path.write_text(json.dumps({"stira_config_overrides": {}}))
    assert bc_window_clamped_in(path) is False


def test_the_clamp_does_not_hide_other_invalid_values(tmp_path) -> None:
    import json

    from stiracore.config.models import AgentConfig

    path = tmp_path / "agent-config.json"
    path.write_text(
        json.dumps({"stira_config_overrides": {"bc_window_size": 3, "bc_threshold": 2.0}})
    )
    with pytest.raises(ValueError, match="bc_threshold"):
        AgentConfig.load(path, tool_config_path=tmp_path / "tools.json")


def test_a_fresh_config_is_not_flagged(tmp_path) -> None:
    from stiracore.config.models import AgentConfig

    assert (
        AgentConfig.load(
            tmp_path / "none.json", tool_config_path=tmp_path / "t.json"
        ).bc_window_clamped
        is False
    )


def test_measured_needs_the_minimum_sample_count() -> None:
    from stiracore.core.coherence import bc_is_measured

    assert [bc_is_measured(n) for n in (0, 4, 5, 20)] == [False, False, True, True]
    for bad in (-1, True, 2.0, None):
        with pytest.raises(ValueError):
            bc_is_measured(bad)


# --- Thresholds and hysteresis ------------------------------------------------------


def _engine_with_window(make_engine, window: list[float]):
    """An engine whose next pre_turn sees exactly ``window`` (last sample = stability)."""
    state = NeuralState(stability=window[-1], bc_window=list(window[:-1]))
    return make_engine(state=state)


def test_entry_is_strictly_below_the_threshold(make_engine) -> None:
    at_threshold = _engine_with_window(make_engine, [0.7, 0.7, 0.7, 0.7, 0.5])
    assert at_threshold.pre_turn().level == BreakLevel.NORMAL
    assert at_threshold.state.bc_score == 0.6

    below = _engine_with_window(make_engine, [0.9, 0.9, 0.9, 0.9, 0.69])
    assert compute_bc_score([0.9, 0.9, 0.9, 0.9, 0.69]) == 0.58
    assert below.pre_turn().level == BreakLevel.COHERENCE


def test_exit_is_strictly_above_the_exit_threshold(make_engine) -> None:
    engine = make_engine(state=NeuralState(stability=0.6, bc_window=[1.0, 0.6, 1.0, 0.6]))
    assert engine.pre_turn().level == BreakLevel.COHERENCE
    # Refill the window so the score lands exactly on 0.7: still COHERENCE.
    engine.state.bc_window = [0.64] * 10 + [0.76] * 9
    engine.state.stability = 0.76
    assert engine.pre_turn().level == BreakLevel.COHERENCE
    assert engine.state.bc_score == 0.7
    # Just above 0.7: back to NORMAL.
    engine.state.bc_window = [0.645] * 10 + [0.755] * 9
    engine.state.stability = 0.755
    assert engine.pre_turn().level == BreakLevel.NORMAL
    assert engine.state.bc_score > 0.7


def test_coherence_only_reinjects_identity(make_engine) -> None:
    engine = _engine_with_window(make_engine, [1.0, 0.6, 1.0, 0.6, 1.0])
    result = engine.pre_turn()
    assert result.level == BreakLevel.COHERENCE
    assert result.actions == BreakAction.INJECT_IDENTITY
    assert not engine.is_write_blocked()


# --- docs/coherence*.md stay in step with the code ----------------------------------

DOCS = Path(__file__).resolve().parents[1] / "docs"


@pytest.mark.parametrize("name", ["coherence.md", "coherence_ja.md"])
def test_documented_defaults_match_the_code(name: str) -> None:
    text = (DOCS / name).read_text(encoding="utf-8")
    block = text.split("<!-- coherence-defaults:begin -->", 1)[1].split(
        "<!-- coherence-defaults:end -->", 1
    )[0]
    rows = dict(re.findall(r"^\|\s*`(\w+)`[^|]*\|\s*([0-9.]+)\s*\|", block, flags=re.MULTILINE))
    config = StiraConfig()
    assert rows == {
        "bc_window_size": str(config.bc_window_size),
        "BC_MIN_SAMPLES": str(BC_MIN_SAMPLES),
        "BC_STDDEV_SCALE": str(BC_STDDEV_SCALE),
        "bc_threshold": str(config.bc_threshold),
        "bc_exit_threshold": str(config.bc_exit_threshold),
    }
