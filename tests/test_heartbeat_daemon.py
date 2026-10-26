"""#238: the opt-in background heartbeat loop (zero-model tick only).

Every scheduling test drives a fake clock through ``poll()``; the thread tests
use the real loop with tiny intervals. Expected values were measured first.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from stiracore.agent.state_persistence import StatePersistence
from stiracore.config import GatewayConfig
from stiracore.core import FileCheckpointStore, NeuralState, StiraConfig, StiraEngine
from stiracore.core.neural_state import BASELINES, DECAY_RATES
from stiracore.heartbeat import (
    DaemonState,
    HeartbeatDaemon,
    HeartbeatGateway,
    HeartbeatLockError,
    HeartbeatOwnerLock,
)
from stiracore.observability.config_api import ConfigStore

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_000_000.0
DIMS = tuple(DECAY_RATES)


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _engine(state, clock, root, **config) -> StiraEngine:
    return StiraEngine(
        state=state,
        config=StiraConfig(**config),
        now_fn=clock,
        checkpoint_store=FileCheckpointStore(root),
    )


def _daemon(tmp_path, *, stability=0.5, gateway_config=None, **kwargs):
    clock = Clock()
    state = NeuralState(stability=stability, last_decay_check=T0, last_stimulus_time=T0)
    engine = _engine(state, clock, tmp_path / "cp")
    gateway = HeartbeatGateway(
        engine,
        gateway_config or GatewayConfig(hb_interval_floor_sec=100.0, hb_interval_ceil_sec=1000.0),
        now_fn=clock,
    )
    daemon = HeartbeatDaemon(gateway, state_dir=tmp_path / "state", now_fn=clock, **kwargs)
    return daemon, clock, state, engine


# --- Opt-in, default off ----------------------------------------------------


def test_gateway_config_heartbeat_is_off_by_default_and_round_trips(tmp_path):
    assert GatewayConfig().hb_enabled is False
    path = tmp_path / "gw.json"
    GatewayConfig(path=path, hb_enabled=True).save()
    assert json.loads(path.read_text())["hb_enabled"] is True
    assert GatewayConfig.load(path).hb_enabled is True
    with pytest.raises(ValueError, match="hb_enabled"):
        GatewayConfig(hb_enabled=1)


@pytest.mark.parametrize(
    ("env", "configured", "expected"),
    [
        (None, False, False),
        (None, True, True),
        ("1", False, True),
        ("on", False, True),
        ("0", True, False),
        ("", True, True),  # empty means "not set"
    ],
)
def test_env_override_for_opt_in(monkeypatch, env, configured, expected):
    from dashboard import stira_server

    if env is None:
        monkeypatch.delenv("STIRA_HEARTBEAT", raising=False)
    else:
        monkeypatch.setenv("STIRA_HEARTBEAT", env)
    assert stira_server._heartbeat_enabled(GatewayConfig(hb_enabled=configured)) is expected


# --- Fake-clock scheduling --------------------------------------------------


def test_ticks_on_interval_stops_and_resumes_after_restart(tmp_path):
    daemon, clock, _, _ = _daemon(tmp_path, stability=0.5)
    # compute_interval(0.5, 100, 1000) = 250 s
    daemon.start(background=False)
    assert daemon.poll() is True  # first tick is immediate
    assert daemon.status()["next_tick_at"] == T0 + 250.0

    clock.now = T0 + 249.0
    assert daemon.poll() is False
    clock.now = T0 + 250.0
    assert daemon.poll() is True
    assert daemon.status()["tick_count"] == 2

    daemon.stop()
    clock.now = T0 + 10_000.0
    assert daemon.poll() is False
    assert daemon.status()["tick_count"] == 2
    assert daemon.status()["running"] is False
    assert daemon.status()["next_tick_at"] is None

    daemon.start(background=False)  # restart: resumes with an immediate tick
    assert daemon.poll() is True
    assert daemon.status()["tick_count"] == 3
    assert daemon.status()["last_tick_at"] == T0 + 10_000.0
    daemon.stop()


def test_clock_moving_back_rebases_schedule_without_decay_jump(tmp_path):
    daemon, clock, state, _ = _daemon(tmp_path, stability=0.3)
    # compute_interval(0.3, 100, 1000) = 100 s (floor)
    daemon.start(background=False)
    daemon.poll()
    before = {dim: getattr(state, dim) for dim in DIMS}

    clock.now = T0 - 3600.0  # NTP step / restored skewed host
    assert daemon.poll() is False  # re-based once, not stalled until T0 + 100
    assert daemon.status()["next_tick_at"] == T0 - 3600.0 + 100.0
    assert daemon.status()["clock_resyncs"] == 1
    clock.now = T0 - 3500.0
    assert daemon.poll() is True
    assert {dim: getattr(state, dim) for dim in DIMS} == before  # resync, no decay
    assert state.last_decay_check == T0 - 3500.0
    assert daemon.status()["clock_resyncs"] == 1  # the same regression only once

    # Decay restarts from the resynchronized time only: 1 h of recovery.
    clock.now = T0 + 100.0
    daemon.poll()
    assert state.stability == pytest.approx(0.3 + DECAY_RATES["stability"] * 0.5)
    daemon.stop()


def test_clock_moving_back_keeps_failure_backoff(tmp_path):
    """Before the fix: a failing resync tick retried on every poll (no backoff)."""
    daemon, clock, _, _ = _daemon(tmp_path, failure_backoff_sec=60.0, max_backoff_sec=600.0)
    calls = []

    def failing_save(_result):
        calls.append(clock.now)
        if len(calls) > 1:
            raise OSError("disk full")

    daemon._on_tick = failing_save
    daemon.start(background=False)
    assert daemon.poll() is True  # success at T0
    clock.now = T0 - 3600.0
    assert daemon.poll() is False  # re-based: the pending 250 s now count from here
    anchor = clock.now
    for expected_delay in (250.0, 60.0, 120.0, 240.0):
        due = daemon.status()["next_tick_at"]
        assert due - anchor == expected_delay
        for offset in (1.0, expected_delay - 1.0):  # nothing before the due time
            clock.now = anchor + offset
            assert daemon.poll() is False
        clock.now = anchor = due
        assert daemon.poll() is True  # the resync tick, then failures with backoff
    assert len(calls) == 5
    assert daemon.status()["consecutive_failures"] == 4
    assert daemon.status()["clock_resyncs"] == 1
    daemon.stop()


def test_clock_moving_back_mid_wait_keeps_only_the_remaining_wait(tmp_path):
    """due=1100, last reading=1090, clock back to 1080 -> due=1090 (not 1180)."""
    daemon, clock, _, _ = _daemon(
        tmp_path,
        gateway_config=GatewayConfig(hb_interval_floor_sec=100.0, hb_interval_ceil_sec=100.0),
    )
    clock.now = 1000.0
    daemon.start(background=False)
    assert daemon.poll() is True
    assert daemon.status()["next_tick_at"] == 1100.0
    clock.now = 1090.0
    assert daemon.poll() is False
    clock.now = 1080.0
    assert daemon.poll() is False
    assert daemon.status()["next_tick_at"] == 1090.0
    clock.now = 1089.0
    assert daemon.poll() is False
    clock.now = 1090.0
    assert daemon.poll() is True
    daemon.stop()


def test_clock_moving_back_mid_backoff_keeps_only_the_remaining_backoff(tmp_path):
    clock = Clock(1000.0)
    daemon = HeartbeatDaemon(
        FlakyGateway(failures=10),
        state_dir=tmp_path,
        now_fn=clock,
        failure_backoff_sec=60.0,
        max_backoff_sec=600.0,
    )
    daemon.start(background=False)
    assert daemon.poll() is True  # failure -> due 1060
    clock.now = 1050.0
    assert daemon.poll() is False
    clock.now = 1040.0  # back 10 s with 10 s of backoff left
    assert daemon.poll() is False
    assert daemon.status()["next_tick_at"] == 1050.0
    clock.now = 1049.0
    assert daemon.poll() is False
    clock.now = 1050.0
    assert daemon.poll() is True  # 2nd failure -> 120 s backoff from here
    assert daemon.status()["next_tick_at"] == 1170.0
    daemon.stop()


@pytest.mark.parametrize("stability", [0.0, 0.3, 0.9, 1.0])
def test_long_sleep_is_one_tick_clamped_to_baseline(tmp_path, stability):
    daemon, clock, state, _ = _daemon(tmp_path, stability=stability)
    daemon.start(background=False)
    daemon.poll()
    clock.now = T0 + 10_000 * 3600.0  # ~14 months asleep
    assert daemon.poll() is True
    assert daemon.status()["tick_count"] == 2  # no burst of catch-up ticks
    assert state.stability == BASELINES["stability"]
    assert all(0.0 <= getattr(state, dim) <= 1.0 for dim in DIMS)
    daemon.stop()


# --- Owner lock -------------------------------------------------------------


def test_second_daemon_on_same_state_dir_is_refused(tmp_path):
    first, _, _, _ = _daemon(tmp_path)
    second, _, _, _ = _daemon(tmp_path)
    first.start(background=False)
    with pytest.raises(HeartbeatLockError):
        second.start(background=False)
    assert second.running is False
    with pytest.raises(RuntimeError, match="is running"):
        first.start(background=False)

    first.stop()
    second.start(background=False)  # released on stop
    assert second.running is True
    second.stop()


def test_owner_lock_is_refused_across_processes(tmp_path):
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; sys.path.insert(0, sys.argv[1]);"
                "from stiracore.heartbeat import HeartbeatOwnerLock as L;"
                "lock = L(sys.argv[2]); lock.acquire(); print('held', flush=True); time.sleep(30)"
            ),
            str(ROOT / "src"),
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(HeartbeatLockError):
            HeartbeatOwnerLock(tmp_path).acquire()
    finally:
        holder.kill()
        holder.wait(10)
        holder.stdout.close()
    # The kernel released the dead owner's lock: no stale lock file problem.
    lock = HeartbeatOwnerLock(tmp_path)
    lock.acquire()
    lock.release()


# --- Failures: never stop silently ------------------------------------------


class FlakyGateway:
    def __init__(self, failures: int) -> None:
        self.gateway_config = GatewayConfig(hb_interval_floor_sec=10.0, hb_interval_ceil_sec=10.0)
        self.failures = failures
        self.calls = 0

    def tick(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("secret detail must not leak")
        return {"level": "normal", "priority": None, "next_interval_sec": 10.0}


def test_tick_failures_back_off_warn_and_recover(tmp_path, caplog):
    clock = Clock()
    gateway = FlakyGateway(failures=4)
    daemon = HeartbeatDaemon(
        gateway,
        state_dir=tmp_path,
        now_fn=clock,
        failure_backoff_sec=60.0,
        max_backoff_sec=300.0,
    )
    daemon.start(background=False)
    delays = []
    with caplog.at_level(logging.INFO, logger="stiracore.heartbeat.daemon"):
        for _ in range(4):
            assert daemon.poll() is True
            status = daemon.status()
            delays.append(status["next_tick_at"] - clock.now)
            clock.now = status["next_tick_at"]
    assert delays == [60.0, 120.0, 240.0, 300.0]  # exponential, capped
    status = daemon.status()
    assert (status["consecutive_failures"], status["degraded"]) == (4, True)
    assert status["last_error"] == "RuntimeError"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # from the 3rd consecutive failure on
    assert "secret detail" not in caplog.text

    assert daemon.poll() is True  # the 5th attempt succeeds
    status = daemon.status()
    assert (status["consecutive_failures"], status["degraded"], status["last_error"]) == (
        0,
        False,
        None,
    )
    assert status["tick_count"] == 1
    daemon.stop()


def test_background_loop_survives_exceptions_and_stops_promptly(tmp_path):
    gateway = FlakyGateway(failures=3)
    gateway.gateway_config = GatewayConfig(hb_interval_floor_sec=0.0, hb_interval_ceil_sec=0.0)
    original = gateway.tick

    def tick():
        result = original()
        return {**result, "next_interval_sec": 0.01}

    gateway.tick = tick
    daemon = HeartbeatDaemon(
        gateway, state_dir=tmp_path, failure_backoff_sec=0.01, min_wait_sec=0.0
    )
    daemon.start()
    deadline = time.monotonic() + 5
    while daemon.status()["tick_count"] < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert daemon.status()["tick_count"] >= 3
    assert gateway.calls >= 6  # 3 failures, then successful ticks

    long_wait = HeartbeatDaemon(FlakyGateway(0), state_dir=tmp_path / "b")
    long_wait.start()  # next tick in 10 s
    started = time.monotonic()
    long_wait.stop()
    daemon.stop()
    assert time.monotonic() - started < 2.0
    assert not daemon.running and not long_wait.running


# --- Persistence: restart recovery ------------------------------------------


def test_process_restart_resumes_decay_from_saved_state(tmp_path):
    clock = Clock()
    path = tmp_path / "neural-state.json"

    def build():
        state = NeuralState(stability=0.3, last_decay_check=T0, last_stimulus_time=T0)
        persistence = StatePersistence(state, path)
        persistence.load()
        engine = _engine(state, clock, tmp_path / "cp")
        persistence.state_lock = engine.state_lock
        gateway = HeartbeatGateway(engine, GatewayConfig(), now_fn=clock)
        daemon = HeartbeatDaemon(
            gateway,
            state_dir=tmp_path,
            now_fn=clock,
            on_tick=lambda _r: persistence.save(),
        )
        return state, daemon

    state, daemon = build()
    daemon.start(background=False)
    clock.now = T0 + 3600.0
    daemon.poll()
    after_first = state.stability
    daemon.stop()  # process exit

    clock.now = T0 + 7200.0
    restored, daemon = build()
    assert restored.last_decay_check == T0 + 3600.0
    daemon.start(background=False)
    daemon.poll()
    # Exactly the 1 h since the persisted tick: no double decay, nothing skipped.
    assert restored.stability == pytest.approx(after_first + DECAY_RATES["stability"] * 0.5)
    daemon.stop()


# --- Concurrency with turns (#147 extended to the loop) ----------------------


@pytest.fixture
def fast_switching():
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(previous)


def test_tick_holds_the_shared_lock_briefly(tmp_path):
    daemon, clock, _, _ = _daemon(tmp_path)
    daemon.start(background=False)
    for step in range(20):
        clock.now = T0 + step * 1000.0
        daemon.poll()
    # Measured: median 0.003 ms, max 0.042 ms over 200 ticks (M1 Max); loose CI bound.
    assert daemon.status()["last_duration_ms"] < 50.0
    daemon.stop()


# --- chat server wiring: opt-in, read-only --------------------------------------


@pytest.fixture
def stira(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    data_dir = tmp_path / "data"
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    created = []

    def make(env=None, **kwargs):
        if env is None:
            monkeypatch.delenv("STIRA_HEARTBEAT", raising=False)
        else:
            monkeypatch.setenv("STIRA_HEARTBEAT", env)
        store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
        app = stira_server.create_app(config_store=store, **kwargs)
        created.append(app)
        return app, data_dir

    yield make
    for app in created:
        if app.extensions.get("heartbeat") is not None:
            app.extensions["heartbeat"].stop()


def test_stira_heartbeat_disabled_by_default_changes_nothing(stira):
    app, data_dir = stira()
    assert app.extensions["heartbeat"] is None
    assert not (data_dir / "heartbeat.lock").exists()
    assert not any(t.name == "stira-heartbeat" for t in threading.enumerate())
    client = app.test_client()
    assert client.get("/api/gateway/status").get_json()["heartbeat"] == {
        "enabled": False,
        "running": False,
    }
    assert client.get("/api/state/history").get_json()["heartbeat"]["enabled"] is False


def test_stira_heartbeat_opt_in_runs_and_is_read_only(stira):
    app, data_dir = stira(env="1")
    daemon = app.extensions["heartbeat"]
    assert daemon is not None and daemon.running
    assert (data_dir / "heartbeat.lock").exists()
    deadline = time.monotonic() + 5
    while daemon.status()["tick_count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    client = app.test_client()
    status = client.get("/api/gateway/status").get_json()["heartbeat"]
    assert (status["enabled"], status["running"], status["tick_count"]) == (True, True, 1)
    assert status["last_level"] == "normal"
    assert status["next_tick_at"] > status["last_tick_at"]
    assert client.get("/api/state/history").get_json()["heartbeat"]["tick_count"] == 1
    # The tick persisted state (restart recovery) and fed the bounded trajectory.
    assert (data_dir / "neural-state.json").exists()
    assert len(client.get("/api/state/history").get_json()["history"]) >= 2

    # No route can start, stop or trigger it (operations belong to #239).
    rules = [rule for rule in app.url_map.iter_rules() if "heartbeat" in rule.rule]
    assert rules == []
    assert all(not callable(value) for value in status.values())


def test_stira_second_server_on_same_state_dir_is_refused(stira):
    from stiracore.config.data_dir_lock import DataDirLockError

    first, _ = stira(env="1")
    with pytest.raises(DataDirLockError) as refused:
        stira(env="1")  # the data-directory owner lock (#285) refuses before the heartbeat
    assert refused.value.owner_pid == os.getpid()
    assert first.extensions["heartbeat"].running


def test_stira_heartbeat_lock_refusal_shows_the_same_in_banner_and_status(stira, capsys):
    # Only reachable in tests now (#285): a second app in the same process on purpose.
    first, _ = stira(env="1")
    capsys.readouterr()
    second, _ = stira(env="1", _tests_share_data_dir=True)
    assert first.extensions["heartbeat"].running
    assert second.extensions["heartbeat"] is None
    status = second.test_client().get("/api/gateway/status").get_json()["heartbeat"]
    assert status == {"enabled": True, "running": False, "last_error": "HeartbeatLockError"}
    banner = [line for line in capsys.readouterr().out.splitlines() if "Heartbeat:" in line]
    assert banner[-1].strip() == "Heartbeat: enabled, not running (HeartbeatLockError)"


# --- Breaker interaction with active decay (documented in heartbeat-daemon.md)


def test_active_decay_releases_warning_but_never_serves_the_critical_cooldown(tmp_path):
    clock = Clock()
    state = NeuralState(stability=0.45, last_decay_check=T0)
    engine = _engine(state, clock, tmp_path / "w")
    assert engine.pre_turn().level.value == "warning"
    engine.post_turn()
    daemon = HeartbeatDaemon(
        HeartbeatGateway(engine, GatewayConfig(), now_fn=clock),
        state_dir=tmp_path / "w",
        now_fn=clock,
    )
    daemon.start(background=False)
    clock.now = T0 + 59 * 3600.0  # 0.45 + 0.0025 * 59 = 0.5975 <= exit 0.6
    daemon.poll()
    assert daemon.status()["last_level"] == "warning"
    clock.now = T0 + 61 * 3600.0
    daemon.poll()
    assert daemon.status()["last_level"] == "normal"  # released by time alone
    daemon.stop()

    clock = Clock()
    state = NeuralState(stability=0.2, last_decay_check=T0)
    engine = _engine(state, clock, tmp_path / "c")
    assert engine.pre_turn().level.value == "critical"
    engine.post_turn()
    daemon = HeartbeatDaemon(
        HeartbeatGateway(engine, GatewayConfig(), now_fn=clock),
        state_dir=tmp_path / "c",
        now_fn=clock,
    )
    daemon.start(background=False)
    clock.now = T0 + 1000 * 3600.0
    daemon.poll()
    assert state.stability == BASELINES["stability"]
    assert daemon.status()["last_level"] == "critical"  # unserved cooldown: ticks don't count
    assert engine._critical_turns == 1
    daemon.stop()


def test_active_decay_lifts_critical_once_the_cooldown_was_served_by_turns(tmp_path):
    """Measured: 3 protected turns at 0.2, then 0.45 @100 h CRITICAL, 0.475 @110 h
    WARNING, 0.7 @200 h NORMAL - all from ticks, without a further turn."""
    clock = Clock()
    state = NeuralState(stability=0.2, last_decay_check=T0)
    engine = _engine(state, clock, tmp_path / "c")
    for _ in range(engine.config.critical_cooldown_turns):
        assert engine.pre_turn().level.value == "critical"
        engine.post_turn()
    assert engine._critical_turns == 3  # served; only the stability exit remains
    daemon = HeartbeatDaemon(
        HeartbeatGateway(engine, GatewayConfig(), now_fn=clock),
        state_dir=tmp_path / "c",
        now_fn=clock,
    )
    daemon.start(background=False)
    seen = {}
    for hours in (100, 110, 200):
        clock.now = T0 + hours * 3600.0
        daemon.poll()
        seen[hours] = (round(state.stability, 4), daemon.status()["last_level"])
    assert seen == {
        100: (0.45, "critical"),  # exit needs stability > 0.45
        110: (0.475, "warning"),  # CRITICAL lifted by time alone; WARNING latch remains
        200: (0.7, "normal"),
    }
    daemon.stop()


# --- Review fixes: bounded backoff, loop crash, stop contract, lock faults ----


def test_backoff_is_bounded_and_survives_1100_consecutive_failures(tmp_path):
    """Before the fix: 2 ** 1024 overflowed float at the 1025th failure."""
    clock = Clock()
    daemon = HeartbeatDaemon(
        FlakyGateway(failures=10**6),
        state_dir=tmp_path,
        now_fn=clock,
        failure_backoff_sec=60.0,
        max_backoff_sec=3600.0,
    )
    daemon.start(background=False)
    for _ in range(1100):
        assert daemon.poll() is True
        delay = daemon.status()["next_tick_at"] - clock.now
        assert 60.0 <= delay <= 3600.0
        clock.now += delay
    assert daemon.status()["consecutive_failures"] == 1100
    assert daemon.running
    assert daemon.backoff_delay(10**9) == 3600.0
    # Effective cap is max(failure_backoff_sec, cap): a cap below the base keeps the base.
    small = HeartbeatDaemon(
        FlakyGateway(0), state_dir=tmp_path / "s", failure_backoff_sec=60.0, max_backoff_sec=10.0
    )
    assert small.backoff_delay(5) == 60.0
    daemon.stop()


def test_unexpected_loop_error_releases_running_flag_and_lock(tmp_path):
    calls = {"n": 0}

    def exploding_clock():
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("clock source failed")
        return T0

    daemon = HeartbeatDaemon(
        FlakyGateway(0),
        state_dir=tmp_path,
        now_fn=exploding_clock,
        min_wait_sec=0.0,
        max_wait_sec=0.01,
    )
    daemon.start()
    deadline = time.monotonic() + 5
    while daemon.running and time.monotonic() < deadline:
        time.sleep(0.01)
    status = daemon.status()
    assert (status["running"], status["last_error"]) == (False, "loop:RuntimeError")
    other = HeartbeatOwnerLock(tmp_path)
    other.acquire()  # the crashed worker did not leak the lock
    other.release()
    assert daemon.stop() is True


def test_stop_times_out_without_releasing_lock_then_completes(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def blocking_save(_result):
        entered.set()
        release.wait(10)

    daemon, _, _, _ = _daemon(tmp_path, on_tick=blocking_save)
    daemon.start()
    assert entered.wait(5)

    started = time.monotonic()
    assert daemon.stop(timeout=0.2) is False  # bounded, reports "not yet"
    assert time.monotonic() - started < 2.0
    status = daemon.status()
    assert (status["state"], status["stopping"], status["running"], status["next_tick_at"]) == (
        "stopping",
        True,
        False,
        None,
    )
    with pytest.raises(HeartbeatLockError):  # the old worker may still write state
        HeartbeatOwnerLock(tmp_path / "state").acquire()
    with pytest.raises(RuntimeError, match="is stopping"):
        daemon.start()

    release.set()
    assert daemon.stop(timeout=5) is True
    status = daemon.status()
    assert (status["stopping"], status["running"]) == (False, False)
    assert status["tick_count"] == 1  # no tick started after the stop request
    daemon._on_tick = None
    daemon.start()  # restart after completion
    assert daemon.running
    assert daemon.stop() is True


def test_poll_driven_stop_completes_when_the_in_flight_poll_ends(tmp_path):
    """Before the fix this needed a second stop(); now the poll finishes the stop."""
    entered, release = threading.Event(), threading.Event()

    def blocking_save(_result):
        entered.set()
        release.wait(10)

    daemon, _, _, _ = _daemon(tmp_path, on_tick=blocking_save)
    daemon.start(background=False)
    poller = threading.Thread(target=daemon.poll)
    poller.start()
    assert entered.wait(5)
    assert daemon.stop(timeout=0.2) is False
    assert daemon.state is DaemonState.STOPPING
    with pytest.raises(HeartbeatLockError):  # the in-flight poll may still write
        HeartbeatOwnerLock(tmp_path / "state").acquire()
    release.set()
    poller.join(5)
    # No second stop(): the finishing poll completed the stop and released the lock.
    assert daemon.state is DaemonState.STOPPED
    assert daemon.status()["state"] == "stopped"
    other = HeartbeatOwnerLock(tmp_path / "state")
    other.acquire()
    other.release()
    assert daemon.poll() is False


def test_external_poll_is_refused_while_the_background_worker_runs(tmp_path):
    daemon, _, _, _ = _daemon(tmp_path)
    daemon.start()
    with pytest.raises(RuntimeError, match="background worker"):
        daemon.poll()
    assert daemon.stop() is True
    assert daemon.poll() is False  # stopped: a no-op, not an error


@pytest.mark.parametrize("failure", ["clock", "thread_init", "thread_start"])
def test_start_failure_rolls_back_and_another_owner_can_acquire(tmp_path, monkeypatch, failure):
    import stiracore.heartbeat.daemon as daemon_module

    def boom(*_args, **_kwargs):
        raise RuntimeError(failure)

    daemon, _, _, _ = _daemon(tmp_path)
    if failure == "clock":
        daemon._now = boom
    elif failure == "thread_init":
        monkeypatch.setattr(daemon_module.threading, "Thread", boom)
    else:
        monkeypatch.setattr(daemon_module.threading.Thread, "start", boom)
    with pytest.raises(RuntimeError, match=failure):
        daemon.start()
    monkeypatch.undo()
    assert daemon.state is DaemonState.STOPPED
    other = HeartbeatOwnerLock(tmp_path / "state")
    other.acquire()  # rolled back: the lock was released at once
    other.release()
    daemon._now = Clock()
    daemon.start(background=False)  # and the daemon itself can start again
    assert daemon.stop() is True


def test_concurrent_stop_during_start_waits_for_the_transaction(tmp_path):
    """stop() during STARTING neither joins an unstarted thread nor gets lost."""
    in_start, proceed = threading.Event(), threading.Event()
    clock = Clock()

    def slow_first_reading():
        in_start.set()
        proceed.wait(5)
        return clock()

    daemon, _, _, _ = _daemon(tmp_path)
    daemon._now = slow_first_reading
    starter = threading.Thread(target=daemon.start)
    starter.start()
    assert in_start.wait(5)
    result = {}
    stopper = threading.Thread(target=lambda: result.update(ok=daemon.stop(timeout=5)))
    stopper.start()
    time.sleep(0.1)
    assert stopper.is_alive()  # waiting on the lifecycle lock, bounded by its deadline
    daemon._now = clock
    proceed.set()
    starter.join(5)
    stopper.join(5)
    assert result == {"ok": True}  # the stop was applied after the start, not lost
    assert daemon.state is DaemonState.STOPPED
    HeartbeatOwnerLock(tmp_path / "state").acquire()


def test_stop_never_exceeds_its_deadline_on_a_busy_lifecycle_lock(tmp_path):
    in_start, proceed = threading.Event(), threading.Event()

    def stuck_clock():
        in_start.set()
        proceed.wait(5)
        return T0

    daemon, _, _, _ = _daemon(tmp_path)
    daemon._now = stuck_clock
    starter = threading.Thread(target=lambda: daemon.start(background=False))
    starter.start()
    assert in_start.wait(5)
    started = time.monotonic()
    assert daemon.stop(timeout=0.2) is False
    assert time.monotonic() - started < 1.0
    proceed.set()
    starter.join(5)
    assert daemon.stop(timeout=1) is True


# --- Hardening (#238 review): poll TOCTOU, start generations, kept stop requests ---


class _HookLock:
    """A plain lock whose acquire/release can run a test hook (lock left free)."""

    def __init__(self) -> None:
        self._inner = threading.Lock()
        self.on_release = None  # called once, after a release, by the releasing thread
        self.before_reacquire = None  # (thread, hook): before a blocking untimed acquire
        self.released_by = None  # (thread, event): set once that thread released the lock

    def acquire(self, blocking=True, timeout=-1):
        hook = self.before_reacquire
        if (
            hook is not None
            and blocking
            and timeout == -1
            and hook[0] is threading.current_thread()
        ):
            self.before_reacquire = None
            hook[1]()
        return self._inner.acquire(blocking, timeout)

    def release(self) -> None:
        self._inner.release()
        released = self.released_by
        if released is not None and released[0] is threading.current_thread():
            self.released_by = None
            released[1].set()
        hook, self.on_release = self.on_release, None
        if hook is not None:
            hook()

    def locked(self) -> bool:
        return self._inner.locked()

    __enter__ = acquire

    def __exit__(self, *_exc) -> None:
        self.release()


def _hooked(daemon) -> _HookLock:
    lock = _HookLock()
    daemon._lock = threading.Condition(lock)
    return lock


def test_external_poll_check_and_tick_reservation_are_one_critical_section(tmp_path):
    """TOCTOU: a restart in background mode between poll()'s check and its reservation."""
    ticks = []
    daemon, _, _, _ = _daemon(tmp_path)
    original_tick = daemon.gateway.tick

    def recording_tick():
        ticks.append((threading.current_thread(), daemon._background))
        return original_tick()

    daemon.gateway.tick = recording_tick
    worker_gate = threading.Event()
    original_run = daemon._run

    def gated_run():  # the new background worker waits until the test lets it go
        worker_gate.wait(5)
        original_run()

    daemon._run = gated_run
    daemon.start(background=False)
    lock = _hooked(daemon)

    def restart_in_background():
        try:
            daemon.stop(timeout=0)
            daemon.start(background=True)
        except RuntimeError:
            pass  # the poll had already reserved its tick: the stop is pending

    lock.on_release = restart_in_background
    try:
        daemon.poll()
    except RuntimeError:
        pass  # refused: the worker owns the ticks now
    finally:
        worker_gate.set()
    me = threading.current_thread()
    # No tick of the external caller ever ran while a background worker was the ticker.
    assert [bg for thread, bg in ticks if thread is me] in ([], [False])
    daemon.stop(timeout=5)
    assert daemon.state is DaemonState.STOPPED


