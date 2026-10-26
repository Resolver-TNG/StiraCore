"""ADR 0012 §2 / §8b (phase 1): reserved ``yolo*`` keys and the running-tool-call refusal.

The real chat server app, offline (no model is called), with and without the allowance.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from stiracore.observability.config_api import ConfigStore
from stiracore.tools.tool_config import RECOVER_WORD
from stiracore.yolo import runtime
from stiracore.yolo.admin_guard import ToolCallTracker, reserved_keys
from stiracore.yolo.allowance import YoloSettings
from stiracore.yolo.routes import SAVE_PATH_PREFIXES
from tests.yolo_host import confirmed_post


def _build(tmp_path, monkeypatch, allowance):
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
    runtime._reset_for_tests(YoloSettings(allowance))
    store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
    return stira_server.create_app(config_store=store)


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    made = []

    def make(allowance="internal"):
        app = _build(tmp_path / str(len(made)), monkeypatch, allowance)
        made.append(app)
        return app

    try:
        yield make
    finally:
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()
        for app in made:
            app.extensions["lifecycle"].shutdown()


@pytest.fixture
def running_tool_call():
    token = object()
    runtime.TRACKER.enter(token)
    try:
        yield token
    finally:
        runtime.TRACKER.leave(token)


def _post(client, path, body):
    return client.post(
        path,
        data=json.dumps(body),
        content_type="application/json",
        headers={"Idempotency-Key": f"k-{id(body)}-{path}"},
    )


def _yolo_rules(app):
    return sorted(
        (rule.rule, tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})))
        for rule in app.url_map.iter_rules()
        if "yolo" in rule.rule
    )


# --- routes ---------------------------------------------------------------------------


def test_without_allowance_there_is_no_yolo_route(make_app):
    """Phase 7c: only the settings routes (where a person enables YOLO for the next start;
    they draw no button, badge or poll and change nothing at run time)."""
    app = make_app("off")
    assert _yolo_rules(app) == [("/api/yolo/settings", ("GET",)), ("/api/yolo/settings", ("POST",))]
    assert app.test_client().get("/api/yolo/status").status_code == 404


def test_with_allowance_only_the_five_yolo_routes_touch_yolo(make_app):
    app = make_app("internal")
    assert _yolo_rules(app) == [
        ("/api/yolo/request", ("POST",)),
        ("/api/yolo/request/cancel", ("POST",)),
        # Phase 7c: the settings (with and without the allowance; the values only).
        ("/api/yolo/settings", ("GET",)),
        ("/api/yolo/settings", ("POST",)),
        ("/api/yolo/status", ("GET",)),
        ("/api/yolo/stop", ("POST",)),
        ("/api/yolo/unlatch", ("POST",)),
    ]
    body = app.test_client().get("/api/yolo/status").get_json()
    assert body["allowance"] == "internal"
    # Phase 5b: offered (no window, no request, not latched, the browser channel up).
    assert body["offered"] is True and body["unavailable"] == []
    assert body["window"] is None and body["request"] is None


# --- reserved keys --------------------------------------------------------------------


def test_reserved_key_scan():
    assert reserved_keys({"yolo": 1}) == ["yolo"]
    assert reserved_keys({"changes": {"tools": {"YOLO_tier": "x"}}}) == ["YOLO_tier"]
    assert reserved_keys({"changes": {"yolo.tier": 1}}) == ["yolo.tier"]
    assert reserved_keys([{"a": [{"tools.yolo": 1}]}]) == ["tools.yolo"]
    assert reserved_keys({"name": "yolo", "values": ["yolo"]}) == []  # values are not keys
    deep: dict = {}
    cursor = deep
    for _ in range(20):
        cursor["a"] = {}
        cursor = cursor["a"]
    assert reserved_keys(deep) == ["<depth>"]  # cannot be checked: reserved


def _save_rules(app):
    return sorted(
        {
            rule.rule
            for rule in app.url_map.iter_rules()
            if rule.rule.startswith(SAVE_PATH_PREFIXES)
            and rule.methods & {"POST", "PUT", "PATCH", "DELETE"}
        }
    )


_FILL = {
    "<job_id>": "j1",
    "<run_id>": "run1",
    "<entry_id>": "e1",
}


def _fill(rule):
    for key, value in _FILL.items():
        rule = rule.replace(key, value)
    return rule


@pytest.mark.parametrize(
    "body",
    [
        {"yolo": "external"},
        {"name": "file_read", "enabled": True, "yolo_window": {"state": "active"}},
        {"changes": {"yolo": {"tier": "external"}}},
        {"changes": {"tools": {"yolo": True}}},
        {"job": {"tools": [], "YOLO": "internal"}},
    ],
)
def test_every_save_path_refuses_a_yolo_key_with_409(make_app, body):
    app = make_app("internal")
    client = app.test_client()
    rules = _save_rules(app)
    # The inventory this contract covers (a new save route is covered automatically).
    assert "/api/config/hub" in rules and "/api/tools/config" in rules
    assert "/api/scheduler/jobs" in rules and "/api/config/history/<entry_id>/rollback" in rules
    for rule in rules:
        response = _post(client, _fill(rule), body)
        assert response.status_code == 409, (rule, response.status_code, response.get_json())
        assert response.get_json()["error_code"] == "yolo_not_settable", rule


def test_without_allowance_save_paths_answer_as_before(make_app):
    app = make_app("off")
    client = app.test_client()
    for rule in _save_rules(app):
        response = _post(client, _fill(rule), {"yolo": "external"})
        assert (response.get_json() or {}).get("error_code") != "yolo_not_settable", rule
    # The legacy rule API ignores unknown keys, as on main.
    response = _post(
        client, "/api/config/tools", {"name": "file_read", "enabled": True, "yolo": "external"}
    )
    assert response.status_code == 200


def test_reserved_keys_need_the_csrf_checks_first(make_app):
    app = make_app("internal")
    response = app.test_client().post(
        "/api/config/hub",
        data=json.dumps({"yolo": 1}),
        content_type="application/json",
        headers={"X-Test-No-Csrf": "1"},
    )
    assert response.status_code == 403  # security runs before the reserved-key check


# --- running tool calls ----------------------------------------------------------------


def test_no_refusal_without_allowance_even_while_a_call_runs(make_app, running_tool_call):
    app = make_app("off")
    assert runtime.tool_call_refusal() is None
    response = _post(
        app.test_client(),
        "/api/config/tools",
        {"name": "http_request", "policy": "ask", "confirm": True},
    )
    assert response.status_code == 200


def test_approve_and_edit_are_refused_while_a_tool_call_runs(make_app, running_tool_call):
    client = make_app("internal").test_client()
    for action in ("approve", "edit"):
        body = {"action": action, "args_hash": "0" * 64, "edited_input": {}}
        response = _post(client, "/api/hitl/r1/decision", body)
        assert response.status_code == 409, action
        assert response.get_json()["code"] == "tool_call_running"
    # Reject and respond narrow: never refused for this reason.
    for body in (
        {"action": "reject"},
        {"action": "respond", "response": "no", "args_hash": "0" * 64},
    ):
        response = _post(client, "/api/hitl/r1/decision", body)
        assert (response.get_json() or {}).get("code") != "tool_call_running", body


def test_loosening_tool_rule_save_is_refused_while_a_call_runs_tightening_is_not(make_app):
    client = make_app("internal").test_client()
    token = object()
    runtime.TRACKER.enter(token)
    try:
        loosen = {"name": "http_request", "policy": "ask", "confirm": True}
        response = _post(client, "/api/config/tools", loosen)
        assert response.status_code == 409
        assert response.get_json()["error_code"] == "tool_call_running"
        response = _post(client, "/api/tools/config", {"recover": RECOVER_WORD})
        assert response.status_code == 409
        assert response.get_json()["error_code"] == "tool_call_running"
    finally:
        runtime.TRACKER.leave(token)
    # After the call: still not applied without the host (phase 2), applied with it.
    response = _post(client, "/api/config/tools", loosen)
    assert response.status_code == 428
    assert response.get_json()["error_code"] == "host_confirmation_required"
    assert confirmed_post(client, "/api/config/tools", loosen).status_code == 200
    runtime.TRACKER.enter(token)
    try:
        tighten = {"name": "http_request", "policy": "never"}
        assert _post(client, "/api/config/tools", tighten).status_code == 200
    finally:
        runtime.TRACKER.leave(token)


def _confirmed_hub_save(client, body):
    """Answer the hub's confirmations (dangers, typed Always names) as a person would."""
    for _ in range(3):
        response = _post(client, "/api/config/hub", body)
        data = response.get_json() or {}
        if data.get("error_code") == "needs_confirmation":
            body["acknowledged_dangers"] = data["needs_confirmation"]
        elif data.get("error_code") == "needs_tool_names":
            body["confirm_always"] = data["needs_tool_names"]
        else:
            return response
    return response


