"""ADR 0012 §8b (phase 2), on the real chat server app (offline, no model call): ``RECOVER``,
loosening saves and scheduler loosenings apply **only** after a host confirmation, while
``STIRA_YOLO_ALLOW`` is set (an approval card's approve / edit is one press since phase 2c) — also when the request is delivered after
the agent's tool call ended (a held handler) or by a client the own-dashboard guard does
not hold (an MCP server / another process: here the test client). Without the allowance
everything answers as on ``main``.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from stiracore.core.engine import BreakLevel, HITLRequest
from stiracore.observability.config_api import ConfigStore
from stiracore.tools.tool_config import RECOVER_WORD
from stiracore.yolo import runtime
from stiracore.yolo.allowance import YoloSettings
from stiracore.yolo.host_ops import HEADER
from tests.yolo_host import confirmed_post, host_decide, post

INPUT = {"path": "notes/a.txt", "content": "hello"}


def _build(tmp_path, monkeypatch, allowance, scheduler=False, host_confirm="command"):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    if scheduler:
        monkeypatch.setenv("STIRA_SCHEDULER", "1")
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
    # These tests pin the host channels (STIRA_YOLO_HOST_CONFIRM=command); the browser
    # channel (the default since revision 6) is pinned by tests/test_yolo_browser_confirm.py.
    runtime._reset_for_tests(YoloSettings(allowance, host_confirm=host_confirm))
    store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
    return stira_server.create_app(config_store=store)


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    made = []

    def make(allowance="internal", **kw):
        app = _build(tmp_path / str(len(made)), monkeypatch, allowance, **kw)
        made.append(app)
        return app

    try:
        yield make
    finally:
        for app in made:
            app.extensions["lifecycle"].shutdown()
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


class Card:
    """A live approval card: a worker waiting in the broker, as a gated tool call does."""

    def __init__(self, app, request_id="c0ffee00" + "0" * 24):
        self.broker = app.extensions["hitl_broker"]
        self.id = request_id
        self.result = {}
        self.thread = threading.Thread(target=self._wait, daemon=True)
        self.thread.start()
        deadline = time.time() + 5
        while not self.pending() and time.time() < deadline:
            time.sleep(0.01)
        assert self.pending(), "the card did not appear"

    def _wait(self):
        self.result["decision"] = self.broker.request(
            HITLRequest(
                tool_name="file_write",
                tool_input=INPUT,
                break_level=BreakLevel.NORMAL,
                reason="Ask",
                tool_use_id="use-1",
                agent_id="stira",
                request_id=self.id,
            )
        )

    def pending(self):
        return [p for p in self.broker.pending() if p["id"] == self.id]

    @property
    def args_hash(self):
        return self.pending()[0]["args_hash"]

    def body(self, action="approve", **extra):
        return {"action": action, "args_hash": self.args_hash, **extra}

    def path(self):
        return f"/api/hitl/{self.id}/decision"

    def decided(self, timeout=5):
        self.thread.join(timeout)
        return self.result.get("decision")


def _required(response):
    data = response.get_json() or {}
    assert response.status_code == 428, (response.status_code, data)
    assert data["code"] == data["error_code"] == "host_confirmation_required"
    return data["operation"]


# --- approvals: one press (phase 2c, operator decision 2026-10-19) ---------------------


def test_without_the_allowance_approve_answers_as_before(make_app):
    app = make_app("off")
    card = Card(app)
    response = post(app.test_client(), card.path(), card.body())
    assert response.status_code == 202 and response.get_json() == {"status": "accepted"}
    assert card.decided().action.value == "approve"
    assert (
        runtime.desk() is None
        and runtime.host_gate(
            "rules_loosen", route="/", body={}, target={}, summary={}, header=None
        )
        is None
    )


@pytest.mark.parametrize("host_confirm", ["command", "browser"])
def test_approve_and_edit_are_one_press_with_the_allowance(make_app, host_confirm):
    """ADR 0012 §8b (2c): a card's approve / edit is a single decision bound to its
    args_hash, not a loosening — no confirmed operation, on either channel."""
    app = make_app(host_confirm=host_confirm)
    client = app.test_client()
    card = Card(app)
    response = post(client, card.path(), card.body())
    assert response.status_code == 202, response.get_json()
    assert card.decided().action.value == "approve"
    edited = Card(app, request_id="c0ffee01" + "0" * 24)
    response = post(
        client, edited.path(), edited.body("edit", edited_input={**INPUT, "content": "e"})
    )
    assert response.status_code == 202, response.get_json()
    assert edited.decided().edited_input["content"] == "e"
    assert runtime.desk().operations.pending() == []  # nothing was requested
    # The decision's own binding still holds: a stale args_hash is refused.
    stale = Card(app, request_id="c0ffee02" + "0" * 24)
    response = post(client, stale.path(), {"action": "approve", "args_hash": "0" * 64})
    assert response.status_code == 409 and stale.pending()


def test_approve_is_still_refused_while_an_agent_tool_call_runs(make_app):
    """The running-tool-call layer of §8b stays on approve / edit (phase 1)."""
    app = make_app()
    card = Card(app)
    token = object()
    runtime.TRACKER.enter(token)
    try:
        response = post(app.test_client(), card.path(), card.body())
    finally:
        runtime.TRACKER.leave(token)
    assert response.status_code == 409 and response.get_json()["code"] == "tool_call_running"
    assert card.pending()


def test_reject_and_respond_never_need_the_host(make_app):
    app = make_app()
    card = Card(app)
    response = post(app.test_client(), card.path(), {"action": "reject"})
    assert response.status_code == 202
    assert card.decided().action.value == "reject"


# --- the confirmed operation's contract (on a loosening tool-rule save) ---------------


LOOSEN = {"name": "http_request", "policy": "ask", "confirm": True}
TIGHTEN_OTHER = {"name": "web_fetch", "policy": "never"}


def _ask_tools(client):
    return client.get("/api/tools/config").get_json()["ask_tools"]


def test_a_loosening_save_without_a_host_confirmation_is_never_applied(make_app):
    client = make_app().test_client()
    operation = _required(post(client, "/api/config/tools", LOOSEN))
    assert operation["kind"] == "rules_loosen" and operation["channel"] == "command"
    assert operation["command"] == f"stira approvals decide {operation['short']}"
    # The same request with the operation, not yet confirmed: refused, nothing saved.
    response = post(client, "/api/config/tools", LOOSEN, op_id=operation["id"])
    assert response.status_code == 409 and response.get_json()["code"] == "host_operation_pending"
    assert "http_request" not in _ask_tools(client)
    host_decide(operation["id"])
    response = post(client, "/api/config/tools", LOOSEN, op_id=operation["id"])
    assert response.status_code == 200, response.get_json()
    assert "http_request" in _ask_tools(client)
    # Tightened again, the re-send of the applied request (a loosening once more): refused.
    assert post(client, "/api/config/tools", {**LOOSEN, "policy": "never"}).status_code == 200
    again = post(client, "/api/config/tools", LOOSEN, op_id=operation["id"], key="another-key")
    assert again.status_code == 409 and again.get_json()["code"] == "host_operation_used"


def test_a_confirmed_operation_is_bound_to_its_body(make_app):
    client = make_app().test_client()
    operation = _required(post(client, "/api/config/tools", LOOSEN))
    host_decide(operation["id"])
    other = {**LOOSEN, "name": "think"}
    response = post(client, "/api/config/tools", other, op_id=operation["id"])
    assert response.status_code == 409 and response.get_json()["code"] == "host_operation_mismatch"
    assert post(client, "/api/config/tools", LOOSEN, op_id=operation["id"]).status_code == 200


def test_delayed_delivery_after_the_tool_call_ended_is_still_not_applied(make_app):
    """The agent's request is built during its tool call and held until the call ended."""
    client = make_app().test_client()
    token = object()
    runtime.TRACKER.enter(token)
    held = {"path": "/api/config/tools", "body": dict(LOOSEN)}  # built during the call
    response = post(client, held["path"], held["body"])
    assert response.get_json()["code"] == "tool_call_running"  # phase 1 layer
    runtime.TRACKER.leave(token)  # the call ended; the handler delivers now
    _required(post(client, held["path"], held["body"]))
    # Forged headers of an operation nobody confirmed do not help either.
    response = post(client, held["path"], held["body"], op_id="0" * 32)
    assert response.get_json()["code"] == "host_operation_unknown"
    assert "http_request" not in _ask_tools(client)


