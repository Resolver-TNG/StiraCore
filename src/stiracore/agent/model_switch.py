"""Apply a saved model selection to a running NXPAgent between turns (#208).

The settings UI saves the provider/model to ``agent-config.json``; a server
calls :func:`apply_model_selection` before each turn so the change takes effect
from the next turn. Only models the server built from configuration are
swapped: an injected ``Model`` instance (``model_spec is None``) is left alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stiracore.providers import ModelSpec, create_model


def selection_spec(model_kwargs: Mapping[str, Any]) -> ModelSpec:
    """The ModelSpec described by ``config.resolver.model_kwargs`` output."""
    model = model_kwargs.get("model")
    if isinstance(model, ModelSpec):
        return model
    return ModelSpec.bedrock(model_kwargs["model_id"], region_name=model_kwargs.get("region_name"))


def apply_model_selection(
    agent: Any, model_kwargs: Mapping[str, Any], *, max_output_tokens: int | None = None
) -> bool:
    """Swap ``agent``'s model when the selection differs; return whether it changed.

    ``max_output_tokens`` is the cap resolved for the new selection (#363): an automatic cap
    follows the model, so it must be taken again with it. Omitted = keep the agent's cap.

    Build errors (missing SDK, invalid options) propagate before anything is
    changed, so a failed switch keeps the current model.
    """
    if getattr(agent, "model_spec", None) is None:
        return False
    spec = selection_spec(model_kwargs)
    # The same model with another cap (a saved max_output_tokens) is rebuilt too.
    if agent.model_spec == spec and (
        max_output_tokens is None or max_output_tokens == agent.max_output_tokens
    ):
        return False
    cap = agent.max_output_tokens if max_output_tokens is None else max_output_tokens
    model = create_model(spec, max_tokens=cap)
    agent.max_output_tokens = cap
    agent.model = model
    agent.model_spec = spec
    agent.provider = spec.provider
    agent.model_id = spec.model_id
    agent.region_name = spec.region_name
    agent._legacy_bedrock = "model" not in model_kwargs
    return True


__all__ = ["apply_model_selection", "selection_spec"]