@pytest.mark.parametrize("which", ["hub", "rollback"])
def test_loosening_hub_save_and_rollback_refused_while_a_call_runs(make_app, which):
    client = make_app("internal").test_client()
    token = object()
    if which == "rollback":
        # Tighten first (recorded in the history), then roll that entry back: a loosening.
        tighten = {"changes": {"tools": {"enabled_tools": []}}}
        assert _confirmed_hub_save(client, tighten).status_code == 200
        entries = client.get("/api/config/history").get_json()["entries"]
        path = f"/api/config/history/{entries[0]['id']}/rollback"
    runtime.TRACKER.enter(token)
    try:
        if which == "hub":
            response = _confirmed_hub_save(client, {"changes": {"tools": {"mode": "trust_all"}}})
        else:
            body = {}
            for _ in range(3):
                response = _post(client, path, body)
                data = response.get_json() or {}
                if data.get("error_code") == "needs_confirmation":
                    body["acknowledged_dangers"] = data["needs_confirmation"]
                elif data.get("error_code") == "needs_tool_names":
                    body["confirm_always"] = data["needs_tool_names"]
                else:
                    break
        assert response.status_code == 409, response.get_json()
        assert response.get_json()["error_code"] == "tool_call_running"
    finally:
        runtime.TRACKER.leave(token)


