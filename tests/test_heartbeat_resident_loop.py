"""#238: the resident (background-thread) heartbeat, not only the ``poll()`` path.

``test_heartbeat_daemon.py`` drives every scheduling case through ``poll()`` on a fake
clock. Here the real worker thread runs against the same injected clock, so the
interval, stop/restart and #230 (clock back, long sleep) cases are checked on the path
The chat server uses. The worker re-reads the fake clock every few milliseconds
(``max_wait_sec``); a test only asserts "no tick" after the worker has read the new
time several times. Expected values were measured first.

Also here: the time the chat server heartbeat holds ``engine.state_lock`` while chat turns
run (real sink and save), and the ``msvcrt`` branch of the owner lock on a simulated
``msvcrt`` (the real Windows path is still unverified; see docs/heartbeat-daemon.md).
"""

from __future__ import annotations

import errno
import json
import statistics
import sys
import threading
import time
import types
from collections import defaultdict
from pathlib import Path

import pytest

from stiracore import owner_lock
from stiracore.config import GatewayConfig
from stiracore.core import NeuralState
from stiracore.core.neural_state import BASELINES, DECAY_RATES
from stiracore.heartbeat import (
    DaemonState,
    HeartbeatDaemon,
    HeartbeatGateway,
    HeartbeatLockError,
    HeartbeatOwnerLock,
)
from stiracore.heartbeat.pacing import compute_interval
from tests.test_heartbeat_daemon import (
    DIMS,
    T0,
    _engine,
    stira,  # noqa: F401 - the chat server app fixture
)


class ReadCountingClock:
    """Fake wall clock shared by the test and the worker; counts the worker's reads."""

    def __init__(self, now: float = T0) -> None:
        self._now = now
        self._lock = threading.Lock()
        self.reads = 0

    def set(self, now: float) -> None:
        with self._lock:
            self._now = now

    def __call__(self) -> float:
        with self._lock:
            self.reads += 1
            return self._now


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


def _settle(clock: ReadCountingClock, reads: int = 6) -> None:
    """Wait until the worker has read the clock ``reads`` more times (2 per loop)."""
    target = clock.reads + reads
    assert _wait_for(lambda: clock.reads >= target), "the worker stopped reading the clock"


def _resident(tmp_path: Path, stability: float):
    clock = ReadCountingClock()
    state = NeuralState(stability=stability, last_decay_check=T0, last_stimulus_time=T0)
    engine = _engine(state, clock, tmp_path / "cp")
    gateway = HeartbeatGateway(
        engine,
        GatewayConfig(hb_interval_floor_sec=100.0, hb_interval_ceil_sec=1000.0),
        now_fn=clock,
    )
    daemon = HeartbeatDaemon(
        gateway,
        state_dir=tmp_path / "state",
        now_fn=clock,
        min_wait_sec=0.0,
        max_wait_sec=0.002,  # real seconds between clock readings
    )
    return daemon, clock, state


def _ticks(daemon: HeartbeatDaemon) -> int:
    return daemon.status()["tick_count"]


# --- interval, stop, restart on the worker thread --------------------------------------


def test_resident_loop_ticks_on_interval_stops_and_resumes_after_restart(tmp_path):
    daemon, clock, state = _resident(tmp_path, stability=0.5)
    # compute_interval(0.5, 100, 1000) = 250 s
    daemon.start()  # background worker
    try:
        assert _wait_for(lambda: _ticks(daemon) == 1)  # first tick at start-up
        assert daemon.status()["next_tick_at"] == T0 + 250.0

        clock.set(T0 + 249.0)
        _settle(clock)
        assert _ticks(daemon) == 1
        clock.set(T0 + 250.0)
        assert _wait_for(lambda: _ticks(daemon) == 2)
        _settle(clock)
        assert _ticks(daemon) == 2  # one tick per due time, no burst
        # 250 s of decay raised stability a little: the next interval follows it.
        expected = compute_interval(state.stability, 100.0, 1000.0)
        assert daemon.status()["next_tick_at"] == pytest.approx(T0 + 250.0 + expected)

        assert daemon.stop(2.0) is True
        assert daemon.state is DaemonState.STOPPED
        reads = clock.reads
        clock.set(T0 + 10_000.0)
        time.sleep(0.05)  # 25 worker periods: a live worker would have ticked
        assert _ticks(daemon) == 2
        assert clock.reads == reads  # no thread left reading the clock
        assert daemon.status()["next_tick_at"] is None

        daemon.start()  # restart: an immediate tick on the new worker
        assert _wait_for(lambda: _ticks(daemon) == 3)
        assert daemon.status()["last_tick_at"] == T0 + 10_000.0
        expected = compute_interval(state.stability, 100.0, 1000.0)
        assert daemon.status()["next_tick_at"] == pytest.approx(T0 + 10_000.0 + expected)
    finally:
        daemon.stop(2.0)


