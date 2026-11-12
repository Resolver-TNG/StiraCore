"""The scripted models' one emitter (#697): the usage each of them reports.

``turn_events`` replaced the stream loops of ``FakeModel`` and ``ScriptedProvider``; these
tests fix the usage contract they had before: the last block that carries a ``usage`` key
wins (also an explicit ``None`` / ``{}``, which resets to the model's fallback), and only
an empty value falls back.
"""

from __future__ import annotations

import asyncio

import pytest
from grants_exec_helpers import ScriptedProvider

from tests.provider_fakes import DEFAULT_USAGE, FakeModel, text_turn, turn_events

_BIG = {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12}
_ZERO = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
_FALLBACK = {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}


def _drain(model) -> list[dict]:
    async def collect():
        return [event async for event in model.stream([], None, None)]

    return asyncio.run(collect())


def _metadata(events) -> list[dict]:
    return [event["metadata"] for event in events if "metadata" in event]


def _fake_usage(turn):
    (metadata,) = _metadata(_drain(FakeModel([turn])))
    return metadata["usage"]


def test_fake_model_without_usage_reports_the_fallback():
    assert _fake_usage([{"text": "a"}]) == _FALLBACK
    assert _fake_usage(text_turn("a")) == _FALLBACK


def test_fake_model_reports_an_explicit_usage():
    assert _fake_usage([{"text": "a", "usage": _BIG}]) == _BIG
    assert _fake_usage([{"text": "a", "usage": _BIG}, {"text": "b"}]) == _BIG  # no key: kept


@pytest.mark.parametrize("reset", [None, {}])
def test_fake_model_an_explicit_empty_usage_resets_to_the_fallback(reset):
    assert _fake_usage([{"text": "a", "usage": _BIG}, {"text": "b", "usage": reset}]) == _FALLBACK


def test_fake_model_an_all_zero_usage_is_a_usage():
    assert _fake_usage([{"text": "a", "usage": _ZERO}]) == _ZERO
    assert _fake_usage([{"text": "a", "usage": _BIG}, {"usage": _ZERO}]) == _ZERO


def test_the_fallback_usage_is_not_shared_between_calls():
    model = FakeModel([text_turn("a"), text_turn("b")])
    (first,) = _metadata(_drain(model))
    first["usage"]["inputTokens"] = 999  # a receiver that edits what it was given
    (second,) = _metadata(_drain(model))
    assert second["usage"] == _FALLBACK
    assert DEFAULT_USAGE == _FALLBACK


def test_scripted_provider_reports_its_usage_and_latency():
    (metadata,) = _metadata(_drain(ScriptedProvider([[{"text": "a"}]], usage=7)))
    assert metadata == {
        "usage": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10},
        "metrics": {"latencyMs": 1},
    }


def test_scripted_provider_without_usage_sends_no_metadata():
    events = _drain(ScriptedProvider([[{"text": "a"}]], usage=None))
    assert _metadata(events) == []
    assert events[-1] == {"messageStop": {"stopReason": "end_turn"}}


def test_turn_events_without_any_usage_sends_no_metadata():
    assert _metadata(turn_events([{"text": "a"}])) == []
    assert _metadata(turn_events([{"text": "a", "usage": {}}], usage=None)) == []