class _CheckpointEvent(threading.Event):
    """A stop-request event whose next is_set() on ``thread`` runs ``hook`` after reading."""

    def __init__(self, thread, hook) -> None:
        super().__init__()
        self._hook = (thread, hook)

    def is_set(self) -> bool:
        value = super().is_set()
        hook = self._hook
        if hook is not None and hook[0] is threading.current_thread():
            self._hook = None
            hook[1]()  # between "no stop requested" and whatever the caller does next
        return value


def test_stop_request_and_tick_reservation_have_one_linearization_point(tmp_path):
    """A stop(0) that completes after poll() saw "no request" must not be followed by a tick.

    The barrier sits right after the poll's request check. Either stop() cannot complete
    there (the check and the reservation are one step), or no tick may run.
    """
    daemon, _, _, _ = _daemon(tmp_path)
    daemon.start(background=False)
    stop_done, result = threading.Event(), {}

    def stop_now():
        result["ok"] = daemon.stop(timeout=0)
        stop_done.set()

    def barrier():
        threading.Thread(target=stop_now).start()
        result["completed_at_barrier"] = stop_done.wait(0.5)

    daemon._generation.stop_requested = _CheckpointEvent(threading.current_thread(), barrier)
    ticked = daemon.poll()
    assert stop_done.wait(5)  # True or False: the reserved tick may already have ended
    assert "completed_at_barrier" in result  # the barrier was reached
    assert not (result["completed_at_barrier"] and ticked)  # no tick after a returned stop
    assert daemon.state is DaemonState.STOPPED  # the kept request still completed the stop
    other = HeartbeatOwnerLock(tmp_path / "state")
    other.acquire()
    other.release()