# --- #230 on the worker thread: clock back, long sleep ----------------------------------


def test_resident_loop_clock_back_and_long_sleep_do_not_jump_stability(tmp_path):
    daemon, clock, state = _resident(tmp_path, stability=0.3)
    # compute_interval(0.3, 100, 1000) = 100 s (floor)
    daemon.start()
    try:
        assert _wait_for(lambda: _ticks(daemon) == 1)
        before = {dim: getattr(state, dim) for dim in DIMS}

        clock.set(T0 - 3600.0)  # NTP step back / a restored, skewed host
        assert _wait_for(lambda: daemon.status()["clock_resyncs"] == 1)
        _settle(clock)
        status = daemon.status()
        assert status["clock_resyncs"] == 1  # re-based once, not on every reading
        assert status["next_tick_at"] == T0 - 3600.0 + 100.0  # not stalled until T0 + 100
        assert status["tick_count"] == 1

        clock.set(T0 - 3500.0)
        assert _wait_for(lambda: _ticks(daemon) == 2)
        assert {dim: getattr(state, dim) for dim in DIMS} == before  # resync, no decay
        assert state.last_decay_check == T0 - 3500.0

        clock.set(T0 + 100.0)  # one hour after the resynchronized time
        assert _wait_for(lambda: _ticks(daemon) == 3)
        assert state.stability == pytest.approx(0.3 + DECAY_RATES["stability"] * 0.5)

        clock.set(T0 + 100.0 + 30 * 86_400.0)  # the host slept for a month
        assert _wait_for(lambda: _ticks(daemon) == 4)
        _settle(clock)
        assert _ticks(daemon) == 4  # one catch-up tick, not 25 920
        assert state.stability == BASELINES["stability"]  # clamped, never past it
        assert all(0.0 <= getattr(state, dim) <= 1.0 for dim in DIMS)
        assert daemon.status()["consecutive_failures"] == 0
    finally:
        daemon.stop(2.0)


# --- the shared lock while turns run (chat server wiring, real sink and save) ----------------


