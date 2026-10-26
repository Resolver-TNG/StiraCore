"""ADR 0012 phase 7c: the YOLO values from ``gateway-config.json`` and the settings page.

* Precedence per value: environment > config > default; where each value came from.
* A hand-edited section never refuses start-up: rounded / left out with a warning, an
  unknown allowance is ``off``.
* ``GET/POST /api/yolo/settings`` exist with and without the allowance; a loosening save
  is confirmed (the page's confirmation without the allowance, the host gate with it);
  refused while a turn / tool call runs, while a window is open, for a pinned value;
  the allowance is used at the next start, the caps from the next window.
* Every other save path still cannot write ``yolo*``; the hub's gateway saves carry the
  section over unchanged. With the allowance off nothing changes (no button, no badge);
  unset is the external default (ADR 0012 addendum 2026-10-24).
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from stiracore.observability.config_api import ConfigStore
from stiracore.yolo import own_dashboard, runtime
from stiracore.yolo.allowance import (
    YoloSettingError,
    YoloSettings,
    parse_settings,
    resolve_values,
)
from stiracore.yolo.config_file import clean, validate
from stiracore.yolo.settings_api import ROUTE, loosenings
from tests.test_new_session import make_stira

BROWSER = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}
YOLO_ENV = (
    "STIRA_YOLO_ALLOW",
    "STIRA_YOLO_MAX_AUTO_APPROVALS",
    "STIRA_YOLO_MAX_EXTERNAL",
    "STIRA_YOLO_MAX_EXEC",
    "STIRA_YOLO_HOST_CONFIRM",
    "STIRA_HOST",
)


@pytest.fixture(autouse=True)
def _no_yolo_env(monkeypatch):
    for name in YOLO_ENV:
        monkeypatch.delenv(name, raising=False)


# --- precedence and the hand-edited section ----------------------------------------------


def test_precedence_is_env_then_config_then_default():
    values, sources, warnings = resolve_values(
        {"STIRA_YOLO_MAX_AUTO_APPROVALS": "50"},
        {"allow": "internal", "max_auto_approvals": 900, "max_external": 10},
    )
    assert values["allow"] == "internal" and sources["allow"] == "config"
    assert values["max_auto_approvals"] == 50 and sources["max_auto_approvals"] == "env"
    assert values["max_external"] == 10 and sources["max_external"] == "config"
    assert values["max_exec"] == 50 and sources["max_exec"] == "default"  # follows the cap
    assert values["host_confirm"] == "browser" and sources["host_confirm"] == "default"
    assert warnings == []


def test_no_section_and_no_variable_is_the_external_default():
    # ADR 0012 addendum 2026-10-24: shown by default; "off" in the file or the variable.
    for section in ({}, None):
        settings = parse_settings({}, section)
        assert settings.enabled and settings == YoloSettings("external")
        assert settings.sources["allow"] == "default"
    assert parse_settings({}, {"allow": "off"}) == YoloSettings()
    assert parse_settings({}, {"allow": "off"}).sources["allow"] == "config"
    assert not parse_settings({"STIRA_YOLO_ALLOW": "off"}, {"allow": "external"}).enabled
    # The variable wins over the file in both directions.
    on = parse_settings({"STIRA_YOLO_ALLOW": "internal"}, {"allow": "off"})
    assert on.allowance == "internal" and on.sources["allow"] == "env"


def test_config_allowance_enables_and_env_still_refuses_as_before():
    on = parse_settings({}, {"allow": "external", "max_exec": 5})
    assert on.enabled and on.allowance == "external" and on.exec_cap == 5
    assert on.sources["allow"] == "config"
    # A cap variable while the allowance is off still refuses (it would suggest YOLO is on).
    with pytest.raises(YoloSettingError, match="is set but the YOLO allowance is off"):
        parse_settings({"STIRA_YOLO_MAX_EXEC": "3"}, {"allow": "off"})
    with pytest.raises(YoloSettingError, match="is set but the YOLO allowance is off"):
        parse_settings({"STIRA_YOLO_ALLOW": "off", "STIRA_YOLO_MAX_EXEC": "3"}, {})
    # ... and is fine with the default allowance (unset, no section).
    assert parse_settings({"STIRA_YOLO_MAX_EXEC": "3"}, {}).exec_cap == 3
    # ... and is fine when the allowance comes from the config.
    assert parse_settings({"STIRA_YOLO_MAX_EXEC": "3"}, {"allow": "internal"}).exec_cap == 3
    # Invalid variables refuse whatever the config says.
    with pytest.raises(YoloSettingError):
        parse_settings({"STIRA_YOLO_ALLOW": "on"}, {"allow": "internal"})
    # A sub-cap variable above the cap variable / default still refuses.
    with pytest.raises(YoloSettingError, match="must not exceed"):
        parse_settings({"STIRA_YOLO_ALLOW": "internal", "STIRA_YOLO_MAX_EXTERNAL": "3000"})


def test_a_config_sub_cap_above_the_cap_is_rounded_with_a_warning():
    values, _sources, warnings = resolve_values(
        {"STIRA_YOLO_MAX_AUTO_APPROVALS": "20"}, {"allow": "internal", "max_external": 500}
    )
    assert values["max_external"] == 20 and warnings == ["max_external_above_cap"]


def test_offer_exec_false_takes_exec_out_of_the_dialog():
    base = {"STIRA_ENABLE_EXEC_TOOLS": "1"}
    assert parse_settings(base, {"allow": "external"}).exec_offered is True
    assert parse_settings(base, {"allow": "external", "offer_exec": False}).exec_offered is False
    # Never on without the exec tools' own variable (no UI turns those on).
    assert parse_settings({}, {"allow": "external", "offer_exec": True}).exec_offered is False


@pytest.mark.parametrize(
    "raw, values, warnings",
    [
        ({"allow": "on"}, {"allow": "off"}, ["allow_invalid"]),
        ({"allow": 1}, {"allow": "off"}, ["allow_invalid"]),
        ({"max_auto_approvals": 0}, {"max_auto_approvals": 1}, ["max_auto_approvals_rounded"]),
        ({"max_exec": 99999}, {"max_exec": 10000}, ["max_exec_rounded"]),
        ({"max_external": True}, {}, ["max_external_invalid"]),
        ({"max_external": 2.5}, {}, ["max_external_invalid"]),
        ({"offer_exec": "yes"}, {"offer_exec": False}, ["offer_exec_invalid"]),
        ({"host_confirm": "sms"}, {}, ["host_confirm_invalid"]),
        ({"tier": "external"}, {}, ["unknown_key"]),
        (["internal"], {}, ["section_invalid"]),
    ],
)
def test_a_hand_edited_section_never_refuses(raw, values, warnings):
    assert clean(raw) == (values, warnings)


def test_a_save_is_validated_exactly():
    assert validate({"allow": "internal", "max_auto_approvals": 10, "max_exec": 10}) == []
    codes = {
        e["field"]: e["code"]
        for e in validate(
            {"allow": "all", "offer_exec": 1, "max_auto_approvals": True, "host_confirm": "x"}
        )
    }
    assert codes == {
        "allow": "invalid_choice",
        "offer_exec": "not_boolean",
        "max_auto_approvals": "out_of_range",
        "host_confirm": "invalid_choice",
    }
    assert validate({"max_auto_approvals": 10, "max_external": 11})[0]["code"] == "exceeds_cap"
    assert validate({"max_external": 11}, cap=10)[0]["params"] == {"max": 10}


def test_loosening_codes():
    base, _s, _w = resolve_values({}, {"allow": "off"})
    assert loosenings(base, base) == []
    after, _s, _w = resolve_values(
        {},
        {
            "allow": "internal",
            "max_auto_approvals": 2001,
            "max_external": 201,
            "max_exec": 201,
            "offer_exec": True,
        },
    )
    assert loosenings(base, after) == [
        "yolo_allow",
        "yolo_cap",
        "yolo_external_cap",
        "yolo_exec_cap",
    ]
    command, _s, _w = resolve_values({}, {"allow": "off", "host_confirm": "command"})
    assert loosenings(base, command) == []  # browser → host: stricter
    assert loosenings(command, base) == ["yolo_host_confirm"]
    no_exec, _s, _w = resolve_values({}, {"allow": "off", "offer_exec": False})
    assert loosenings(no_exec, base) == ["yolo_offer_exec"]
    # ADR 0012 addendum 2026-10-24: off → the default (a removed "off") is a loosening;
    # the default → off is not.
    default, _s, _w = resolve_values({}, {})
    assert loosenings(base, default) == ["yolo_allow"]
    assert loosenings(default, base) == []


# --- the app ------------------------------------------------------------------------------


def _build(tmp_path, monkeypatch, settings, section=None):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

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
    gateway = tmp_path / "gw.json"
    if section is not None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        gateway.write_text(json.dumps({"yolo": section}), "utf-8")
    runtime._reset_for_tests(settings)
    store = ConfigStore(gateway, tmp_path / "agent.json", tmp_path / "tools.json")
    return stira_server.create_app(config_store=store)


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(own_dashboard, "armed", lambda: object())

    def make(settings=None, section=None):
        app = _build(tmp_path / str(len(made)), monkeypatch, settings, section)
        made.append(app)
        return app

    try:
        yield make
    finally:
        for app in made:
            app.extensions["lifecycle"].shutdown()
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


def _gw(app):
    path = app.extensions["config_store"].gateway_path
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def _get(client):
    response = client.get(ROUTE)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _save(client, values, **extra):
    version = _get(client)["version"]
    return client.post(ROUTE, json={"values": values, "base_version": version, **extra})


OFF = {"allow": "off"}  # ADR 0012 addendum 2026-10-24: unset is the external default


def test_with_allow_off_only_the_settings_routes_exist(make_app):
    app = make_app(section=OFF)
    rules = sorted(
        (r.rule, tuple(sorted(r.methods - {"HEAD", "OPTIONS"})))
        for r in app.url_map.iter_rules()
        if "yolo" in r.rule
    )
    assert rules == [(ROUTE, ("GET",)), (ROUTE, ("POST",))]
    client = app.test_client()
    assert client.get("/api/yolo/status").status_code == 404
    page = client.get("/chat").get_data(as_text=True)
    assert "yolo_ui.js" not in page and "yolo-slot" not in page  # no button, badge, poll
    body = _get(client)
    assert body["enabled"] is False and body["restart_pending"] == []
    allow = body["fields"]["allow"]
    assert allow == {
        "saved": "off",
        "value": "off",
        "source": "config",
        "running": "off",
        "default": "external",
        "env": "STIRA_YOLO_ALLOW",
        "pinned": False,
        "applies": "restart",
        "pending_restart": False,
    }
    assert body["fields"]["max_exec"]["applies"] == "next_window"
    assert body["fields"]["max_exec"]["min"] == 0 and body["fields"]["max_exec"]["max"] == 10000
    assert _gw(app) == {"yolo": OFF}  # a GET writes nothing


def test_enabling_needs_the_confirmation_and_applies_at_the_next_start(make_app):
    app = make_app(section=OFF)
    client = app.test_client()
    refused = _save(client, {"allow": "internal"})
    assert refused.status_code == 409
    assert refused.get_json()["needs_confirmation"] == ["yolo_allow"]
    assert _gw(app) == {"yolo": OFF}
    wrong = _save(client, {"allow": "internal"}, confirm=True, acknowledged=["yolo_cap"])
    assert wrong.status_code == 409 and _gw(app) == {"yolo": OFF}
    unticked = _save(client, {"allow": "internal"}, acknowledged=["yolo_allow"])
    assert unticked.status_code == 409 and _gw(app) == {"yolo": OFF}
    saved = _save(client, {"allow": "internal"}, confirm=True, acknowledged=["yolo_allow"])
    assert saved.status_code == 200, saved.get_json()
    body = saved.get_json()
    assert body["saved"] is True and body["loosened"] == ["yolo_allow"]
    assert body["restart_pending"] == ["allow"]
    assert body["fields"]["allow"]["value"] == "internal"
    assert body["fields"]["allow"]["running"] == "off"
    assert _gw(app)["yolo"] == {"allow": "internal"}
    # Nothing at run time: still no YOLO route, no window possible before the restart.
    assert not runtime.enabled()
    assert client.get("/api/yolo/status").status_code == 404


def test_the_next_start_reads_the_saved_allowance(make_app):
    app = make_app(section={"allow": "external", "max_auto_approvals": 30, "offer_exec": False})
    assert runtime.enabled()  # _reset_for_tests(None): parsed from the section at create_app
    status = app.test_client().get("/api/yolo/status").get_json()
    assert status["allowance"] == "external" and status["caps"]["auto_approvals"] == 30
    assert status["sources"]["allow"] == "config"
    assert status["settings"]["offer_exec"] is False and status["exec_offered"] is False


def test_a_narrowing_applies_without_a_confirmation(make_app):
    app = make_app(section={"allow": "external", "max_exec": 50})
    client = app.test_client()
    saved = _save(client, {"allow": "internal", "max_exec": 10})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["loosened"] == []
    assert _gw(app)["yolo"] == {"allow": "internal", "max_exec": 10}
    # null removes a value (its default applies): 10 → 200 is a loosening, and this server
    # has the allowance (read from the section), so the host gate asks.
    removed = _save(client, {"max_exec": None})
    assert removed.status_code == 428
    assert removed.get_json()["operation"]["summary"]["changes"] == ["yolo_exec_cap"]


@pytest.mark.parametrize(
    "values, field, code",
    [
        ({"allow": "all"}, "allow", "invalid_choice"),
        ({"max_auto_approvals": 0}, "max_auto_approvals", "out_of_range"),
        ({"max_auto_approvals": 10001}, "max_auto_approvals", "out_of_range"),
        ({"max_auto_approvals": True}, "max_auto_approvals", "out_of_range"),
        ({"max_external": 2001}, "max_external", "exceeds_cap"),
        ({"max_exec": -1}, "max_exec", "out_of_range"),
        ({"offer_exec": "true"}, "offer_exec", "not_boolean"),
        ({"host_confirm": "sms"}, "host_confirm", "invalid_choice"),
    ],
)
def test_invalid_values_are_refused_and_nothing_is_written(make_app, values, field, code):
    app = make_app()
    response = _save(app.test_client(), values, confirm=True, acknowledged=[])
    assert response.status_code == 400, response.get_json()
    assert [(e["field"], e["code"]) for e in response.get_json()["errors"]] == [(field, code)]
    assert _gw(app) == {}


def test_unknown_fields_and_bad_bodies_are_refused(make_app):
    client = make_app().test_client()
    version = _get(client)["version"]
    for body in (
        {"values": {"tier": "external"}, "base_version": version},
        {"values": {}, "base_version": version},
        {"values": {"allow": "internal"}, "base_version": version, "extra": 1},
        ["allow"],
    ):
        assert client.post(ROUTE, json=body).status_code == 400, body


def test_a_stale_version_is_refused(make_app):
    client = make_app().test_client()
    response = client.post(ROUTE, json={"values": {"max_exec": 1}, "base_version": "0" * 16})
    assert response.status_code == 409 and response.get_json()["code"] == "version_conflict"


def test_a_value_pinned_by_its_variable_cannot_be_saved(make_app, monkeypatch):
    app = make_app(section={"allow": "internal"})
    monkeypatch.setenv("STIRA_YOLO_MAX_EXEC", "5")
    client = app.test_client()
    body = _get(client)
    assert body["fields"]["max_exec"]["pinned"] is True
    assert body["fields"]["max_exec"]["source"] == "env"
    response = _save(client, {"max_exec": 1})
    assert response.status_code == 400
    assert response.get_json()["errors"][0]["code"] == "pinned_by_env"
    # Turning the allowance off while a cap variable is set: the next start would refuse.
    off = _save(client, {"allow": "off"})
    assert off.status_code == 400 and off.get_json()["errors"][0]["code"] == "env_needs_allow"


def test_the_allowance_cannot_be_saved_on_a_wildcard_listener(make_app, monkeypatch):
    app = make_app()
    monkeypatch.setenv("STIRA_HOST", "0.0.0.0")
    client = app.test_client()
    assert _get(client)["wildcard_bind"] is True
    response = _save(client, {"allow": "internal"}, confirm=True, acknowledged=["yolo_allow"])
    assert response.status_code == 400
    assert response.get_json()["errors"][0]["code"] == "wildcard_bind"
    assert _gw(app) == {}


def test_refused_while_a_turn_or_a_tool_call_runs(make_app):
    app = make_app()
    client = app.test_client()
    version = _get(client)["version"]
    body = {
        "values": {"allow": "internal"},
        "base_version": version,
        "confirm": True,
        "acknowledged": ["yolo_allow"],
    }
    lock = app.extensions["turn_lock"]
    with lock:
        response = client.post(ROUTE, json=body)
    assert response.status_code == 409 and response.get_json()["code"] == "turn_running"
    token = object()
    runtime.TRACKER.enter(token)
    try:
        response = client.post(ROUTE, json=body)
    finally:
        runtime.TRACKER.leave(token)
    assert response.status_code == 409 and response.get_json()["code"] == "tool_call_running"
    assert _gw(app) == {}
    assert client.post(ROUTE, json=body).status_code == 200  # a person's save afterwards


def test_the_hub_and_the_config_routes_cannot_write_the_section(make_app):
    app = make_app(section={"allow": "internal", "max_exec": 7})
    client = app.test_client()
    before = _gw(app)["yolo"]
    hub = client.get("/api/config/hub").get_json()
    for body in (
        {"changes": {"yolo": {"allow": "external"}}, "base_version": hub["version"]},
        {"changes": {"yolo.allow": "external"}, "base_version": hub["version"]},
    ):
        response = client.post("/api/config/hub", json=body)
        assert response.status_code in (400, 409), response.get_json()
    # A hub save of another group carries the section over unchanged.
    saved = client.post(
        "/api/config/hub",
        json={"changes": {"classifier": {"choice": "rule"}}, "base_version": hub["version"]},
    )
    assert saved.status_code == 200, saved.get_json()
    classifier = client.post("/api/config/jev", json={"classifier": "auto"})
    assert classifier.status_code == 200, classifier.get_json()
    assert _gw(app)["yolo"] == before


def test_an_agent_turn_cannot_save_without_the_allowance(tmp_path, monkeypatch):
    """The scripted model runs code inside its own turn (as a tool would): refused."""
    runtime._reset_for_tests(YoloSettings())
    gen = make_stira(tmp_path, monkeypatch)
    stira = next(gen)
    try:
        client = stira["client"]
        version = client.get(ROUTE).get_json()["version"]
        answers = []

        def during():
            answers.append(
                client.post(
                    ROUTE,
                    json={
                        "values": {"allow": "external"},
                        "base_version": version,
                        "confirm": True,
                        "acknowledged": ["yolo_allow"],
                    },
                    headers={"X-CSRF-Token": stira["token"]},
                )
            )

        stira["model"].during = during
        stira["chat"]("hello")
        assert answers and answers[0].status_code == 409
        assert answers[0].get_json()["code"] == "turn_running"
        assert client.get(ROUTE).get_json()["fields"]["allow"]["saved"] is None
    finally:
        gen.close()
        runtime._reset_for_tests()


# --- with the allowance: the host gate, the next window ----------------------------------


def _browser_confirm(client, answer):
    operation = answer["operation"]
    response = client.post(
        f"/api/host-operations/{operation['id']}/confirm",
        json={
            "token": operation["confirm_token"],
            "acknowledged": True,
            "instance": operation["instance"],
        },
        headers=BROWSER,
    )
    assert response.status_code == 200, response.get_json()
    return operation["id"]


def test_with_the_allowance_a_loosening_is_a_host_confirmed_operation(make_app):
    app = make_app(YoloSettings("internal", host_confirm="browser"))
    client = app.test_client()
    version = _get(client)["version"]
    body = {"values": {"max_auto_approvals": 2500}, "base_version": version}
    # The page's own confirm flag is not enough here: the host gate decides.
    first = client.post(ROUTE, json={**body})
    assert first.status_code == 428, first.get_json()
    answer = first.get_json()
    assert answer["operation"]["kind"] == "yolo_settings"
    assert answer["operation"]["summary"] == {"action": "yolo_settings", "changes": ["yolo_cap"]}
    assert _gw(app) == {}
    op_id = _browser_confirm(client, answer)
    again = client.post(ROUTE, json=body, headers={"X-Stira-Host-Operation": op_id})
    assert again.status_code == 200, again.get_json()
    assert again.get_json()["caps_applied"] is True
    assert _gw(app)["yolo"] == {"max_auto_approvals": 2500}
    status = client.get("/api/yolo/status").get_json()
    assert status["caps"]["auto_approvals"] == 2500  # the next window uses it
    # A second use of the same confirmation is refused.
    reused = client.post(ROUTE, json=body, headers={"X-Stira-Host-Operation": op_id})
    assert reused.status_code == 409


def test_an_open_window_refuses_every_save_and_keeps_its_caps(make_app):
    app = make_app(YoloSettings("internal", host_confirm="browser", cap=40))
    client = app.test_client()
    press = client.post("/api/yolo/request", json={"tier": "internal"})
    assert press.status_code == 202, press.get_json()
    data = press.get_json()
    confirmed = client.post(
        f"/api/host-operations/{data['request']['id']}/confirm",
        json={
            "token": data["confirm"]["token"],
            "acknowledged": True,
            "instance": data["confirm"]["instance"],
        },
        headers=BROWSER,
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    window = client.get("/api/yolo/status").get_json()["window"]
    assert window is not None and window["limit"] == 40
    assert _get(client)["busy"] is True
    narrowing = _save(client, {"max_auto_approvals": 10})
    assert narrowing.status_code == 409 and narrowing.get_json()["code"] == "yolo_busy"
    assert client.get("/api/yolo/status").get_json()["window"]["limit"] == 40
    # The object itself refuses too (checked under its own lock).
    assert runtime.desk().yolo.set_caps(10, 0, 0) is False
    assert runtime.apply_caps(10, 0, 0) is False
    client.post("/api/yolo/stop")
    saved = _save(client, {"max_auto_approvals": 10})
    assert saved.status_code == 200, saved.get_json()
    assert client.get("/api/yolo/status").get_json()["caps"]["auto_approvals"] == 10


def test_set_caps_checks_its_values():
    from stiracore.yolo.requests import YoloRequests

    desk = YoloRequests(
        allowance="internal",
        incarnation="0" * 32,
        record=None,
        actor="local:t",
        clocks_supported=False,
    )
    for bad in ((0, 0, 0), (True, 0, 0), (10, 11, 0), (10, 0, -1), (10.0, 0, 0), (10001, 0, 0)):
        assert desk.set_caps(*bad) is False, bad
    assert desk.set_caps(10, 5, 0) is True
    assert (desk.cap, desk.external_cap, desk.exec_cap) == (10, 5, 0)


def test_the_running_server_reads_the_allowance_once(make_app):
    """A saved allowance is used at the next start: never re-read at run time."""
    app = make_app(section={"allow": "internal"})
    assert runtime.current().allowance == "internal"
    client = app.test_client()
    version = _get(client)["version"]
    body = {"values": {"allow": "external"}, "base_version": version}
    first = client.post(ROUTE, json=body)
    assert first.status_code == 428, first.get_json()
    op_id = _browser_confirm(client, first.get_json())
    saved = client.post(ROUTE, json=body, headers={"X-Stira-Host-Operation": op_id})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["restart_pending"] == ["allow"]
    assert runtime.current().allowance == "internal"
    assert client.get("/api/yolo/status").get_json()["allowance"] == "internal"
    assert _gw(app)["yolo"] == {"allow": "external"}


# --- review round (PR #444): audit row, shared listen address, start-up lines -------------


def _settings_rows(app):
    store = app.extensions["yolo_settings_record"].__self__.store
    return [e.attributes for e in store.get_events(event_type="audit.yolo.settings_saved")]


# The row's random parts (#515): checked by their shape, never searched for a value.
_RANDOM_FIELDS = {"operation_id": 32, "incarnation_ref": 64}
_ROW_KEYS = {
    "schema", "operation_id", "phase", "actor_kind", "actor", "surface", "incarnation_ref",
    "key_id", "delta_class", "window_id", "generation_from", "generation_to",
    "authority_epoch", "fields", "changed",
}  # fmt: skip


def _assert_no_setting_value(row):
    """No value of the saved settings (``external``, ``500``) is anywhere in the row.

    The ids and hashes are random hex, so ``"500" in json.dumps(row)`` failed whenever one
    happened to contain it (#515). They are checked by their shape (and the actor against
    this OS account); everything else, keys included, keeps the substring check of the
    whole JSON. The row may not have any other key.
    """
    import os
    import re

    from stiracore.yolo.host_channel import principal_for

    rest = {k: v for k, v in row.items() if k not in _RANDOM_FIELDS and k != "actor"}
    text = json.dumps(rest)
    assert "external" not in text.replace('"max_external"', ""), text
    assert "500" not in text, text
    for key, size in _RANDOM_FIELDS.items():
        assert re.fullmatch(f"[0-9a-f]{{{size}}}", str(row.get(key))), (key, row.get(key))
    expected_actor = principal_for(os.getuid()) if hasattr(os, "getuid") else "local:unknown"
    assert row.get("actor") == expected_actor
    assert set(row) == _ROW_KEYS, set(row) ^ _ROW_KEYS
    assert [row["generation_from"], row["generation_to"], row["authority_epoch"]] == [0, 0, 0]


def test_a_loosening_save_writes_one_audit_row_with_field_names_only(make_app):
    app = make_app(section=OFF)
    client = app.test_client()
    saved = _save(
        client,
        {"allow": "external", "max_external": 500, "max_exec": 10},
        confirm=True,
        acknowledged=["yolo_allow", "yolo_external_cap"],
    )
    assert saved.status_code == 200, saved.get_json()
    rows = _settings_rows(app)
    assert len(rows) == 1
    row = rows[0]
    assert row["fields"] == ["allow", "max_external"]
    assert row["changed"] == ["allow", "max_exec", "max_external"]
    assert row["actor_kind"] == "human" and row["surface"] == "admin"
    assert row["delta_class"] == "loosening"
    _assert_no_setting_value(row)
    # A narrowing save is not a loosening: no row (and never refused for the audit).
    narrowed = _save(client, {"max_external": 100})
    assert narrowed.status_code == 200, narrowed.get_json()
    assert len(_settings_rows(app)) == 1
    # The rollback history is not written (one record, in the audit).
    history = app.extensions["config_store"].history_path
    assert not history.exists() or "yolo" not in history.read_text("utf-8")


def test_the_value_check_is_not_fooled_by_random_hex_that_contains_500(make_app, monkeypatch):
    """#515: ids and hashes are random hex; one with ``500`` (or ``10``) in it is no leak."""
    import hashlib
    import types

    from stiracore.yolo import settings_api

    seed = next(
        i.to_bytes(32, "big")
        for i in range(100_000)
        if "500" in hashlib.sha256(i.to_bytes(32, "big")).hexdigest()
    )
    fixed = types.SimpleNamespace(
        token_hex=lambda n=16: ("5001" * n)[: 2 * n], token_bytes=lambda n=32: seed[:n]
    )
    monkeypatch.setattr(settings_api, "secrets", fixed)
    app = make_app(section=OFF)
    saved = _save(
        app.test_client(),
        {"allow": "external", "max_external": 500, "max_exec": 10},
        confirm=True,
        acknowledged=["yolo_allow", "yolo_external_cap"],
    )
    assert saved.status_code == 200, saved.get_json()
    (row,) = _settings_rows(app)
    assert "500" in row["operation_id"] and "500" in row["incarnation_ref"]
    assert "500" in json.dumps(row)  # the old substring check would have failed here
    _assert_no_setting_value(row)
    # The structural check still catches a real leak of a value.
    leaks = [
        {"cap": 500},
        {"note": "external"},
        {"note": "cap=500"},
        {"values": {"external": True}},
        {"key_id": "cap=500"},  # inside an allowed key, as part of a string
        {"changed": ["allow", {"external": True}]},  # a nested dict key
        {"delta_class": "allow external"},
        {"generation_to": 500},
    ]
    for leak in leaks:
        with pytest.raises(AssertionError):
            _assert_no_setting_value({**row, **leak})


