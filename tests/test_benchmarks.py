import inspect
import time
from unittest.mock import Mock

import pytest

from stiracore.core.amygdala import AmygdalaPass
from stiracore.core.engine import StiraConfig, StiraEngine
from stiracore.core.neural_state import NeuralState
from stiracore.middleware.identity_guard import IdentityGuard

# Target: sub-millisecond operations. Allow 10 ms per operation on slow CI.
CI_BOUND_SECONDS = 0.010
ITERATIONS = 1_000


@pytest.fixture(autouse=True)
def forbid_model_construction(monkeypatch):
    from strands import models
    from strands.models import bedrock

    forbidden = Mock(side_effect=AssertionError("Benchmarks must remain entirely offline"))
    monkeypatch.setattr(models, "BedrockModel", forbidden)
    monkeypatch.setattr(bedrock, "BedrockModel", forbidden)
    return forbidden


@pytest.fixture
def average_duration():
    def measure(operation):
        # Warm caches before timing; fixture construction is deliberately
        # excluded. Batch averages avoid flaky single-sample assertions.
        for _ in range(20):
            operation()
        start = time.perf_counter()
        for _ in range(ITERATIONS):
            operation()
        elapsed = time.perf_counter() - start
        return elapsed / ITERATIONS

    return measure


@pytest.fixture
def scorer():
    return AmygdalaPass()


@pytest.fixture
def state():
    return NeuralState()


@pytest.fixture
def guard(state):
    parameters = inspect.signature(StiraEngine).parameters
    state_parameter = next(name for name in ("state", "neural_state") if name in parameters)
    engine = StiraEngine(**{state_parameter: state, "config": StiraConfig()})
    return IdentityGuard(engine, ["*/soul.md", "*/SOUL.md"])


def test_amygdala_scoring_latency(scorer, average_duration):
    content = (
        "Alice confirmed the decision after investigating an error "
        "and recording the recovery procedure."
    )

    def operation():
        return scorer.score(content, {"source": "offline-benchmark"})

    assert 0.0 <= operation() <= 1.0
    duration = average_duration(operation)
    assert duration < CI_BOUND_SECONDS, f"Scoring averaged {duration * 1000:.3f} ms"


def test_identity_guard_check_latency(guard, average_duration):
    arguments = {
        "command": "printf replacement > agents/offline/SOUL.md",
    }

    def operation():
        return guard.check_tool_call("shell", arguments)

    assert operation().allowed is False
    duration = average_duration(operation)
    assert duration < CI_BOUND_SECONDS, f"Guard checking averaged {duration * 1000:.3f} ms"


def test_neural_state_stimulus_latency(state, average_duration):
    def operation():
        state.apply_stimulus("joy", intensity=0.1)

    duration = average_duration(operation)
    assert duration < CI_BOUND_SECONDS, f"Stimulus application averaged {duration * 1000:.3f} ms"
    assert 0.0 <= state.stability <= 1.0
    assert 0.0 <= state.satisfaction <= 1.0
