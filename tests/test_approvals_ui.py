"""#189: approval queue/cards/history backed by the bound HITL broker (#146).

A fake agent (scripted model + fake write tool) raises real approval requests at
CRITICAL HALT; the HTTP API that the chat cards, the nav chip and the approvals
page share is exercised end to end, including CSRF and idempotency keys.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from flask import Flask, render_template
from strands import tool
from test_subagent import ScriptedModel

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import FileCheckpointStore, NeuralState, StiraConfig
from stiracore.core.approval import args_hash
from stiracore.observability.chat_api import HITLApprovalBroker, create_chat_blueprint
from stiracore.observability.security import install_security
from stiracore.observability.shell import STIRA_NAV, install_shell

DASH = Path(__file__).resolve().parents[1] / "dashboard"


def _fake_agent(tmp_path, broker, writes, calls):
    @tool
    def file_write(path: str, content: str, cwd: str = "") -> str:
        """Write a file (fake: records the call only)."""
        writes.append((path, content))
        return "ok"

    model = ScriptedModel([[{"toolUse": call}] for call in calls] + [[{"text": "turn continued"}]])
    return NXPAgent(
        model=model,
        state=NeuralState(stability=0.1),
        config=StiraConfig(critical_action="halt"),
        on_hitl_request=broker.request,
        tools=[file_write],
        agent_id="fake-agent",
        checkpoint_store=FileCheckpointStore(tmp_path / "checkpoints"),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )


def _client(broker):
    app = Flask(__name__)
    settings = install_security(app, idempotent_endpoints=("/api/hitl/*/decision",))
    app.register_blueprint(
        create_chat_blueprint(object(), runner=lambda message: "unused", hitl_broker=broker)
    )
    client = app.test_client()
    return client, {"X-CSRF-Token": settings.token}


def _pending(client, count=1, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get("/api/hitl/pending").json
        if len(body["requests"]) >= count:
            return body
        time.sleep(0.01)
    raise AssertionError("no approval request appeared")


CALL = {
    "toolUseId": "w1",
    "name": "file_write",
    "input": {"path": "notes.md", "content": "hello", "cwd": "/work/project"},
}


def test_fake_agent_request_is_displayed_bound_and_decided_once_with_history(tmp_path):
    broker = HITLApprovalBroker(turn_id_provider=lambda: "turn-0001")
    client, csrf = _client(broker)
    writes = []
    agent = _fake_agent(tmp_path, broker, writes, [CALL])
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(agent, "write it")
        body = _pending(client)
        (request,) = body["requests"]
        assert isinstance(body["server_time"], float)
        # What the card shows: target, cwd, normalized args, expiry, why.
        assert request["tool_name"] == "file_write" and request["agent_id"] == "fake-agent"
        assert request["cwd"] == "/work/project"
        assert request["normalized_input"] == (
            '{"content":"hello","cwd":"/work/project","path":"notes.md"}'
        )
        assert request["args_hash"] == args_hash(CALL["input"])
        assert request["expires_at"] > body["server_time"]
        assert request["break_level"] == "critical" and "CRITICAL HALT" in request["reason"]
        assert request["turn_id"] == "turn-0001"

        url = f"/api/hitl/{request['id']}/decision"
        payload = {"action": "approve", "args_hash": request["args_hash"], "client_id": "tab-a"}
        # Without the CSRF token the decision never reaches the broker.
        no_csrf = client.post(url, json=payload, headers={"X-Test-No-Csrf": "1"})
        assert no_csrf.status_code == 403
        first = client.post(url, json=payload, headers={**csrf, "Idempotency-Key": "k-1"})
        assert first.status_code == 202
        # Retransmission (same key) and a second tab (new key) are both refused.
        again = client.post(url, json=payload, headers={**csrf, "Idempotency-Key": "k-1"})
        assert again.status_code == 409 and again.json["code"] == "duplicate_request"
        other = client.post(
            url, json={**payload, "client_id": "tab-b"}, headers={**csrf, "Idempotency-Key": "k-2"}
        )
        assert (other.status_code, other.json["code"]) == (409, "already_decided")
        assert str(turn.result(timeout=5)).strip() == "turn continued"
    assert writes == [("notes.md", "hello")], "approved exactly once"
    assert client.get("/api/hitl/pending").json["requests"] == []
    (entry,) = client.get("/api/hitl/history").json["history"]
    assert entry["status"] == "approved" and entry["client_id"] == "tab-a"
    assert entry["resolver"] == "operator" and entry["turn_id"] == "turn-0001"
    assert entry["normalized_input"] == request["normalized_input"]
    assert "tool_input" not in entry


def test_reject_reason_reaches_history_and_agent_and_expiry_is_recorded(tmp_path):
    clock = {"now": 1000.0}
    broker = HITLApprovalBroker(now_fn=lambda: clock["now"])
    client, csrf = _client(broker)
    writes = []
    second = {**CALL, "toolUseId": "w2", "input": {"path": "b.md", "content": "x"}}
    agent = _fake_agent(tmp_path, broker, writes, [CALL, second])
    agent.engine._now_fn = lambda: clock["now"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(agent, "write both")
        request = _pending(client)["requests"][0]
        response = client.post(
            f"/api/hitl/{request['id']}/decision",
            json={"action": "reject", "reason": "wrong file"},
            headers={**csrf, "Idempotency-Key": "r-1"},
        )
        assert response.status_code == 202
        # Second request: nobody answers (UI unreachable) -> expires -> rejected.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            pending = client.get("/api/hitl/pending").json["requests"]
            if pending and pending[0]["id"] != request["id"]:
                break
            time.sleep(0.01)
        clock["now"] += 121  # past the engine-bound expiry (hitl_timeout_sec=120)
        assert str(turn.result(timeout=5)).strip() == "turn continued"
    assert writes == []
    history = client.get("/api/hitl/history?limit=10").json["history"]
    assert [(h["status"], h["resolver"]) for h in history] == [
        ("expired", "system"),
        ("rejected", "operator"),
    ]
    assert history[1]["resolution_reason"] == "wrong file"
    results = [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    texts = [r["content"][0]["text"] for r in results]
    assert any("wrong file" in text for text in texts), texts
    assert any("approval expired" in text for text in texts), texts
    assert client.get("/api/hitl/history?limit=0").status_code == 400


def test_approvals_page_nav_and_chip_share_the_queue():
    from test_ui_observer_pages import _parse

    from stiracore.observability.i18n import install_i18n

    app = Flask(
        __name__, template_folder=str(DASH / "templates"), static_folder=str(DASH / "static")
    )
    install_i18n(app)
    install_shell(app, STIRA_NAV)
    install_security(app)
    app.register_blueprint(
        create_chat_blueprint(
            object(), runner=lambda message: "unused", hitl_broker=HITLApprovalBroker()
        )
    )
    app.add_url_rule("/approvals", "approvals", lambda: render_template("approvals.html"))
    client = app.test_client()
    html = client.get("/approvals").get_data(as_text=True)
    assert ("/approvals", "page") in _parse(html).nav
    assert 'href="/approvals" id="nav-approvals"' in html
    assert 'data-approvals-url="/api/hitl/pending"' in html
    for needle in ('id="approval-list"', 'id="approval-history"', 'id="approval-live"'):
        assert needle in html
    chat = client.get("/chat").get_data(as_text=True)
    assert 'id="approval-list"' in chat and "js/approvals.js" in chat
