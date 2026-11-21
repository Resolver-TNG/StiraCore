"""Per-task model routing (docs/task-models.md): resolution order, start-up refusal,
fail closed (no fallback), no tools, one record per call without text, default unchanged."""

from __future__ import annotations

import asyncio

import pytest

from stiracore.agent import task_models as tm
from stiracore.agent.context_management import ContextManagementSettings, _summarizing_manager
from stiracore.config.models import GatewayConfig
from stiracore.observability import turns_view
from stiracore.observability.turn_events import validate_event
from stiracore.providers.spec import ModelSpec
from tests.provider_fakes import FakeModel, text_turn

HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
MAIN = ModelSpec("bedrock", "global.anthropic.claude-sonnet-5", {"region_name": "ap-northeast-1"})
AWS = {"AWS_PROFILE": "x"}


def _router(script, **routes):
    built = []

    def build(spec):
        model = FakeModel(script, model_id=spec.model_id)
        built.append((spec, model))
        return model

    records = []
    resolved = tm.resolve_routes(routes, MAIN, AWS)
    return tm.TaskModelRouter(resolved, MAIN, build=build, on_record=records.append), built, records


async def _drain(model, **kwargs):
    events = []
    async for event in model.stream(
        [{"role": "user", "content": [{"text": "secret page text"}]}], **kwargs
    ):
        events.append(event)
    return events


# --- default unchanged ------------------------------------------------------------------


def test_default_every_task_inherits_and_the_file_is_unchanged() -> None:
    config = GatewayConfig()
    assert config.task_models == {}
    assert "task_models" not in config.as_dict()
    routes = tm.resolve_routes(config.task_models, MAIN, {})
    assert set(routes) == set(tm.TASKS) == {"tool_summary", "context_summary", "visual_review"}
    off = routes.pop("visual_review")  # unset = off, not inherit (ADR 0015 rev 6, S6)
    assert off.off and off.source == "off" and off.spec is None
    assert all(route.inherit and route.source == "inherit" for route in routes.values())
    assert "visual_review" not in config.as_dict() and config.visual_review == {}
    routes = tm.resolve_routes(config.task_models, MAIN, {})
    router = tm.TaskModelRouter(routes, MAIN)
    chat = FakeModel([], model_id="chat")
    # The chat model itself, behind the task's no-tools boundary (review r588).
    assert router.model_for("tool_summary", chat).inner is chat
    assert router.model_for("context_summary", chat).inner is chat
    assert router.model_for("context_summary", None) is None
    assert not router.routed()


def test_saved_task_models_round_trip(tmp_path) -> None:
    path = tmp_path / "gateway-config.json"
    GatewayConfig(task_models={"tool_summary": HAIKU}, path=path).save()
    assert GatewayConfig.load(path).task_models == {"tool_summary": HAIKU}


# --- visual_review: unset = off, and a vision model (ADR 0015 rev 6, S6) ----------------


def test_visual_review_unset_is_off_and_builds_nothing() -> None:
    built = []
    router = tm.TaskModelRouter(tm.resolve_routes({}, MAIN, {}), MAIN, build=built.append)
    chat = FakeModel([], model_id="chat")
    assert router.model_for("visual_review", chat) is None  # never the chat model
    assert router.model_for("visual_review", None) is None
    assert built == [] and not router.routed()
    # Off: no settings row (nothing runs); set, it has one like the others.
    assert [r["task"] for r in router.describe()] == ["tool_summary", "context_summary"]
    assert [r["task"] for r in tm.describe_routes({}, MAIN, {})] == [
        "tool_summary",
        "context_summary",
    ]
    on = tm.TaskModelRouter(tm.resolve_routes({"visual_review": HAIKU}, MAIN, AWS), MAIN)
    row = on.describe()[-1]
    assert (row["task"], row["off"], row["inherit"], row["model_id"]) == (
        "visual_review",
        False,
        False,
        HAIKU,
    )
    assert row["env"] == "STIRA_TASK_MODEL_VISUAL_REVIEW"
    # An empty variable does not turn it on.
    routes = tm.resolve_routes({}, MAIN, {"STIRA_TASK_MODEL_VISUAL_REVIEW": " "})
    assert routes["visual_review"].off


