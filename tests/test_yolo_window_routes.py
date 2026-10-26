"""ADR 0012 phase 3 on the real chat server app (offline, no model call, browser channel):
``POST /api/yolo/stop``, ``/new``, the server's stop, the unlatch route, the status
fields — and that an active window changes **no** tool call outside a human chat turn;
phase 5a: inside one, exactly the internal Ask tools are approved automatically, and
Never, the floors, CRITICAL and every other tool still go to a person.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from types import SimpleNamespace

import pytest

from stiracore.core import HITLAction
from stiracore.core.engine import HITLDecision
from stiracore.yolo import own_dashboard, runtime
from stiracore.yolo.host_ops import HEADER
from stiracore.yolo.window import MARKER_NAME, WindowMarker
from tests.test_yolo_browser_confirm import BROWSER, _confirm_request, _press, confirm
from tests.test_yolo_host_gate import _build


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    made = []
    # Premise 2 of §8b (the own-dashboard guard) is armed by main() at a real start.
    monkeypatch.setattr(own_dashboard, "armed", lambda: object())

    def make(allowance="internal"):
        app = _build(tmp_path / str(len(made)), monkeypatch, allowance, host_confirm="browser")
        made.append(app)
        return app

    try:
        yield make
    finally:
        for app in made:
            app.extensions["lifecycle"].shutdown()
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


def _open(client, tier="internal"):
    data = _press(client, tier)
    response = _confirm_request(client, data)
    assert response.status_code == 200, response.get_json()
    status = client.get("/api/yolo/status").get_json()
    assert status["window"]["state"] == "active", status
    return status


def _data_dir(app):
    return runtime.desk().yolo._marker.path.parent


def test_status_adds_the_window_fields_only(make_app):
    client = make_app("internal").test_client()
    status = client.get("/api/yolo/status").get_json()
    assert status["window"] is None and status["last_end"] is None
    assert status["latches"] == [] and status["latch_reason"] is None
    # Phase 5b: the button is offered while nothing is open, and not while a window is.
    assert status["offered"] is True and status["unavailable"] == []
    assert status["auto_approvals"] == {"window": None, "items": []}
    opened = _open(client)
    assert opened["offered"] is False and opened["unavailable"] == ["yolo_active"]
    assert opened["auto_approvals"] == {"window": opened["window"]["id"], "items": []}
    window = opened["window"]
    assert window["tier"] == "internal" and window["remaining_sec"] > 6 * 3600 - 60
    assert window["ends_at"] == window["t0"] + 6 * 3600 and window["cap"] == 2000
    assert set(window) == {
        "id", "state", "tier", "reason", "end_class", "pause_reason", "t0", "ends_at",
        "remaining_sec", "cap", "used",
        # phase 5a (additive): slots, pause state, automatic approvals
        "reserved", "limit", "auto_approved", "paused", "pauses_last_hour", "resume_in_sec",
        "effective_tier",
        # phase 6 (additive): the external sub-cap
        "external_used", "external_reserved", "external_limit",
        # phase 7 (additive): the exec flag and the exec sub-cap
        "exec", "exec_used", "exec_reserved", "exec_limit", "exec_effective",
        # #448 (additive): every tool call of the window's period, and its turns
        "tool_calls", "turns",
        # #455 (additive): the breaker's reason codes behind pause_reason
        "pause_breaker_reasons", "pause_breaker_inputs",
    }  # fmt: skip
    assert window["exec"] is False and window["exec_limit"] == 0
    assert window["exec_effective"] is False
    assert window["used"] == window["reserved"] == window["auto_approved"] == 0
    assert window["limit"] == 2000 and window["paused"] is False
    assert window["effective_tier"] == "internal"
    # A window of tier internal has no external sub-cap.
    assert window["external_used"] == window["external_reserved"] == 0
    assert window["external_limit"] == 0


def test_stop_ends_the_window_and_is_never_refused(make_app):
    app = make_app("internal")
    client = app.test_client()
    _open(client)
    # Not refused while an agent tool call runs, and needs no confirmation.
    token = object()
    runtime.TRACKER.enter(token)
    try:
        response = client.post("/api/yolo/stop")
    finally:
        runtime.TRACKER.leave(token)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["window"] is None and body["last_end"]["reason"] == "revoked_stop"
    assert body["latched"] is False
    assert WindowMarker(_data_dir(app)).read().state == "closed"
    # Twice, or with nothing open: still 200.
    assert client.post("/api/yolo/stop").status_code == 200
    _open(client)  # press again


def test_stop_fences_a_pending_request(make_app):
    client = make_app("internal").test_client()
    data = _press(client, "internal")
    client.post("/api/yolo/stop")
    request = client.get("/api/yolo/status").get_json()["request"]
    assert request["state"] == "fenced" and request["reason"] == "stop"
    assert _confirm_request(client, data).status_code == 409


def test_new_session_ends_the_window(make_app):
    client = make_app("internal").test_client()
    _open(client)
    reset = client.post("/api/chat/session/new", headers={"Idempotency-Key": "new-w"})
    assert reset.status_code == 200, reset.get_json()
    status = client.get("/api/yolo/status").get_json()
    assert status["window"] is None and status["last_end"]["reason"] == "revoked_new_session"


def test_the_servers_stop_closes_the_window_and_the_next_start_is_clear(make_app):
    app = make_app("internal")
    client = app.test_client()
    _open(client)
    data_dir = _data_dir(app)
    app.extensions["lifecycle"].shutdown()
    assert WindowMarker(data_dir).read().state == "closed"


def test_an_orphan_marker_latches_and_the_unlatch_route_needs_a_confirmation(make_app, tmp_path):
    # Simulate a crash between the marker and ``started`` of an earlier process.
    data_dir = tmp_path / "0" / "data"
    data_dir.mkdir(parents=True)
    orphan = secrets.token_hex(16)
    WindowMarker(data_dir).write("open", orphan, "b" * 64)
    app = make_app("internal")
    client = app.test_client()
    status = client.get("/api/yolo/status").get_json()
    assert status["latched"] is True
    assert status["latches"] == [
        {"subject": "orphan_marker", "window_id": orphan, "reason": "orphan_marker"}
    ]
    refused = client.post("/api/yolo/request", json={"tier": "internal"})
    assert refused.status_code == 409 and refused.get_json()["code"] == "yolo_latched"
    assert client.post("/api/yolo/unlatch", json={"window_id": "0" * 32}).status_code == 409
    first = client.post("/api/yolo/unlatch", json={"window_id": orphan})
    assert first.status_code == 428, first.get_json()
    operation = first.get_json()["operation"]
    assert operation["kind"] == "yolo_unlatch" and operation["word"] == "UNLATCH"
    assert operation["summary"] == {"action": "unlatch", "operation": "orphan_marker"}
    # Not confirmed yet: nothing changes.
    early = client.post(
        "/api/yolo/unlatch", json={"window_id": orphan}, headers={HEADER: operation["id"]}
    )
    assert early.status_code == 409
    assert confirm(client, operation).status_code == 200
    done = client.post(
        "/api/yolo/unlatch", json={"window_id": orphan}, headers={HEADER: operation["id"]}
    )
    assert done.status_code == 200, done.get_json()
    assert done.get_json()["latched"] is False
    assert WindowMarker(data_dir).read().state == "closed_by_unlatch"
    _open(client)  # the button would be offered again


def test_the_unlatch_route_is_refused_while_a_tool_call_runs(make_app, tmp_path):
    data_dir = tmp_path / "0" / "data"
    data_dir.mkdir(parents=True)
    orphan = secrets.token_hex(16)
    WindowMarker(data_dir).write("open", orphan, "b" * 64)
    client = make_app("internal").test_client()
    token = object()
    runtime.TRACKER.enter(token)
    try:
        response = client.post("/api/yolo/unlatch", json={"window_id": orphan})
    finally:
        runtime.TRACKER.leave(token)
    assert response.status_code == 409 and response.get_json()["code"] == "tool_call_running"


def test_the_press_is_refused_while_a_tool_call_runs(make_app):
    # ADR 0012 §8b: the running-tool-call refusal applies to the request too (the press),
    # not only to its confirmation and the unlatch — no request, no confirm token.
    client = make_app("external").test_client()
    token = object()
    runtime.TRACKER.enter(token)
    try:
        response = client.post("/api/yolo/request", json={"tier": "internal"})
    finally:
        runtime.TRACKER.leave(token)
    body = response.get_json()
    assert response.status_code == 409 and body["code"] == "tool_call_running", body
    assert "confirm" not in body and "request" not in body
    status = client.get("/api/yolo/status").get_json()
    assert status["request"] is None and status["window"] is None, status
    # Once the call has finished the same press is accepted.
    response = client.post("/api/yolo/request", json={"tier": "internal"})
    assert response.status_code == 202, response.get_json()


def test_without_the_allowance_there_is_no_marker_and_no_route(make_app, tmp_path):
    app = make_app("off")
    client = app.test_client()
    assert client.post("/api/yolo/stop").status_code == 404
    assert client.post("/api/yolo/unlatch", json={}).status_code == 404
    assert not list(tmp_path.rglob(MARKER_NAME))


# --- an active window changes no tool call (the phase-3 acceptance) ---------------------


def test_an_active_window_changes_no_tool_call(make_app, monkeypatch):
    """Outside a human chat / Stage turn (webhook, resume, job …: the origin mark is
    unset) an active window changes nothing (ADR 0012 §3, phase 5a)."""
    from dashboard import stira_server

    captured = {}
    real = stira_server.NXPAgent

    def capture(*args, **kwargs):
        agent = real(*args, **kwargs)
        captured.update(agent=agent, tools=kwargs["tools"])
        return agent

    monkeypatch.setattr(stira_server, "NXPAgent", capture)
    app = make_app("internal")
    client = app.test_client()
    agent = captured["agent"]
    ask = client.get("/api/tools/config").get_json()["ask_tools"]
    assert ask, "needs at least one Ask tool"
    asked = []

    def request_hitl(request):
        asked.append((request.tool_name, request.reason))
        return HITLDecision(action=HITLAction.REJECT)

    monkeypatch.setattr(agent.engine, "request_hitl", request_hitl)

    def calls():
        out = []
        for name in ask:
            event = SimpleNamespace(
                tool_use={"name": name, "toolUseId": secrets.token_hex(8), "input": {"x": 1}},
                cancel_tool=None,
                invocation_state={},
            )
            agent.checkpoint_hooks.before_tool(event)
            out.append((name, event.cancel_tool))
        return out

    def spec_hash():
        specs = [getattr(t, "tool_spec", None) or {"name": t.tool_name} for t in captured["tools"]]
        return hashlib.sha256(json.dumps(specs, sort_keys=True, default=str).encode()).hexdigest()

    before_spec, before_prompt = spec_hash(), agent.system_prompt
    without = calls()
    asked_without = list(asked)
    asked.clear()
    _open(client)
    with_window = calls()
    assert with_window == without  # every Ask still goes to a person (here: rejected)
    assert asked == asked_without and all(reason for _n, reason in asked)
    assert spec_hash() == before_spec and agent.system_prompt == before_prompt


def _stira(make_app, monkeypatch, allowance="internal"):
    from dashboard import stira_server

    captured = {}
    real = stira_server.NXPAgent

    def capture(*args, **kwargs):
        agent = real(*args, **kwargs)
        captured.update(agent=agent, tools=kwargs["tools"])
        return agent

    monkeypatch.setattr(stira_server, "NXPAgent", capture)
    app = make_app(allowance)
    return app, captured


def _gate_call(agent, name, tool_input):
    event = SimpleNamespace(
        tool_use={"name": name, "toolUseId": secrets.token_hex(8), "input": tool_input},
        cancel_tool=None,
        invocation_state={},
    )
    agent.checkpoint_hooks.before_tool(event)
    if event.cancel_tool is None:
        agent.checkpoint_hooks.binding_verifier.before_tool(event)
    return event


def test_phase5a_internal_ask_tools_are_approved_in_a_human_turn(make_app, monkeypatch):
    from stiracore.yolo import origin
    from stiracore.yolo.tiers import INTERNAL, tool_class

    app, captured = _stira(make_app, monkeypatch)
    client = app.test_client()
    agent = captured["agent"]
    ask = client.get("/api/tools/config").get_json()["ask_tools"]
    internal = [name for name in ask if tool_class(name) == INTERNAL]
    others = [name for name in ask if tool_class(name) != INTERNAL]
    assert internal and others, ask  # the default rules have both kinds
    asked = []

    def request_hitl(request):
        asked.append((request.tool_name, request.reason))
        return HITLDecision(action=HITLAction.REJECT)

    monkeypatch.setattr(agent.engine, "request_hitl", request_hitl)
    workspace = agent.write_policy.workspace
    args = {"path": str(workspace / "yolo-probe.md"), "content": "x", "file_path": "a.py"}
    # The server's cwd inside the workspace: ``lsp_*``'s relative path and ``journal``'s
    # ``<cwd>/journal`` are inside it too (outside: test_phase5a_nothing_outside_…).
    monkeypatch.chdir(workspace)
    _open(client)
    with origin.human_turn(agent.agent_id):
        results = {name: _gate_call(agent, name, dict(args)).cancel_tool for name in ask}
    for name in internal:
        assert results[name] is None, (name, results[name])
    for name in others:
        assert results[name] and results[name].startswith("HITL rejected"), name
    assert sorted(n for n, _ in asked) == sorted(others)
    window = client.get("/api/yolo/status").get_json()["window"]
    assert window["used"] == len(internal) and window["reserved"] == 0
    assert window["auto_approved"] == len(internal)


def test_phase5a_nothing_outside_the_workspace_is_approved(make_app, monkeypatch, tmp_path):
    """PR #435 review M1, measured on the real app: with the window active, an internal
    tool that reaches outside the workspace / write roots is never approved
    automatically. Writes there are refused before anyone is asked (``path_denial``,
    the write roots); reads (``~/.ssh`` …), ``lsp_*``, images and ``journal`` (cwd)
    go to the person's card (``yolo.tiers.scope_refusal``). Only the gate runs here
    (``before_tool``): no tool executes, nothing is read or written."""
    import os

    from stiracore.yolo import origin
    from tests.test_yolo_browser_confirm import BROWSER as headers

    app, captured = _stira(make_app, monkeypatch)
    client = app.test_client()
    agent = captured["agent"]
    asked = []
    monkeypatch.setattr(
        agent.engine,
        "request_hitl",
        lambda request: (
            asked.append(request.tool_name),
            HITLDecision(action=HITLAction.REJECT),
        )[1],
    )
    # The read tools are Always by default (no card, YOLO never asked): an Ask rule
    # (a tightening save) is what lets YOLO answer them at all.
    for name in ("file_read", "view_image", "image_reader"):
        saved = client.post(
            "/api/config/tools",
            json={"name": name, "policy": "ask"},
            headers={**headers, "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"]},
        )
        assert saved.status_code == 200, saved.get_json()
    workspace = agent.write_policy.workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "notes.md").write_text("x", encoding="utf-8")
    os.symlink(outside, workspace / "link-out")  # inside by name, outside when resolved
    home = os.path.expanduser("~")
    refused = [  # writes outside the roots: refused, no card, never YOLO
        ("file_write", {"path": f"{home}/.zshrc", "content": "x"}),
        ("file_write", {"path": f"{home}/Library/LaunchAgents/x.plist", "content": "x"}),
        ("editor", {"command": "create", "path": f"{home}/.zshrc", "file_text": "x"}),
        ("file_write", {"path": "link-out/x.md", "content": "x"}),
        # #464: journal's date is its only target argument: not YYYY-MM-DD → refused
        ("journal", {"action": "write", "date": "../../../outside/x", "content": "x"}),
    ]
    carded = [  # the person's card
        ("editor", {"command": "view", "path": f"{home}/.ssh/id_rsa"}),
        ("file_read", {"path": f"{home}/.ssh/id_rsa"}),
        ("file_read", {"path": "~/.ssh/id_rsa"}),
        ("file_read", {"path": "../../../outside/x.md"}),
        ("file_read", {"path": "link-out/x.md"}),
        ("file_read", {"path": ["notes.md"]}),  # not a plain str: undecidable
        ("file_read", {}),
        ("view_image", {"path": str(outside / "a.png")}),
        ("image_reader", {"image_path": str(outside / "a.png")}),
        ("lsp_diagnostics", {"file_path": str(outside / "a.py")}),
        ("lsp_hover", {"file_path": str(outside / "a.py"), "line": 1, "character": 1}),
        ("journal", {"action": "write", "content": "x"}),  # <cwd>/journal: outside here
    ]
    inside = [
        ("file_read", {"path": "notes.md"}),
        ("editor", {"command": "view", "path": str(workspace / "notes.md")}),
        ("file_write", {"path": "sub/new.md", "content": "x"}),
    ]
    monkeypatch.chdir(tmp_path)  # the server's cwd outside every root
    _open(client)
    with origin.human_turn(agent.agent_id):
        for name, args in refused:
            event = _gate_call(agent, name, args)
            assert event.cancel_tool and "not sent for approval" in event.cancel_tool, args
        assert asked == []
        for name, args in carded:
            event = _gate_call(agent, name, args)
            assert event.cancel_tool and event.cancel_tool.startswith("HITL rejected"), args
        assert asked == [name for name, _ in carded]
        assert client.get("/api/yolo/status").get_json()["window"]["auto_approved"] == 0
        for name, args in inside:
            assert _gate_call(agent, name, args).cancel_tool is None, args
    window = client.get("/api/yolo/status").get_json()["window"]
    assert window["auto_approved"] == window["used"] == len(inside)
    assert len(asked) == len(carded)


def test_phase5a_the_mark_of_another_agent_or_a_finished_turn_covers_nothing(make_app, monkeypatch):
    """PR #435 review M2 on the real app: the human-turn mark is bound to its agent
    and its turn. A context copied during the turn and run after it, a mark of another
    agent id and a thread started inside the turn are not covered: the card."""
    import contextvars
    import threading

    from stiracore.yolo import origin

    app, captured = _stira(make_app, monkeypatch)
    client = app.test_client()
    agent = captured["agent"]
    asked = []
    monkeypatch.setattr(
        agent.engine,
        "request_hitl",
        lambda request: (asked.append(request.tool_name), HITLDecision(HITLAction.REJECT))[1],
    )
    path = str(agent.write_policy.workspace / "a.md")

    def call():
        return _gate_call(agent, "file_write", {"path": path, "content": "x"}).cancel_tool

    _open(client)
    in_thread = {}
    with origin.human_turn(agent.agent_id):
        copied = contextvars.copy_context()  # e.g. a task / executor job made in the turn
        worker = threading.Thread(target=lambda: in_thread.update(result=call()))
        worker.start()
        worker.join()
    assert in_thread["result"].startswith("HITL rejected")  # threads start unmarked
    assert copied.run(call).startswith("HITL rejected")  # run after the turn ended
    with origin.human_turn("another-agent"):
        assert call().startswith("HITL rejected")
    assert asked == ["file_write"] * 3
    with origin.human_turn(agent.agent_id):
        assert call() is None  # the running turn of this agent: covered
    assert client.get("/api/yolo/status").get_json()["window"]["auto_approved"] == 1


def test_phase5a_never_floor_and_critical_still_go_to_a_person(make_app, monkeypatch):
    from stiracore.core import BreakLevel
    from stiracore.core.hitl_reasons import CRITICAL_HALT
    from stiracore.yolo import origin

    app, captured = _stira(make_app, monkeypatch)
    client = app.test_client()
    agent = captured["agent"]
    asked = []
    monkeypatch.setattr(
        agent.engine,
        "request_hitl",
        lambda request: (
            asked.append((request.tool_name, request.reason)),
            HITLDecision(action=HITLAction.REJECT),
        )[1],
    )
    _open(client)
    workspace = agent.write_policy.workspace
    data_dir = runtime.desk().yolo._marker.path.parent
    with origin.human_turn(agent.agent_id):
        # The floor: the data directory (the window marker lives there) — refused before
        # anyone is asked, also with the window active.
        floor = _gate_call(agent, "file_write", {"path": str(data_dir / "x.json"), "content": "x"})
        assert floor.cancel_tool and "not sent for approval" in floor.cancel_tool
        # Never: a tightening save, then the call is refused at step 1.
        from tests.test_yolo_browser_confirm import BROWSER as headers

        saved = client.post(
            "/api/config/tools",
            json={"name": "journal", "policy": "never"},
            headers={**headers, "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"]},
        )
        assert saved.status_code == 200, saved.get_json()
        never = _gate_call(agent, "journal", {"action": "write", "content": "x"})
        assert never.cancel_tool and "Never" in never.cancel_tool
        assert asked == []
        # CRITICAL halt: the person's card (reason CRITICAL_HALT), never YOLO.
        agent.engine.config.critical_action = "halt"
        agent.engine.state.stability = 0.1
        agent.engine.pre_turn()
        assert agent.engine.breaker_status().level == BreakLevel.CRITICAL
        halted = _gate_call(agent, "file_write", {"path": str(workspace / "a.md"), "content": "x"})
        assert asked == [("file_write", CRITICAL_HALT)]
        assert halted.cancel_tool and halted.cancel_tool.startswith("HITL rejected")
    window = client.get("/api/yolo/status").get_json()["window"]
    assert window["auto_approved"] == 0 and window["state"] == "paused"
    assert window["pause_reason"] == "breaker_critical"


def test_phase5a_tool_specs_and_prompt_do_not_depend_on_the_window(make_app, monkeypatch):
    """Automatic approval is a gate decision only: the model input is the same with no
    window, with an active one and after approvals (the allowance on/off is pinned in
    ``test_yolo_default_unchanged``)."""
    from stiracore.yolo import origin

    app, captured = _stira(make_app, monkeypatch)
    client = app.test_client()
    agent = captured["agent"]

    def spec_hash():
        specs = [getattr(t, "tool_spec", None) or {"name": t.tool_name} for t in captured["tools"]]
        return hashlib.sha256(json.dumps(specs, sort_keys=True, default=str).encode()).hexdigest()

    before = (spec_hash(), agent.system_prompt, [t.tool_name for t in captured["tools"]])
    _open(client)
    with origin.human_turn(agent.agent_id):
        workspace = agent.write_policy.workspace
        event = _gate_call(agent, "file_write", {"path": str(workspace / "a.md"), "content": "x"})
        assert event.cancel_tool is None
    after = (spec_hash(), agent.system_prompt, [t.tool_name for t in captured["tools"]])
    assert after == before
    # #508: the only YOLO text the model reads is the Tool Availability line on the window.
    yolo_lines = [line for line in agent.system_prompt.splitlines() if "yolo" in line.lower()]
    assert yolo_lines[0] == "YOLO: no window is open." and len(yolo_lines) == 2
    live = [line for line in agent.system_prompt_provider().splitlines() if "yolo" in line.lower()]
    assert live[0].startswith("YOLO: a window is open (internal only)")


def test_nothing_outside_the_yolo_package_reads_the_window():
    """Static: only the YOLO package, its routes and the wiring import the request
    service; no gate, hook or tool module touches the window (phase 5 will)."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    readers = []
    for path in [*root.glob("src/stiracore/**/*.py"), *root.glob("dashboard/*.py")]:
        if "/yolo/" in path.as_posix():
            continue
        text = path.read_text("utf-8")
        if "yolo.requests" in text or "yolo.window" in text or "._win" in text:
            readers.append(path.relative_to(root).as_posix())
    assert readers == []


@pytest.mark.parametrize("path", ["/api/yolo/stop", "/api/yolo/unlatch"])
def test_the_new_routes_keep_the_csrf_check(make_app, path):
    client = make_app("internal").test_client()
    response = client.post(path, json={}, headers={**BROWSER, "X-CSRF-Token": "wrong"})
    assert response.status_code == 403, response.status_code
