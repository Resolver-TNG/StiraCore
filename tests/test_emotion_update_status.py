"""#367: ``/api/gateway/status`` says which classifier runs and why emotions did not move.

Additive fields only: ``classifier.mode`` / ``classifier.fallback`` and ``emotion_updates``.
Codes outside the fixed vocabularies never reach the response (allow-list, no free text).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from stiracore.agent.stimulus_classifier import (
    NO_UPDATE_REASONS,
    RuleBasedClassifier,
    SokudanClassifier,
)
from tests.test_config_location import _Agent

RULE_ONLY = "WARNING: emotion classification is rule-only"


def _start(monkeypatch, tmp_path, *, classifier=None, updates=None, choice=None, sokudan=False):
    from dashboard import stira_server
    from stiracore.config import models
    from stiracore.observability import config_api
    from stiracore.tools import tool_config

    repo = tmp_path / "repo"
    data_dir = repo / ".local" / "stira"
    data_dir.mkdir(parents=True)
    if choice is not None:
        config = {"jev_enabled": True, "jev_classifier": choice}
        (data_dir / "gateway-config.json").write_text(json.dumps(config), encoding="utf-8")

    class Agent(_Agent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._stimulus_classifier = (
                classifier if classifier is not None else kwargs.get("stimulus_classifier")
            )

        @property
        def emotion_updates(self):
            return updates

    monkeypatch.setattr(config_api, "DEFAULT_TOOL_CONFIG_PATH", tool_config.DEFAULT_PATH)
    monkeypatch.setattr(
        config_api, "DEFAULT_GATEWAY_CONFIG_PATH", models.DEFAULT_GATEWAY_CONFIG_PATH
    )
    monkeypatch.setattr(config_api, "DEFAULT_AGENT_CONFIG_PATH", models.DEFAULT_AGENT_CONFIG_PATH)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "NXPAgent", Agent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    monkeypatch.setattr(stira_server, "_sokudan_installed", lambda: sokudan)
    return stira_server.create_app()


def _status(app) -> dict:
    return app.test_client().get("/api/gateway/status").get_json()


def test_rule_fallback_is_reported(monkeypatch, tmp_path):
    status = _status(_start(monkeypatch, tmp_path, classifier=RuleBasedClassifier()))
    classifier = status["classifier"]
    assert classifier["mode"] == "rule_only"
    # "auto" (the default) asked for Sokudan when installed: rules are the fallback.
    assert classifier["fallback"] is True
    # Existing fields unchanged.
    assert (classifier["name"], classifier["is_jev"], classifier["model"]) == (
        "RuleBasedClassifier",
        False,
        None,
    )


def test_an_explicit_rule_choice_is_not_a_fallback(monkeypatch, tmp_path):
    app = _start(monkeypatch, tmp_path, choice="rule")
    classifier = _status(app)["classifier"]
    assert classifier["mode"] == "rule_only"
    assert classifier["fallback"] is False


def test_sokudan_is_reported(monkeypatch, tmp_path):
    app = _start(monkeypatch, tmp_path, classifier=SokudanClassifier(lazy=True), sokudan=True)
    classifier = _status(app)["classifier"]
    assert (classifier["mode"], classifier["fallback"], classifier["is_jev"]) == (
        "sokudan",
        False,
        True,
    )


def test_before_the_first_turn_the_mode_is_unknown(monkeypatch, tmp_path):
    classifier = _status(_start(monkeypatch, tmp_path))["classifier"]
    assert classifier["name"] == "unresolved"
    assert (classifier["mode"], classifier["fallback"]) == ("unknown", None)


def test_emotion_updates_are_counted(monkeypatch, tmp_path):
    updates = {
        "turns": 19,
        "updated": 7,
        "reasons": {**dict.fromkeys(NO_UPDATE_REASONS, 0), "no_stimulus": 12},
        "last_reason": "no_stimulus",
    }
    status = _status(
        _start(monkeypatch, tmp_path, classifier=RuleBasedClassifier(), updates=updates)
    )
    assert status["emotion_updates"] == {
        "turns": 19,
        "updated": 7,
        "not_updated": 12,
        "reasons": {**dict.fromkeys(NO_UPDATE_REASONS, 0), "no_stimulus": 12},
        "last": {"updated": False, "reason": "no_stimulus"},
    }


def test_an_updated_last_turn(monkeypatch, tmp_path):
    updates = {
        "turns": 1,
        "updated": 1,
        "reasons": dict.fromkeys(NO_UPDATE_REASONS, 0),
        "last_reason": None,
    }
    status = _status(_start(monkeypatch, tmp_path, updates=updates))
    assert status["emotion_updates"]["last"] == {"updated": True, "reason": None}


def test_no_turn_yet(monkeypatch, tmp_path):
    updates = {
        "turns": 0,
        "updated": 0,
        "reasons": dict.fromkeys(NO_UPDATE_REASONS, 0),
        "last_reason": None,
    }
    status = _status(_start(monkeypatch, tmp_path, updates=updates))
    assert status["emotion_updates"]["turns"] == 0
    assert status["emotion_updates"]["last"] is None


def test_codes_outside_the_vocabulary_never_reach_the_response(monkeypatch, tmp_path):
    updates = {
        "turns": 3,
        "updated": 1,
        "reasons": {"no_stimulus": 1, "user said <script>": 1, "classifier_error": "x"},
        "last_reason": "free text from somewhere",
    }
    view = _status(_start(monkeypatch, tmp_path, updates=updates))["emotion_updates"]
    assert set(view["reasons"]) == set(NO_UPDATE_REASONS)
    assert view["reasons"]["no_stimulus"] == 1
    assert view["reasons"]["classifier_error"] == 0
    assert view["last"] == {"updated": False, "reason": "unknown"}


def test_an_agent_without_counts_reports_null(monkeypatch, tmp_path):
    assert _status(_start(monkeypatch, tmp_path))["emotion_updates"] is None


def test_start_up_warns_once_when_rules_are_the_fallback(monkeypatch, tmp_path, capsys):
    _start(monkeypatch, tmp_path, sokudan=False)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith(RULE_ONLY)]
    assert len(lines) == 1
    assert '".[jev]"' in lines[0]


def test_start_up_is_quiet_with_sokudan_or_an_explicit_rule_choice(monkeypatch, tmp_path, capsys):
    _start(monkeypatch, tmp_path / "a", sokudan=True)
    assert RULE_ONLY not in capsys.readouterr().out
    _start(monkeypatch, tmp_path / "b", choice="rule")
    assert RULE_ONLY not in capsys.readouterr().out


def test_a_choice_saved_while_running_does_not_change_the_running_fallback(monkeypatch, tmp_path):
    """Review M-1: the classifier is resolved once at start-up (#227, "applies at next start").

    Saving ``rule`` while the server runs under ``auto`` does not swap the classifier, so
    the status keeps describing the one that runs (rules because Sokudan is missing) and
    the settings say the change is pending until a restart.
    """
    app = _start(monkeypatch, tmp_path, classifier=RuleBasedClassifier())
    client = app.test_client()
    token = client.get("/api/csrf").get_json()["token"]
    saved = client.post(
        "/api/config/jev", json={"classifier": "rule"}, headers={"X-CSRF-Token": token}
    )
    assert saved.status_code == 200, saved.get_data(as_text=True)
    jev = client.get("/api/config/jev").get_json()
    assert (jev["current"], jev["startup_choice"]) == ("rule", "auto")
    assert jev["applies"] == "next_start"
    classifier = client.get("/api/gateway/status").get_json()["classifier"]
    assert (classifier["mode"], classifier["fallback"]) == ("rule_only", True)


def test_an_unreadable_config_still_warns_truthfully(monkeypatch, tmp_path, capsys):
    """Review M-1: with no readable choice the agent resolves as ``auto`` (rules without
    Sokudan), so the start-up line describes what runs."""
    from dashboard import stira_server

    def broken(*_args, **_kwargs):
        raise ValueError("broken gateway config")

    monkeypatch.setattr(stira_server, "classifier_choice", broken)
    _start(monkeypatch, tmp_path, sokudan=False)
    assert RULE_ONLY in capsys.readouterr().out