def test_visual_review_requires_vision() -> None:
    text_only = ModelSpec("llamacpp", "qwen3-32b", {})
    vision = ModelSpec("llamacpp", "qwen3-32b", {"capabilities": {"vision": True}})
    # ``inherit`` must be written out, and counts only when the chat model's row says vision.
    for source in ({"visual_review": "inherit"}, {}):
        env = {} if source else {"STIRA_TASK_MODEL_VISUAL_REVIEW": "inherit"}
        with pytest.raises(tm.TaskModelError) as caught:
            tm.resolve_routes(source, text_only, env)
        assert (caught.value.code, caught.value.task) == ("no_vision", "visual_review")
    route = tm.resolve_routes({"visual_review": "inherit"}, vision, {})["visual_review"]
    assert route.inherit and not route.off and route.source == "config"
    # A routed model without vision refuses the start; one with vision is accepted.
    with pytest.raises(tm.TaskModelError) as caught:
        tm.resolve_routes({"visual_review": "qwen3-4b"}, vision, {})
    assert caught.value.code == "no_vision"
    route = tm.resolve_routes({"visual_review": HAIKU}, MAIN, AWS)["visual_review"]
    assert route.spec.model_id == HAIKU and not route.off
    # The other tasks need no vision, and an unset visual_review never refuses a start.
    assert tm.resolve_routes({"tool_summary": "qwen3-4b"}, text_only, {})["visual_review"].off
    # The settings rows show the code instead of raising.
    (row,) = [
        r
        for r in tm.describe_routes({"visual_review": "inherit"}, text_only, {})
        if r["task"] == "visual_review"
    ]
    assert row["error"] == "no_vision" and "no_vision" in tm.CODES


def test_visual_review_inherit_is_recorded_and_takes_no_tools() -> None:
    vision = ModelSpec("llamacpp", "qwen3-32b", {"capabilities": {"vision": True}})
    records = []
    router = tm.TaskModelRouter(
        tm.resolve_routes({"visual_review": "inherit"}, vision, {}),
        vision,
        on_record=records.append,
    )
    chat = FakeModel([text_turn("{}")], model_id="qwen3-32b")
    model = router.model_for("visual_review", chat)
    assert model.inner is chat
    asyncio.run(_drain(model))
    assert [(r["task"], r["provider"], r["status"]) for r in records] == [
        ("visual_review", "llamacpp", "success")
    ]
    with pytest.raises(tm.TaskModelError) as caught:
        asyncio.run(_drain(model, tool_specs=[{"name": "shell"}]))
    assert caught.value.code == "tools_not_allowed"


def test_saved_visual_review_budget_round_trips(tmp_path) -> None:
    path = tmp_path / "gateway-config.json"
    GatewayConfig(visual_review={"daily_tokens": 20000}, path=path).save()
    assert GatewayConfig.load(path).visual_review == {"daily_tokens": 20000}


# --- resolution order -------------------------------------------------------------------


def test_env_beats_config_beats_inherit() -> None:
    env = {**AWS, "STIRA_TASK_MODEL_TOOL_SUMMARY": HAIKU}
    routes = tm.resolve_routes(
        {"tool_summary": "global.anthropic.claude-sonnet-5", "context_summary": HAIKU}, MAIN, env
    )
    assert (routes["tool_summary"].source, routes["tool_summary"].spec.model_id) == ("env", HAIKU)
    assert (routes["context_summary"].source, routes["context_summary"].spec.model_id) == (
        "config",
        HAIKU,
    )
    # An empty variable does not pin; "inherit" in the variable pins the chat model.
    env = {
        **AWS,
        "STIRA_TASK_MODEL_TOOL_SUMMARY": " ",
        "STIRA_TASK_MODEL_CONTEXT_SUMMARY": "inherit",
    }
    routes = tm.resolve_routes({"tool_summary": HAIKU, "context_summary": HAIKU}, MAIN, env)
    assert routes["tool_summary"].source == "config"
    assert routes["context_summary"].inherit and routes["context_summary"].source == "env"