def test_stale_stop_never_finishes_a_restarted_generation(tmp_path):
    """A stop() of generation 1 that wakes up after a restart leaves generation 2 alone."""
    entered, release = threading.Event(), threading.Event()

    def blocking_save(_result):
        entered.set()
        release.wait(10)

    daemon, clock, _, _ = _daemon(tmp_path, on_tick=blocking_save)
    daemon.start(background=False)
    first = getattr(daemon, "generation", None)
    poller = threading.Thread(target=daemon.poll)
    poller.start()
    assert entered.wait(5)
    lock = _hooked(daemon)
    restarted = threading.Event()
    result = {}
    stopper = threading.Thread(target=lambda: result.update(ok=daemon.stop(timeout=5)))
    lock.before_reacquire = (stopper, lambda: restarted.wait(5))
    stopper_waiting = threading.Event()
    lock.released_by = (stopper, stopper_waiting)
    stopper.start()
    # The stopper took the lifecycle lock while generation 1 was ticking, and let it go
    # to wait for that tick: its target is generation 1.
    assert stopper_waiting.wait(5)
    release.set()
    poller.join(5)
    assert daemon.state is DaemonState.STOPPED  # the in-flight poll finished generation 1
    daemon._on_tick = None
    daemon.start(background=False)  # generation 2
    second = getattr(daemon, "generation", None)
    restarted.set()
    stopper.join(5)
    assert result == {"ok": True}  # generation 1 did stop
    assert daemon.state is DaemonState.RUNNING
    with pytest.raises(HeartbeatLockError):  # generation 2 still owns the state directory
        HeartbeatOwnerLock(tmp_path / "state").acquire()
    clock.now += 10_000.0
    assert daemon.poll() is True
    assert daemon.stop() is True
    assert second != first and second[0] == first[0]  # same process incarnation


