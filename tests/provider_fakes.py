"""A scripted, offline Strands ``Model`` for provider contract tests (#204/#209).

``FakeModel`` implements the real ``strands.models.Model`` protocol and emits
the same normalised ``StreamEvent`` dicts every Strands provider produces, so
NXPAgent can run whole turns without any API key, network, or cloud SDK.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from strands.models import Model


def text_turn(text: str, *, usage: dict[str, int] | None = None) -> list[dict[str, Any]]:
    return [{"text": text, **({"usage": usage} if usage else {})}]


def tool_turn(name: str, tool_input: dict[str, Any], tool_use_id: str = "tool-1") -> list:
    return [{"toolUse": {"name": name, "toolUseId": tool_use_id, "input": tool_input}}]


def truncated_tool_turn(
    name: str,
    raw_input: str,
    *,
    tool_use_id: str = "tool-1",
    usage: dict[str, int] | None = None,
) -> list:
    """A stream cut off by ``max_tokens`` in the middle of a tool call's arguments."""
    block: dict[str, Any] = {
        "toolUse": {"name": name, "toolUseId": tool_use_id, "input": {}, "raw_input": raw_input}
    }
    return [block, {"stop_reason": "max_tokens", **({"usage": usage} if usage else {})}]


def thinking_cut_turn(reasoning: str, *, usage: dict[str, int] | None = None) -> list:
    """A call that spends the whole cap on thinking: a reasoning block, then ``max_tokens``."""
    return [
        {"reasoning": reasoning},
        {"stop_reason": "max_tokens", **({"usage": usage} if usage else {})},
    ]


DEFAULT_USAGE = {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}


def turn_events(turn, *, usage: dict[str, int] | None = None, latency_ms: int = 0):
    """The Strands stream events of one scripted assistant turn (#697).

    The one place that turns a script entry into ``StreamEvent`` dicts: the scripted
    models (``FakeModel`` here, ``ScriptedProvider`` in ``grants_exec_helpers.py``, the
    small ones listed in ``docs/regression-set.md``) replay their turns through it and keep
    only their own bookkeeping (what they record, what an empty script means).

    ``turn`` is a list of blocks: ``{"text": ...}``, ``{"reasoning": ...}`` or
    ``{"toolUse": {...}}``; optional keys ``stop_reason`` (e.g. ``"max_tokens"``),
    ``usage`` and ``raise`` (an exception raised when the turn starts).

    Usage: the last block that has a ``usage`` key wins, also with ``None`` / ``{}`` (an
    explicit reset). An empty result falls back to the ``usage`` argument; when that is
    empty too, no ``metadata`` event is sent. A dict of zeros is a usage, not empty. The
    event carries a copy. ``latency_ms`` is the latency that event reports.
    """
    fallback = usage
    usage = None
    stop_reason = "end_turn"
    yield {"messageStart": {"role": "assistant"}}
    for block in turn:
        if "raise" in block:
            raise block["raise"]
        if "stop_reason" in block:
            stop_reason = block["stop_reason"]
        usage = block.get("usage", usage)
        if "toolUse" in block:
            stop_reason = "tool_use"
            use = block["toolUse"]
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"name": use["name"], "toolUseId": use["toolUseId"]}}
                }
            }
            # ``raw_input`` is the argument text as streamed, e.g. cut off mid-string by max_tokens.
            payload = use["raw_input"] if "raw_input" in use else json.dumps(use["input"])
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": payload}}}}
            yield {"contentBlockStop": {}}
        elif "reasoning" in block:
            # Thinking streamed before (or instead of) the reply, as adaptive thinking does.
            yield {"contentBlockStart": {"start": {}}}
            delta = {"reasoningContent": {"text": block["reasoning"]}}
            yield {"contentBlockDelta": {"delta": delta}}
            sig = {"reasoningContent": {"signature": "fake-signature"}}
            yield {"contentBlockDelta": {"delta": sig}}
            yield {"contentBlockStop": {}}
        elif "text" in block:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": block["text"]}}}
            yield {"contentBlockStop": {}}
    yield {"messageStop": {"stopReason": stop_reason}}
    usage = usage or fallback
    if usage:
        yield {"metadata": {"usage": dict(usage), "metrics": {"latencyMs": latency_ms}}}


class FakeModel(Model):
    """Replay scripted turns; records every request it receives.

    Each script entry is a turn for :func:`turn_events`; an exhausted script fails the test.
    """

    def __init__(self, script: list[list[dict[str, Any]]] | None = None, model_id: str = "fake"):
        self.script = list(script or [text_turn("ok")])
        self.config: dict[str, Any] = {"model_id": model_id}
        self.requests: list[dict[str, Any]] = []

    def get_config(self) -> dict[str, Any]:
        return dict(self.config)

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("FakeModel does not implement structured output")
        yield  # pragma: no cover - makes this an async generator

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.requests.append(
            {
                "messages": copy.deepcopy(messages),
                "tool_names": [spec["name"] for spec in tool_specs or []],
                "system_prompt": system_prompt,
            }
        )
        if not self.script:
            raise AssertionError("FakeModel script exhausted")
        turn = self.script.pop(0)
        for event in turn_events(turn, usage=DEFAULT_USAGE):
            yield event
