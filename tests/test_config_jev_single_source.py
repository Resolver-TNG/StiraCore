"""#162: ``/api/config/jev`` reports the classifier choice the runtime really uses.

``jev_enabled: false`` is "auto" (Sokudan when installed, else rules), the same
reading as the settings hub (#227) and ``startup_classifier``. It used to be shown
as ``"rule"`` while the running agent used Sokudan.
"""

from __future__ import annotations

import json

import pytest
from flask import Flask

from stiracore.config.models import GatewayConfig
from stiracore.observability import config_api
from stiracore.observability.config_api import ConfigStore, create_config_blueprint

SOKUDAN = {"name": "SokudanClassifier", "is_jev": True, "model": "m"}
RULE = {"name": "RuleBasedClassifier", "is_jev": False, "model": None}


def _store(tmp_path):
    return ConfigStore(
        gateway_path=tmp_path / "gateway.json",
        agent_path=tmp_path / "agent.json",
        tool_path=tmp_path / "tools.json",
    )


def _client(tmp_path, effective=None):
    app = Flask(__name__)
    provider = None if effective is None else (lambda: effective)
    app.register_blueprint(
        create_config_blueprint(_store(tmp_path), effective_classifier_provider=provider)
    )
    return app.test_client()


@pytest.fixture
def sokudan_installed(monkeypatch):
    monkeypatch.setattr(config_api, "_sokudan_available", lambda: True)


@pytest.fixture
def sokudan_missing(monkeypatch):
    monkeypatch.setattr(config_api, "_sokudan_available", lambda: False)


def test_default_config_is_auto_and_matches_the_sokudan_runtime(tmp_path, sokudan_installed):
    # The live probe of 2026-10-12: default config, Sokudan installed and running.
    data = _client(tmp_path, SOKUDAN).get("/api/config/jev").json
    assert data["current"] == "auto"
    assert data["jev_enabled"] is False
    assert data["resolved"] == "SokudanClassifier"
    assert data["effective_matches_config"] is True
    assert data["restart_required"] is False
    assert data["applies"] == "next_start"
    assert {item["id"] for item in data["classifiers"]} == {"auto", "rule", "sokudan"}


def test_auto_without_sokudan_resolves_to_rules(tmp_path, sokudan_missing):
    data = _client(tmp_path, RULE).get("/api/config/jev").json
    assert data["current"] == "auto"
    assert data["resolved"] == "RuleBasedClassifier"
    assert data["fallback"] is False
    assert data["effective_matches_config"] is True


def test_saved_rule_while_sokudan_runs_needs_a_restart(tmp_path, sokudan_installed):
    client = _client(tmp_path, SOKUDAN)
    saved = client.post("/api/config/jev", json={"classifier": "rule"})
    assert saved.status_code == 200, saved.json
    assert saved.json["current"] == "rule"
    assert saved.json["applies"] == "next_start"
    data = client.get("/api/config/jev").json
    assert data["current"] == "rule"
    assert data["resolved"] == "RuleBasedClassifier"
    assert data["effective_matches_config"] is False
    assert data["restart_required"] is True


def test_posting_a_classifier_sets_the_choice_the_runtime_reads(tmp_path, sokudan_installed):
    """The posted value is the single source: no hidden jev_enabled left behind."""
    client = _client(tmp_path)
    store = _store(tmp_path)
    for choice, enabled in (("rule", True), ("auto", False), ("sokudan", True), ("auto", False)):
        response = client.post("/api/config/jev", json={"classifier": choice})
        assert response.status_code == 200, response.json
        gateway = GatewayConfig.load(store.gateway_path)
        assert gateway.jev_enabled is enabled
        assert config_api.hub.classifier_choice(gateway) == choice
        assert client.get("/api/config/jev").json["current"] == choice


def test_conflicting_classifier_and_switch_are_refused(tmp_path):
    client = _client(tmp_path)
    for body in (
        {"classifier": "sokudan", "jev_enabled": False},
        {"classifier": "auto", "jev_enabled": True},
    ):
        response = client.post("/api/config/jev", json=body)
        assert response.status_code == 400
        assert response.json["error_code"] == "classifier_conflict"
    assert not (tmp_path / "gateway.json").exists()


