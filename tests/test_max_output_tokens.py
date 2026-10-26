"""Output cap and ``stop=max_tokens`` turns (#363).

* the default cap follows the selected model's documented output limit (never above it);
* a stream cut off mid-``file_write`` never runs the tool;
* the counts of the cut-off call are recorded; without counts the record carries a reason code.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask
from strands import tool
from strands.types.exceptions import MaxTokensReachedException

from stiracore.agent import nxp_agent
from stiracore.agent.model_switch import apply_model_selection
from stiracore.agent.stimulus_classifier import RuleBasedClassifier
from stiracore.config import GatewayConfig, resolve_config
from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore
from stiracore.providers import ModelSpec
from stiracore.providers.capabilities import (
    AUTO_MAX_OUTPUT_TOKENS,
    CAPABILITY_TABLE,
    LEGACY_MAX_OUTPUT_TOKENS,
    automatic_cap_source,
    capabilities_for,
    lookup_capabilities,
    resolve_max_output_tokens,
    unknown_limit_max_output,
)
from tests.provider_fakes import (
    FakeModel,
    text_turn,
    thinking_cut_turn,
    tool_turn,
    truncated_tool_turn,
)

# --- the cap ---------------------------------------------------------------


# The automatic cap for a model whose thinking shares the output cap (#416).
THINKING_AUTO = AUTO_MAX_OUTPUT_TOKENS + 32_000


@pytest.mark.parametrize(
    ("provider", "model_id", "expected"),
    [
        # Thinking shares the cap: 32000 for the answer + the row's thinking reserve (#416),
        # never above the documented limit (Opus 4.1: 32K).
        ("bedrock", "global.anthropic.claude-sonnet-5-5", THINKING_AUTO),
        ("bedrock", "global.anthropic.claude-opus-5", THINKING_AUTO),
        ("bedrock", "global.anthropic.claude-fable-5-1", THINKING_AUTO),
        ("bedrock", "global.anthropic.claude-haiku-4-5-20251001-v1:0", THINKING_AUTO),
        ("bedrock", "us.anthropic.claude-opus-4-1-20250805-v1:0", 32_000),
        ("anthropic", "claude-sonnet-4-5", THINKING_AUTO),
        # Documented OpenAI / Gemini rows (#416).
        ("openai", "gpt-5", THINKING_AUTO),
        ("openai_responses", "gpt-5-mini-2025-08-07", THINKING_AUTO),
        ("openai", "o4-mini", THINKING_AUTO),
        ("openai", "gpt-4.1", 32_000),  # no thinking: 32000, under its 32,768
        ("gemini", "gemini-2.5-pro", THINKING_AUTO),
        # Family members whose limit is lower or not recorded: the provider's safe default.
        ("openai", "gpt-5-chat-latest", LEGACY_MAX_OUTPUT_TOKENS),
        ("openai", "o3-pro", LEGACY_MAX_OUTPUT_TOKENS),
        ("gemini", "gemini-2.5-flash-preview-tts", LEGACY_MAX_OUTPUT_TOKENS),
        # Known row without a recorded limit, and unknown models: the provider's default —
        # 8192 for hosted APIs (they reject a cap above the model's limit), the automatic
        # value for local servers (no rejection, no bill).
        ("bedrock", "anthropic.claude-3-5-sonnet-20240620-v1:0", LEGACY_MAX_OUTPUT_TOKENS),
        ("openai_responses", "fugu-cyber", LEGACY_MAX_OUTPUT_TOKENS),
        ("bedrock", "vendor.some-new-model-v1", LEGACY_MAX_OUTPUT_TOKENS),
        ("llamacpp", "qwen3-8b", AUTO_MAX_OUTPUT_TOKENS),
        ("ollama", "some-local-model", AUTO_MAX_OUTPUT_TOKENS),
    ],
)
def test_automatic_cap_follows_the_model(provider, model_id, expected):
    assert resolve_max_output_tokens(None, provider, model_id) == expected
    source = "provider_default" if expected in (8192,) or provider in _LOCAL else "model"
    assert automatic_cap_source(provider, model_id) == source


_LOCAL = ("llamacpp", "ollama")


def test_unknown_limit_default_is_per_provider():
    for provider in ("bedrock", "anthropic", "openai", "openai_responses", "gemini", "litellm"):
        assert unknown_limit_max_output(provider) == LEGACY_MAX_OUTPUT_TOKENS, provider
    for provider in _LOCAL:
        assert unknown_limit_max_output(provider) == AUTO_MAX_OUTPUT_TOKENS, provider
    assert unknown_limit_max_output("no-such-provider") == LEGACY_MAX_OUTPUT_TOKENS


def test_explicit_value_always_wins():
    assert resolve_max_output_tokens(6144, "bedrock", "global.anthropic.claude-sonnet-5-5") == 6144
    assert resolve_max_output_tokens(200_000, "bedrock", "vendor.unknown") == 200_000


def test_automatic_cap_never_exceeds_the_documented_limit():
    checked = 0
    for providers, pattern, caps in CAPABILITY_TABLE:
        if caps.max_output is None:
            continue
        model_id = pattern.replace("*", "x").replace("[0-9]", "1")
        for provider in providers:
            assert lookup_capabilities(provider, model_id) is caps, (provider, pattern)
            cap = resolve_max_output_tokens(None, provider, model_id)
            assert cap <= caps.max_output, (provider, pattern)
            reserve = caps.thinking_reserve if caps.thinking_uses_output_budget else None
            assert cap <= AUTO_MAX_OUTPUT_TOKENS + (reserve or 0)
            checked += 1
    assert checked >= 20


def test_thinking_reserve_only_where_thinking_shares_the_cap():
    for _providers, pattern, caps in CAPABILITY_TABLE:
        if caps.thinking_reserve:
            assert caps.thinking_uses_output_budget, pattern
    # A row without thinking gets no reserve even when one is configured.
    spec = ModelSpec.bedrock(
        "global.anthropic.claude-sonnet-5-5",
        region_name="us-east-1",
        capabilities={"thinking_uses_output_budget": False},
    )
    assert resolve_max_output_tokens(None, spec) == AUTO_MAX_OUTPUT_TOKENS
    spec = ModelSpec.bedrock(
        "global.anthropic.claude-sonnet-5-5",
        region_name="us-east-1",
        capabilities={"thinking_reserve": 8_000},
    )
    assert resolve_max_output_tokens(None, spec) == AUTO_MAX_OUTPUT_TOKENS + 8_000
    with pytest.raises(ValueError, match="invalid value"):
        capabilities_for(spec, overrides={"thinking_reserve": -1})


def test_a_spec_level_capability_override_lowers_the_automatic_cap():
    spec = ModelSpec.bedrock("vendor.model", region_name="us-east-1")
    assert resolve_max_output_tokens(None, spec) == LEGACY_MAX_OUTPUT_TOKENS
    spec = ModelSpec.bedrock(
        "global.anthropic.claude-sonnet-5-5",
        region_name="us-east-1",
        capabilities={"max_output": 16_000},
    )
    assert capabilities_for(spec).max_output == 16_000
    assert resolve_max_output_tokens(None, spec) == 16_000


def test_gateway_default_is_automatic_and_resolves_per_model(tmp_path):
    path = tmp_path / "gateway.json"
    config = GatewayConfig(path=path)
    assert config.max_output_tokens is None
    config.save()
    assert json.loads(path.read_text())["max_output_tokens"] is None
    assert resolve_config(GatewayConfig.load(path), environ={}).max_output_tokens == THINKING_AUTO
    unknown = GatewayConfig(model_id="vendor.some-new-model-v1")
    assert resolve_config(unknown, environ={}).max_output_tokens == LEGACY_MAX_OUTPUT_TOKENS
    pinned = GatewayConfig(max_output_tokens=64_000)
    assert resolve_config(pinned, environ={}).max_output_tokens == 64_000


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8192"])
def test_invalid_gateway_cap_is_rejected(value):
    with pytest.raises(ValueError, match="positive integer"):
        GatewayConfig(max_output_tokens=value)


def test_model_switch_takes_the_cap_resolved_for_the_new_model(monkeypatch):
    built = Mock(return_value=object())
    monkeypatch.setattr("stiracore.agent.model_switch.create_model", built)
    agent = SimpleNamespace(
        model_spec=ModelSpec.bedrock("vendor.old", region_name="us-east-1"),
        max_output_tokens=LEGACY_MAX_OUTPUT_TOKENS,
    )
    kwargs = {"model_id": "global.anthropic.claude-sonnet-5-5", "region_name": "us-east-1"}
    assert apply_model_selection(agent, kwargs, max_output_tokens=32_000) is True
    assert built.call_args.kwargs["max_tokens"] == 32_000
    assert agent.max_output_tokens == 32_000


# --- a stream cut off by max_tokens ------------------------------------------

CUT_ARGS = '{"path": "game.html", "content": "<!doctype html><script>let score = 0;\\nfunction'
USAGE = {"inputTokens": 1234, "outputTokens": 8192, "totalTokens": 9426}


@pytest.fixture(autouse=True)
def _forbid_bedrock(monkeypatch):
    monkeypatch.setattr(
        nxp_agent, "BedrockModel", Mock(side_effect=AssertionError("Bedrock must not be built"))
    )


def _agent(script, calls):
    @tool
    def file_write(path: str, content: str) -> str:
        """Write a file."""
        calls.append((path, content))
        return "written"

    model = FakeModel(script)
    agent = nxp_agent.NXPAgent(
        model=model, tools=[file_write], stimulus_classifier=RuleBasedClassifier()
    )
    return agent, model


def test_cut_off_tool_call_is_never_executed():
    calls: list = []
    agent, model = _agent([truncated_tool_turn("file_write", CUT_ARGS, usage=USAGE)], calls)

    with pytest.raises(MaxTokensReachedException):
        agent("write a long game")

    assert calls == []
    # The partial assistant message stays in history, but its tool use became plain text,
    # so neither this turn nor the next one can run the half-written call.
    assistant = [m for m in agent.agent.messages if m["role"] == "assistant"]
    assert assistant and all("toolUse" not in block for m in assistant for block in m["content"])
    assert "incomplete" in json.dumps(assistant[-1]["content"])
    assert len(model.requests) == 1


def test_a_syntactically_complete_call_is_not_run_either_when_the_stop_was_max_tokens():
    # The cut can land right after the closing brace: the arguments parse, but the model
    # was stopped, not finished. The stop reason decides, not the JSON.
    calls: list = []
    complete = json.dumps({"path": "game.html", "content": "<html></html>"})
    agent, _ = _agent([truncated_tool_turn("file_write", complete, usage=USAGE)], calls)
    with pytest.raises(MaxTokensReachedException):
        agent("write a page")
    assert calls == []


def test_control_the_same_call_runs_when_it_is_complete():
    # Guards the test above: with a complete call the tool does run, so "no call" is the
    # max_tokens handling and not a broken fixture.
    calls: list = []
    args = {"path": "game.html", "content": "<html></html>"}
    agent, _ = _agent([tool_turn("file_write", args), text_turn("done")], calls)
    agent("write a short page")
    assert calls == [("game.html", "<html></html>")]


def test_next_turn_after_a_cut_off_call_runs_clean():
    calls: list = []
    agent, model = _agent(
        [truncated_tool_turn("file_write", CUT_ARGS, usage=USAGE), text_turn("sorry, shorter")],
        calls,
    )
    with pytest.raises(MaxTokensReachedException):
        agent("write a long game")
    assert str(agent("try again")).strip() == "sorry, shorter"
    assert calls == []
    assert not any("toolUse" in b for m in model.requests[1]["messages"] for b in m["content"])


def test_failed_call_usage_is_kept_on_the_agent():
    agent, model = _agent([truncated_tool_turn("file_write", CUT_ARGS, usage=USAGE)], [])
    assert agent.last_failed_usage is None
    with pytest.raises(MaxTokensReachedException):
        agent("write a long game")
    assert agent.last_failed_usage == {"inputTokens": 1234, "outputTokens": 8192}
    # A later successful turn clears it.
    model.script.append(text_turn("ok"))
    agent("hi")
    assert agent.last_failed_usage is None


def test_provider_error_before_any_usage_leaves_no_counts():
    agent, _ = _agent([[{"raise": RuntimeError("provider exploded")}]], [])
    with pytest.raises(RuntimeError):
        agent("hi")
    assert agent.last_failed_usage is None


# --- what the page and the turn record get --------------------------------------


def _parse(body):
    out = []
    for block in body.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


def _client(agent, recorder=None, runner=None):
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(agent, runner=runner or agent, turn_recorder=recorder)
    )
    return app.test_client()


def _completed(events):
    return next(d for n, d in events if n == "ui_event" and d["type"] == "turn.completed")["data"]


def test_cut_off_turn_reports_reason_usage_and_runs_no_tool(tmp_path):
    calls: list = []
    agent, _ = _agent([truncated_tool_turn("file_write", CUT_ARGS, usage=USAGE)], calls)
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))
    client = _client(agent, recorder)

    events = _parse(client.post("/api/chat", json={"message": "write"}).get_data(True))

    error = next(d for n, d in events if n == "error")
    assert error["code"] == "max_tokens"
    assert "出力の上限に達して途中で切れました" in error["error"]
    assert "cut off" in error["error"]
    assert calls == []
    assert not [d for n, d in events if n == "ui_event" and d["type"] == "tool_call.created"]
    completed = _completed(events)
    assert (completed["stop"], completed["error"]) == ("max_tokens", True)
    assert completed["usage"]["input_tokens"] == 1234
    assert completed["usage"]["output_tokens"] == 8192
    assert "usage_reason" not in completed
    assert completed["model"] == "fake"  # #393's model field is written beside the counts
    listed = client.get("/api/turns?limit=1").get_json()["turns"][0]
    assert listed["stop"] == "max_tokens" and listed["usage"]["output_tokens"] == 8192
    # The failed call was billed: the session totals include it.
    session = client.get("/api/gateway/status").get_json()["session"]
    assert (session["total_input_tokens"], session["total_output_tokens"]) == (1234, 8192)


def test_no_counts_means_a_reason_code_not_a_bare_null(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))

    def runner(_message):
        raise MaxTokensReachedException("cut")

    client = _client(SimpleNamespace(), recorder, runner)
    events = _parse(client.post("/api/chat", json={"message": "x"}).get_data(True))
    completed = _completed(events)
    assert completed["stop"] == "max_tokens"
    assert completed["usage_reason"] == "E_USAGE_NOT_REPORTED"
    listed = client.get("/api/turns?limit=1").get_json()["turns"][0]
    assert listed["usage_reason"] == "E_USAGE_NOT_REPORTED"


def test_generic_failure_records_a_reason_too(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))
    agent, _ = _agent([[{"raise": RuntimeError("provider exploded")}]], [])
    events = _parse(
        _client(agent, recorder).post("/api/chat", json={"message": "x"}).get_data(True)
    )
    completed = _completed(events)
    assert completed["stop"] == "error"
    assert completed["usage_reason"] == "E_USAGE_NOT_REPORTED"
    assert completed["model"] == "fake"  # #393's model field is written beside it


def test_a_good_turn_has_counts_and_no_reason(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))
    agent, _ = _agent([text_turn("hi", usage=USAGE)], [])
    events = _parse(
        _client(agent, recorder).post("/api/chat", json={"message": "x"}).get_data(True)
    )
    completed = _completed(events)
    assert completed["stop"] == "completed" and completed["usage"]["output_tokens"] == 8192
    assert "usage_reason" not in completed


# --- review round: logs, stale usage, config path, same-model cap, cancelled ----------


@pytest.mark.parametrize("exc", [MaxTokensReachedException("CANARY"), RuntimeError("CANARY")])
def test_failure_logs_carry_the_type_and_a_code_not_the_message(exc, caplog):
    def runner(_message):
        raise exc

    caplog.set_level("DEBUG")
    body = _client(SimpleNamespace(), None, runner).post("/api/chat", json={"message": "x"})
    assert "CANARY" not in caplog.text and "CANARY" not in body.get_data(True)
    assert type(exc).__name__ in caplog.text
    assert "E_MAX_TOKENS" in caplog.text or "E_CHAT_FAILED" in caplog.text
    assert all(record.exc_info is None for record in caplog.records if "NXPAgent" in record.msg)


def test_a_failure_before_the_sdk_does_not_recount_the_previous_turns_usage():
    agent, model = _agent([text_turn("one", usage=USAGE)], [])
    agent("first")
    assert agent.agent.event_loop_metrics.accumulated_usage["outputTokens"] == 8192

    def refuse(**_kwargs):
        raise RuntimeError("pre_turn exploded")

    agent.engine.pre_turn = refuse  # fails before this turn's Agent exists
    with pytest.raises(RuntimeError):
        agent("second")
    assert agent.last_failed_usage is None
    assert len(model.requests) == 1


def test_a_provider_failure_after_a_good_turn_does_not_inherit_its_usage():
    agent, _ = _agent(
        [text_turn("one", usage=USAGE), [{"raise": RuntimeError("provider exploded")}]], []
    )
    agent("first")
    with pytest.raises(RuntimeError):
        agent("second")
    assert agent.last_failed_usage is None


def test_cancelled_turn_records_e_usage_cancelled(tmp_path):
    """An explicit stop (#452) before any count: ``E_USAGE_CANCELLED``. Closing the page
    is not a stop any more (tests/test_chat_detached_turn.py)."""
    import threading

    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))
    started, release = threading.Event(), threading.Event()

    def runner(_message):
        yield "first part "
        started.set()
        release.wait(5)
        yield "second part"
        yield "never reached"

    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(SimpleNamespace(), runner=runner, turn_recorder=recorder)
    )
    client = app.test_client()
    response = client.post("/api/chat", json={"message": "x"}, buffered=False)
    iterator = iter(response.response)
    assert started.wait(5)
    assert client.post("/api/chat/cancel").status_code == 202
    release.set()
    body = b"".join(iterator).decode()
    assert "second part" in body and "never reached" not in body
    stored = recorder.store.recent_turns(1)[0]
    assert stored["stop"] == "cancelled"
    assert stored["usage_reason"] == "E_USAGE_CANCELLED"
    assert stored["error"] is True


def test_resolve_config_pins_and_automatics_per_gateway_file(tmp_path):
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"max_output_tokens": 12_345}))
    assert resolve_config(GatewayConfig.load(path), environ={}).max_output_tokens == 12_345
    path.write_text(json.dumps({"model_id": "vendor.some-new-model-v1"}))
    assert resolve_config(GatewayConfig.load(path), environ={}).max_output_tokens == 8192
    # An environment model switch moves the automatic cap with the model.
    auto = GatewayConfig.load(path)
    env = {"STIRA_MODEL": "global.anthropic.claude-sonnet-5-5"}
    assert resolve_config(auto, environ=env).max_output_tokens == THINKING_AUTO


def test_same_model_with_a_new_cap_is_rebuilt(monkeypatch):
    built = Mock(return_value=object())
    monkeypatch.setattr("stiracore.agent.model_switch.create_model", built)
    kwargs = {"model_id": "global.anthropic.claude-sonnet-5-5", "region_name": "us-east-1"}
    spec = ModelSpec.bedrock(kwargs["model_id"], region_name="us-east-1")
    agent = SimpleNamespace(model_spec=spec, max_output_tokens=8192)
    assert apply_model_selection(agent, kwargs, max_output_tokens=8192) is False
    assert apply_model_selection(agent, kwargs) is False
    built.assert_not_called()
    assert apply_model_selection(agent, kwargs, max_output_tokens=20_000) is True
    assert built.call_args.kwargs["max_tokens"] == 20_000 and agent.max_output_tokens == 20_000


# --- thinking used the cap before the reply began (#416) ----------------------------------

THINK_USAGE = {"inputTokens": 900, "outputTokens": 300, "totalTokens": 1200}


def test_a_call_cut_in_its_thinking_is_marked_before_reply():
    # Measured live in #396 (Sonnet 5.5, cap 300): the cut message held a reasoning block only.
    agent, _ = _agent([thinking_cut_turn("Plan: a canvas, a loop, ...", usage=THINK_USAGE)], [])
    with pytest.raises(MaxTokensReachedException):
        agent("write a long game")
    assert agent.last_cut_off == {"before_reply": True, "reasoning": True}
    assert agent.last_failed_usage == {"inputTokens": 900, "outputTokens": 300}


def test_an_empty_cut_message_is_before_reply_without_reasoning():
    # A provider that does not stream its reasoning (no summary) leaves the message empty.
    agent, _ = _agent([[{"stop_reason": "max_tokens", "usage": THINK_USAGE}]], [])
    with pytest.raises(MaxTokensReachedException):
        agent("x")
    assert agent.last_cut_off == {"before_reply": True, "reasoning": False}


@pytest.mark.parametrize(
    "script",
    [
        [truncated_tool_turn("file_write", CUT_ARGS, usage=USAGE)],  # a tool call had begun
        [[{"reasoning": "hm"}, {"text": "Here is the"}, {"stop_reason": "max_tokens"}]],
    ],
)
def test_a_cut_after_the_reply_began_is_not_before_reply(script):
    agent, _ = _agent(script, [])
    with pytest.raises(MaxTokensReachedException):
        agent("x")
    assert agent.last_cut_off["before_reply"] is False


def test_cut_off_state_is_cleared_by_the_next_turn_and_other_errors():
    agent, model = _agent([thinking_cut_turn("...")], [])
    with pytest.raises(MaxTokensReachedException):
        agent("x")
    model.script.append(text_turn("ok"))
    agent("y")
    assert agent.last_cut_off is None
    model.script.append([{"raise": RuntimeError("provider exploded")}])
    with pytest.raises(RuntimeError):
        agent("z")
    assert agent.last_cut_off is None


def test_the_page_gets_the_before_reply_reason_and_the_limit(tmp_path):
    agent, _ = _agent([thinking_cut_turn("...", usage=THINK_USAGE)], [])
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))
    events = _parse(
        _client(agent, recorder).post("/api/chat", json={"message": "w"}).get_data(True)
    )
    error = next(d for n, d in events if n == "error")
    assert (error["code"], error["cut"], error["limit"]) == ("max_tokens", "before_reply", 8192)
    assert "返答を書き始める前に出力の上限に達しました" in error["error"]
    assert "before the reply began" in error["error"]
    completed = _completed(events)
    assert (completed["stop"], completed["cut"]) == ("max_tokens", "before_reply")
    assert completed["usage"]["output_tokens"] == 300


def test_a_cut_mid_reply_keeps_the_generic_reason_and_no_cut_field(tmp_path):
    agent, _ = _agent([truncated_tool_turn("file_write", CUT_ARGS, usage=USAGE)], [])
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))
    events = _parse(
        _client(agent, recorder).post("/api/chat", json={"message": "w"}).get_data(True)
    )
    error = next(d for n, d in events if n == "error")
    assert "cut" not in error and error["limit"] == 8192
    assert "出力の上限に達して途中で切れました" in error["error"]
    assert "cut" not in _completed(events)
