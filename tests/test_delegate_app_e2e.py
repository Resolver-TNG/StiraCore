"""The real chat server process with ``delegate`` (#486, ADR 0016 phase 1), offline.

``tests/stira_headless_harness.py`` against ``tests/fake_bedrock_endpoint.py`` (the
server's own ``BedrockModel``; no cloud call). Fresh install, delegation on by default.

Measured: ``delegate`` is offered and its call is a person's card (Ask); approved, the child
runs ``file_read`` (Always on a fresh install) through its own gate; the child's tool row is
in the parent's turn as a name only (no input / output preview); the child is not given
``file_write`` (Ask on a fresh install) and nothing is written, with no card for it; the
parent's turn usage includes the child's model calls. Switched off on the settings page,
the tool is no longer offered at the next turn.
"""

from __future__ import annotations

import json

import pytest

from tests.fake_bedrock_endpoint import FakeBedrock
from tests.test_headless_file_tools_e2e import POSIX_ONLY, Server


@pytest.fixture
def fake():
    with FakeBedrock() as endpoint:
        yield endpoint


def _offered(fake, index=-1) -> list[str]:
    body = fake.requests[index]["body"]
    return [tool["toolSpec"]["name"] for tool in (body.get("toolConfig") or {}).get("tools", [])]


def _events(server, turn_id) -> list[dict]:
    return json.loads(server.request("GET", f"/api/turns/{turn_id}/events")[1])["events"]


@POSIX_ONLY
def test_delegate_in_the_real_app(tmp_path, fake):
    server = Server(tmp_path, fake, "devnull")
    try:
        (server.workspace).mkdir(parents=True, exist_ok=True)
        (server.workspace / "notes.txt").write_text("alpha line\n", encoding="utf-8")
        task = 'CHILDCALL file_read {"path": "notes.txt", "mode": "view"} ENDCALL'
        done = server.turn(
            {"task": task, "tools": ["file_read", "file_write"]}, "approve", "delegate"
        )
        assert done["card"] is not None and done["card"]["tool_name"] == "delegate"
        assert "delegate" in _offered(fake, 0)
        # the child's own model call: only file_read was given (file_write is Ask)
        child_requests = [
            r for r in fake.requests if "delegated worker" in json.dumps(r["body"].get("system"))
        ]
        assert child_requests, "the child never called the model"
        assert {name for name in _offered(fake, fake.requests.index(child_requests[0]))} == {
            "file_read"
        }
        result = next(r for r in fake.tool_results() if "status: " in json.dumps(r))
        text = " ".join(c.get("text", "") for c in result["content"])
        assert text.startswith("status: done"), text
        assert "not given: file_write" in text and "tools: 1 calls (file_read×1)" in text
        # the turn: the child's row by name only; usage counts parent + child calls
        turn = json.loads(server.request("GET", "/api/turns?limit=1")[1])["turns"][0]
        events = _events(server, turn["turn_id"])
        rows = [e["data"] for e in events if e["type"].startswith("tool_call.")]
        child_rows = [r for r in rows if r["name"].startswith("↳ ")]
        assert child_rows and all(r["name"].endswith("file_read") for r in child_rows)
        assert all(
            r.get("input_preview") is None and r.get("output_preview") is None for r in child_rows
        )
        assert not any("input_stored" in r or "output_stored" in r for r in child_rows)
        completed = next(e["data"] for e in events if e["type"] == "turn.completed")
        model_calls = len(fake.requests)
        assert completed["usage"]["input_tokens"] == 10 * model_calls  # parent 2 + child 2
        # no card was raised for anything but delegate; nothing written
        assert not (server.workspace / "x.txt").exists()

        # switched off on the settings page -> not offered at the next turn
        state = json.loads(server.request("GET", "/api/config/hub")[1])
        status, body = server.request(
            "POST",
            "/api/config/hub",
            {"changes": {"delegation": {"enabled": False}}, "base_version": state["version"]},
        )
        assert status == 200, body
        before = len(fake.requests)
        status, _ = server.request("POST", "/api/chat", {"message": "hello"})
        assert status == 200
        assert "delegate" not in _offered(fake, before)
    finally:
        server.stop()


