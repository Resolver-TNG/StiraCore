"""#547: a modifying ``http_request`` runs only inside a person's approval of that call.

The upstream tool asks ``[y/*]`` on the terminal before POST/PUT/PATCH/DELETE. Stira's
consent bridge answers it (never the terminal, never ``BYPASS_TOOL_CONSENT``) only while
the gate mark of a person-approved call is being used; a GET asks nothing and needs none.
The live-app paths (approve / reject / Always) are in ``tests/attack/test_a4_external_send``.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stiracore.agent.checkpoint_hooks import ApprovalBindingVerifier
from stiracore.tools import tool_consent, tool_registry
from stiracore.tools.http_consent import HttpConsentGatedTool
from stiracore.tools.tool_consent import mark_gate_passed
from tests.attack.sink import Sink

pytest.importorskip("strands_tools.http_request")
NAME = "http_request"
MODULE = tool_consent.CONSENT_TOOL_MODULES[NAME]


@pytest.fixture
def sink():
    sink = Sink()
    yield sink
    sink.close()


@pytest.fixture
def terminal(monkeypatch):
    """The real terminal prompt: never to be read."""
    read = Mock(side_effect=AssertionError("terminal read"))
    monkeypatch.setattr("strands_tools.utils.user_input.get_user_input_async", read)
    monkeypatch.setattr("strands_tools.utils.user_input.get_user_input", read)
    return read


def _tool():
    tool = tool_registry.WRITE_TOOLS[NAME]
    inner = tool
    while not isinstance(inner, HttpConsentGatedTool):
        inner = inner.wrapped  # the own-dashboard guard is outside
    return tool


def _use(url: str, method: str = "POST", use_id: str = "h1") -> dict:
    return {
        "toolUseId": use_id,
        "name": NAME,
        "input": {"method": method, "url": url, "body": "{}"},
    }


async def _collect(tool, use: dict, state: dict) -> dict:
    state.setdefault("agent", None)
    events = [event async for event in tool.stream(use, state)]
    return events[-1].tool_result


def _run(use: dict, state: dict | None = None) -> dict:
    return asyncio.run(_collect(_tool(), use, {} if state is None else state))


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def test_the_bridge_covers_http_request_and_ignores_the_real_environment(monkeypatch):
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    assert NAME in tool_consent.install_consent_bridge()
    module = sys.modules[MODULE]
    assert module.get_user_input.stira_consent_bridge == NAME
    assert module.os.environ.get("BYPASS_TOOL_CONSENT") is None
    with tool_consent.consent_granted(NAME):
        assert module.os.environ.get("BYPASS_TOOL_CONSENT") == "true"
        assert module.get_user_input("proceed? [y/*]") == "y"
    assert module.os.environ.get("PATH") == __import__("os").environ.get("PATH")


@pytest.mark.parametrize("bypass", [None, "true"])
def test_a_post_without_a_gate_mark_is_refused_and_never_sent(monkeypatch, sink, terminal, bypass):
    if bypass:
        monkeypatch.setenv("BYPASS_TOOL_CONSENT", bypass)
    result = _run(_use(f"{sink.url}/in"))
    assert result["status"] == "error" and "no Stira gate approval" in _text(result)
    assert sink.hits == [] and not terminal.called


def test_an_approved_post_is_sent_once_and_its_mark_does_not_replay(sink, terminal):
    use = _use(f"{sink.url}/in")
    state: dict = {}
    assert mark_gate_passed(state, use)
    first = _run(use, state)
    assert first["status"] == "success", first
    again = _run(use, state)
    assert again["status"] == "error" and "no Stira gate approval" in _text(again)
    assert [(h[0], h[1]) for h in sink.hits] == [("POST", "/in")]
    assert not terminal.called


def test_arguments_changed_after_the_gate_are_refused(sink, terminal):
    use = _use(f"{sink.url}/in")
    state: dict = {}
    assert mark_gate_passed(state, use)
    result = _run({**use, "input": {**use["input"], "body": '{"other": 1}'}}, state)
    assert result["status"] == "error" and "arguments changed" in _text(result)
    assert sink.hits == []


def test_a_get_runs_without_a_mark(sink, terminal):
    result = _run(_use(f"{sink.url}/g", method="GET"))
    assert result["status"] == "success", result
    assert [h[1] for h in sink.hits] == ["/g"] and not terminal.called


def test_an_unverifiable_bridge_refuses_the_post_without_the_terminal(monkeypatch, sink, terminal):
    monkeypatch.delattr(sys.modules[MODULE], "get_user_input")
    use = _use(f"{sink.url}/in")
    state: dict = {}
    assert mark_gate_passed(state, use)
    result = _run(use, state)
    assert result["status"] == "error"
    assert "cannot route the upstream terminal confirmation" in _text(result)
    assert sink.hits == [] and not terminal.called


def test_the_registry_drops_http_request_when_its_bridge_cannot_be_verified(monkeypatch):
    monkeypatch.delattr(sys.modules[MODULE], "get_user_input")
    tools = {NAME: object(), "current_time": object()}
    assert tool_registry._drop_unbridged(tools) == {NAME}


def _gate_event(method: str):
    use = {"toolUseId": "g1", "name": NAME, "input": {"method": method, "url": "http://x/"}}
    return SimpleNamespace(tool_use=use, invocation_state={}, cancel_tool=False)


@pytest.mark.parametrize("method", ["POST", "put", "PATCH", "DELETE"])
def test_the_gate_marks_a_modifying_call_only_for_a_person(method):
    verifier = ApprovalBindingVerifier(engine=Mock())
    refused = _gate_event(method)
    verifier._issue_gate_mark(refused, exec_mark_allowed=False)
    assert "person approved this exact call" in refused.cancel_tool
    assert method.upper() in refused.cancel_tool
    assert not refused.invocation_state.get(tool_consent.GATE_PASSED_KEY)
    approved = _gate_event(method)
    verifier._issue_gate_mark(approved, exec_mark_allowed=True)
    assert approved.cancel_tool is False
    assert "g1" in approved.invocation_state[tool_consent.GATE_PASSED_KEY]


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_the_gate_leaves_other_methods_alone(method):
    event = _gate_event(method)
    ApprovalBindingVerifier(engine=Mock())._issue_gate_mark(event, exec_mark_allowed=False)
    assert event.cancel_tool is False
    assert not event.invocation_state.get(tool_consent.GATE_PASSED_KEY)
