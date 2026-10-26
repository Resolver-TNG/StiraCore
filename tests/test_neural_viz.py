import copy

import pytest
from flask import Flask

from dashboard.app import create_app
from stiracore.core import NeuralState, StiraConfig
from stiracore.observability.neural_history import (
    HISTORY_LIMIT,
    NeuralHistory,
    create_neural_history_blueprint,
    history_payload,
)


@pytest.fixture
def observer(tmp_path):
    path = tmp_path / "state.json"
    NeuralState(stability=0.9).save(path)
    app = create_app(
        path, tmp_path / "gateway.json", tmp_path / "agent.json", tmp_path / "tools.json"
    )
    return app, path


def test_history_shape_and_no_mutation(observer):
    app, path = observer
    original = path.read_bytes()
    store = app.extensions["state_store"]
    before = copy.deepcopy(store.state.to_dict())
    client = app.test_client()
    for _ in range(3):
        response = client.get("/api/state/history")
        data = response.json
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert data["history"] == []
        assert data["count"] == 0
        assert len(data["dimensions"]) == 6
        assert len(data["emotion_matrix"]["values"]) == 6
        assert len(data["prediction"]) == 25
        assert data["thresholds"] == {"warning": 0.5, "critical": 0.3}
    assert before == store.state.to_dict()
    assert path.read_bytes() == original
    assert store.engine._turn_count == 0


def test_recorded_stimuli_limits_restart(observer):
    app, path = observer
    client = app.test_client()
    for _ in range(12):
        assert (
            client.post("/api/stimulus", json={"stimulus": "joy", "intensity": 0.1}).status_code
            == 200
        )
    data = client.get("/api/state/history?limit=2").json
    assert data["count"] == 2
    assert [row["turn"] for row in data["history"]] == [11, 12]
    assert data["history"][-1]["stimulus"] == "joy"
    assert set(data["dimensions"]) <= data["history"][-1]["state"].keys()
    assert len(client.get("/api/history").json) == 10  # legacy contract
    assert create_app(path).test_client().get("/api/state/history").json["count"] == 12


@pytest.mark.parametrize("value", ["0", "-1", "501", "oops", "1.5", ""])
def test_bad_limit(observer, value):
    app, _ = observer
    assert app.test_client().get(f"/api/state/history?limit={value}").status_code == 400


def test_corrupt_source(observer):
    app, path = observer
    path.write_text("bad json")
    assert app.test_client().get("/api/state/history").status_code == 503


def test_prediction_matches_core_without_mutation(monkeypatch):
    from stiracore.observability import neural_history

    monkeypatch.setattr(neural_history.time, "time", lambda: 100000.0)
    state = NeuralState(stability=0.9, vigilance=0.0, last_decay_check=100000.0)
    before = state.to_dict()
    data = history_payload(state, StiraConfig(), [], 100)
    expected = copy.deepcopy(state)
    expected.apply_decay(now_fn=lambda: 100000.0 + 24 * 3600, min_interval_sec=0)
    assert data["prediction"][-1]["state"] == expected.snapshot()
    assert state.to_dict() == before


def test_live_history_blueprint_bounded():
    history = NeuralHistory()
    state = NeuralState()
    for i in range(HISTORY_LIMIT + 10):
        history.record(state, stimulus="joy" if i % 2 else None)
    assert len(history.snapshot()) == HISTORY_LIMIT
    snapshot = history.snapshot()
    snapshot[0]["state"]["trust"] = 0
    assert history.snapshot()[0]["state"]["trust"] == state.trust
    app = Flask(__name__)
    app.register_blueprint(
        create_neural_history_blueprint(
            lambda limit: history_payload(state, StiraConfig(), history.snapshot(), limit)
        )
    )
    assert app.test_client().get("/api/state/history?limit=5").json["count"] == 5


def test_page_assets_and_legacy_history(observer):
    app, _ = observer
    client = app.test_client()
    response = client.get("/observability")
    assert response.status_code == 200
    assert b'id="neural-chart"' in response.data
    assert b'id="log-body"' in response.data
    assert b"<style" not in response.data
    assert client.get("/static/js/neural_viz.js").status_code == 200
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]


def test_live_gateway_records_turn_without_polling(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from dashboard import stira_server
    from stiracore.agent import nxp_agent
    from stiracore.agent.stimulus_classifier import StimulusResult

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))

    def build_agent(**kwargs):
        return Mock(
            return_value="Hello",
            messages=list(kwargs.get("messages") or []),
            system_prompt=kwargs.get("system_prompt", ""),
        )

    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=build_agent))
    from stiracore.agent import stimulus_classifier

    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    app = stira_server.create_app()
    client = app.test_client()
    assert client.get("/observability").status_code == 200
    initial = client.get("/api/state/history").json
    assert initial["count"] == 1
    response = client.post("/api/chat", json={"message": "hello"})
    assert "event: turn" in response.text
    data = client.get("/api/state/history").json
    assert data["count"] == 2
    assert data["history"][-1]["stimulus"] == "stimulus"
    assert data["current"]["state"]["satisfaction"] > initial["current"]["state"]["satisfaction"]
