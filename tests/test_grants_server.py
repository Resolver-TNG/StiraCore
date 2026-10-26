"""The grant control plane in the chat server (ADR 0011 W4 of #296): the off switch, the
routes, the watchdog look, ``/new``, the shutdown step and a restart. Offline: no model."""

from __future__ import annotations

import json
import threading
from unittest.mock import Mock

import pytest

MARKER = "goal-marker-srv5d1"
SAME = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}


def _build(tmp_path, monkeypatch, gateway: dict):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.observability.config_api import ConfigStore

    data = tmp_path / "data"
    monkeypatch.setattr(stira_server, "DATA_DIR", data)
    monkeypatch.setenv("STIRA_YOLO_ALLOW", "off")
    model = Mock(side_effect=AssertionError("model reached"))
    for method in ("stream", "structured_output", "converse"):
        getattr(model, method).side_effect = AssertionError(f"model.{method} reached")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=model))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda **_kw: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    store.gateway_path.write_text(json.dumps(gateway), encoding="utf-8")
    return lambda: stira_server.create_app(config_store=store), data


def _close(app):
    try:
        app.extensions["lifecycle"].shutdown()
    finally:
        for name in ("heartbeat", "scheduler"):
            if app.extensions.get(name) is not None:
                app.extensions[name].stop()
        lock = app.extensions.get("data_dir_lock")
        if lock is not None:
            lock.release()


@pytest.fixture
def stage_c(tmp_path, monkeypatch):
    from stiracore.yolo import own_dashboard

    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    # These tests drive the browser channel: the user's explicit weaker choice.
    gateway = {"autonomy": {"stage_c": True, "allow_browser_confirm": True}}
    build, data = _build(tmp_path, monkeypatch, gateway)
    app = build()
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = app.extensions["grants"]._policy()
    roots = list(getattr(policy, "write_roots", None) or [])
    if roots:
        ws = roots[0] / "grant-ws"
        ws.mkdir(parents=True, exist_ok=True)
    app.extensions["grants"]._roots = lambda: [*roots, tmp_path / "ws"]
    app.extensions["grants"]._policy = lambda: None
    yield app, data, ws, build
    _close(app)


