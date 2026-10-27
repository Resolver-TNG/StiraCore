"""W5 (#296) in the chat server: the executor exists only with ``autonomy.stage_c_exec``,
its route is guarded, a missing provider token count refuses before any model call, a
person's chat message pre-empts, and the shutdown step joins the iteration. Offline."""

from __future__ import annotations

import threading

import pytest
from test_grants_server import SAME, _build, _close, _issue

EXEC = {"autonomy": {"stage_c": True, "stage_c_exec": True, "allow_browser_confirm": True}}


@pytest.fixture
def stage_c_exec(tmp_path, monkeypatch):
    from stiracore.yolo import own_dashboard

    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    build, data = _build(tmp_path, monkeypatch, EXEC)
    app = build()
    policy = app.extensions["grants"]._policy()
    roots = list(getattr(policy, "write_roots", None) or [])
    ws = (roots[0] if roots else tmp_path) / "grant-ws"
    ws.mkdir(parents=True, exist_ok=True)
    yield app, data, ws
    _close(app)


def _threads():
    return [t for t in threading.enumerate() if t.name.startswith("grants-iteration")]


def test_stage_c_alone_builds_no_executor_and_no_step_route(tmp_path, monkeypatch):
    build, _data = _build(tmp_path, monkeypatch, {"autonomy": {"stage_c": True}})
    app = build()
    try:
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert "/api/grants/<grant_id>/step" not in rules
        assert "grant_executor" not in app.extensions and _threads() == []
        assert "iteration" not in str(app.test_client().get("/api/grants").get_json())
    finally:
        _close(app)
    assert "grant_executor" not in app.extensions["lifecycle"].shutdown()


def test_the_executor_switch_needs_stage_c():
    from stiracore.grants.service import settings_from

    off = settings_from({"stage_c_exec": True})
    assert not off.stage_c_exec and "stage_c_exec_needs_stage_c" in off.warnings
    assert not settings_from({"stage_c": True, "stage_c_exec": 1}).stage_c_exec
    assert not settings_from({"stage_c": True, "stage_c_exec": "true"}).stage_c_exec
    on = settings_from({"stage_c": True, "stage_c_exec": True})
    assert on.stage_c_exec and on.iteration_wall_sec == 600 and not on.warnings
    assert settings_from({"stage_c": True, "iteration_wall_sec": 5}).iteration_wall_sec == 600
    assert settings_from({"stage_c": True, "iteration_wall_sec": 900}).iteration_wall_sec == 900
    assert (
        "autonomy_unknown_key" in settings_from({"stage_c": True, "stage_c_execs": True}).warnings
    )


def test_no_request_bound_refuses_before_any_model_call(stage_c_exec):
    app, _data, ws = stage_c_exec
    app.extensions["grant_executor"]._estimator = None
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    response = client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME)
    assert response.status_code == 409
    assert response.get_json() == {
        "code": "grant_iteration_refused",
        "error_code": "grant_iteration_refused",
        "reason": "token_count_unavailable",
    }
    view = client.get(f"/api/grants/{grant_id}").get_json()
    assert view["iteration"]["state"] == "idle" and view["iteration"]["number"] == 0
    assert _threads() == []
    listed = client.get("/api/grants").get_json()
    assert listed["executor"]["playbook"].startswith("playbook_")
    assert listed["grants"][0]["iteration"]["state"] == "idle"


def test_the_step_route_refuses_a_body_and_a_cross_site_request(stage_c_exec):
    app, _data, ws = stage_c_exec
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    assert (
        client.post(f"/api/grants/{grant_id}/step", json={"go": 1}, headers=SAME).status_code == 400
    )
    cross = client.post(
        f"/api/grants/{grant_id}/step",
        json={},
        headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert cross.status_code == 403


def test_one_iteration_through_the_route_claims_and_awaits_verification(stage_c_exec):
    from grants_exec_helpers import Counter, ScriptedProvider, use

    app, _data, ws = stage_c_exec
    executor = app.extensions["grant_executor"]
    provider = ScriptedProvider(
        [[use("goal_report", {"kind": "claim_complete", "code": "goal_met"})]]
    )
    executor._estimator = Counter()
    executor._inner_model = lambda _parent: provider
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    started = client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME)
    assert started.status_code == 200, started.get_json()
    assert started.get_json()["iteration"]["state"] in ("running", "idle")
    assert executor.wait_idle(20)
    view = client.get(f"/api/grants/{grant_id}").get_json()
    assert view["iteration"]["end"] == "claimed"
    assert view["grant"]["goal_state"] == "awaiting_verification"
    assert view["grant"]["state"] == "active" and _threads() == []