def test_delegate_is_offered_only_in_a_persons_turn():
    """M4 (#493 review): ``_stira_tools`` adds ``delegate`` only inside a person's chat /
    Stage turn (the route's human-turn mark); outside it (webhook, bot, approval resume,
    any unattended turn) the tool is not in the list — no card nobody watches, no tokens."""
    from dashboard.stira_server import _stira_tools
    from stiracore.tools.delegate_tool import DelegateTool, DelegationSettings
    from stiracore.yolo import origin

    tool = DelegateTool(settings=DelegationSettings)

    def names(level=None) -> set:
        return {
            getattr(t, "tool_name", None)
            for t in _stira_tools([], set(), level, delegate_tool=tool)
        }

    assert "delegate" not in names()
    with origin.human_turn("stira"):
        assert "delegate" in names()
        assert "delegate" in names("coherence")  # writes_allowed: COHERENCE writes
        assert "delegate" not in names("warning")
    with origin.human_turn("stira", automatic=True):  # a window's automatic continue
        assert origin.covered("stira")  # YOLO still covers it (ADR 0014)
        assert "delegate" not in names()  # round 2 minor 3: nobody may be watching
    assert "delegate" not in names()


@POSIX_ONLY
def test_an_unattended_webhook_turn_is_not_offered_delegate(tmp_path, fake, monkeypatch):
    """M4, the real app: a chat turn's model request lists ``delegate``; a signed webhook
    turn (same agent, same runner, no person) does not."""
    import hashlib
    import hmac
    import urllib.request

    monkeypatch.setenv("WEBHOOK_SECRET_GENERIC", "test-secret")
    server = Server(tmp_path, fake, "devnull")
    try:
        status, _ = server.request("POST", "/api/chat", {"message": "hello"})
        assert status == 200
        assert "delegate" in _offered(fake)
        before = len(fake.requests)
        body = json.dumps({"event": "created"}).encode()
        digest = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            server.base + "/api/webhooks/generic", data=body, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Hub-Signature-256", "sha256=" + digest)
        with urllib.request.urlopen(req, timeout=120) as response:
            assert response.status == 200
        assert len(fake.requests) > before
        webhook_tools = _offered(fake, before)
        assert webhook_tools and "delegate" not in webhook_tools
        assert "file_read" in webhook_tools  # the rest of the list is unchanged
    finally:
        server.stop()


@POSIX_ONLY
def test_a_spoofing_label_or_child_tool_name_is_stored_cleaned(tmp_path, fake):
    """#493 round 3 major-1, the real app and its event store (turns.db): the parent
    model's label carries a right-to-left override, a zero-width space, a fullwidth ``<``
    and a line break; the child calls an unregistered name with a trailing line break (the
    SDK's name check lets that one through). The stored rows hold neither, the label is
    one line, and the row reads ``↳ <label> · no_such_tool``."""
    import unicodedata

    server = Server(tmp_path, fake, "devnull")
    try:
        label = "\u202erea\u200bder\uff1cx\n\u21b3 ok"
        task = "CHILDCALL no_such_tool\n {} ENDCALL"
        done = server.turn({"task": task, "label": label}, "approve", "delegate")
        assert done["card"] is not None
        turn = json.loads(server.request("GET", "/api/turns?limit=1")[1])["turns"][0]
        rows = [
            e["data"]
            for e in _events(server, turn["turn_id"])
            if e["type"].startswith("tool_call.")
        ]
        child_rows = [r for r in rows if r["name"].startswith("\u21b3 ")]
        assert child_rows, rows
        for row in child_rows:
            name = row["name"]
            assert name == "\u21b3 reader&lt;x \u21b3 ok \u00b7 no_such_tool", name
            assert not any(unicodedata.category(c) in ("Cf", "Cc") for c in name), name
    finally:
        server.stop()