def _issue(client, ws, goal=f"tidy {MARKER} docs"):
    body = {
        "goal": goal,
        "scope": {"tools": ["file_read", "editor"], "workspace_subtree": str(ws)},
        "budget": {"total_tokens": 1000, "per_tick_tokens": 100},
        "verifier": "user",
        "confirm": "ISSUE",
    }
    first = client.post("/api/grants/issue", json=body, headers=SAME)
    assert first.status_code == 428, first.get_json()
    op = first.get_json()["operation"]
    confirmed = client.post(
        f"/api/grants/operations/{op['id']}/confirm",
        json={"token": op["confirm_token"], "acknowledged": True, "instance": op["instance"]},
        headers=SAME,
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    done = client.post(
        "/api/grants/issue", json=body, headers={**SAME, "X-Stira-Host-Operation": op["id"]}
    )
    assert done.status_code == 200, done.get_json()
    return done.get_json()


def test_with_stage_c_off_nothing_is_registered_created_or_started(tmp_path, monkeypatch):
    build, data = _build(tmp_path, monkeypatch, {})
    before = {t.ident for t in threading.enumerate()}
    app = build()
    try:
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert not [r for r in rules if r.startswith("/api/grants")]
        assert "grants" not in app.extensions
        client = app.test_client()
        assert client.get("/api/grants").status_code == 404
        html = client.get("/settings").get_data(as_text=True)
        assert "sec-grants" not in html and "settings_grants.js" not in html
        assert not (data / "grants.json").exists()
        names = [t.name for t in threading.enumerate() if t.ident not in before]
        assert not [n for n in names if "grant" in n]
    finally:
        _close(app)
    assert "grants" not in app.extensions["lifecycle"].shutdown()


def test_stage_c_on_the_routes_the_panel_and_a_full_issue(stage_c):
    app, data, ws, _build_again = stage_c
    client = app.test_client()
    html = client.get("/settings").get_data(as_text=True)
    assert 'id="sec-grants"' in html and "settings_grants.js" in html
    out = _issue(client, ws)
    grant_id = out["grant"]["grant_id"]
    listed = client.get("/api/grants").get_json()
    assert listed["grants"][0]["state"] == "active"
    assert MARKER not in json.dumps(listed) and str(ws) not in json.dumps(listed)
    one = client.get(f"/api/grants/{grant_id}").get_json()
    assert MARKER not in json.dumps(one)
    refused = client.get(f"/api/grants/{grant_id}/goal?confirm=SHOW")
    assert refused.status_code == 403 and refused.headers["Cache-Control"] == "no-store"
    shown = client.get(
        f"/api/grants/{grant_id}/goal?confirm=SHOW",
        headers={"X-Stira-Goal-View": out["goal_view_token"]},
    )
    assert shown.status_code == 200 and MARKER in shown.get_json()["goal"]
    assert shown.headers["Cache-Control"] == "no-store"
    stored = (data / "grants.json").read_text()
    assert MARKER in stored


def test_the_confirmation_needs_a_same_origin_browser_fetch(stage_c):
    app, _data, ws, _ = stage_c
    client = app.test_client()
    body = {
        "goal": "x",
        "scope": {"tools": ["file_read"], "workspace_subtree": str(ws)},
        "budget": {"total_tokens": 10, "per_tick_tokens": 1},
        "verifier": "user",
        "confirm": "ISSUE",
    }
    op = client.post("/api/grants/issue", json=body, headers=SAME).get_json()["operation"]
    confirm = {"token": op["confirm_token"], "acknowledged": True, "instance": op["instance"]}
    bare = client.post(
        f"/api/grants/operations/{op['id']}/confirm",
        json=confirm,
        headers={"Origin": "http://localhost"},
    )
    assert bare.status_code == 409 and bare.get_json()["host_code"] == "host_browser_refused"


def test_kill_and_the_watchdog_revoke_the_grant(stage_c):
    app, _data, ws, _ = stage_c
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    assert client.post("/api/authority/kill", json={}).status_code == 200
    app.extensions["authority_watchdog"].run_pass()  # the look runs in every pass
    grant = app.extensions["grants"]._grants[grant_id]
    assert (grant.status, grant.reason) == ("revoked", "kill_switch")


def test_new_session_revokes_and_the_breaker_hook_halts(stage_c):
    app, _data, ws, _ = stage_c
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    engine = app.extensions["grants"]
    epoch = app.extensions["authority"].epoch
    app.extensions["grants_breaker_hook"]("normal", "warning")  # the engine's hook
    assert engine._grants[grant_id].status == "halted"
    assert app.extensions["authority"].epoch >= epoch + 2  # the authority's and the stop's
    client.post("/api/grants/" + grant_id + "/revoke", json={})
    second = _issue(client, ws)["grant"]["grant_id"]
    client.post(
        "/api/chat/session/new",
        json={},
        headers={"Idempotency-Key": "k-1", **SAME},
    )
    grant = engine._grants[second]
    assert (grant.status, grant.reason) == ("revoked", "session_reset")


def test_the_shutdown_step_and_a_restart(stage_c, tmp_path, monkeypatch):
    app, _data, ws, build = stage_c
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    result = app.extensions["lifecycle"].shutdown()
    assert result["grants"] == "stopped"
    from dashboard.stira_server import _stopped_line

    assert "grants" not in _stopped_line(result)  # quiet when clean
    assert "grants: unhealthy" in _stopped_line({**result, "grants": "unhealthy"})
    for name in ("heartbeat", "scheduler"):
        if app.extensions.get(name) is not None:
            app.extensions[name].stop()
    app.extensions["data_dir_lock"].release()
    again = build()
    try:
        grant = again.extensions["grants"]._grants[grant_id]
        assert (grant.status, grant.reason, grant.goal_status) == ("revoked", "restart", "unmet")
        events = again.extensions["authority_recorder"].log.events()
        pending = list(again.extensions["authority_recorder"]._pending)
        codes = [e["reason_code"] for e in [*events, *pending] if e.get("subject_id") == grant_id]
        assert "revoked_restart" in codes
    finally:
        _close(again)


# --- fix round 1: human presence, fences --------------------------------------------------


def test_with_yolo_off_a_running_tool_call_refuses_every_grant_operation(stage_c):
    """Opus probe2: YOLO off, a tool call registered as running → issue, confirm and
    resume are refused (the tracker is on the agent's hooks with stage C alone)."""
    from stiracore.yolo import runtime

    app, _data, ws, _ = stage_c
    assert app.extensions["tool_calls_tracked"] is True
    assert runtime.enabled() is False
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    client.post(f"/api/grants/{grant_id}/pause", json={})
    body = {
        "goal": "x",
        "scope": {"tools": ["file_read"], "workspace_subtree": str(ws)},
        "budget": {"total_tokens": 10, "per_tick_tokens": 1},
        "verifier": "user",
        "confirm": "ISSUE",
    }
    token = object()
    runtime.TRACKER.enter(token)
    try:
        resume = client.post(f"/api/grants/{grant_id}/resume", json={}, headers=SAME)
        assert resume.status_code == 409 and resume.get_json()["host_code"] == "tool_call_running"
        client.post(f"/api/grants/{grant_id}/revoke", json={})
        issue = client.post("/api/grants/issue", json=body, headers=SAME)
        assert issue.status_code == 409 and issue.get_json()["host_code"] == "tool_call_running"
    finally:
        runtime.TRACKER.leave(token)
    op = client.post("/api/grants/issue", json=body, headers=SAME).get_json()["operation"]
    runtime.TRACKER.enter(token)
    try:
        confirm = client.post(
            f"/api/grants/operations/{op['id']}/confirm",
            json={"token": op["confirm_token"], "acknowledged": True, "instance": op["instance"]},
            headers=SAME,
        )
        assert confirm.status_code == 409 and confirm.get_json()["host_code"] == "tool_call_running"
    finally:
        runtime.TRACKER.leave(token)


def test_stage_c_off_and_yolo_off_keeps_the_hook_list(tmp_path, monkeypatch):
    build, _data = _build(tmp_path, monkeypatch, {})
    app = build()
    try:
        assert app.extensions["tool_calls_tracked"] is False
    finally:
        _close(app)


def test_the_host_command_channel_refuses_the_browser_confirmation(tmp_path, monkeypatch):
    """Opus probe4: STIRA_YOLO_HOST_CONFIRM=command → the grants use the desk's operations;
    a browser confirmation is refused (host_confirm_on_host) and the host decides."""
    from stiracore.yolo import own_dashboard, runtime
    from stiracore.yolo.allowance import YoloSettings

    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    build, _data = _build(tmp_path, monkeypatch, {"autonomy": {"stage_c": True}})
    runtime._reset_for_tests(YoloSettings("internal", host_confirm="command"))
    app = build()
    try:
        grants = app.extensions["grants"]
        assert grants.channel == "command"
        ws = tmp_path / "ws"
        ws.mkdir()
        grants._roots = lambda: [ws]
        grants._policy = lambda: None
        client = app.test_client()
        body = {
            "goal": "x",
            "scope": {"tools": ["file_read"], "workspace_subtree": str(ws)},
            "budget": {"total_tokens": 10, "per_tick_tokens": 1},
            "verifier": "user",
            "confirm": "ISSUE",
        }
        first = client.post("/api/grants/issue", json=body, headers=SAME)
        op = first.get_json()["operation"]
        assert first.status_code == 428 and op["channel"] == "command"
        assert not op.get("confirm_token")
        refused = client.post(
            f"/api/grants/operations/{op['id']}/confirm",
            json={"token": "0" * 32, "acknowledged": True, "instance": op["instance"]},
            headers=SAME,
        )
        assert refused.status_code == 409
        assert refused.get_json()["host_code"] == "host_confirm_on_host"
        polled = client.get(f"/api/grants/operations/{op['id']}").get_json()["operation"]
        assert polled["state"] == "pending"
        runtime.desk().operations.decide(
            op["id"],
            incarnation=runtime.desk().operations.incarnation,
            decision="confirm",
            principal="local:test",
            channel="command",
        )
        done = client.post(
            "/api/grants/issue", json=body, headers={**SAME, "X-Stira-Host-Operation": op["id"]}
        )
        assert done.status_code == 200 and done.get_json()["grant"]["state"] == "active"
    finally:
        _close(app)
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


def test_the_watchdog_pass_re_reads_the_grant_store(stage_c):
    app, data, ws, _ = stage_c
    client = app.test_client()
    grant_id = _issue(client, ws)["grant"]["grant_id"]
    grants = app.extensions["grants"]
    watchdog = app.extensions["authority_watchdog"]
    # #626: the watchdog's own thread (woken by the issue's and the stop's lifecycle
    # events) could read the store while it is swapped below — a read in flight across the
    # unlink + write sees another inode at its open and correctly says ``unsafe``. Its own
    # passes end first; the pass the test needs runs explicitly.
    assert watchdog.stop_passes(5.0) == "stopped"
    assert grants.stop(5.0) == "stopped"
    path = data / "grants.json"
    text = path.read_text()
    path.unlink()
    path.write_text(text)  # another file behind the store
    path.chmod(0o600)
    watchdog.run_pass()
    assert grants.stop(5.0) == "unhealthy"
    assert grants.unhealthy_reason == "grant_store_replaced"
    assert grants._grants[grant_id].reason == "unhealthy"


def test_a_store_swapped_under_an_open_read_is_unsafe(stage_c, monkeypatch):
    """#626, the flake's mechanism, made deterministic: the store is replaced between the
    reader's ``lstat`` and its ``open`` — the opened file is another inode: ``unsafe``."""
    from stiracore.authority import private_file

    app, data, ws, _ = stage_c
    client = app.test_client()
    _issue(client, ws)
    grants = app.extensions["grants"]
    watchdog = app.extensions["authority_watchdog"]
    assert watchdog.stop_passes(5.0) == "stopped"
    assert grants.stop(5.0) == "stopped"
    path = data / "grants.json"
    text = path.read_text()
    real_open = private_file.os.open
    swapped = []

    def open_after_a_swap(target, flags, *args, **kwargs):
        if str(target) == str(path) and not swapped:
            swapped.append(True)
            path.unlink()
            path.write_text(text)
            path.chmod(0o600)
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(private_file.os, "open", open_after_a_swap)
    watchdog.run_pass()
    assert grants.stop(5.0) == "unhealthy"
    monkeypatch.undo()
    assert swapped and grants.unhealthy_reason == "grant_store_unsafe"


def test_with_yolo_off_and_no_host_confirmation_the_loosenings_are_refused(tmp_path, monkeypatch):
    """Astra r2 B1 (astra-r2-nonbrowser-unset.py): YOLO off, host confirmation unset, no
    tool call running — a plain client cannot issue (and so cannot resume or verify)."""
    from stiracore.yolo import own_dashboard

    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    build, data = _build(tmp_path, monkeypatch, {"autonomy": {"stage_c": True}})
    app = build()
    try:
        grants = app.extensions["grants"]
        assert grants.confirm_layer == "unavailable"
        ws = tmp_path / "ws"
        ws.mkdir()
        grants._roots = lambda: [ws]
        grants._policy = lambda: None
        client = app.test_client()
        body = {
            "goal": "x",
            "scope": {"tools": ["file_read"], "workspace_subtree": str(ws)},
            "budget": {"total_tokens": 10, "per_tick_tokens": 1},
            "verifier": "user",
            "confirm": "ISSUE",
        }
        issue = client.post("/api/grants/issue", json=body, headers=SAME)
        data_ = issue.get_json()
        assert issue.status_code == 409 and data_["host_code"] == "host_channel_required"
        assert (
            "STIRA_YOLO_HOST_CONFIRM" in data_["hint"] and "allow_browser_confirm" in data_["hint"]
        )
        assert "operation" not in data_  # no token handed out
        assert client.get("/api/grants").get_json()["confirm_layer"] == "unavailable"
        assert not (data / "grants.json").exists()
    finally:
        _close(app)
