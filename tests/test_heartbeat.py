from __future__ import annotations

import asyncio
import threading

import pytest

import stiracore.heartbeat.gateway as gateway_module
from stiracore.config import GatewayConfig
from stiracore.core import (
    BreakAction,
    BreakLevel,
    BreakResult,
    NeuralState,
    StiraConfig,
    StiraEngine,
)
from stiracore.heartbeat import HeartbeatGateway, compute_interval


def test_compute_interval_scales_with_stability() -> None:
    floor = 300.0
    ceil = 10_800.0

    stable = compute_interval(0.9, floor, ceil)
    unstable = compute_interval(0.1, floor, ceil)

    assert stable == pytest.approx(8_748.0)
    assert unstable == floor
    assert stable > unstable


@pytest.mark.parametrize(
    ("stability", "floor", "ceil", "expected"),
    [
        (0.0, 300.0, 10_800.0, 300.0),
        (1.0, 300.0, 10_800.0, 10_800.0),
        (-1.0, 300.0, 10_800.0, 300.0),
        (2.0, 300.0, 10_800.0, 10_800.0),
        (0.5, 1_000.0, 100.0, 250.0),
    ],
)
def test_compute_interval_clamps_inputs_and_bounds(
    stability: float,
    floor: float,
    ceil: float,
    expected: float,
) -> None:
    assert compute_interval(stability, floor, ceil) == expected


def test_tick_actively_applies_decay_with_injected_time() -> None:
    now = [1_000.0]
    state = NeuralState(
        trust=0.8,
        stability=0.9,
        last_decay_check=now[0],
        last_stimulus_time=now[0],
    )
    engine = StiraEngine(state=state)
    gateway = HeartbeatGateway(
        engine,
        GatewayConfig(),
        now_fn=lambda: now[0],
    )

    assert gateway.tick()["decay_deltas"] == {}

    now[0] += 3_600.0
    result = gateway.tick()

    assert result["decay_deltas"]["trust"] == pytest.approx(-0.001)
    assert result["decay_deltas"]["stability"] == pytest.approx(-0.005)
    assert state.last_decay_check == now[0]


def test_tick_sends_detached_snapshot_to_sink() -> None:
    received: list[dict[str, object]] = []
    state = NeuralState(last_decay_check=2_000.0)
    gateway = HeartbeatGateway(
        StiraEngine(state=state),
        GatewayConfig(),
        now_fn=lambda: 2_000.0,
        snapshot_sink=received.append,
    )

    result = gateway.tick()

    assert received == [result["snapshot"]]
    assert received[0]["turn"] == 1
    assert received[0]["timestamp"] == 2_000.0
    assert received[0]["stability"] == state.stability
    assert received[0]["bc_score"] == state.bc_score
    assert set(state.snapshot()).issubset(received[0])


@pytest.mark.parametrize(
    ("stability", "bc_score", "expected_level", "expected_priority"),
    [
        (0.8, 0.9, "normal", None),
        (0.4, 0.9, "warning", None),
        (0.8, 0.5, "warning", None),
        (0.2, 0.9, "critical", "P0"),
    ],
)
def test_tick_classifies_level_and_priority(
    stability: float,
    bc_score: float,
    expected_level: str,
    expected_priority: str | None,
) -> None:
    state = NeuralState(stability=stability, last_decay_check=1_000.0)
    state.bc_score = bc_score
    gateway = HeartbeatGateway(
        StiraEngine(state=state, config=StiraConfig()),
        GatewayConfig(),
        now_fn=lambda: 1_000.0,
    )

    result = gateway.tick()

    assert result["level"] == expected_level
    assert result["priority"] == expected_priority


def test_tick_has_no_model_or_agent_dependency() -> None:
    class ZeroModelEngine:
        def __init__(self) -> None:
            self.state = NeuralState(last_decay_check=1_000.0)
            self.config = StiraConfig()
            # The shared turn/heartbeat state lock is part of the contract (#147).
            self.state_lock = threading.RLock()

        def breaker_status(self):
            return BreakResult(BreakLevel.NORMAL, BreakAction.NONE)

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"Heartbeat accessed unexpected engine API: {name}")

    result = HeartbeatGateway(
        ZeroModelEngine(),
        GatewayConfig(),
        now_fn=lambda: 1_000.0,
    ).tick()

    assert result["level"] == "normal"