def test_a_target_changed_after_the_request_is_a_stale_operation(make_app):
    client = make_app().test_client()
    operation = _required(post(client, "/api/config/tools", LOOSEN))
    assert post(client, "/api/config/tools", TIGHTEN_OTHER).status_code == 200  # a narrowing
    host_decide(operation["id"])
    response = post(client, "/api/config/tools", LOOSEN, op_id=operation["id"])
    assert response.status_code == 409 and response.get_json()["code"] == "stale_operation"
    assert "http_request" not in _ask_tools(client)


def test_a_denied_or_cancelled_operation_is_never_applied(make_app):
    client = make_app().test_client()
    operation = _required(post(client, "/api/config/tools", LOOSEN))
    host_decide(operation["id"], "deny")
    response = post(client, "/api/config/tools", LOOSEN, op_id=operation["id"])
    assert response.get_json()["code"] == "host_operation_denied"
    operation = _required(post(client, "/api/config/tools", LOOSEN))
    cancelled = client.post(f"/api/host-operations/{operation['id']}/cancel")
    assert cancelled.get_json()["operation"]["state"] == "cancelled"
    response = post(client, "/api/config/tools", LOOSEN, op_id=operation["id"])
    assert response.get_json()["code"] == "host_operation_cancelled"
    assert "http_request" not in _ask_tools(client)


