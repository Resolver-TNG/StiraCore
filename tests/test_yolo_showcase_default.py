"""ADR 0012 addendum 2026-10-24: the YOLO button is shown by default ("show it and let people
choose"), and nothing more.

* Unset (no variable, no ``yolo.allow``): the allowance is ``external`` — ``GET
  /api/yolo/status`` answers ``offered`` with ``sources.allow == "default"``, the chat and
  Stage pages carry the button's slot.
* ``STIRA_YOLO_ALLOW=off`` or ``"yolo": {"allow": "off"}``: no status route, no button.
* The variable wins over the config in both directions.
* Before a press nothing is approved: no request, no window, the approver answers nothing,
  and the default never offers "Include exec".
* On a wildcard listener the default is ``off`` (the settings page shows it so).
"""

from __future__ import annotations

import hashlib
import json
import sys
from unittest.mock import Mock

import pytest

from stiracore.observability.config_api import ConfigStore
from stiracore.yolo import origin, own_dashboard, runtime
from stiracore.yolo.allowance import DEFAULT_ALLOW, parse_settings
from stiracore.yolo.config_file import DEFAULTS
from stiracore.yolo.settings_api import describe

YOLO_ENV = (
    "STIRA_YOLO_ALLOW",
    "STIRA_YOLO_MAX_AUTO_APPROVALS",
    "STIRA_YOLO_MAX_EXTERNAL",
    "STIRA_YOLO_MAX_EXEC",
    "STIRA_YOLO_HOST_CONFIRM",
    "STIRA_ENABLE_EXEC_TOOLS",
)
MAC = sys.platform == "darwin"  # the only platform whose clocks were measured (§7)


@pytest.fixture(autouse=True)
def _unset(monkeypatch):
    for name in YOLO_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    made = []
    monkeypatch.setenv("STIRA_STAGE", "1")
    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )

    def make(section=None):
        root = tmp_path / str(len(made))
        root.mkdir()
        monkeypatch.setattr(stira_server, "DATA_DIR", root / "data")
        gateway = root / "gw.json"
        if section is not None:
            gateway.write_text(json.dumps({"yolo": section}), "utf-8")
        runtime._reset_for_tests()
        store = ConfigStore(gateway, root / "agent.json", root / "tools.json")
        app = stira_server.create_app(config_store=store)
        made.append(app)
        return app

    try:
        yield make
    finally:
        for app in made:
            app.extensions["lifecycle"].shutdown()
        runtime._reset_for_tests()


def test_the_default_is_external_from_the_default():
    assert DEFAULT_ALLOW == "external" == DEFAULTS["allow"]
    settings = parse_settings({})
    assert settings.enabled and settings.allowance == "external"
    assert settings.sources["allow"] == "default"
    assert settings.allows("internal") and settings.allows("external")
    # Caps and the confirmation as before (ADR 0012 Open (5), §8b revision 6).
    assert (settings.cap, settings.external_cap, settings.exec_cap) == (2000, 200, 200)
    assert settings.host_confirm == "browser"


def test_the_default_never_offers_exec_an_explicit_external_does():
    assert parse_settings({"STIRA_ENABLE_EXEC_TOOLS": "1"}).exec_offered is False
    explicit = {"STIRA_ENABLE_EXEC_TOOLS": "1", "STIRA_YOLO_ALLOW": "external"}
    assert parse_settings(explicit).exec_offered is True
    by_config = parse_settings({"STIRA_ENABLE_EXEC_TOOLS": "1"}, {"allow": "external"})
    assert by_config.exec_offered is True and by_config.sources["allow"] == "config"


@pytest.mark.parametrize(
    ("env", "section", "enabled", "source"),
    [
        ({}, None, True, "default"),
        ({"STIRA_YOLO_ALLOW": "off"}, None, False, "env"),
        ({}, {"allow": "off"}, False, "config"),
        ({"STIRA_YOLO_ALLOW": "off"}, {"allow": "external"}, False, "env"),  # env wins
        ({"STIRA_YOLO_ALLOW": "internal"}, {"allow": "off"}, True, "env"),  # env wins
        ({"STIRA_YOLO_ALLOW": ""}, {"allow": "off"}, False, "config"),  # empty = unset
    ],
)
def test_precedence_env_config_default(env, section, enabled, source):
    settings = parse_settings(env, section)
    assert settings.enabled is enabled
    assert settings.sources["allow"] == source


@pytest.mark.parametrize("page", ["/chat?lang=en", "/stage/"])
def test_the_default_shows_the_button_and_status_says_where_it_came_from(make_app, page):
    app = make_app()
    client = app.test_client()
    html = client.get(page).get_data(as_text=True)
    assert "yolo_ui.js" in html and "yolo-slot" in html
    status = client.get("/api/yolo/status")
    assert status.status_code == 200
    body = status.get_json()
    assert body["allowance"] == "external"
    assert body["sources"]["allow"] == "default"
    assert body["exec_offered"] is False
    assert body["offered"] is MAC, body["unavailable"]
    # Nothing is approved before a press: no request, no window, nothing latched.
    assert body["request"] is None and body["window"] is None and body["latched"] is False


def test_before_a_press_the_approver_answers_nothing(make_app):
    make_app()
    digest = hashlib.sha256(b"{}").hexdigest()
    with origin.human_turn("default"):
        grant, reason = runtime.APPROVER.approve(
            tool_name="file_write",
            args_digest=digest,
            tool_use_id="x",
            level="normal",
            agent_id="default",
        )
    assert grant is None and reason


@pytest.mark.parametrize(
    "how", [("env", {"STIRA_YOLO_ALLOW": "off"}, None), ("config", {}, {"allow": "off"})]
)
def test_off_removes_the_button_and_the_status_route(make_app, monkeypatch, how):
    _name, env, section = how
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = make_app(section)
    client = app.test_client()
    for page in ("/chat?lang=en", "/stage/"):
        html = client.get(page).get_data(as_text=True)
        assert "yolo_ui.js" not in html and "yolo-slot" not in html, page
    assert client.get("/api/yolo/status").status_code == 404
    assert not runtime.enabled()


def test_the_settings_page_reads_external_by_default_and_off_on_a_wildcard():
    plain = describe(None, environ={}, wildcard_bind=False)["fields"]["allow"]
    assert (plain["value"], plain["source"], plain["default"]) == (
        "external",
        "default",
        "external",
    )
    wildcard = describe(None, environ={}, wildcard_bind=True)["fields"]["allow"]
    assert (wildcard["value"], wildcard["source"], wildcard["default"]) == (
        "off",
        "default",
        "off",
    )
    saved_off = describe({"allow": "off"}, environ={}, wildcard_bind=False)["fields"]["allow"]
    assert (saved_off["value"], saved_off["source"]) == ("off", "config")
    pinned = describe(None, environ={"STIRA_YOLO_ALLOW": "off"}, wildcard_bind=False)
    assert pinned["fields"]["allow"]["pinned"] is True
    assert pinned["fields"]["allow"]["value"] == "off"
