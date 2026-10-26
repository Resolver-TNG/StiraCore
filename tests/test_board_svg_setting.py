"""#453: static SVG on the Stage board (gateway ``ui.board_svg``), **on by default** since the
ADR 0012 addendum of 2026-10-24 ("show it and let people choose"; the file says ``false`` only
when a person turned it off).

The setting is a settings-hub group (``board.svg``): turning it off needs no confirmation,
turning it (back) on is a danger the operator confirms (``acknowledged_dangers:
["board_svg"]``), every change is in the settings history and can be rolled back, and the
Stage page carries ``data-board-svg="on"`` only while it is on. The
CSP of the page is unchanged (the picture is a ``data:`` image, already allowed by img-src).
The sanitiser itself is tested in ``tests/stage_svg_test.cjs``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import Flask

from stiracore.config import GatewayConfig
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.observability.security import CSP

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)


def _store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )


@pytest.fixture
def store(tmp_path):
    return _store(tmp_path)


@pytest.fixture
def client(store):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    return app.test_client()


def _state(client):
    return client.get("/api/config/hub").get_json()


def _save(client, changes, **extra):
    return client.post(
        "/api/config/hub",
        json={"changes": changes, "base_version": _state(client)["version"], **extra},
    )


# --- the gateway value ------------------------------------------------------------------------


def test_ui_board_svg_is_a_boolean_kept_only_when_off():
    from stiracore.config.models import ui_board_svg

    assert GatewayConfig().ui == {"home": "chat"}
    assert ui_board_svg(GatewayConfig().ui) is True  # the default: on
    assert GatewayConfig(ui={"home": "chat", "board_svg": True}).ui == {"home": "chat"}
    off = GatewayConfig(ui={"board_svg": False}).ui
    assert off == {"home": "chat", "board_svg": False} and ui_board_svg(off) is False
    for bad in ({"board_svg": 1}, {"board_svg": "true"}, {"board_svg": None}, {"svg": True}):
        with pytest.raises(ValueError):
            GatewayConfig(ui=bad)


def test_a_file_without_board_svg_is_written_back_unchanged(tmp_path):
    path = tmp_path / "gateway-config.json"
    GatewayConfig(path=path).save()
    assert json.loads(path.read_text(encoding="utf-8"))["ui"] == {"home": "chat"}


# --- the hub group ----------------------------------------------------------------------------


def test_on_by_default_and_not_a_standing_danger(client):
    state = _state(client)
    assert state["values"]["board"] == {"svg": True}
    assert state["defaults"]["board"] == {"svg": True}
    assert "board_svg" not in state["dangers"]
    assert state["apply"]["board"] == {"status": "saved", "applies": "reload"}
    off, on = {**hub.default_values(), "board": {"svg": False}}, hub.default_values()
    assert hub.dangers(on) == [] and hub.dangers(off) == []
    # Only turning it on (from off) is confirmed; turning it off is a tightening.
    assert hub.new_dangers(off, on) == ["board_svg"]
    assert hub.new_dangers(on, off) == []
    assert hub.new_dangers(on, on) == []


def test_turning_off_needs_no_confirmation_and_on_again_does(client, store):
    off = _save(client, {"board": {"svg": False}})
    assert off.status_code == 200, off.get_json()
    data = json.loads(store.gateway_path.read_text(encoding="utf-8"))
    assert data["ui"] == {"home": "chat", "board_svg": False}
    refused = _save(client, {"board": {"svg": True}})
    assert refused.status_code == 400
    assert refused.get_json()["needs_confirmation"] == ["board_svg"]
    assert json.loads(store.gateway_path.read_text(encoding="utf-8"))["ui"]["board_svg"] is False
    validate = client.post("/api/config/hub/validate", json={"changes": {"board": {"svg": True}}})
    assert validate.get_json()["needs_confirmation"] == ["board_svg"]


def test_off_is_saved_in_gateway_ui_and_in_the_history_and_can_be_rolled_back(client, store):
    saved = _save(client, {"board": {"svg": False}})
    assert saved.status_code == 200, saved.get_json()
    body = saved.get_json()
    assert body["saved"] == ["board"]
    assert body["state"]["values"]["board"] == {"svg": False}
    assert body["state"]["dangers"] == []
    data = json.loads(store.gateway_path.read_text(encoding="utf-8"))
    assert data["ui"] == {"home": "chat", "board_svg": False}
    history = client.get("/api/config/history").get_json()["entries"]
    assert history[0]["changes"] == [{"field": "board.svg", "from": True, "to": False}]
    # rolling the "off" back (= on again) is a loosening: it asks
    version = _state(client)["version"]
    refused = client.post(
        f"/api/config/history/{history[0]['id']}/rollback", json={"base_version": version}
    )
    assert refused.status_code == 400
    assert refused.get_json()["needs_confirmation"] == ["board_svg"]
    rolled = client.post(
        f"/api/config/history/{history[0]['id']}/rollback",
        json={"base_version": version, "acknowledged_dangers": ["board_svg"]},
    )
    assert rolled.status_code == 200, rolled.get_json()
    assert rolled.get_json()["state"]["values"]["board"] == {"svg": True}
    # on is the default again: the file no longer names it
    assert json.loads(store.gateway_path.read_text(encoding="utf-8"))["ui"] == {"home": "chat"}
    # rolling the "on" back (= off again) is a tightening: no confirmation
    entries = client.get("/api/config/history").get_json()["entries"]
    again = client.post(
        f"/api/config/history/{entries[0]['id']}/rollback",
        json={"base_version": _state(client)["version"]},
    )
    assert again.status_code == 200, again.get_json()
    assert again.get_json()["state"]["values"]["board"] == {"svg": False}


def test_other_gateway_saves_keep_the_board_value(client, store):
    _save(client, {"board": {"svg": False}})
    saved = _save(client, {"classifier": {"choice": "rule"}})
    assert saved.status_code == 200, saved.get_json()
    data = json.loads(store.gateway_path.read_text(encoding="utf-8"))
    assert data["ui"].get("board_svg") is False
    assert data["jev_classifier"] == "rule"


# One save of several groups, and the other save routes (review of #485, major 1): the board
# value never overwrites another group's change, and no other save drops the board value.
# ``hub`` cases change the group in the *same* hub save as ``board.svg: off``; ``route`` cases
# save through their own route while the board is off. Then the board is turned on again alone
# (confirmed), and the other value must still be there.
def _model_target():
    from stiracore.agent.model_factory import DEFAULT_MODELS

    return min(set(DEFAULT_MODELS.values()) - {GatewayConfig().model_id})


_SAME_SAVE = {
    "classifier": (
        {"classifier": {"choice": "rule"}},
        lambda s: (s.load()[0].jev_enabled, s.load()[0].jev_classifier) == (True, "rule"),
    ),
    "tools": (
        {"tools": {"mode": "custom", "enabled_tools": ["file_read"]}},
        lambda s: (s.load_tools().mode, s.load_tools().enabled_tools) == ("custom", {"file_read"}),
    ),
    "breaker": (
        {"breaker": {"stability_warning": 0.55}},
        lambda s: s.load()[1].stira_config_overrides["stability_warning"] == 0.55,
    ),
    "write": (
        {"write": {"roots": list(hub.WRITE_SCOPES["workspace_tmp"])}},
        lambda s: s.load()[1].write_roots == hub.WRITE_SCOPES["workspace_tmp"],
    ),
}
_ROUTES = {
    "model": (
        lambda: ("/api/config/models", {"model_id": _model_target()}),
        lambda s: s.load()[1].model_override == _model_target(),
    ),
    "jev": (
        lambda: ("/api/config/jev", {"classifier": "rule"}),
        lambda s: s.load()[0].jev_classifier == "rule" and s.load()[0].jev_enabled,
    ),
    "tool_rule": (
        lambda: ("/api/config/tools", {"name": "file_read", "enabled": False}),
        lambda s: "file_read" not in s.load_tools().enabled_tools,
    ),
    "tool_page": (
        lambda: ("/api/tools/config", {"mode": "custom", "enabled_tools": ["file_read"]}),
        lambda s: s.load_tools().enabled_tools == {"file_read"},
    ),
    "yolo": (
        lambda: ("/api/yolo/settings", "yolo"),
        lambda s: s.load()[0].yolo == {"max_exec": 10},
    ),
}


@pytest.fixture
def full_client(store):
    """The config routes and the YOLO settings route (a narrowing save needs no host)."""
    from stiracore.yolo import settings_api

    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: "normal"))
    settings_api.install(
        app,
        config_store=store,
        turn_running=lambda: False,
        wildcard_bind=lambda: False,
        environ={},
    )
    return app.test_client()


def _board_on(store) -> bool:
    from stiracore.config.models import ui_board_svg

    gateway, _agent = store.load()
    return ui_board_svg(gateway.ui)


@pytest.mark.parametrize(
    ("kind", "name"),
    [("hub", name) for name in _SAME_SAVE] + [("route", name) for name in _ROUTES],
)
def test_board_and_every_other_save_keep_each_other(full_client, store, kind, name):
    client = full_client
    if kind == "hub":
        changes, check = _SAME_SAVE[name]
        # An added write root is confirmed too (#536).
        extra = ["write_roots_widened"] if "write" in changes else []
        saved = _save(
            client,
            {**changes, "board": {"svg": False}},
            acknowledged_dangers=extra,
        )
        assert saved.status_code == 200, saved.get_json()
        assert saved.get_json()["saved"] == [g for g in hub.GROUPS if g in (*changes, "board")]
    else:
        request, check = _ROUTES[name]
        off = _save(client, {"board": {"svg": False}})
        assert off.status_code == 200, off.get_json()
        route, body = request()
        if body == "yolo":
            version = client.get(route).get_json()["version"]
            body = {"values": {"max_exec": 10}, "base_version": version}
        response = client.post(route, json=body)
        assert response.status_code == 200, response.get_json()
    assert check(store), name
    assert not _board_on(store), name
    on = _save(client, {"board": {"svg": True}}, acknowledged_dangers=["board_svg"])
    assert on.status_code == 200, on.get_json()
    assert on.get_json()["saved"] == ["board"]
    assert check(store), name
    assert _board_on(store)


def test_not_a_boolean_is_refused(client):
    response = _save(client, {"board": {"svg": "yes"}}, acknowledged_dangers=["board_svg"])
    assert response.status_code == 400
    assert {"field": "board.svg", "code": "not_boolean", "params": {}} in response.get_json()[
        "errors"
    ]
    assert _save(client, {"board": {"other": True}}).status_code == 400


# --- the Stage page ---------------------------------------------------------------------------


def _stage_app(tmp_path, monkeypatch, ui):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    monkeypatch.setenv("STIRA_STAGE", "1")
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    store = _store(tmp_path)
    if ui is not None:
        GatewayConfig(ui=ui, path=store.gateway_path).save()
    return stira_server.create_app(config_store=store), store


@pytest.mark.parametrize(
    ("ui", "on"),
    [
        (None, True),
        ({"home": "chat"}, True),
        ({"board_svg": True}, True),
        ({"board_svg": False}, False),
    ],
)
def test_stage_page_marks_the_setting_and_keeps_the_csp(tmp_path, monkeypatch, ui, on):
    app, _ = _stage_app(tmp_path, monkeypatch, ui)
    page = app.test_client().get("/stage/")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert ('data-board-svg="on"' in html) is on
    assert page.headers["Content-Security-Policy"] == CSP == EXPECTED_CSP
    assert "unsafe-eval" not in CSP


def test_stage_page_reads_the_setting_on_each_load(tmp_path, monkeypatch):
    app, store = _stage_app(tmp_path, monkeypatch, None)
    client = app.test_client()
    assert 'data-board-svg="on"' in client.get("/stage/").get_data(as_text=True)  # default
    GatewayConfig(ui={"board_svg": False}, path=store.gateway_path).save()
    assert 'data-board-svg="on"' not in client.get("/stage/").get_data(as_text=True)
    GatewayConfig(ui={"board_svg": True}, path=store.gateway_path).save()
    assert 'data-board-svg="on"' in client.get("/stage/").get_data(as_text=True)
    # an unreadable file is off (never on by accident)
    store.gateway_path.write_text("{not json", encoding="utf-8")
    assert 'data-board-svg="on"' not in client.get("/stage/").get_data(as_text=True)


def test_register_stage_without_a_working_reader_is_off(monkeypatch):
    import stiracore.observability.stage as stage_mod

    root = Path(__file__).resolve().parents[1]
    captured = {}
    monkeypatch.setattr(stage_mod, "render_template", lambda name, **kw: captured.update(kw) or "")

    def broken():
        raise OSError("x")

    for reader in (None, broken, lambda: "yes", lambda: 1):
        app = Flask(__name__)
        stage_mod.register_stage(app, root / "dashboard" / "stage", board_svg=reader)
        captured.clear()
        with app.test_request_context("/stage/"):
            app.view_functions["stage_page"]()
        assert captured == {"board_svg": False}, reader
