import inspect
from unittest.mock import Mock

import pytest

from stiracore.agent import model_factory

HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.fixture
def bedrock_constructor(monkeypatch):
    constructor = Mock(name="BedrockModel", return_value=object())
    monkeypatch.setattr(model_factory, "BedrockModel", constructor)
    monkeypatch.setattr(
        model_factory,
        "Agent",
        Mock(side_effect=AssertionError("Routing must not execute an agent")),
    )
    return constructor


@pytest.fixture
def create_for_context(bedrock_constructor):
    def create(role, context, **kwargs):
        parameters = inspect.signature(model_factory.create_model_for_context).parameters
        role_parameter = next(name for name in ("role", "role_or_id") if name in parameters)
        return model_factory.create_model_for_context(
            **{role_parameter: role, "context": context, **kwargs}
        )

    return create


@pytest.mark.parametrize("role", list(model_factory.ModelRole))
def test_heartbeat_routes_every_role_to_haiku(create_for_context, bedrock_constructor, role):
    result = create_for_context(role, model_factory.ExecutionContext.HEARTBEAT)

    bedrock_constructor.assert_called_once()
    assert bedrock_constructor.call_args.kwargs["model_id"] == HAIKU
    assert result is bedrock_constructor.return_value


def test_cron_orchestrator_routes_to_haiku(create_for_context, bedrock_constructor):
    create_for_context(
        model_factory.ModelRole.ORCHESTRATOR,
        model_factory.ExecutionContext.CRON,
    )

    bedrock_constructor.assert_called_once()
    assert bedrock_constructor.call_args.kwargs["model_id"] == HAIKU


def test_custom_routing_overrides_without_mutating_defaults(
    create_for_context, bedrock_constructor
):
    role = model_factory.ModelRole.ORCHESTRATOR
    context = model_factory.ExecutionContext.CRON
    defaults = {key: dict(routes) for key, routes in model_factory.MODEL_ROUTING.items()}
    custom = {context: {role: "offline.custom-model"}}

    result = create_for_context(role, context, custom_routing=custom)

    assert result is bedrock_constructor.return_value
    assert bedrock_constructor.call_args.kwargs["model_id"] == "offline.custom-model"
    assert custom == {context: {role: "offline.custom-model"}}
    assert model_factory.MODEL_ROUTING == defaults


@pytest.mark.parametrize("role", list(model_factory.ModelRole))
def test_normal_context_preserves_default_role_routing(
    create_for_context, bedrock_constructor, role
):
    create_for_context(role, model_factory.ExecutionContext.NORMAL)

    assert bedrock_constructor.call_args.kwargs["model_id"] == model_factory.DEFAULT_MODELS[role]


def test_create_model_forwards_options_without_network(bedrock_constructor):
    session = Mock(name="offline_session")
    result = model_factory.create_model(
        "literal-model-id",
        region="eu-west-1",
        temperature=0.2,
        max_tokens=123,
        boto_session=session,
    )

    bedrock_constructor.assert_called_once_with(
        model_id="literal-model-id",
        region_name="eu-west-1",
        temperature=0.2,
        max_tokens=123,
        boto_session=session,
    )
    assert result is bedrock_constructor.return_value
