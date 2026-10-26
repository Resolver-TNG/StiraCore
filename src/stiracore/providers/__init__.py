"""Provider-neutral model selection for Stira (#203/#204).

``ModelSpec`` + ``create_model`` build any Strands ``Model`` implementation;
Bedrock stays the default so existing ``model_id``/``region_name`` code keeps
working unchanged.
"""

from .capabilities import ModelCapabilities, capabilities_for, resolve_max_output_tokens
from .factory import (
    DEFAULT_MAX_TOKENS,
    ProviderUnavailableError,
    create_model,
    load_provider_class,
    provider_available,
    provider_of,
)
from .spec import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    ModelSpec,
    ProviderInfo,
    as_model_spec,
    normalize_provider,
)
from .usage import NormalizedUsage, Pricing, estimate_cost, normalize_strands_usage

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "ModelCapabilities",
    "ModelSpec",
    "NormalizedUsage",
    "Pricing",
    "ProviderInfo",
    "ProviderUnavailableError",
    "as_model_spec",
    "capabilities_for",
    "create_model",
    "estimate_cost",
    "load_provider_class",
    "normalize_provider",
    "normalize_strands_usage",
    "provider_available",
    "provider_of",
    "resolve_max_output_tokens",
]