def test_scheduler_create_and_resume_refused_while_a_call_runs_pause_is_not(
    make_app, running_tool_call
):
    client = make_app("internal").test_client()
    for path in ("/api/scheduler/jobs", "/api/scheduler/jobs/j1/resume"):
        response = _post(client, path, {"job": {}, "confirm": True})
        assert response.status_code == 409, path
        assert response.get_json()["code"] == "tool_call_running"
    for path in ("/api/scheduler/jobs/j1/pause", "/api/scheduler/runs/run1/cancel"):
        response = _post(client, path, {})
        assert (response.get_json() or {}).get("code") != "tool_call_running", path


# --- the tracker on a real Strands agent ------------------------------------------------


def test_tracker_counts_a_call_only_while_it_executes():
    from strands import Agent, tool
    from strands.hooks import BeforeToolCallEvent, HookProvider

    from tests.test_subagent import ScriptedModel

    tracker = ToolCallTracker()
    seen = []

    @tool
    def probe() -> str:
        """Report how many tool calls are running."""
        seen.append(tracker.running())
        return "ok"

    @tool
    def blocked() -> str:
        """Never runs."""
        seen.append("ran")
        return "no"

    class Cancel(HookProvider):
        def register_hooks(self, registry, **kwargs):
            registry.add_callback(BeforeToolCallEvent, self.before)

        def before(self, event):
            if event.tool_use["name"] == "blocked":
                event.cancel_tool = "refused"

    model = ScriptedModel(
        [
            [
                {"toolUse": {"name": "probe", "toolUseId": "u1", "input": {}}},
                {"toolUse": {"name": "blocked", "toolUseId": "u2", "input": {}}},
            ],
            [{"text": "done"}],
        ]
    )
    agent = Agent(
        model=model, tools=[probe, blocked], hooks=[Cancel(), tracker], callback_handler=None
    )
    agent("go")
    assert seen == [1]  # the cancelled call was never counted, the probe saw itself
    assert tracker.running() == 0


def test_tracker_drops_leftovers_at_the_end_of_the_invocation():
    tracker = ToolCallTracker()
    agent = object()
    event = Mock(tool_use={"name": "x"}, agent=agent, cancel_tool=False)
    tracker.before_tool(event)
    assert tracker.running() == 1
    tracker.after_invocation(Mock(agent=object()))  # another agent: untouched
    assert tracker.running() == 1
    tracker.after_invocation(Mock(agent=agent))
    assert tracker.running() == 0