def test_a_loosening_save_whose_audit_row_fails_is_refused(make_app):
    app = make_app(section=OFF)
    client = app.test_client()

    def broken(*_a, **_k):
        raise OSError("disk full")

    app.extensions["yolo_settings_record"] = broken
    refused = _save(client, {"allow": "internal"}, confirm=True, acknowledged=["yolo_allow"])
    assert refused.status_code == 503 and refused.get_json()["code"] == "audit_failed"
    assert _gw(app) == {"yolo": OFF}
    # A narrowing still applies (never blocked by the audit).
    narrowed = _save(client, {"max_exec": 5})
    assert narrowed.status_code == 200 and _gw(app)["yolo"] == {**OFF, "max_exec": 5}


@pytest.mark.parametrize(
    "host", ["", "  ", "127.0.0.1", " 0.0.0.0 ", "::", "[::]", "192.168.0.9", "localhost"]
)
def test_the_wildcard_check_and_the_start_read_the_same_address(monkeypatch, host):
    from dashboard import stira_server
    from stiracore.observability import security

    monkeypatch.setenv("STIRA_HOST", host)
    monkeypatch.setenv("STIRA_ALLOW_REMOTE", "1")
    started = security.resolve_bind_host()
    assert stira_server._listens_on_wildcard() is own_dashboard.is_wildcard(started)