def test_poll_driven_stop_returns_within_its_deadline_while_the_lock_is_busy(tmp_path):
    """Waiting for an in-flight poll never re-acquires the lifecycle lock past the deadline."""
    entered, release = threading.Event(), threading.Event()

    def blocking_save(_result):
        entered.set()
        release.wait(10)

    daemon, _, _, _ = _daemon(tmp_path, on_tick=blocking_save)
    daemon.start(background=False)
    poller = threading.Thread(target=daemon.poll)
    poller.start()
    assert entered.wait(5)
    result = {}

    def timed_stop():
        started = time.monotonic()
        result["ok"] = daemon.stop(timeout=0.3)
        result["elapsed"] = time.monotonic() - started

    lock = _hooked(daemon)
    stopper = threading.Thread(target=timed_stop)
    stopper_waiting = threading.Event()
    lock.released_by = (stopper, stopper_waiting)
    stopper.start()
    # Passed its lifecycle-lock section (the tick is in flight) and now waits for it.
    assert stopper_waiting.wait(5)
    with daemon._lock:  # another lifecycle-lock holder, past the stopper's deadline
        stopper.join(1.5)
    stopper.join(5)
    release.set()
    poller.join(5)
    assert result["ok"] is False and result["elapsed"] < 1.0
    assert daemon.state is DaemonState.STOPPED  # the finishing poll completed the stop