def test_run_forever_ticks_at_computed_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    stop_event = asyncio.Event()
    wait_timeouts: list[float] = []
    state = NeuralState(stability=0.5, last_decay_check=1_000.0)
    gateway = HeartbeatGateway(
        StiraEngine(state=state),
        GatewayConfig(hb_interval_floor_sec=100.0, hb_interval_ceil_sec=1_000.0),
        now_fn=lambda: 1_000.0,
    )

    async def fake_wait_for(awaitable: object, timeout: float) -> None:
        # Close the coroutine we are handed so it is not left un-awaited.
        if hasattr(awaitable, "close"):
            awaitable.close()
        wait_timeouts.append(timeout)
        if len(wait_timeouts) == 2:
            stop_event.set()
            return
        raise TimeoutError

    monkeypatch.setattr(gateway_module.asyncio, "wait_for", fake_wait_for)

    asyncio.run(gateway.run_forever(stop_event))

    assert wait_timeouts == [250.0, 250.0]
    assert len(gateway.snapshots) == 2


def test_run_forever_stops_promptly_when_event_set_during_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop set during the (possibly multi-hour) wait must not block."""
    stop_event = asyncio.Event()
    state = NeuralState(stability=0.9, last_decay_check=1_000.0)
    gateway = HeartbeatGateway(
        StiraEngine(state=state),
        GatewayConfig(),  # ceil defaults to 10800s; a blocking sleep would hang
        now_fn=lambda: 1_000.0,
    )

    async def stop_during_wait(awaitable: object, timeout: float) -> None:
        if hasattr(awaitable, "close"):
            awaitable.close()
        stop_event.set()  # event set mid-wait -> loop must exit next check

    monkeypatch.setattr(gateway_module.asyncio, "wait_for", stop_during_wait)

    asyncio.run(gateway.run_forever(stop_event))

    # Exactly one tick, then the interrupted wait returns and the loop exits.
    assert len(gateway.snapshots) == 1


def test_default_snapshot_sink_is_bounded() -> None:
    state = NeuralState(stability=0.9, last_decay_check=1_000.0)
    clock = {"t": 1_000.0}

    def now_fn() -> float:
        clock["t"] += 3600.0
        return clock["t"]

    gateway = HeartbeatGateway(
        StiraEngine(state=state),
        GatewayConfig(),
        now_fn=now_fn,
        snapshot_limit=3,
    )
    for _ in range(10):
        gateway.tick()

    # Bounded to the most recent 3 snapshots, not all 10.
    assert len(gateway.snapshots) == 3
    assert [s["turn"] for s in gateway.snapshots] == [8, 9, 10]


@pytest.mark.parametrize("interval,elapsed,decayed", [(7200.0, 3600.0, False), (10.0, 50.0, True)])
def test_tick_honors_configured_decay_interval(interval, elapsed, decayed):
    state = NeuralState(stability=0.9, last_decay_check=1000.0, last_stimulus_time=1000.0)
    engine = StiraEngine(state=state, config=StiraConfig(decay_min_interval_sec=interval))
    result = HeartbeatGateway(engine, GatewayConfig(), now_fn=lambda: 1000.0 + elapsed).tick()
    assert bool(result["decay_deltas"]) is decayed
    assert state.last_decay_check == (1000.0 + elapsed if decayed else 1000.0)


def test_tick_preserves_critical_cooldown_without_advancing_turns():
    state = NeuralState(stability=0.2, last_decay_check=1000.0)
    engine = StiraEngine(state=state, now_fn=lambda: 1000.0)
    assert engine.pre_turn().level == BreakLevel.CRITICAL
    engine.post_turn()
    state.stability = 0.8
    gateway = HeartbeatGateway(engine, GatewayConfig(), now_fn=lambda: 1000.0)
    for _ in range(10):
        result = gateway.tick()
        assert (result["level"], result["priority"]) == ("critical", "P0")
    assert engine._critical_turns == 1
    assert engine._turn_count == 1


def test_tick_preserves_warning_hysteresis_and_active_turn_snapshot():
    state = NeuralState(stability=0.4, last_decay_check=1000.0)
    engine = StiraEngine(state=state, now_fn=lambda: 1000.0)
    engine.pre_turn()
    engine.post_turn()
    state.stability = 0.55
    gateway = HeartbeatGateway(engine, GatewayConfig(), now_fn=lambda: 1000.0)
    assert gateway.tick()["level"] == "warning"
    engine.pre_turn()
    state.stability = 0.9
    assert gateway.tick()["level"] == "warning"
    engine.post_turn()
    assert gateway.tick()["level"] == "normal"