def test_another_source_of_the_address_reaches_both_the_start_and_the_save(make_app, monkeypatch):
    """One function decides the address: change its source and both follow (PR #444 M2)."""
    from stiracore.observability import security

    app = make_app()
    monkeypatch.setattr(security, "bind_host_setting", lambda default="127.0.0.1": "0.0.0.0")
    monkeypatch.setenv("STIRA_ALLOW_REMOTE", "1")
    assert security.resolve_bind_host() == "0.0.0.0"  # what main() would listen on
    client = app.test_client()
    assert _get(client)["wildcard_bind"] is True
    response = _save(client, {"allow": "internal"}, confirm=True, acknowledged=["yolo_allow"])
    assert response.status_code == 400
    assert response.get_json()["errors"][0]["code"] == "wildcard_bind"
    assert _gw(app) == {}


def test_an_unreadable_config_file_is_off_with_one_warning(tmp_path, monkeypatch, caplog):
    from dashboard import stira_server

    monkeypatch.setattr(stira_server, "_YOLO_READ_WARNED", [])
    broken = tmp_path / "gateway-config.json"
    broken.write_text("{not json", "utf-8")
    with caplog.at_level("WARNING"):
        assert stira_server._yolo_config_section(broken) == {}
        assert stira_server._yolo_config_section(broken) == {}
    lines = [r.getMessage() for r in caplog.records if "could not be read" in r.getMessage()]
    assert len(lines) == 1
    assert str(tmp_path) not in lines[0]