@pytest.mark.parametrize("background", [False, True])
def test_stop_request_on_a_busy_lifecycle_lock_is_kept_for_the_start(tmp_path, background):
    """stop() that times out while start() holds the lock is not lost (#285 shutdown)."""
    in_start, proceed = threading.Event(), threading.Event()
    clock = Clock()

    def stuck_clock():
        in_start.set()
        proceed.wait(5)
        return clock()

    daemon, _, _, _ = _daemon(tmp_path)
    daemon._now = stuck_clock
    starter = threading.Thread(target=lambda: daemon.start(background=background))
    starter.start()
    assert in_start.wait(5)
    assert daemon.stop(timeout=0.1) is False  # the lifecycle lock is busy
    daemon._now = clock
    proceed.set()
    starter.join(5)
    deadline = time.monotonic() + 5
    while daemon.state is not DaemonState.STOPPED and time.monotonic() < deadline:
        time.sleep(0.01)
    assert daemon.state is DaemonState.STOPPED  # the start consumed the kept request
    assert daemon.status()["tick_count"] == 0  # no tick after the stop request
    other = HeartbeatOwnerLock(tmp_path / "state")
    other.acquire()
    other.release()
    daemon.start(background=False)  # a new generation is not stopped by the old request
    assert daemon.running
    assert daemon.poll() is True
    assert daemon.stop() is True