def test_the_operation_status_route(make_app):
    client = make_app().test_client()
    operation = _required(post(client, "/api/config/tools", LOOSEN))
    status = client.get(f"/api/host-operations/{operation['id']}").get_json()["operation"]
    assert status["state"] == "pending" and status["kind"] == "rules_loosen"
    assert set(status["summary"]) == {"source", "changes"}
    assert client.get(f"/api/host-operations/{'1' * 32}").status_code == 404
    assert client.get("/api/host-operations/not-an-id").status_code == 404


# --- tool rules, RECOVER, hub, rollback ----------------------------------------------


@pytest.mark.parametrize("path", ["/api/config/tools", "/api/tools/config"])
def test_loosening_tool_rule_saves_apply_only_after_the_host(make_app, path):
    app = make_app()
    client = app.test_client()
    before = client.get("/api/tools/config").get_json()
    body = (
        LOOSEN
        if path == "/api/config/tools"
        else {
            "enabled_tools": [*before["enabled_tools"], "http_request"],
            "ask_tools": [*before["ask_tools"], "http_request"],
            "confirm": True,
        }
    )
    operation = _required(post(client, path, body))
    assert client.get("/api/tools/config").get_json() == before
    host_decide(operation["id"])
    assert post(client, path, body, op_id=operation["id"]).status_code == 200
    assert client.get("/api/tools/config").get_json() != before


def test_tightening_needs_no_host(make_app):
    client = make_app().test_client()
    response = post(client, "/api/config/tools", {"name": "http_request", "policy": "never"})
    assert response.status_code == 200