@pytest.mark.parametrize(
    "environ, section, names",
    [
        ({}, {"allow": "internal"}, ["allow"]),  # the browser by default: not from the file
        ({}, {"allow": "internal", "host_confirm": "browser"}, ["allow", "host_confirm"]),
        ({}, {"allow": "internal", "host_confirm": "command"}, ["allow"]),
        ({}, {"allow": "external", "max_auto_approvals": 3000}, ["allow", "max_auto_approvals"]),
        ({}, {"allow": "internal", "max_auto_approvals": 10}, ["allow"]),
        ({"STIRA_YOLO_ALLOW": "internal"}, {}, None),  # from the variable: no line
        ({"STIRA_YOLO_ALLOW": "internal"}, {"max_exec": 900}, ["max_exec"]),
        ({}, {"allow": "off", "max_auto_approvals": 5000}, None),  # off: nothing in effect
        ({}, {"max_auto_approvals": 5000}, ["max_auto_approvals"]),  # the default allowance
        ({}, {}, None),  # the default allowance alone: the server's own short line
    ],
)
def test_the_start_up_line_names_config_loosened_fields_only(environ, section, names):
    from dashboard import stira_server

    settings = parse_settings(environ, section)
    line = stira_server._yolo_config_notice(settings)
    if names is None:
        assert line is None
        return
    assert line.startswith("INFO: YOLO settings from gateway-config.json are in effect (")
    assert line.split("(", 1)[1].split(";")[0].split(", ") == names
    for value in ("internal", "external", "3000", "900", "browser", "command"):
        assert value not in line