def test_a_bare_id_reuses_the_chat_provider_and_its_settings() -> None:
    main = ModelSpec("llamacpp", "qwen3-32b", {"base_url": "http://127.0.0.1:9000"})
    spec = tm.parse_ref("tool_summary", "qwen3-4b", main)
    assert (spec.provider, spec.model_id, dict(spec.options)) == (
        "llamacpp",
        "qwen3-4b",
        {"base_url": "http://127.0.0.1:9000"},
    )
    # A Bedrock id with a colon stays a Bedrock id of the chat's region.
    spec = tm.parse_ref("tool_summary", HAIKU, MAIN)
    assert (spec.provider, spec.model_id, spec.region_name) == ("bedrock", HAIKU, "ap-northeast-1")


def test_another_provider_gets_none_of_the_chat_endpoint_or_key_settings() -> None:
    main = ModelSpec("openai", "gpt-5", {"base_url": "https://proxy.example", "api_key_env": "K"})
    spec = tm.parse_ref("tool_summary", "llamacpp:qwen3-4b", main)
    assert (spec.provider, dict(spec.options)) == ("llamacpp", {})
    spec = tm.parse_ref("tool_summary", f"bedrock:{HAIKU}", main, region="us-east-1")
    assert (spec.provider, dict(spec.options)) == ("bedrock", {"region_name": "us-east-1"})


# --- refused at start-up ------------------------------------------------------------------


def test_an_unknown_task_is_refused_in_the_file_and_in_the_environment() -> None:
    with pytest.raises(ValueError, match="unknown task"):
        GatewayConfig(task_models={"web_magic": HAIKU})
    with pytest.raises(tm.TaskModelError) as caught:
        tm.resolve_routes({}, MAIN, {"STIRA_TASK_MODEL_WEB_MAGIC": HAIKU})
    assert caught.value.code == "unknown_task"
    with pytest.raises(ValueError):
        GatewayConfig(task_models={"tool_summary": ""})


def test_an_unknown_model_is_refused_and_the_value_is_not_echoed() -> None:
    with pytest.raises(tm.TaskModelError) as caught:
        tm.resolve_routes({"tool_summary": "my-pasted-sk-value"}, MAIN, AWS)
    assert caught.value.code == "unknown_model"
    assert caught.value.task == "tool_summary"
    assert "my-pasted-sk-value" not in str(caught.value)


def test_a_missing_sdk_or_missing_credentials_are_refused(tmp_path) -> None:
    from stiracore.providers import provider_available

    if not provider_available("openai"):
        with pytest.raises(tm.TaskModelError) as caught:
            tm.resolve_routes({"tool_summary": "openai:gpt-5-mini"}, MAIN, AWS)
        assert caught.value.code == "provider_unavailable"
    # A local provider of the chat: nothing to configure.
    main = ModelSpec("llamacpp", "qwen3-32b", {})
    assert tm.resolve_routes({"tool_summary": "qwen3-4b"}, main, {})["tool_summary"].spec
    # Another hosted provider without credentials: Bedrock with no AWS hints from llama.cpp
    # (no AWS variables, no ~/.aws files).
    with pytest.raises(tm.TaskModelError) as caught:
        tm.resolve_routes({"tool_summary": f"bedrock:{HAIKU}"}, main, {"HOME": str(tmp_path)})
    assert caught.value.code == "credentials_not_configured"
    routes = tm.resolve_routes({"tool_summary": f"bedrock:{HAIKU}"}, main, AWS)
    assert routes["tool_summary"].spec.provider == "bedrock"
    # The chat's own key variable, named but not set: refused (no other key is looked for).
    keyed = ModelSpec("bedrock", MAIN.model_id, {"api_key_env": "STIRA_TEST_UNSET_KEY"})
    with pytest.raises(tm.TaskModelError) as caught:
        tm.resolve_routes({"tool_summary": HAIKU}, keyed, AWS)
    assert caught.value.code == "credentials_not_configured"


def test_the_settings_rows_show_the_code_instead_of_raising() -> None:
    rows = tm.describe_routes({"tool_summary": "no-such-model"}, MAIN, AWS)
    by_task = {row["task"]: row for row in rows}
    assert by_task["tool_summary"]["error"] == "unknown_model"
    assert by_task["context_summary"]["inherit"] is True
    assert by_task["context_summary"]["model_id"] == MAIN.model_id
    assert by_task["tool_summary"]["env"] == "STIRA_TASK_MODEL_TOOL_SUMMARY"


# --- calls: record, no tools, fail closed -------------------------------------------------