def test_recover_needs_the_host(make_app):
    client = make_app().test_client()
    response = post(client, "/api/tools/config", {"recover": RECOVER_WORD})
    operation = _required(response)
    assert operation["kind"] == "recover"
    assert runtime.desk().operations.status(operation["id"])["word"] == "RECOVER"


def _hub_body(client, body):
    """Answer the hub's own confirmations first (as the page does)."""
    for _ in range(3):
        response = post(client, "/api/config/hub", body)
        data = response.get_json() or {}
        if data.get("error_code") == "needs_confirmation":
            body["acknowledged_dangers"] = data["needs_confirmation"]
        elif data.get("error_code") == "needs_tool_names":
            body["confirm_always"] = data["needs_tool_names"]
        else:
            return response, body
    return response, body


def test_a_loosening_hub_save_needs_the_host(make_app):
    client = make_app().test_client()
    response, body = _hub_body(client, {"changes": {"tools": {"mode": "trust_all"}}})
    operation = _required(response)
    assert operation["kind"] == "rules_loosen"
    assert client.get("/api/config/hub").get_json()["values"]["tools"]["mode"] != "trust_all"
    host_decide(operation["id"])
    assert post(client, "/api/config/hub", body, op_id=operation["id"]).status_code == 200
    assert client.get("/api/config/hub").get_json()["values"]["tools"]["mode"] == "trust_all"


def test_a_rollback_that_loosens_needs_the_host(make_app):
    client = make_app().test_client()
    response, _ = _hub_body(client, {"changes": {"tools": {"enabled_tools": []}}})
    assert response.status_code == 200  # tightening: no host
    entry = client.get("/api/config/history").get_json()["entries"][0]["id"]
    path = f"/api/config/history/{entry}/rollback"
    body = {}
    for _ in range(3):
        response = post(client, path, body)
        data = response.get_json() or {}
        if data.get("error_code") == "needs_confirmation":
            body["acknowledged_dangers"] = data["needs_confirmation"]
        elif data.get("error_code") == "needs_tool_names":
            body["confirm_always"] = data["needs_tool_names"]
        else:
            break
    operation = _required(response)
    host_decide(operation["id"])
    assert post(client, path, body, op_id=operation["id"]).status_code == 200


# --- scheduler ------------------------------------------------------------------------


def test_scheduler_create_and_resume_need_the_host_pause_does_not(make_app):
    from stiracore.control.harvest_job import ACTION_NAME

    app = make_app(scheduler=True)
    client = app.test_client()
    job = {
        "job_id": "nightly",
        "name": "nightly",
        "action": ACTION_NAME,
        "schedule": {"type": "interval", "every_sec": 3600, "anchor": 1_800_000_000.0},
    }
    body = {"job": job, "confirm": True}
    operation = _required(post(client, "/api/scheduler/jobs", body))
    summary = runtime.desk().operations.status(operation["id"])["summary"]
    assert summary == {"operation": "create", "job": "-"}  # never the job's text
    assert client.get("/api/scheduler").get_json()["jobs"] == []
    host_decide(operation["id"])
    assert post(client, "/api/scheduler/jobs", body, op_id=operation["id"]).status_code == 200
    paused = post(client, "/api/scheduler/jobs/nightly/pause", {})
    assert paused.status_code == 200  # a narrowing
    response = post(client, "/api/scheduler/jobs/nightly/resume", {"confirm": True})
    assert runtime.desk().operations.status(_required(response)["id"])["summary"]["job"] == (
        "#" + runtime.target_digest("nightly")[:8]
    )
    runtime.desk().operations.cancel(response.get_json()["operation"]["id"])
    resume = confirmed_post(client, "/api/scheduler/jobs/nightly/resume", {"confirm": True})
    assert resume.status_code == 200, resume.get_json()
    assert client.get("/api/scheduler").get_json()["jobs"][0]["state"] == "active"


# --- a client the own-dashboard guard does not hold (an MCP server, another process) --


