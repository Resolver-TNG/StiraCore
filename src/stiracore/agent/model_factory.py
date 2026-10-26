"""Reusable role- and execution-context-based model factories.

Routing values are provider-neutral: a plain string is a Bedrock model ID
(the historical behaviour), while a :class:`~stiracore.providers.ModelSpec`
selects any Strands provider (#204).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from strands import Agent
from strands.models import BedrockModel, Model

from stiracore.providers import ModelSpec
from stiracore.providers import create_model as create_model_from_spec

# A route is a Bedrock model ID (legacy) or a provider-neutral ModelSpec.
ModelRef = str | ModelSpec


class ModelRole(Enum):
    ORCHESTRATOR = "orchestrator"
    CODER = "coder"
    REVIEWER = "reviewer"
    RESEARCHER = "researcher"
    FAST_GATE = "fast_gate"


class ExecutionContext(Enum):
    NORMAL = "normal"
    CRON = "cron"
    HEARTBEAT = "heartbeat"


DEFAULT_MODELS: dict[ModelRole, ModelRef] = {
    ModelRole.ORCHESTRATOR: "global.anthropic.claude-opus-5-5",
    ModelRole.CODER: "global.openai.gpt-6-astra",
    ModelRole.REVIEWER: "global.anthropic.claude-sonnet-5",
    ModelRole.RESEARCHER: "global.anthropic.claude-sonnet-5",
    ModelRole.FAST_GATE: "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}


MODEL_ROUTING: dict[ExecutionContext, dict[ModelRole, ModelRef]] = {
    ExecutionContext.NORMAL: DEFAULT_MODELS,
    ExecutionContext.CRON: {
        ModelRole.ORCHESTRATOR: "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        ModelRole.CODER: "global.anthropic.claude-sonnet-5",
        ModelRole.REVIEWER: "global.anthropic.claude-sonnet-5",
        ModelRole.RESEARCHER: "global.anthropic.claude-sonnet-5",
        ModelRole.FAST_GATE: "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    },
    ExecutionContext.HEARTBEAT: {
        role: "global.anthropic.claude-haiku-4-5-20251001-v1:0" for role in ModelRole
    },
}


def create_model(
    role_or_id: ModelRole | str | ModelSpec,
    region: str = "ap-northeast-1",
    **kwargs: Any,
) -> Model:
    """Create a model from a role, an explicit Bedrock model ID, or a ModelSpec.

    Strings are used verbatim as Bedrock model IDs, not interpreted as role
    names. Additional keyword arguments are forwarded to BedrockModel for
    strings (for example temperature, max_tokens, or boto_session) and merged
    into ``ModelSpec.options`` for specs. ``region`` applies to Bedrock only.
    """
    if isinstance(role_or_id, ModelRole):
        target: ModelRef = DEFAULT_MODELS[role_or_id]
    elif isinstance(role_or_id, (str, ModelSpec)):
        target = role_or_id
    else:
        raise TypeError("role_or_id must be a ModelRole, model ID string, or ModelSpec.")

    if "model_id" in kwargs or "region_name" in kwargs:
        raise ValueError("Set the model ID through role_or_id and the region through region.")

    if isinstance(target, ModelSpec):
        spec = target.with_options(**kwargs) if kwargs else target
        if spec.provider == "bedrock" and "region_name" not in spec.options:
            spec = spec.with_options(region_name=region)
        return create_model_from_spec(spec)

    model_id = target
    if not model_id.strip():
        raise ValueError("The model ID must not be empty.")

    return BedrockModel(model_id=model_id, region_name=region, **kwargs)


def model_ref_for_context(
    context: ExecutionContext,
    role: ModelRole,
    custom_routing: Mapping[ExecutionContext, Mapping[ModelRole, ModelRef]] | None = None,
) -> ModelRef:
    """Return the route (model ID or ModelSpec) without building a model."""
    if not isinstance(role, ModelRole):
        raise TypeError("role must be a ModelRole.")

    routing = custom_routing or MODEL_ROUTING
    models = routing.get(context, DEFAULT_MODELS)
    return models.get(role, DEFAULT_MODELS[role])


def create_model_for_context(
    context: ExecutionContext,
    role: ModelRole,
    region: str = "ap-northeast-1",
    custom_routing: Mapping[ExecutionContext, Mapping[ModelRole, ModelRef]] | None = None,
    **kwargs: Any,
) -> Model:
    """Create a model using execution-context routing.

    A nonempty custom routing replaces MODEL_ROUTING. Missing contexts and
    missing roles fall back to DEFAULT_MODELS. Routes may be ModelSpecs.
    """
    target = model_ref_for_context(context, role, custom_routing)
    return create_model(target, region=region, **kwargs)


def create_agent(
    role_or_id: ModelRole | str | ModelSpec,
    region: str,
    tools: list,
    system_prompt: str,
    **kwargs: Any,
) -> Agent:
    """Create a role-based model and wrap it in a Strands Agent.

    Additional keyword arguments configure the model. For additional
    Agent options, call create_model and construct Agent directly.
    """
    model = create_model(role_or_id, region=region, **kwargs)
    return Agent(model=model, tools=tools, system_prompt=system_prompt)


__all__ = [
    "DEFAULT_MODELS",
    "MODEL_ROUTING",
    "ExecutionContext",
    "ModelRef",
    "ModelRole",
    "ModelSpec",
    "create_agent",
    "create_model",
    "create_model_for_context",
    "model_ref_for_context",
]
