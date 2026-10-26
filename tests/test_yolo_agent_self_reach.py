"""ADR 0012 §8b (phase 1), end to end: the agent's own ``http_request`` on a live chat server.

Real app on the offline scripted model of tests/test_new_session.py, served on a real
loopback port, ``http_request`` set to Always with the upstream confirmation bypassed
(the worst operator settings), ``STIRA_YOLO_ALLOW=internal``.

* With the own-dashboard guard armed (as ``main()`` does): the agent cannot even read
  ``/api/csrf``.
* With the guard *not* armed — standing in for a path the guard cannot hold (an MCP
  server or another process of the same user) — a loosening it sends during its own
  tool call is refused with ``tool_call_running`` and nothing is loosened.
"""

from __future__ import annotations

import json
import threading

import pytest

from stiracore.yolo import own_dashboard, runtime
from stiracore.yolo.allowance import YoloSettings
from tests.approval_helpers import approving_cards, set_rule
from tests.test_new_session import make_stira
from tests.yolo_host import confirmed_post

WAIT = 5


@pytest.fixture
def stira(tmp_path, monkeypatch):
    runtime._reset_for_tests(YoloSettings("internal"))
    try:
        yield from make_stira(tmp_path, monkeypatch)
    finally:
        own_dashboard.disarm()
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


@pytest.fixture
def served(stira, monkeypatch):
    pytest.importorskip("strands_tools.http_request")
    from werkzeug.serving import make_server

    app, client = stira["app"], stira["client"]
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    saved = confirmed_post(  # the operator's own save, confirmed on the host (phase 2)
        client,
        "/api/config/tools",
        {
            "name": "http_request",
            "enabled": True,
            "policy": "always",
            "confirm": True,
            "confirm_always": ["http_request"],
        },
    )
    assert saved.status_code == 200, saved.get_json()
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    try:
        yield stira, server.server_port
    finally:
        server.shutdown()
        thread.join(WAIT)


def _tool_outputs(stira, body):
    turn_id = json.loads(
        next(b for b in body.split("\n\n") if b.startswith("event: turn\n")).split("data: ", 1)[1]
    )["turn_id"]
    events = stira["client"].get(f"/api/turns/{turn_id}/events").get_json()["events"]
    return [
        e["data"].get("output_preview") or ""
        for e in events
        if e["type"] == "tool_call.updated" and e["data"].get("name") == "http_request"
    ]


def _call(stira, request):
    stira["model"].script = [
        [{"toolUse": {"name": "http_request", "toolUseId": "self-call", "input": request}}],
        [{"text": "tried"}],
    ]
    return _tool_outputs(stira, stira["chat"]("call your own dashboard"))


def test_agent_http_request_cannot_read_the_csrf_token_when_armed(served):
    stira, port = served
    control = _call(stira, {"method": "GET", "url": f"http://127.0.0.1:{port}/api/csrf"})
    assert any("Status Code: 200" in output for output in control), control  # reachable
    own_dashboard.arm("127.0.0.1", port)
    token = stira["token"]
    for url in (f"http://127.0.0.1:{port}/api/csrf", f"http://localhost:{port}/api/csrf"):
        outputs = _call(stira, {"method": "GET", "url": url})
        assert outputs, url
        assert all(token not in output for output in outputs), url
        assert all("Status Code" not in output for output in outputs), outputs
        # #495: refused before anything is sent, with the reason code.
        assert any("E_OWN_DASHBOARD" in output for output in outputs), outputs


def test_loosening_sent_during_the_tool_call_is_refused_and_nothing_loosens(served):
    stira, port = served  # guard not armed: a path it does not hold
    client = stira["client"]
    # Since #547 a POST goes out only after a person approved it (BYPASS_TOOL_CONSENT is
    # ignored): Ask, and the card is approved; the running tool call must still refuse.
    saved = set_rule(client, "http_request", "ask", post=confirmed_post)
    assert saved.status_code == 200, saved.get_json()
    before = client.get("/api/tools/config").get_json()
    with approving_cards(client, stira["token"]) as approved:
        outputs = _call(
            stira,
            {
                "method": "POST",
                "url": f"http://127.0.0.1:{port}/api/config/tools",
                "headers": {
                    "X-CSRF-Token": stira["token"],
                    "Content-Type": "application/json",
                },
                "body": json.dumps(
                    {
                        "name": "file_write",
                        "policy": "always",
                        "confirm": True,
                        "confirm_always": ["file_write"],
                    }
                ),
            },
        )
    assert [card["tool_name"] for card in approved] == ["http_request"]
    assert any("409" in output and "tool_call_running" in output for output in outputs), outputs
    assert client.get("/api/tools/config").get_json() == before
    assert runtime.TRACKER.running() == 0  # the call ended: the person's save goes through