class TimedRLock:
    """Wraps the engine's RLock (same mutual exclusion) and times outermost holds/waits."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._local = threading.local()
        self._record = threading.Lock()
        self.holds: dict[str, list[float]] = defaultdict(list)
        self.waits: dict[str, list[float]] = defaultdict(list)

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        asked = time.perf_counter()
        got = self._inner.acquire(blocking, timeout)
        if got:
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                self._local.since = time.perf_counter()
                with self._record:
                    self.waits[threading.current_thread().name].append(
                        (self._local.since - asked) * 1000.0
                    )
            self._local.depth = depth + 1
        return got

    def release(self) -> None:
        self._local.depth -= 1
        if self._local.depth == 0:
            held = (time.perf_counter() - self._local.since) * 1000.0
            with self._record:
                self.holds[threading.current_thread().name].append(held)
        self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "median_ms": round(statistics.median(ordered), 4),
        "p99_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))], 4),
        "max_ms": round(ordered[-1], 4),
    }


def test_stira_heartbeat_holds_the_state_lock_briefly_while_turns_run(
    stira,  # noqa: F811 - the imported fixture
    monkeypatch,
):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent
    from tests.provider_fakes import FakeModel, text_turn

    class FastGateway(HeartbeatGateway):
        def tick(self):  # the real tick, re-scheduled after 1 ms instead of minutes
            return {**super().tick(), "next_interval_sec": 0.001}

    def fast_daemon(gateway, **kwargs):
        return HeartbeatDaemon(gateway, min_wait_sec=0.0, max_wait_sec=0.001, **kwargs)

    persistences = []

    class RecordedPersistence(stira_server.StatePersistence):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            persistences.append(self)

    monkeypatch.setattr(stira_server, "StatePersistence", RecordedPersistence)
    monkeypatch.setattr(stira_server, "HeartbeatGateway", FastGateway)
    monkeypatch.setattr(stira_server, "HeartbeatDaemon", fast_daemon)
    turns = 8
    model = FakeModel([text_turn(f"r{i}") for i in range(turns)])
    monkeypatch.setattr(nxp_agent, "BedrockModel", lambda **kw: model)
    from strands import Agent as RealAgent

    monkeypatch.setattr(nxp_agent, "Agent", RealAgent)  # the fixture forbids model calls
    app, data_dir = stira(env="1")
    daemon = app.extensions["heartbeat"]
    engine = daemon.gateway.engine
    timed = TimedRLock(engine.state_lock)
    # The same underlying RLock (a tick in flight keeps its exclusion); the per-tick save
    # copies the state under the persistence's reference to it, so that one is timed too.
    assert [p.state_lock for p in persistences] == [engine.state_lock]
    engine.state_lock = timed
    persistences[0].state_lock = timed
    client = app.test_client()
    ticks_before = _ticks(daemon)
    replies = []
    for i in range(turns):
        replies.append(client.post("/api/chat", json={"message": f"m{i}"}).text)
    ticks = _ticks(daemon) - ticks_before
    assert daemon.stop(5.0) is True

    assert all(f"r{i}" in reply for i, reply in enumerate(replies)), replies
    assert ticks >= turns  # the worker kept ticking during the turns
    heartbeat_holds = timed.holds["stira-heartbeat"]
    turn_waits = [w for name, ws in timed.waits.items() if name != "stira-heartbeat" for w in ws]
    turn_holds = [h for name, hs in timed.holds.items() if name != "stira-heartbeat" for h in hs]
    assert heartbeat_holds and turn_waits
    measured = {
        "ticks": ticks,
        "heartbeat_hold": _summary(heartbeat_holds),
        "turn_wait": _summary(turn_waits),
        "turn_hold": _summary(turn_holds),
    }
    print(f"\n#238 state_lock measurement: {json.dumps(measured)}")
    # Measured on an M1 Max (2026-10-21, 8 turns): see docs/heartbeat-daemon.md "Turns".
    # Loose bounds for CI runners; a tick that held the lock for a save or a model
    # call would be orders of magnitude above them.
    assert measured["heartbeat_hold"]["max_ms"] < 50.0
    assert measured["turn_wait"]["p99_ms"] < 50.0
    state = engine.state
    saved = json.loads((data_dir / "neural-state.json").read_text(encoding="utf-8"))
    assert {d: saved[d] for d in DIMS} == {d: getattr(state, d) for d in DIMS}


# --- the Windows branch of the owner lock, on a simulated msvcrt --------------------------


class _FakeMsvcrt(types.ModuleType):
    """``msvcrt.locking`` as documented: byte ranges from the current position, per handle.

    ``LK_NBLCK`` on a range another descriptor holds and ``LK_UNLCK`` on a range this
    descriptor does not hold raise ``OSError(EACCES)``, like CPython's wrapper of
    ``_locking``. Not a substitute for a Windows run: mandatory locking, sharing modes
    and ``SetEndOfFile`` on a locked file are not modelled.
    """

    LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK = 0, 1, 2, 3, 4

    def __init__(self) -> None:
        super().__init__("msvcrt")
        self.held: dict[tuple[int, int, int], int] = {}
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        import os

        info = os.fstat(fd)
        start = os.lseek(fd, 0, os.SEEK_CUR)
        self.calls.append((mode, start, nbytes))
        keys = [(info.st_dev, info.st_ino, start + i) for i in range(nbytes)]
        if mode == self.LK_NBLCK:
            if any(self.held.get(key, fd) != fd for key in keys):
                raise OSError(errno.EACCES, "Permission denied")
            self.held.update(dict.fromkeys(keys, fd))
        elif mode == self.LK_UNLCK:
            if any(self.held.get(key) != fd for key in keys):
                raise OSError(errno.EACCES, "Permission denied")
            for key in keys:
                del self.held[key]
        else:  # the owner lock never blocks
            raise AssertionError(f"unexpected locking mode {mode}")


class _NtOs:
    """``os`` as ``owner_lock`` sees it on Windows: only ``name`` differs."""

    name = "nt"

    def __getattr__(self, attribute):
        import os

        return getattr(os, attribute)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows runs the real msvcrt path")
def test_owner_lock_windows_branch_on_a_simulated_msvcrt(tmp_path, monkeypatch):
    fake = _FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(owner_lock, "os", _NtOs())

    first, second = HeartbeatOwnerLock(tmp_path), HeartbeatOwnerLock(tmp_path)
    first.acquire()
    assert fake.calls == [(fake.LK_NBLCK, 0, 1)]  # byte 0, never blocking
    with pytest.raises(HeartbeatLockError):  # EACCES is contention, not another error
        second.acquire()
    assert second.held is False
    assert (tmp_path / "heartbeat.lock").read_text(encoding="ascii").strip().isdigit()
    first.release()
    # The note moved the position to the end of the file: the unlock seeks back to 0.
    assert fake.calls[-1] == (fake.LK_UNLCK, 0, 1)
    assert fake.held == {}
    second.acquire()  # released means another owner can take it
    second.release()

    # The daemon over the same branch: a second heartbeat is refused, then resumes.
    clock = ReadCountingClock()
    state = NeuralState(stability=0.5, last_decay_check=T0, last_stimulus_time=T0)
    gateway = HeartbeatGateway(
        _engine(state, clock, tmp_path / "cp"), GatewayConfig(), now_fn=clock
    )
    one = HeartbeatDaemon(gateway, state_dir=tmp_path / "state", now_fn=clock)
    two = HeartbeatDaemon(gateway, state_dir=tmp_path / "state", now_fn=clock)
    one.start(background=False)
    with pytest.raises(HeartbeatLockError):
        two.start(background=False)
    assert two.state is DaemonState.STOPPED
    one.stop()
    two.start(background=False)
    two.stop()
    assert fake.held == {}
