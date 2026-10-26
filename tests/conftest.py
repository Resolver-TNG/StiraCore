"""Shared fixtures; core tests never construct an AWS or Bedrock client."""

import time
from dataclasses import dataclass, field

import pytest

from stiracore.core import EMOTION_MATRIX, NeuralState, StiraConfig, StiraEngine


@dataclass
class FakeClock:
    """A controllable epoch clock compatible with ``now_fn``."""

    now: float = field(default_factory=time.time)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        self.now += seconds
        return self.now


@pytest.fixture
def fake_clock():
    return FakeClock()


@pytest.fixture
def default_state():
    return NeuralState()


@pytest.fixture
def default_config():
    return StiraConfig()


@pytest.fixture
def known_stimulus():
    assert EMOTION_MATRIX, "The stimulus matrix must not be empty"
    return next(iter(EMOTION_MATRIX))


@pytest.fixture
def make_engine(fake_clock):
    """Build isolated engines sharing the test's injectable clock."""

    def factory(state=None, config=None, on_breaker=None):
        return StiraEngine(
            state=state if state is not None else NeuralState(),
            config=config if config is not None else StiraConfig(),
            on_breaker=on_breaker,
            now_fn=fake_clock,
        )

    return factory


# The default checkpoint store is redirected to tmp_path for every test, the dashboard
# ones included, by the root conftest.py (``_isolate_default_checkpoints``, #514).


# --- W2 (#296): no authority thread outlives a test ----------------------------------
# Every app build starts an ``authority-watchdog`` (and settlements start short-lived
# ``authority-settle`` / ``authority-kill-persist`` threads). A thread left running
# writes (and fsyncs) during later tests. After every test the running watchdogs are
# stopped; in the authority test modules a left-over one, or any authority thread still
# alive after a grace period, fails the test that leaked it (with the thread names).

_AUTHORITY_MODULES = {
    "test_authority_latches",
    "test_authority_lifecycle",
    "test_authority_watchdog",
    # ADR 0011 W4: the grant control plane starts no thread of its own (its look runs in
    # the watchdog's pass); a left-over authority or grants thread fails these too.
    "test_grants_schema",
    "test_grants_state",
    "test_grants_store",
    "test_grants_service",
    "test_grants_server",
    # W5: the executor's iteration thread (``grants-iteration-*``) never outlives a test.
    "test_grants_executor",
    "test_grants_executor_server",
}
_TRANSIENT_GRACE_SEC = 10.0


def _authority_threads(before):
    import threading

    return [
        t
        for t in threading.enumerate()
        if t.name.startswith(("authority-", "grants")) and t.ident not in before and t.is_alive()
    ]


@pytest.fixture(autouse=True)
def _no_leaked_authority_threads(request):
    import sys
    import threading

    before = {t.ident for t in threading.enumerate()}
    yield
    module = sys.modules.get("stiracore.authority.watchdog")
    leaked_watchdogs = []
    if module is not None:
        leaked_watchdogs = [d._thread.name for d in module.live() if d._thread.ident not in before]
        module.stop_all(5.0)
    strict = request.module.__name__.rpartition(".")[2] in _AUTHORITY_MODULES
    if not strict:
        return
    deadline = time.monotonic() + _TRANSIENT_GRACE_SEC
    left = _authority_threads(before)
    while left and time.monotonic() < deadline:
        time.sleep(0.02)
        left = _authority_threads(before)
    assert not leaked_watchdogs, f"the test left a running watchdog: {leaked_watchdogs}"
    assert not left, f"authority threads outlived the test: {[t.name for t in left]}"
