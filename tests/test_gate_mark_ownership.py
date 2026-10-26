"""#255: a gate mark is released only by the call it was issued to, never FIFO by id.

The unit tests drive ``ApprovalBindingVerifier`` with stand-in events; the
integration tests run the real SDK ``Agent`` with ``ConcurrentToolExecutor`` and
two calls that share a ``toolUseId``, ordered with hooks so the sequences from
the issue happen deterministically (reverse completion; a cancelled call that
was never marked finishing while another call's mark is outstanding).
"""

from __future__ import annotations

import asyncio
import types
from typing import Any
from unittest.mock import Mock

from strands import Agent
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.tools.executors import ConcurrentToolExecutor
from strands.tools.tools import AgentTool
from strands.types._events import ToolResultEvent
from test_subagent import ScriptedModel

from stiracore.agent.checkpoint_hooks import ApprovalBindingVerifier
from stiracore.tools.tool_consent import (
    GATE_ISSUED_KEY,
    GATE_PASSED_KEY,
    GateMark,
    claim_gate,
)


def _use(role: str, use_id: str = "t1") -> dict:
    return {"toolUseId": use_id, "name": "file_write", "input": {"path": "/x/a.md", "role": role}}


def _before(verifier, state, use, cancel: Any = False):
    event = types.SimpleNamespace(tool_use=use, invocation_state=state, cancel_tool=cancel)
    verifier.before_tool(event)
    return event


def _after(verifier, state, use):
    verifier.after_tool(types.SimpleNamespace(tool_use=use, invocation_state=state, result={}))


def _marks(state) -> dict:
    return state.get(GATE_PASSED_KEY, {})


def _claim(state, use):
    return claim_gate("file_write", state, use["toolUseId"], dict(use["input"]))


# --- Unit: ownership by call identity ------------------------------------------


def test_reverse_completion_releases_each_calls_own_mark():
    """The issue's sequence: A claims, B (same id) is marked, B ends first, then A."""
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    a, b = _use("A"), _use("A")  # same id and arguments, two distinct calls
    _before(verifier, state, a)
    assert isinstance(_claim(state, a), GateMark)
    _before(verifier, state, b)
    mark_b = _marks(state)["t1"]
    _after(verifier, state, b)  # B finishes first, without claiming
    assert _marks(state) == {}  # B's own unclaimed mark is gone at once
    _after(verifier, state, a)
    assert _marks(state) == {} and state[GATE_ISSUED_KEY] == {}
    assert mark_b is not None


def test_first_call_finishing_keeps_a_later_calls_mark():
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    a, b = _use("A"), _use("A")
    _before(verifier, state, a)
    _claim(state, a)
    _before(verifier, state, b)
    mark_b = _marks(state)["t1"]
    _after(verifier, state, a)  # A finishes: B has not run yet, its mark stays
    assert _marks(state)["t1"] is mark_b
    assert _claim(state, b) is mark_b
    _after(verifier, state, b)
    assert _marks(state) == {} and state[GATE_ISSUED_KEY] == {}


def test_cancelled_unmarked_call_consumes_nothing():
    """C (same id) is cancelled before the verifier and finishes while A's mark waits."""
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    a, c = _use("A"), _use("A")
    _before(verifier, state, a)
    mark_a = _marks(state)["t1"]
    assert _before(verifier, state, c, cancel="HITL rejected").cancel_tool == "HITL rejected"
    assert _marks(state)["t1"] is mark_a  # C's cancel does not revoke A's mark
    _after(verifier, state, c)
    assert _marks(state)["t1"] is mark_a  # nor does C's AfterToolCall
    assert _claim(state, a) is mark_a
    _after(verifier, state, a)
    assert state[GATE_ISSUED_KEY] == {}


def test_after_for_a_foreign_or_copied_tool_use_consumes_nothing():
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    a = _use("A")
    _before(verifier, state, a)
    mark_a = _marks(state)["t1"]
    _after(verifier, state, dict(a))  # equal but not the call the mark was issued to
    _after(verifier, state, _use("A", "t2"))
    _after(verifier, {}, a)  # another invocation_state
    assert _marks(state)["t1"] is mark_a
    _after(verifier, state, a)
    assert _marks(state) == {}


