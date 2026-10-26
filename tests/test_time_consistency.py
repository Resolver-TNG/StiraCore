"""#147: time and update consistency across heartbeat, turns and persistence.

Every test drives a controllable clock; expected values are computed from the
decay law (linear toward the baseline, half rate when recovering from below)
and were measured before being fixed here.
"""

from __future__ import annotations

import json
import sys
import threading

import pytest

from stiracore.agent.state_persistence import StatePersistence
from stiracore.config import GatewayConfig
from stiracore.core import (
    BreakLevel,
    FileCheckpointStore,
    NeuralState,
    StiraConfig,
    StiraEngine,
)
from stiracore.core.neural_state import BASELINES, DECAY_RATES
from stiracore.heartbeat import HeartbeatGateway

T0 = 1_000_000.0
DIMS = tuple(DECAY_RATES)


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _state(**overrides) -> NeuralState:
    values = {
        "trust": 0.8,
        "curiosity": 0.8,
        "vigilance": 0.6,
        "attachment": 0.8,
        "stability": 0.3,
        "satisfaction": 0.8,
        "last_decay_check": T0,
        "last_stimulus_time": T0,
    }
    values.update(overrides)
    return NeuralState(**values)


def _expected(initial: NeuralState, seconds: float) -> dict[str, float]:
    hours = seconds / 3600.0
    out = {}
    for dim, rate in DECAY_RATES.items():
        old, base = getattr(initial, dim), BASELINES[dim]
        if old > base:
            out[dim] = max(base, old - rate * hours)
        elif old < base:
            out[dim] = min(base, old + rate * hours * 0.5)
        else:
            out[dim] = old
    return out


def _values(state: NeuralState) -> dict[str, float]:
    return {dim: getattr(state, dim) for dim in DIMS}


def _close(actual: dict, expected: dict, tol: float = 1e-9) -> bool:
    return all(abs(actual[d] - expected[d]) <= tol for d in DIMS)


def _engine(state: NeuralState, clock: Clock, root, **config) -> StiraEngine:
    return StiraEngine(
        state=state,
        config=StiraConfig(**config),
        now_fn=clock,
        checkpoint_store=FileCheckpointStore(root),
    )


# --- Decay count and elapsed time ------------------------------------------


@pytest.mark.parametrize("step", [60, 61, 120, 300, 1800, 3600])
def test_decay_total_does_not_depend_on_tick_frequency(step, tmp_path):
    """Measured before #147: 60 s ticks lost all trust decay and all stability
    recovery in an hour (per-tick rounding to 4 decimals)."""
    clock = Clock()
    state = _state()
    engine = _engine(state, clock, tmp_path / "cp")
    hb = HeartbeatGateway(engine, GatewayConfig(), now_fn=clock)
    calls = 0
    while clock.now < T0 + 3600:
        clock.now = min(T0 + 3600, clock.now + step)
        # Alternate the two decay drivers sharing one state and clock.
        if calls % 2:
            engine.pre_turn()
            engine.post_turn()
        else:
            hb.tick()
        calls += 1
    # A final span shorter than the minimum interval is deferred, never dropped.
    consumed = state.last_decay_check - T0
    assert 3600 - 60 < consumed <= 3600
    assert _close(_values(state), _expected(_state(), consumed)), _values(state)


def test_same_instant_is_decayed_once_by_hb_and_turn(tmp_path):
    clock = Clock(T0 + 600)
    state = _state()
    engine = _engine(state, clock, tmp_path / "cp")
    hb = HeartbeatGateway(engine, GatewayConfig(), now_fn=clock)
    applied = [bool(hb.tick()["decay_deltas"])]
    before = _values(state)
    engine.pre_turn()
    engine.post_turn()
    applied.append(_values(state) != before)
    applied.append(bool(hb.tick()["decay_deltas"]))
    assert applied == [True, False, False]
    assert _close(_values(state), _expected(_state(), 600))


def test_min_interval_defers_but_never_drops_elapsed_time():
    clock = Clock()
    state = _state()
    counts = []
    for _ in range(8):  # 8 calls 30 s apart, min interval 60 s
        clock.now += 30
        counts.append(bool(state.apply_decay(now_fn=clock, min_interval_sec=60)))
    assert counts == [False, True] * 4
    assert _close(_values(state), _expected(_state(), 240))


# --- Clock reversal, restart and long stop ---------------------------------


def test_clock_moving_backwards_resyncs_without_decay():
    """Measured before #147: decay froze until the clock passed the old
    timestamp again (an hour here)."""
    clock = Clock(T0 - 3600)
    state = _state()
    assert state.apply_decay(now_fn=clock) == {}
    assert _values(state) == _values(_state())
    assert state.last_decay_check == T0 - 3600
    clock.now = T0 - 3600 + 120
    state.apply_decay(now_fn=clock)
    assert _close(_values(state), _expected(_state(), 120))


