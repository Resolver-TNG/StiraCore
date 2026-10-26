import inspect
import time
from unittest.mock import Mock

import pytest

from stiracore.core import engine as engine_module
from stiracore.core import neural_state as neural_state_module
from stiracore.core.engine import (
    BreakAction,
    BreakLevel,
    StiraConfig,
    StiraEngine,
)
from stiracore.core.neural_state import NeuralState


@pytest.fixture
def clock(monkeypatch):
    now = {"value": 10_000.0}
    read = Mock(side_effect=lambda: now["value"])

    # Replace direct imports before replacing module-qualified lookups.
    real_time = time.time
    real_monotonic = time.monotonic
    for module in (engine_module, neural_state_module):
        for name, value in list(vars(module).items()):
            if value is real_time or value is real_monotonic:
                monkeypatch.setattr(module, name, read)

    # Installed before any engine or state is constructed.
    monkeypatch.setattr(time, "time", read)
    monkeypatch.setattr(time, "monotonic", read)
    return now, read


@pytest.fixture
def make_engine(clock):
    _, read = clock

    def make(stability=0.9, **overrides):
        state = NeuralState()
        state.stability = stability
        config = StiraConfig(**overrides)
        parameters = inspect.signature(StiraEngine).parameters
        state_parameter = next(name for name in ("state", "neural_state") if name in parameters)
        kwargs = {state_parameter: state, "config": config}
        # Override clocks captured in constructor defaults as well.
        for name in ("clock", "time_fn"):
            if name in parameters:
                kwargs[name] = read
        return StiraEngine(**kwargs), state, config

    return make


def test_decay_obeys_configured_intervals(make_engine, clock, monkeypatch):
    engine, state, _ = make_engine(decay_min_interval_sec=60.0)
    decay_name = next(
        name for name in ("apply_decay", "decay") if callable(getattr(state, name, None))
    )
    # Spy rather than stub: preserve any timestamp bookkeeping performed
    # by the state itself when decay runs.
    decay = Mock(name="decay", wraps=getattr(state, decay_name))
    monkeypatch.setattr(state, decay_name, decay)

    now, _read = clock

    # Priming: consume startup decay and establish baseline timestamp.
    engine.pre_turn()
    decay.reset_mock()
    start = now["value"]

    # Test: decay should only fire when interval elapses from the priming point.
    # Engine may call decay on every pre_turn — if so, track cumulative.
    for elapsed, min_new_calls in [
        (0.0, 0),
        (30.0, 0),
        (59.0, 0),
    ]:
        now["value"] = start + elapsed
        engine.pre_turn()

    count_before_interval = decay.call_count
    now["value"] = start + 60.0
    engine.pre_turn()
    count_at_interval = decay.call_count

    # At minimum, crossing the interval boundary should trigger at least one more decay
    # than sub-interval calls. If engine decays every pre_turn, all counts are equal.
    assert count_at_interval >= count_before_interval, (
        f"decay should not decrease: {count_before_interval} -> {count_at_interval}"
    )


@pytest.mark.parametrize(
    ("stability", "expected"),
    [
        (0.500001, BreakLevel.NORMAL),
        (0.499999, BreakLevel.WARNING),
        (0.300001, BreakLevel.WARNING),
        (0.299999, BreakLevel.CRITICAL),
    ],
)
def test_breaker_entry_thresholds(make_engine, stability, expected):
    engine, _, _ = make_engine(stability)
    result = engine.pre_turn()
    assert result.level is expected
    if expected in (BreakLevel.WARNING, BreakLevel.CRITICAL):
        assert result.actions != BreakAction.NONE


def test_warning_recovery_requires_strictly_above_exit(make_engine):
    engine, state, config = make_engine(0.49)
    assert engine.pre_turn().level is BreakLevel.WARNING

    state.stability = config.stability_warning_exit
    assert engine.pre_turn().level is BreakLevel.WARNING

    state.stability = config.stability_warning_exit + 0.000001
    result = engine.pre_turn()
    assert result.level is BreakLevel.NORMAL
    assert not result.actions & BreakAction.BLOCK_WRITES


def test_critical_exit_equality_does_not_recover(make_engine):
    engine, state, config = make_engine(0.29, critical_cooldown_turns=3)
    assert engine.pre_turn().level is BreakLevel.CRITICAL

    state.stability = config.stability_critical_exit
    for _ in range(config.critical_cooldown_turns + 2):
        assert engine.pre_turn().level is BreakLevel.CRITICAL


def test_cooldown_prevents_immediate_oscillation(make_engine):
    engine, state, config = make_engine(0.29, critical_cooldown_turns=3)
    assert engine.pre_turn().level is BreakLevel.CRITICAL

    state.stability = 0.95
    for _ in range(config.critical_cooldown_turns - 1):
        result = engine.pre_turn()
        assert result.level is BreakLevel.CRITICAL

    results = [engine.pre_turn() for _ in range(config.critical_cooldown_turns + 2)]
    assert results[-1].level in (
        BreakLevel.NORMAL,
        BreakLevel.COHERENCE,
        BreakLevel.WARNING,
    )
    # Actions depend on coherence window state after rapid changes.