def test_a_routed_call_leaves_one_record_with_tokens_and_no_text() -> None:
    router, built, records = _router(
        [text_turn("short summary", usage={"inputTokens": 120, "outputTokens": 9})],
        tool_summary=HAIKU,
    )
    chat = FakeModel([], model_id="chat")
    model = router.model_for("tool_summary", chat)
    assert model is not chat and built[0][0].model_id == HAIKU
    asyncio.run(_drain(model))
    assert built[0][1].requests[0]["tool_names"] == []
    assert chat.requests == []
    (record,) = records
    assert record["task"] == "tool_summary" and record["model"] == HAIKU
    assert (record["input_tokens"], record["output_tokens"], record["status"]) == (
        120,
        9,
        "success",
    )
    assert "secret page text" not in repr(record) and "short summary" not in repr(record)
    assert router.records == [record]
    # The record is a valid turn event, and the page shows codes and numbers only.
    event = {
        "schema_version": 1,
        "event_id": "e1",
        "turn_id": "t1",
        "seq": 1,
        "ts": 1.0,
        "type": "task_model.call",
        "data": record,
    }
    validate_event(event)
    shown = turns_view.page_event(1, event)
    assert shown["type"] == "task_model_call"
    assert shown["tokens"] == {"input_tokens": 120, "output_tokens": 9}
    assert "usage" not in shown  # not added to the turn's own total
    assert shown["model"] == HAIKU


def test_a_task_without_tools_refuses_tool_specs() -> None:
    router, built, records = _router([text_turn("x")], tool_summary=HAIKU)
    model = router.model_for("tool_summary")
    spec = {"name": "file_write", "description": "d", "inputSchema": {"json": {}}}
    with pytest.raises(tm.TaskModelError) as caught:
        asyncio.run(_drain(model, tool_specs=[spec]))
    assert caught.value.code == "tools_not_allowed"
    assert built[0][1].requests == []  # never reached the model
    assert records[0]["status"] == "refused"


def test_a_failing_routed_model_fails_the_task_without_a_fallback() -> None:
    router, _built, records = _router([[{"raise": TimeoutError("boom")}]], tool_summary=HAIKU)
    chat = FakeModel([text_turn("chat answer")], model_id="chat")
    model = router.model_for("tool_summary", chat)
    with pytest.raises(tm.TaskModelError) as caught:
        asyncio.run(_drain(model))
    assert caught.value.code == "model_failed"
    assert chat.requests == []  # no retry on the chat model
    assert records[0]["status"] == "error" and records[0]["reason"]


def test_a_model_that_cannot_be_built_is_an_error_not_the_chat_model() -> None:
    routes = tm.resolve_routes({"tool_summary": HAIKU}, MAIN, AWS)

    def build(spec):
        raise ImportError("sdk gone")

    router = tm.TaskModelRouter(routes, MAIN, build=build)
    with pytest.raises(tm.TaskModelError) as caught:
        router.model_for("tool_summary", object())
    assert caught.value.code == "provider_unavailable"


def test_the_context_summary_uses_the_routed_model_not_the_chat_model() -> None:
    router, built, records = _router(
        [text_turn("the summary", usage={"inputTokens": 50, "outputTokens": 5})],
        context_summary=HAIKU,
    )
    chat = FakeModel([], model_id="chat")

    class _Agent:
        model = chat

    manager = _summarizing_manager(
        ContextManagementSettings.from_options(True, {}),
        router.model_for("context_summary", chat),
    )
    messages = [
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "hi"}]},
    ]
    summary = manager._generate_summary_with_model(messages, _Agent())
    assert "the summary" in summary["content"][0]["text"]
    assert chat.requests == []
    assert built[0][1].requests[0]["tool_names"] == []
    assert records[0]["task"] == "context_summary"


def test_the_inherited_context_summary_is_the_chat_model() -> None:
    from stiracore.agent.nxp_agent import NXPAgent

    agent = NXPAgent(model=FakeModel([], model_id="chat"))
    assert agent.task_models is None
    agent.task_models = tm.inherit_router(MAIN)
    assert agent.task_models.model_for("context_summary", agent.model).inner is agent.model