def test_retry_of_the_same_call_replaces_its_own_mark():
    """A retry (or a resume that skipped AfterToolCall) re-enters BeforeToolCall."""
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    a = _use("A")
    _before(verifier, state, a)
    first = _marks(state)["t1"]
    assert not _before(verifier, state, a).cancel_tool  # not "already in flight"
    second = _marks(state)["t1"]
    assert second is not first and len(state[GATE_ISSUED_KEY]) == 1
    _before(verifier, state, a, cancel="stop")  # cancelled attempt drops its own mark
    assert _marks(state) == {} and state[GATE_ISSUED_KEY] == {}


def test_released_record_does_not_outlive_the_call():
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    for index in range(5):
        use = _use("A", f"t{index}")
        _before(verifier, state, use)
        _after(verifier, state, use)
    assert _marks(state) == {} and state[GATE_ISSUED_KEY] == {}


# --- Integration: the real SDK executor ----------------------------------------


class _FakeFileWrite(AgentTool):
    """Named ``file_write`` so the verifier marks it; claims only when asked to."""

    def __init__(self, script) -> None:
        super().__init__()
        self.script = script

    @property
    def tool_name(self) -> str:
        return "file_write"

    @property
    def tool_spec(self) -> Any:
        return {
            "name": "file_write",
            "description": "fake",
            "inputSchema": {"json": {"type": "object"}},
        }

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        text = await self.script(tool_use, invocation_state)
        yield ToolResultEvent(
            {"toolUseId": tool_use["toolUseId"], "status": "success", "content": [{"text": text}]}
        )


class _Order(HookProvider):
    """Registered before the verifier: orders Before callbacks, observes After ones."""

    def __init__(self, before=None, after=None) -> None:
        self.before, self.after = before, after

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before)
        # AfterToolCall callbacks run in reverse: this one runs after the verifier's.
        registry.add_callback(AfterToolCallEvent, self._after)

    async def _before(self, event: BeforeToolCallEvent) -> None:
        if self.before is not None:
            await self.before(event)

    def _after(self, event: AfterToolCallEvent) -> None:
        if self.after is not None:
            self.after(event)


def _run(tool, order, roles):
    calls = [{"toolUse": _use(role)} for role in roles]
    agent = Agent(
        model=ScriptedModel([calls, [{"text": "done"}]]),
        tools=[tool],
        hooks=[order, ApprovalBindingVerifier(Mock())],
        tool_executor=ConcurrentToolExecutor(),
        callback_handler=None,
    )
    agent("go")
    return agent


def test_sdk_reverse_completion_releases_the_finished_calls_mark():
    a_claimed, b_done = asyncio.Event(), asyncio.Event()
    seen: dict[str, Any] = {}

    async def script(tool_use, state):
        seen["state"] = state
        if tool_use["input"]["role"] == "A":
            seen["claim_a"] = claim_gate("file_write", state, "t1", tool_use["input"])
            a_claimed.set()
            await b_done.wait()  # A finishes after B
            return "A"
        return "B (never claims)"

    async def before(event):
        if event.tool_use["input"]["role"] == "B":
            await a_claimed.wait()  # B passes the gate only after A claimed

    def after(event):
        if event.tool_use["input"]["role"] == "B":
            seen["after_b"] = dict(event.invocation_state.get(GATE_PASSED_KEY, {}))
            b_done.set()

    _run(_FakeFileWrite(script), _Order(before, after), ["A", "B"])
    assert isinstance(seen["claim_a"], GateMark)
    assert seen["after_b"] == {}  # B's unclaimed mark went with B, not with A later
    assert seen["state"][GATE_PASSED_KEY] == {}
    assert seen["state"][GATE_ISSUED_KEY] == {}


def test_sdk_cancelled_unmarked_call_does_not_take_another_calls_mark():
    a_issued, c_done = asyncio.Event(), asyncio.Event()
    seen: dict[str, Any] = {}

    async def script(tool_use, state):
        seen["state"] = state
        a_issued.set()
        await c_done.wait()  # A claims only after the cancelled C has finished
        seen["claim_a"] = claim_gate("file_write", state, "t1", tool_use["input"])
        return "A"

    async def before(event):
        if event.tool_use["input"]["role"] == "C":
            await a_issued.wait()
            event.cancel_tool = "cancelled before the gate"

    def after(event):
        if event.tool_use["input"]["role"] == "C":
            c_done.set()

    agent = _run(_FakeFileWrite(script), _Order(before, after), ["A", "C"])
    assert isinstance(seen["claim_a"], GateMark), seen["claim_a"]
    assert seen["state"][GATE_PASSED_KEY] == {}
    assert seen["state"][GATE_ISSUED_KEY] == {}
    results = [
        block["toolResult"]
        for message in agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    assert sorted(r["status"] for r in results) == ["error", "success"]