def test_stop_request_on_a_busy_lock_while_running_is_kept_for_the_worker(tmp_path):
    """A background worker holding the lifecycle lock (slow clock) still sees the request."""
    gateway = FlakyGateway(failures=0)
    daemon = HeartbeatDaemon(gateway, state_dir=tmp_path, min_wait_sec=0.0, max_wait_sec=0.05)
    daemon.start()
    deadline = time.monotonic() + 5
    while daemon.status()["tick_count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    in_clock, unstick = threading.Event(), threading.Event()
    real_now = daemon._now

    def slow_clock():  # blocks only the worker's reading under the lifecycle lock
        if threading.current_thread() is daemon._thread and daemon._lock._lock.locked():
            in_clock.set()
            unstick.wait(5)
            return real_now() + 10_000.0  # a tick is due when the worker resumes
        return real_now()

    daemon._now = slow_clock
    assert in_clock.wait(5)
    assert daemon.stop(timeout=0.1) is False  # the lifecycle lock is busy
    daemon._now = real_now
    unstick.set()
    deadline = time.monotonic() + 5
    while daemon.state is not DaemonState.STOPPED and time.monotonic() < deadline:
        time.sleep(0.01)
    assert daemon.state is DaemonState.STOPPED  # no second stop() needed
    assert daemon.status()["tick_count"] == 1  # no tick after the stop request


def test_incarnation_differs_per_process_and_is_not_written_to_the_state_dir(tmp_path):
    """Fresh per interpreter; the generation's incarnation never lands in the state dir.

    "Not read back" holds by construction (a module-level CSPRNG draw, no loader); the
    on-disk check makes sure there is nothing to read back from the daemon's files.
    """
    from stiracore.heartbeat import daemon as daemon_module

    code = "from stiracore.heartbeat import daemon as d; print(d.INCARNATION)"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    seen = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(seen) == 2 and all(len(value) == 32 for value in seen)  # 128 bits, hex
    assert daemon_module.INCARNATION not in seen

    daemon, _, _, _ = _daemon(tmp_path)
    daemon.start(background=False)
    assert daemon.generation[0] == daemon_module.INCARNATION
    assert daemon.poll() is True
    assert daemon.stop() is True
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert daemon_module.INCARNATION.encode() not in path.read_bytes(), path.name


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork only")
def test_forked_child_draws_a_new_incarnation_and_the_parent_keeps_its_own(tmp_path):
    from stiracore.heartbeat import daemon as daemon_module

    parent_before = daemon_module.INCARNATION
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: report and leave without running pytest teardown
        try:
            os.write(write_end, daemon_module.INCARNATION.encode())
        finally:
            os._exit(0)
    os.close(write_end)
    with os.fdopen(read_end, "rb") as reader:
        child = reader.read().decode()
    os.waitpid(pid, 0)
    assert len(child) == 32 and child != parent_before
    assert daemon_module.INCARNATION == parent_before


@pytest.mark.parametrize("failing", ["ftruncate", "write"])
def test_pid_note_failure_keeps_a_releasable_lock(tmp_path, monkeypatch, failing):
    # The shared owner-lock implementation (#322).
    import stiracore.owner_lock as daemon_module

    def broken(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(daemon_module.os, failing, broken)
    lock = HeartbeatOwnerLock(tmp_path)
    lock.acquire()  # informational note failed; the lock itself is held
    assert lock.held
    with pytest.raises(HeartbeatLockError):
        HeartbeatOwnerLock(tmp_path).acquire()
    lock.release()
    monkeypatch.undo()
    other = HeartbeatOwnerLock(tmp_path)
    other.acquire()  # nothing leaked
    other.release()


def test_lock_errors_other_than_contention_propagate_and_close_the_fd(tmp_path, monkeypatch):
    import errno

    # The shared owner-lock implementation (#322).
    import stiracore.owner_lock as daemon_module

    closed = []
    real_close = daemon_module.os.close
    monkeypatch.setattr(daemon_module.os, "close", lambda fd: (closed.append(fd), real_close(fd)))

    def io_error(_fd):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(daemon_module, "_try_lock", io_error)
    with pytest.raises(OSError) as raised:
        HeartbeatOwnerLock(tmp_path).acquire()
    assert not isinstance(raised.value, HeartbeatLockError)
    assert raised.value.errno == errno.EIO
    assert len(closed) == 1

    def contention(_fd):
        raise BlockingIOError(errno.EWOULDBLOCK, "busy")

    monkeypatch.setattr(daemon_module, "_try_lock", contention)
    with pytest.raises(HeartbeatLockError):
        HeartbeatOwnerLock(tmp_path).acquire()
    assert len(closed) == 2


def test_stira_survives_a_non_lock_heartbeat_start_failure(stira, monkeypatch):
    import errno

    def broken(self):
        raise OSError(errno.EROFS, "read-only file system")

    monkeypatch.setattr(HeartbeatOwnerLock, "acquire", broken)
    app, _ = stira(env="1")
    assert app.extensions["heartbeat"] is None
    status = app.test_client().get("/api/gateway/status").get_json()["heartbeat"]
    assert status == {"enabled": True, "running": False, "last_error": "OSError"}


# --- Review fixes: lost-update tests that really race on the same dimension --


class SlowDecayState(NeuralState):
    """apply_decay with a widened read-modify-write window (for race tests)."""

    def apply_decay(self, *, now_fn=None, min_interval_sec=60.0):
        import copy as _copy
        from dataclasses import fields

        work = NeuralState(**{f.name: _copy.deepcopy(getattr(self, f.name)) for f in fields(self)})
        self.entered.set()
        self.proceed.wait(2)
        deltas = work.apply_decay(now_fn=now_fn, min_interval_sec=min_interval_sec)
        for name in (*DIMS, "last_decay_check"):
            setattr(self, name, getattr(work, name))
        return deltas


@pytest.mark.parametrize("locked", [True, False])
def test_tick_and_turn_serialize_on_the_same_dimension(tmp_path, locked):
    """Control: without the shared lock the stimulus is lost (the test can fail)."""
    import contextlib

    clock = Clock()
    state = SlowDecayState(stability=0.9, trust=0.9, last_decay_check=T0)
    state.entered, state.proceed = threading.Event(), threading.Event()
    engine = _engine(state, clock, tmp_path / "cp", decay_min_interval_sec=0)
    if not locked:
        engine.state_lock = contextlib.nullcontext()
    daemon = HeartbeatDaemon(
        HeartbeatGateway(engine, GatewayConfig(), now_fn=clock),
        state_dir=tmp_path,
        now_fn=clock,
    )
    clock.now = T0 + 10 * 3600.0  # decay lowers stability/trust toward baseline
    daemon.start(background=False)
    ticker = threading.Thread(target=daemon.poll)
    ticker.start()
    assert state.entered.wait(5)  # the tick is between its read and its write
    turn = threading.Thread(target=lambda: engine.post_turn("joy", 1.0))
    turn.start()
    turn.join(0.3)  # locked: blocked on state_lock; unlocked: already applied
    state.proceed.set()
    ticker.join(5)
    turn.join(5)
    daemon.stop()

    # The serialization the lock enforces: tick (decay) first, then the turn.
    reference = NeuralState(stability=0.9, trust=0.9, last_decay_check=T0)
    reference.apply_decay(now_fn=lambda: T0 + 10 * 3600.0, min_interval_sec=0)
    reference.apply_stimulus("joy", 1.0, now_fn=lambda: T0 + 10 * 3600.0)
    expected = {d: getattr(reference, d) for d in DIMS}
    actual = {d: getattr(state, d) for d in DIMS}
    if locked:
        assert actual == expected
    else:
        assert actual != expected  # the joy update was overwritten by the decay write
        assert actual["stability"] == pytest.approx(0.9 - 10 * DECAY_RATES["stability"])


class TickingClock:
    """Thread-safe clock that advances 10 s on every read: every decay is real."""

    def __init__(self, now: float = T0) -> None:
        self.now = now
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self.now += 10.0
            return self.now


def test_moving_clock_ticks_turns_and_saves_match_their_serial_order(
    tmp_path, fast_switching, monkeypatch
):
    """Decay and stimuli both change the same dimensions while the clock moves.

    Every state mutation logs itself (inside the shared lock); replaying that
    log sequentially must reproduce the live state exactly (no lost update).
    Saves run on both sides; the file left behind (no final save here) must
    hold the newest snapshot any save took, equal to the replay up to that
    point (no rollback by an older concurrent snapshot). With engine.state_lock
    replaced by a no-op this test fails (measured: 10/10 runs).
    """
    import stiracore.agent.state_persistence as persistence_module

    turns = 600
    clock = TickingClock()
    state = NeuralState(stability=0.9, trust=0.9, curiosity=0.9, last_decay_check=T0)
    log = []
    real_decay, real_stimulus = state.apply_decay, state.apply_stimulus

    def logged_decay(*, now_fn=None, min_interval_sec=60.0):
        now = now_fn()
        log.append(("decay", now))
        time.sleep(0)  # yield inside the critical section to provoke interleaving
        return real_decay(now_fn=lambda: now, min_interval_sec=min_interval_sec)

    def logged_stimulus(stimulus, intensity=1.0, *, now_fn=None):
        now = now_fn()
        log.append((stimulus, now))
        time.sleep(0)
        return real_stimulus(stimulus, intensity, now_fn=lambda: now)

    state.apply_decay, state.apply_stimulus = logged_decay, logged_stimulus
    snapshots = []  # (log position, data) taken by each save, under the lock
    real_state_dict = persistence_module._state_dict

    def recording_state_dict(target):
        data = real_state_dict(target)
        snapshots.append((len(log), {d: data[d] for d in DIMS}))
        return data

    monkeypatch.setattr(persistence_module, "_state_dict", recording_state_dict)
    engine = _engine(state, clock, tmp_path / "cp", decay_min_interval_sec=0)
    persistence = StatePersistence(state, tmp_path / "s.json", state_lock=engine.state_lock)
    daemon = HeartbeatDaemon(
        HeartbeatGateway(
            engine,
            GatewayConfig(hb_interval_floor_sec=0.0, hb_interval_ceil_sec=0.0),
            now_fn=clock,
        ),
        state_dir=tmp_path,
        now_fn=clock,
        # Saves (fsync) on both sides, but not on every operation: an fsync per
        # step would serialize the threads on I/O and hide the race.
        on_tick=lambda r: persistence.save() if r["snapshot"]["turn"] % 50 == 0 else None,
        min_wait_sec=0.0,
    )

    def turn_worker():
        for i in range(turns):
            # Alternate so the values stay mid-range (no clamping hides the order).
            engine.post_turn("threat" if i % 2 else "joy", 0.5)
            if i % 50 == 49 or i == turns - 1:
                persistence.save()

    daemon.start()
    try:
        worker = threading.Thread(target=turn_worker)
        worker.start()
        worker.join(60)
    finally:
        assert daemon.stop(timeout=10) is True

    assert sum(1 for kind, _ in log if kind != "decay") == turns
    assert sum(1 for kind, _ in log if kind == "decay") > 10

    def replay(entries):
        reference = NeuralState(stability=0.9, trust=0.9, curiosity=0.9, last_decay_check=T0)
        for kind, now in entries:
            if kind == "decay":
                reference.apply_decay(now_fn=lambda n=now: n, min_interval_sec=0)
            else:
                reference.apply_stimulus(kind, 0.5, now_fn=lambda n=now: n)
        return {d: getattr(reference, d) for d in DIMS}

    assert {d: getattr(state, d) for d in DIMS} == replay(log)
    assert len(snapshots) > 5
    position, newest = max(snapshots, key=lambda item: item[0])
    saved = json.loads(persistence.path.read_text(encoding="utf-8"))
    assert {d: saved[d] for d in DIMS} == newest
    assert newest == replay(log[:position])


def test_stira_turns_and_heartbeat_ticks_with_real_sink_and_saves(stira, monkeypatch):
    from stiracore.agent import nxp_agent
    from tests.provider_fakes import FakeModel, text_turn

    turns = 6
    model = FakeModel([text_turn(f"r{i}") for i in range(turns)])
    monkeypatch.setattr(nxp_agent, "BedrockModel", lambda **kw: model)
    from strands import Agent as RealAgent

    monkeypatch.setattr(nxp_agent, "Agent", RealAgent)  # the fixture forbids model calls
    app, data_dir = stira(env="1")
    daemon = app.extensions["heartbeat"]
    client = app.test_client()
    done = threading.Event()
    replies = []

    def chat():
        try:
            for i in range(turns):
                replies.append(client.post("/api/chat", json={"message": f"m{i}"}).text)
        finally:
            done.set()

    worker = threading.Thread(target=chat)
    worker.start()
    ticks = 0
    while not done.is_set():
        daemon._tick_once(time.time())  # the real on_tick (save) + sink path
        ticks += 1
    worker.join(30)
    assert all(f"r{i}" in reply for i, reply in enumerate(replies)), replies
    assert ticks > 0
    state = app.extensions["heartbeat"].gateway.engine.state
    saved = json.loads((data_dir / "neural-state.json").read_text(encoding="utf-8"))
    assert {d: saved[d] for d in DIMS} == {d: getattr(state, d) for d in DIMS}
    history = client.get("/api/state/history?limit=500").get_json()
    assert history["heartbeat"]["tick_count"] >= ticks
    assert len(history["history"]) <= 500


# --- Coexistence with #243 write_roots (merge of #250) ----------------------


def test_heartbeat_setting_and_write_roots_do_not_clobber_each_other(stira, tmp_path):
    from stiracore.identity_paths import WORKSPACE_ROOT

    GatewayConfig(path=tmp_path / "gw.json", hb_enabled=True).save()
    app, data_dir = stira()  # STIRA_HEARTBEAT unset: the gateway file opts in
    daemon = app.extensions["heartbeat"]
    assert daemon is not None and daemon.running
    client = app.test_client()
    hub = client.get("/api/config/hub").get_json()
    assert hub["values"]["write"] == {"roots": [WORKSPACE_ROOT]}
    saved = client.post(
        "/api/config/hub",
        json={
            "changes": {"write": {"roots": [WORKSPACE_ROOT, "/tmp"]}},
            "base_version": hub["version"],
            "acknowledged_dangers": ["write_roots_widened"],  # #536
        },
    )
    assert saved.status_code == 200, saved.get_json()
    store = app.extensions["config_store"]
    gateway, agent = store.load()
    assert gateway.hb_enabled is True  # a write-scope save keeps the heartbeat opt-in
    assert agent.write_roots == [WORKSPACE_ROOT, "/tmp"]
    # The host-side owner lock is not an agent write: write_roots do not gate it.
    assert (data_dir / "heartbeat.lock").exists()
    assert daemon.status()["state"] == "running"