def test_switch_only_keeps_the_saved_classifier(tmp_path, sokudan_missing):
    client = _client(tmp_path)
    (tmp_path / "gateway.json").write_text(json.dumps({"jev_classifier": "sokudan"}))
    enabled = client.post("/api/config/jev", json={"jev_enabled": True}).json
    assert enabled["current"] == "sokudan"
    # Sokudan asked for but not installed: the runtime falls back to rules.
    assert enabled["resolved"] == "RuleBasedClassifier"
    assert enabled["fallback"] is True
    assert client.post("/api/config/jev", json={"jev_enabled": False}).json["current"] == "auto"


class _ClassifierAgent:
    """Stands in for NXPAgent: keeps the injected classifier like the real one does."""

    def __init__(self, *, tools=(), state=None, config=None, stimulus_classifier=None, **_kw):
        from stiracore.core import StiraEngine

        self.engine = StiraEngine(state=state, config=config)
        self.tools = tuple(tools)
        self._stimulus_classifier = stimulus_classifier


@pytest.mark.parametrize(
    ("saved", "expected"),
    [({"jev_enabled": True, "jev_classifier": "rule"}, "rule"), ({}, "auto")],
)
def test_stira_settings_and_runtime_agree(tmp_path, monkeypatch, saved, expected):
    """The real server: the classifier it builds is the one the settings report."""
    from dashboard import stira_server

    monkeypatch.setattr(config_api, "_sokudan_available", lambda: False)
    data_dir = tmp_path / "stira"
    data_dir.mkdir()
    (data_dir / "gateway-config.json").write_text(json.dumps(saved), encoding="utf-8")
    store = ConfigStore(
        data_dir / "gateway-config.json",
        data_dir / "agent-config.json",
        data_dir / "tool-config.json",
    )
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "NXPAgent", _ClassifierAgent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    app = stira_server.create_app(store)
    data = app.test_client().get("/api/config/jev").json
    assert data["current"] == expected
    if expected == "auto":
        # "auto" resolves lazily on the first turn: unknown, never a false mismatch.
        assert data["effective"]["name"] == "unresolved"
        assert data["effective_matches_config"] is None
    else:
        assert data["effective"]["name"] == "RuleBasedClassifier"
        assert data["effective_matches_config"] is True
    assert data["restart_required"] is False


# --- review round (#330) -------------------------------------------------------------

UNRESOLVED = {"name": "unresolved", "is_jev": False, "model": None}


def _client_with_start(tmp_path, startup_choice, effective=UNRESOLVED):
    app = Flask(__name__)
    runtime = None if startup_choice is None else (lambda: {"classifier_choice": startup_choice})
    app.register_blueprint(
        create_config_blueprint(
            _store(tmp_path),
            effective_classifier_provider=lambda: effective,
            runtime_settings_provider=runtime,
        )
    )
    return app.test_client()


def test_another_choice_saved_before_the_first_turn_needs_a_restart(tmp_path, sokudan_missing):
    """Started on "auto" (unresolved until the first turn), then "rule" was saved."""
    client = _client_with_start(tmp_path, "auto")
    assert client.get("/api/config/jev").json["restart_required"] is False
    assert client.post("/api/config/jev", json={"classifier": "rule"}).status_code == 200
    data = client.get("/api/config/jev").json
    assert data["startup_choice"] == "auto" and data["current"] == "rule"
    assert data["effective_matches_config"] is None
    assert data["restart_required"] is True
    # Saved back to what the server started with: no restart needed any more.
    client.post("/api/config/jev", json={"classifier": "auto"})
    assert client.get("/api/config/jev").json["restart_required"] is False


def test_unresolved_with_an_unknown_start_choice_is_unknown(tmp_path):
    data = _client_with_start(tmp_path, None).get("/api/config/jev").json
    assert data["startup_choice"] is None
    assert data["restart_required"] is None


@pytest.mark.parametrize("method", ["get", "post"])
def test_an_unreadable_config_gives_a_fixed_error_without_its_text(tmp_path, method):
    (tmp_path / "gateway.json").write_text("{not json", encoding="utf-8")
    client = _client(tmp_path)
    response = (
        client.get("/api/config/jev")
        if method == "get"
        else client.post("/api/config/jev", json={"classifier": "rule"})
    )
    assert response.status_code == 503
    assert response.json == {"error": "Config unavailable.", "error_code": "config_unavailable"}
    assert str(tmp_path) not in response.get_data(as_text=True)