def test_an_outside_client_with_the_csrf_token_cannot_loosen_or_open(make_app):
    """It cannot loosen or open YOLO without a confirmation. It **can** approve a pending
    card in one press — as without the allowance (phase 2c; ADR 0012 §8b cost / §8a
    residual: a same-user program outside the server is out of scope)."""
    app = make_app("external")
    card = Card(app)
    client = app.test_client()  # stands in for an MCP server process on loopback
    token = client.get("/api/csrf").get_json()["token"]
    assert token  # it can read the token: CSRF does not stop it
    forged = [
        ("/api/tools/config", {"recover": RECOVER_WORD}),
        ("/api/config/tools", LOOSEN),
    ]
    for path, body in forged:
        response = post(client, path, body)
        assert response.status_code in (428, 429), (path, response.get_json())
        response = post(client, path, body, op_id="f" * 32)
        assert response.status_code in (409, 429), path
    yolo = client.post("/api/yolo/request", json={"tier": "external"})
    assert yolo.status_code == 202
    status = client.get("/api/yolo/status").get_json()
    assert status["window"] is None and status["request"]["state"] == "pending"
    assert "http_request" not in client.get("/api/tools/config").get_json()["enabled_tools"]
    assert post(client, card.path(), card.body()).status_code == 202  # the documented residual
    assert card.decided().action.value == "approve"


# --- the YOLO request routes ----------------------------------------------------------


def test_yolo_request_route_creates_a_request_the_host_confirms_and_it_opens(make_app):
    app = make_app("internal")
    client = app.test_client()
    response = client.post("/api/yolo/request", json={"tier": "internal"})
    assert response.status_code == 202, response.get_json()
    data = response.get_json()
    assert data["opening"] == "on_confirmation"
    assert data["command"] == f"stira yolo confirm {data['request']['short']}"
    again = client.post("/api/yolo/request", json={"tier": "internal"})
    assert again.status_code == 409 and again.get_json()["code"] == "yolo_request_pending"
    assert client.post("/api/yolo/request", json={"tier": "external"}).status_code == 409
    host_decide(data["request"]["id"])
    status = client.get("/api/yolo/status").get_json()
    # Phase 3: the window opens (still no button: ``offered`` stays false).
    assert status["window"]["state"] in ("active", "paused") and status["offered"] is False
    assert status["request"]["state"] == "opened" and status["latched"] is False


def test_yolo_request_cancel_route(make_app):
    client = make_app("internal").test_client()
    request_id = client.post("/api/yolo/request", json={"tier": "internal"}).get_json()["request"][
        "id"
    ]
    response = client.post("/api/yolo/request/cancel", json={"request_id": request_id})
    assert response.get_json()["request"]["state"] == "cancelled"
    assert client.post("/api/yolo/request/cancel", json={}).status_code == 409


def test_the_yolo_and_host_routes_need_the_csrf_token(make_app):
    client = make_app("internal").test_client()
    for path, body in (
        ("/api/yolo/request", {"tier": "internal"}),
        ("/api/yolo/request/cancel", {}),
        (f"/api/host-operations/{'a' * 32}/cancel", {}),
    ):
        response = client.post(path, json=body, headers={"X-Test-No-Csrf": "1"})
        assert response.status_code == 403, path
    assert runtime.desk().yolo.status()["request"] is None


def test_the_host_channel_descriptor_exists_only_with_the_allowance(make_app, tmp_path):
    from stiracore.yolo.host_channel import DESCRIPTOR_NAME

    make_app("off")
    assert not (tmp_path / "0" / "data" / DESCRIPTOR_NAME).exists()
    make_app("internal")
    assert (tmp_path / "1" / "data" / DESCRIPTOR_NAME).exists()


def test_the_header_name_is_the_one_the_page_sends():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert HEADER in (root / "dashboard/static/js/api.js").read_text("utf-8")
    assert HEADER in (root / "dashboard/stage/js/approval.js").read_text("utf-8")