def test_forward_jump_then_correction_is_not_counted_twice():
    clock = Clock(T0 + 3600)
    state = _state()
    state.apply_decay(now_fn=clock)  # the (spurious) forward hour is applied once
    after_jump = _values(state)
    clock.now = T0 + 10  # NTP corrects the clock
    assert state.apply_decay(now_fn=clock) == {}
    assert _values(state) == after_jump
    clock.now = T0 + 130
    state.apply_decay(now_fn=clock)
    assert _close(_values(state), _expected(_state(), 3600 + 120))


def test_long_stop_settles_on_baselines_without_overshoot():
    state = _state(stability=0.05, vigilance=0.95, trust=0.0)
    state.apply_decay(now_fn=lambda: T0 + 365 * 86400)
    assert _values(state) == {dim: BASELINES[dim] for dim in DIMS}


def test_restart_applies_downtime_once(tmp_path):
    path = tmp_path / "state.json"
    clock = Clock()
    first = _state()
    StatePersistence(first, path).save()

    clock.now = T0 + 2 * 3600  # process down for two hours
    restored = NeuralState()
    StatePersistence(restored, path).load()
    assert restored.last_decay_check == T0
    engine = _engine(restored, clock, tmp_path / "cp")
    hb = HeartbeatGateway(engine, GatewayConfig(), now_fn=clock)
    engine.pre_turn()
    engine.post_turn()
    hb.tick()  # same instant: nothing more
    assert _close(_values(restored), _expected(_state(), 2 * 3600))


def _restart(tmp_path, stability: float, downtime: float) -> BreakLevel:
    path = tmp_path / "state.json"
    StatePersistence(_state(stability=stability), path).save()
    restored = NeuralState()
    StatePersistence(restored, path).load()
    clock = Clock(T0 + downtime)
    engine = _engine(restored, clock, tmp_path / "cp")
    return engine.pre_turn().level


@pytest.mark.parametrize(
    ("stability", "downtime", "level"),
    [
        (0.2, 60, BreakLevel.CRITICAL),  # still below the entry threshold
        (0.2, 30 * 86400, BreakLevel.NORMAL),  # long stop recovered to baseline
        (0.55, 60, BreakLevel.NORMAL),
    ],
)
def test_restart_breaker_level_follows_restored_stability(tmp_path, stability, downtime, level):
    assert _restart(tmp_path, stability, downtime) == level


@pytest.mark.xfail(
    strict=True,
    reason="#147: breaker hysteresis/cooldown lives only in the engine (and in "
    "checkpoints the chat server never resumes); a restart during CRITICAL cooldown "
    "comes back as WARNING",
)
def test_restart_during_critical_cooldown_stays_critical(tmp_path):
    path = tmp_path / "state.json"
    clock = Clock()
    state = _state(stability=0.2)
    engine = _engine(state, clock, tmp_path / "cp")
    assert engine.pre_turn().level == BreakLevel.CRITICAL
    engine.post_turn()
    state.stability = 0.4  # above critical entry (0.3), below its exit (0.45)
    assert engine.pre_turn().level == BreakLevel.CRITICAL  # hysteresis holds in-process
    engine.post_turn()
    StatePersistence(state, path).save()
    assert _restart_from(path, tmp_path, clock) == BreakLevel.CRITICAL


def _restart_from(path, tmp_path, clock) -> BreakLevel:
    restored = NeuralState()
    StatePersistence(restored, path).load()
    engine = _engine(restored, clock, tmp_path / "fresh")
    return engine.pre_turn().level


# --- Concurrent HB / turn / save: no lost updates ---------------------------


@pytest.fixture
def fast_switching():
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # provoke interleaving
    yield
    sys.setswitchinterval(previous)


def _race(root, turns: int = 300) -> tuple[NeuralState, NeuralState, StatePersistence]:
    clock = Clock()
    state = _state(stability=0.7, trust=0.5, curiosity=0.5, vigilance=0.1)
    engine = _engine(state, clock, root / "cp", decay_min_interval_sec=0)
    hb = HeartbeatGateway(engine, GatewayConfig(), now_fn=clock)
    persistence = StatePersistence(state, root / "s.json", state_lock=engine.state_lock)

    def turn_worker():
        for _ in range(turns):
            engine.post_turn("joy", 0.01)

    def hb_worker():
        for _ in range(turns):
            hb.tick()  # the clock does not move: decay is a numeric no-op

    def save_worker():
        for _ in range(40):
            persistence.save()

    workers = [threading.Thread(target=f) for f in (turn_worker, hb_worker, save_worker)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)

    reference = _state(stability=0.7, trust=0.5, curiosity=0.5, vigilance=0.1)
    sequential = _engine(reference, Clock(), root / "ref", decay_min_interval_sec=0)
    for _ in range(turns):
        sequential.post_turn("joy", 0.01)
    return state, reference, persistence


def test_concurrent_heartbeat_turns_and_saves_lose_no_update(tmp_path, fast_switching):
    """Measured before #147: 20/20 runs lost stimulus updates to a concurrent tick."""
    for run in range(3):
        state, reference, persistence = _race(tmp_path / str(run))
        assert _values(state) == _values(reference), f"run {run} lost updates"
        persistence.save()
        saved = json.loads(persistence.path.read_text(encoding="utf-8"))
        assert {dim: saved[dim] for dim in DIMS} == _values(state)
