"""Provider-neutral contract for NXPAgent turns (#209).

Every test runs against ``contract_model`` — a scripted fake Strands ``Model``
by default, so the suite needs no API key, network, or AWS credentials. The
same assertions describe what any provider must satisfy for Stira's turn
pipeline (text, tool use, streaming, images, max_tokens, errors).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from unittest.mock import Mock

import pytest
from PIL import Image
from strands import tool
from strands.types.exceptions import MaxTokensReachedException, ModelThrottledException

from stiracore.agent import nxp_agent
from stiracore.agent.stimulus_classifier import RuleBasedClassifier
from stiracore.agent.subagent import create_nxp_subagent
from stiracore.core import NeuralState
from tests.provider_fakes import FakeModel, text_turn, tool_turn


@pytest.fixture(autouse=True)
def _forbid_bedrock(monkeypatch):
    monkeypatch.setattr(
        nxp_agent, "BedrockModel", Mock(side_effect=AssertionError("Bedrock must not be built"))
    )


def make_agent(model, **kwargs):
    return nxp_agent.NXPAgent(model=model, stimulus_classifier=RuleBasedClassifier(), **kwargs)


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def test_text_turn_updates_state_once():
    model = FakeModel([text_turn("pong")])
    agent = make_agent(model, system_prompt="sys")

    result = agent("ping")

    assert str(result).strip() == "pong"
    assert result.stop_reason == "end_turn"
    assert model.requests[0]["system_prompt"] == "sys"
    assert agent.last_turn_summary is not None


@tool
def echo(text: str) -> str:
    """Return the text unchanged."""
    return f"echo:{text}"


def test_tool_call_round_trip():
    model = FakeModel([tool_turn("echo", {"text": "hi"}), text_turn("done")])
    agent = make_agent(model, tools=[echo])

    result = agent("use the tool")

    assert str(result).strip() == "done"
    assert model.requests[0]["tool_names"] == ["echo"]
    tool_results = [
        block["toolResult"]
        for message in model.requests[1]["messages"]
        for block in message["content"]
        if "toolResult" in block
    ]
    assert tool_results[0]["status"] == "success"
    assert tool_results[0]["content"] == [{"text": "echo:hi"}]


def test_write_tools_are_withheld_from_any_provider_under_breaker():
    model = FakeModel([text_turn("ok")])
    state = NeuralState(stability=0.1)
    agent = make_agent(model, tools=[echo], state=state)
    agent.config.blocked_tool_names = frozenset({"echo"})

    agent("hello")

    assert model.requests[0]["tool_names"] == []


def test_streaming_deltas_arrive_in_order():
    model = FakeModel([[{"text": "a"}, {"text": "b"}]])
    parent = make_agent(model)
    child = create_nxp_subagent(parent, "stream", stimulus_classifier=RuleBasedClassifier())

    async def collect():
        deltas, final = [], None
        async for event in child.agent.stream_async("go"):
            if "data" in event:
                deltas.append(event["data"])
            if "result" in event:
                final = event["result"]
        return deltas, final

    deltas, final = asyncio.run(collect())
    assert deltas == ["a", "b"]
    assert final.stop_reason == "end_turn"


def test_image_blocks_reach_the_model_unchanged():
    raw = png_bytes()
    model = FakeModel([text_turn("red")])
    agent = make_agent(model)
    prompt = [{"text": "color?"}, {"image": {"format": "png", "source": {"bytes": raw}}}]

    agent(prompt)

    assert model.requests[0]["messages"][-1]["content"] == prompt


def test_max_tokens_closes_the_turn_without_a_stimulus():
    model = FakeModel([[{"text": "partial"}, {"stop_reason": "max_tokens"}]])
    agent = make_agent(model)
    before = agent.state.to_dict()

    with pytest.raises(MaxTokensReachedException):
        agent("write a lot")

    after = agent.state.to_dict()
    assert {k: after[k] for k in ("trust", "stability")} == {
        k: before[k] for k in ("trust", "stability")
    }
    assert agent.last_turn_summary is None or not agent.last_turn_summary.get("deltas")


def _count_post_turns(agent):
    calls = []
    original = agent.engine.post_turn

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    agent.engine.post_turn = spy
    return calls


def test_provider_errors_propagate_and_close_the_turn():
    model = FakeModel([[{"raise": RuntimeError("provider exploded")}], text_turn("recovered")])
    agent = make_agent(model)
    calls = _count_post_turns(agent)

    with pytest.raises(RuntimeError, match="provider exploded"):
        agent("hi")
    assert len(calls) == 1 and calls[0].get("stimulus") is None

    # The agent stays usable on the same provider after a failed turn.
    assert str(agent("again")).strip() == "recovered"


def test_throttling_is_retried_by_the_sdk_within_one_turn(monkeypatch):
    # Providers normalise rate limits to ModelThrottledException; Strands retries.
    monkeypatch.setattr("strands.agent.agent.INITIAL_DELAY", 0)
    model = FakeModel([[{"raise": ModelThrottledException("slow down")}], text_turn("ok")])
    agent = make_agent(model)
    calls = _count_post_turns(agent)

    assert str(agent("hi")).strip() == "ok"
    assert len(model.requests) == 2
    assert len(calls) == 1


def test_chat_api_pipeline_streams_and_reports_usage(tmp_path):
    from dashboard.app import create_app

    usage = {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10}
    model = FakeModel([text_turn("hello", usage=usage)])
    agent = make_agent(model)
    app = create_app(state_path=tmp_path / "state.json", chat_agent=agent)
    image = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png_bytes()).decode(),
        },
    }

    response = app.test_client().post("/api/chat", json={"message": "hi", "images": [image]})

    events = [
        (block.split("\n")[0][7:], json.loads(block.split("data: ", 1)[1]))
        for block in response.text.split("\n\n")
        if block.startswith("event: ")
    ]
    assert ("delta", {"text": "hello"}) in events
    turn = next(data for name, data in events if name == "turn")
    assert (turn["input_tokens"], turn["output_tokens"]) == (7, 3)
    content = model.requests[0]["messages"][-1]["content"]
    assert content[0] == {"text": "hi"} and content[1]["image"]["format"] == "png"