# --- review r588 --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "STIRA_TASK_MODEL_tool_summary",
        "STIRA_TASK_MODEL_Tool_Summary",
        "stira_task_model_tool_summary",
        "Stira_Task_Model_CONTEXT_SUMMARY",
    ],
)
def test_a_variable_name_in_another_case_is_refused_not_ignored(name) -> None:
    with pytest.raises(tm.TaskModelError) as caught:
        tm.resolve_routes({}, MAIN, {**AWS, name: "ollama:qwen3:8b"})
    assert caught.value.code == "unknown_task"
    assert "qwen3" not in str(caught.value)


_SPEC = {"name": "file_write", "description": "d", "inputSchema": {"json": {}}}


@pytest.mark.parametrize("routed", [False, True], ids=["inherit", "routed"])
def test_tool_specs_never_reach_the_model_of_a_no_tools_task(routed) -> None:
    chat = FakeModel([text_turn("x")], model_id="chat")
    if routed:
        router, built, _records = _router([text_turn("x")], tool_summary=HAIKU)
    else:
        router, built = tm.inherit_router(MAIN), []
    model = router.model_for("tool_summary", chat)
    with pytest.raises(tm.TaskModelError) as caught:
        asyncio.run(_drain(model, tool_specs=[_SPEC]))
    assert caught.value.code == "tools_not_allowed"
    assert chat.requests == []
    assert all(fake.requests == [] for _spec, fake in built)
    with pytest.raises(tm.TaskModelError):
        model.structured_output(object, [])


def test_an_inherited_task_without_tools_still_reaches_the_chat_model_unchanged() -> None:
    chat = FakeModel([text_turn("plain")], model_id="chat")
    model = tm.inherit_router(MAIN).model_for("context_summary", chat)
    asyncio.run(_drain(model, system_prompt="S"))
    (request,) = chat.requests
    assert request["system_prompt"] == "S" and request["tool_names"] == []
    assert model.get_config() == chat.get_config()


SECRET = "sk-live-0123456789abcdef https://signed.example/x?sig=abc page body"


def _formatted(error: BaseException) -> str:
    import traceback

    return "".join(traceback.format_exception(error))


def test_a_failed_call_does_not_carry_the_provider_error_into_the_traceback(caplog) -> None:
    router, _built, _records = _router([[{"raise": RuntimeError(SECRET)}]], tool_summary=HAIKU)
    model = router.model_for("tool_summary")
    with caplog.at_level("DEBUG"), pytest.raises(tm.TaskModelError) as caught:
        asyncio.run(_drain(model))
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "sk-live" not in _formatted(caught.value)
    assert "sk-live" not in caplog.text


def test_a_failed_build_does_not_carry_the_error_into_the_traceback(caplog) -> None:
    routes = tm.resolve_routes({"tool_summary": HAIKU}, MAIN, AWS)

    def build(spec):
        raise ImportError(SECRET)

    router = tm.TaskModelRouter(routes, MAIN, build=build)
    with caplog.at_level("DEBUG"), pytest.raises(tm.TaskModelError) as caught:
        router.model_for("tool_summary", object())
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "sk-live" not in _formatted(caught.value)
    assert "sk-live" not in caplog.text


def test_the_agent_turn_hands_the_routed_model_to_the_context_summary(
    monkeypatch, tmp_path
) -> None:
    # Through NXPAgent: removing the routing in nxp_agent.py fails this test.
    from stiracore.agent import context_management as cm
    from stiracore.agent.nxp_agent import NXPAgent

    seen = []
    real = cm.build_turn_components

    def spy(settings, stash, *, summary_model=None):
        seen.append(summary_model)
        return real(settings, stash, summary_model=summary_model)

    monkeypatch.setattr(cm, "build_turn_components", spy)
    from stiracore.agent.stimulus_classifier import RuleBasedClassifier
    from stiracore.core.neural_state import NeuralState

    agent = NXPAgent(
        model=FakeModel([text_turn("hi")], model_id="chat"),
        state=NeuralState(stability=0.9),
        stimulus_classifier=RuleBasedClassifier(),
        context_management=cm.ContextManagementSettings(enabled=True),
        context_stash_dir=tmp_path / "stash",
    )
    router, _built, _records = _router([], context_summary=HAIKU)
    agent.task_models = router
    agent("hello")
    assert seen and isinstance(seen[-1], tm.RoutedModel)
    assert seen[-1].model_id == HAIKU