def test_a_persons_chat_message_signals_before_the_turn_lock(stage_c_exec):
    app, _data, _ws = stage_c_exec
    executor = app.extensions["grant_executor"]
    before = executor.preempt_signal.seq
    client = app.test_client()
    client.post("/api/chat", json={"message": "hello"}, headers=SAME)
    assert executor.preempt_signal.seq == before + 1


def test_the_shutdown_step_joins_a_running_iteration(stage_c_exec, monkeypatch):
    from grants_exec_helpers import Counter, ScriptedProvider, use

    from stiracore.grants import executor as executor_module

    # The call ends by itself (0.5 s hold) after the stop; the 0.5 s grace measured from
    # the stop could run out on a slow runner before the rest of the call (the report
    # tool, the end) is done, which is the "abandoned" path, not this one (#662).
    monkeypatch.setattr(executor_module, "ABANDON_GRACE_SEC", 4.0)
    app, _data, ws = stage_c_exec
    executor = app.extensions["grant_executor"]
    hold, started = threading.Event(), threading.Event()
    provider = ScriptedProvider([[use("goal_report", {"kind": "progress", "code": "step_done"})]])
    provider.on_call = lambda n: (started.set(), hold.wait(0.5))
    executor._estimator = Counter()
    executor._inner_model = lambda _parent: provider
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    assert client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME).status_code == 200
    assert started.wait(10)
    steps = app.extensions["lifecycle"].shutdown()
    assert "grant_executor" not in steps or steps["grant_executor"] == "stopped"
    assert _threads() == []


def test_the_step_route_refuses_while_an_agent_tool_call_runs(stage_c_exec):
    """Astra B7: the issue / resume / verify guard applies to /step too."""
    from grants_exec_helpers import Counter, ScriptedProvider

    from stiracore.yolo import runtime

    app, _data, ws = stage_c_exec
    executor = app.extensions["grant_executor"]
    provider = ScriptedProvider([[{"text": "done"}]])
    executor._estimator = Counter()
    executor._inner_model = lambda _parent: provider
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    token = "w5-tool-origin"
    runtime.TRACKER.enter(token)
    try:
        response = client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME)
    finally:
        runtime.TRACKER.leave(token)
    assert response.status_code == 409
    assert response.get_json()["host_code"] == "tool_call_running"
    assert provider.calls == 0 and _threads() == []


def test_a_grant_card_never_lands_on_a_normal_turns_event_stream(stage_c_exec):
    """Astra r3 B2: the grant card's end (withdrawn by the person's message) arrives while
    the next normal turn runs — it must not take that turn's sequence numbers nor show on
    the activity board."""
    import time as _time

    from grants_exec_helpers import Counter, ScriptedProvider, use

    app, _data, ws = stage_c_exec
    executor = app.extensions["grant_executor"]
    broker = app.extensions["hitl_broker"]
    recorder = broker._on_event.__self__
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    parent = executor._parent()
    parent.checkpoint_hooks.policy_check = lambda n: "ask" if n == "editor" else "always"
    executor._inner_model = lambda _p: ScriptedProvider(
        [[use("editor", {"command": "create", "path": str(ws / "clean.md"), "file_text": "clean"})]]
    )
    executor._estimator = Counter()
    release = threading.Event()
    original = broker._notify

    def delayed(kind, info):
        if str(info.get("agent_id", "")).startswith("grant:"):
            release.wait(10)  # land the grant's event inside the normal turn (if any is sent)
        original(kind, info)

    broker._notify = delayed
    try:
        assert client.post(f"/api/grants/{grant_id}/step", json={}, headers=SAME).status_code == 200
        deadline = _time.monotonic() + 5
        while not broker.pending() and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert broker.pending()
        executor.preempt_signal.arrive()  # the chat route's order: the signal, then the lock
        lock = app.extensions["turn_lock"]
        assert lock.acquire(False)
        try:
            recorder.active.set("normal-turn")
            recorder.emit("turn.started", {"prompt_chars": 5, "image_count": 0})
            release.set()
            assert executor.wait_idle(5)
            recorder.emit("model.started", {"call_id": "normal-call"})
            events = client.get("/api/turns/normal-turn/events").get_json()["events"]
            assert [(e["seq"], e["type"]) for e in events] == [
                (1, "turn.started"),
                (2, "model.started"),
            ]
            activity = client.get("/api/activity").get_json()
            assert not [i for i in activity["items"] if i.get("kind") == "approval"]
        finally:
            recorder.active.clear()
            lock.release()
    finally:
        release.set()
