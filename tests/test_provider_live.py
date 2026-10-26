"""Opt-in live smoke against a real provider (#209). Skipped unless configured.

    STIRA_LIVE_MODEL="ollama:llama3.2:3b"            # or openai:..., llamacpp:..., a Bedrock ID
    STIRA_LIVE_BASE_URL="http://127.0.0.1:11434"     # optional (Ollama/llama.cpp/OpenAI-compatible)
    STIRA_LIVE_TOOLS=1                                 # also require one tool call
    pytest tests/test_provider_live.py -m live

API keys come from each SDK's usual environment variables (never from this file).
CI never sets STIRA_LIVE_MODEL, so the default run needs no keys and no AWS.
"""

from __future__ import annotations

import os

import pytest
from strands import tool

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.stimulus_classifier import RuleBasedClassifier
from stiracore.providers import ModelSpec

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("STIRA_LIVE_MODEL"), reason="STIRA_LIVE_MODEL not set"),
]


def live_spec() -> ModelSpec:
    options = {}
    if os.environ.get("STIRA_LIVE_BASE_URL"):
        options["base_url"] = os.environ["STIRA_LIVE_BASE_URL"]
    return ModelSpec.parse(os.environ["STIRA_LIVE_MODEL"], **options)


def test_live_text_turn():
    agent = NXPAgent(
        model=live_spec(), max_output_tokens=256, stimulus_classifier=RuleBasedClassifier()
    )
    result = agent("Reply with one short friendly sentence.")
    assert str(result).strip()
    assert result.stop_reason == "end_turn"
    usage = result.metrics.accumulated_usage
    assert usage.get("outputTokens", 0) > 0


calls: list[str] = []


@tool
def lookup_color(name: str) -> str:
    """Return the favourite colour of the named person."""
    calls.append(name)
    return "teal"


@pytest.mark.skipif(not os.environ.get("STIRA_LIVE_TOOLS"), reason="STIRA_LIVE_TOOLS not set")
def test_live_tool_call():
    calls.clear()
    agent = NXPAgent(
        model=live_spec(),
        max_output_tokens=512,
        tools=[lookup_color],
        stimulus_classifier=RuleBasedClassifier(),
    )
    result = agent("Use the lookup_color tool to find Ada's favourite colour, then answer.")
    assert calls, "the model did not call the tool"
    assert "teal" in str(result).lower()
