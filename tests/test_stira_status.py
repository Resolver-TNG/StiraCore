"""chat server status/config views reflect the live engine, not stale defaults (#161, #162)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.core.breaker_sim import _DiscardCheckpointStore
from stiracore.core.engine import StiraEngine
from stiracore.core.neural_state import NeuralState
from stiracore.observability.config_api import ConfigStore, create_config_blueprint

SERVER = Path(__file__).resolve().parent.parent / "dashboard" / "stira_server.py"


@pytest.fixture(scope="module")
def server():
    spec = importlib.util.spec_from_file_location("stira_server_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine() -> tuple[StiraEngine, list[float]]:
    clock = [1_700_000_000.0]
    engine = StiraEngine(
        state=NeuralState(last_stimulus_time=clock[0], last_decay_check=clock[0]),
        now_fn=lambda: clock[0],
        checkpoint_store=_DiscardCheckpointStore(),
    )
    return engine, clock


def _turn(engine: StiraEngine, clock: list[float], stimulus: str | None) -> None:
    clock[0] += 60
    engine.pre_turn()
    engine.post_turn(stimulus=stimulus, intensity=1.0 if stimulus else 0.0)


def test_breaker_view_follows_engine_levels(server):
    engine, clock = _engine()
    view = {"level": "unknown", "last_changed": None}

    _turn(engine, clock, None)
    view = server._breaker_view(engine, view)
    assert view["level"] == "normal"
    first_changed = view["last_changed"]
    assert first_changed is not None

    # last_changed stays stable while the level is unchanged.
    _turn(engine, clock, None)
    assert server._breaker_view(engine, view) == view

    seen = {view["level"]}
    for _ in range(20):
        _turn(engine, clock, "loss")
        view = server._breaker_view(engine, view)
        seen.add(view["level"])
    assert {"warning", "critical"} <= seen
    assert view["level"] == "critical"


def test_classifier_view_describes_resolved_classifier(server):
    class Agent:
        _stimulus_classifier = None

    assert server._classifier_view(Agent())["name"] == "unresolved"

    class SokudanClassifier:
        model_id = "GeneLab/sokudan-ja-310m"

    agent = Agent()
    agent._stimulus_classifier = SokudanClassifier()
    view = server._classifier_view(agent)
    assert view == {
        "name": "SokudanClassifier",
        "is_jev": True,
        "model": "GeneLab/sokudan-ja-310m",
        "mode": "sokudan",  # #367, additive
    }


def _jev_client(tmp_path, effective):
    store = ConfigStore(
        gateway_path=tmp_path / "gateway.json",
        agent_path=tmp_path / "agent.json",
        tool_path=tmp_path / "tools.json",
    )
    app = Flask(__name__)
    app.register_blueprint(
        create_config_blueprint(store, effective_classifier_provider=lambda: effective)
    )
    return app.test_client()


def test_config_jev_reports_effective_classifier_mismatch(tmp_path, monkeypatch):
    # Default config is "auto" (#162); without Sokudan it resolves to rules, so a
    # running Sokudan is a mismatch that a restart resolves.
    from stiracore.observability import config_api

    monkeypatch.setattr(config_api, "_sokudan_available", lambda: False)
    effective = {"name": "SokudanClassifier", "is_jev": True, "model": "m"}
    data = _jev_client(tmp_path, effective).get("/api/config/jev").json
    assert data["current"] == "auto"
    assert data["effective"] == effective
    assert data["effective_matches_config"] is False
    assert data["restart_required"] is True


def test_config_jev_unresolved_classifier_is_unknown(tmp_path):
    effective = {"name": "unresolved", "is_jev": False, "model": None}
    data = _jev_client(tmp_path, effective).get("/api/config/jev").json
    assert data["effective_matches_config"] is None


def test_config_jev_without_provider_is_unchanged(tmp_path):
    store = ConfigStore(
        gateway_path=tmp_path / "g.json", agent_path=tmp_path / "a.json", tool_path=tmp_path / "t"
    )
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store))
    data = app.test_client().get("/api/config/jev").json
    assert "effective" not in data
    assert data["current"] == "auto"


def test_breaker_view_adds_reasons_and_last_transition(server):
    """#455: level / last_changed keep their shape; reasons, inputs, last_transition join."""
    engine, clock = _engine()
    view = {"level": "unknown", "last_changed": None}
    _turn(engine, clock, None)
    view = server._breaker_view(engine, view)
    assert view["level"] == "normal"
    assert view["reasons"] == ["within_thresholds"]
    assert view["last_transition"] is None
    changed = view["last_changed"]
    for _ in range(8):
        _turn(engine, clock, "loss")
        view = server._breaker_view(engine, view)
    assert view["level"] == "warning"
    assert view["last_changed"] is not None and changed is not None
    assert view["reasons"] == ["stability_below_warning"]
    assert view["inputs"]["stability_warning"] == 0.5
    assert view["last_transition"]["from"] == "normal"
    assert view["last_transition"]["to"] == "warning"
    assert view["last_transition"]["reasons"] == ["stability_below_warning"]
    assert set(view) == {"level", "last_changed", "reasons", "inputs", "last_transition"}


def test_yolo_breaker_reasons_follow_the_stricter_live_level(server):
    engine, clock = _engine()
    _turn(engine, clock, None)
    assert server._yolo_breaker_reasons(engine)["reasons"] == ["within_thresholds"]
    engine.state.stability = 0.2  # live state lower than the evaluated level
    detail = server._yolo_breaker_reasons(engine)
    assert detail["reasons"] == ["stability_below_critical"]
    assert detail["inputs"] == {"stability": 0.2, "stability_critical": 0.3}
