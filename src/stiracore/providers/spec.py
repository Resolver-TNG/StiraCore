"""Provider-neutral model specification (#204).

A :class:`ModelSpec` names *which* Strands ``Model`` implementation to build and
with what options. It holds no client objects of its own and is safe to keep in
configuration once secrets are expressed as environment-variable names (#205).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

DEFAULT_PROVIDER = "bedrock"


@dataclass(frozen=True)
class ProviderInfo:
    """Static facts about one Strands model provider."""

    name: str
    module: str
    class_name: str
    # Stira extra that installs the optional SDK (None = already a core dependency).
    extra: str | None
    # Phase 1 providers are covered by contract tests and documented setup.
    phase: int


PROVIDERS: Mapping[str, ProviderInfo] = MappingProxyType(
    {
        info.name: info
        for info in (
            ProviderInfo("bedrock", "strands.models.bedrock", "BedrockModel", None, 1),
            ProviderInfo("anthropic", "strands.models.anthropic", "AnthropicModel", "anthropic", 1),
            ProviderInfo("openai", "strands.models.openai", "OpenAIModel", "openai", 1),
            ProviderInfo(
                "openai_responses",
                "strands.models.openai_responses",
                "OpenAIResponsesModel",
                "openai",
                1,
            ),
            ProviderInfo("ollama", "strands.models.ollama", "OllamaModel", "ollama", 1),
            ProviderInfo("llamacpp", "strands.models.llamacpp", "LlamaCppModel", None, 1),
            ProviderInfo("gemini", "strands.models.gemini", "GeminiModel", "gemini", 2),
            ProviderInfo("litellm", "strands.models.litellm", "LiteLLMModel", "litellm", 2),
            ProviderInfo("mistral", "strands.models.mistral", "MistralModel", "mistral", 2),
            ProviderInfo("writer", "strands.models.writer", "WriterModel", "writer", 2),
            ProviderInfo("llamaapi", "strands.models.llamaapi", "LlamaAPIModel", "llamaapi", 2),
            ProviderInfo(
                "sagemaker", "strands.models.sagemaker", "SageMakerAIModel", "sagemaker", 2
            ),
        )
    }
)

_ALIASES = {
    "aws": "bedrock",
    "amazon-bedrock": "bedrock",
    "openai-responses": "openai_responses",
    "llama.cpp": "llamacpp",
    "llama-cpp": "llamacpp",
    "google": "gemini",
    "llama-api": "llamaapi",
}


def normalize_provider(name: str) -> str:
    """Return the canonical provider name or raise ``ValueError``."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("provider must be a nonempty string")
    key = name.strip().lower()
    key = _ALIASES.get(key, key)
    if key not in PROVIDERS:
        raise ValueError(
            f"Unknown model provider {name!r}. Known providers: {', '.join(sorted(PROVIDERS))}"
        )
    return key


_SECRET_OPTION = re.compile(r"key|token|secret|password|authorization|credential", re.IGNORECASE)


def redact_options(value: Any) -> Any:
    """Mask secret-looking option values (``*_env`` names are references, kept)."""
    if isinstance(value, Mapping):
        return {
            key: "***"
            if isinstance(key, str) and _SECRET_OPTION.search(key) and not key.endswith("_env")
            else redact_options(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(redact_options(item) for item in value)
    return value


@dataclass(frozen=True, eq=True, repr=False)
class ModelSpec:
    """Which provider/model to use and the provider options to pass through.

    ``options`` are forwarded to the provider's Strands ``Model`` constructor
    after a small, documented normalisation (see ``create_model``):
    ``client_args`` for the HTTP client, ``base_url`` for OpenAI-compatible or
    local servers, ``region_name`` for Bedrock, and generation parameters such
    as ``max_tokens`` / ``temperature`` / ``top_p``.
    """

    provider: str
    model_id: str
    options: Mapping[str, Any] = field(default_factory=dict)

    __hash__ = None  # options may hold unhashable values

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", normalize_provider(self.provider))
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id must be a nonempty string")
        if not isinstance(self.options, Mapping) or any(
            not isinstance(key, str) for key in self.options
        ):
            raise ValueError("options must be a mapping with string keys")
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))

    def __repr__(self) -> str:
        options = redact_options(dict(self.options))
        return (
            f"ModelSpec(provider={self.provider!r}, model_id={self.model_id!r}, "
            f"options={options!r})"
        )

    @classmethod
    def bedrock(cls, model_id: str, region_name: str | None = None, **options: Any) -> ModelSpec:
        """Bedrock shorthand matching the historical ``model_id``/``region_name`` pair."""
        if region_name is not None:
            options["region_name"] = region_name
        return cls(DEFAULT_PROVIDER, model_id, options)

    @classmethod
    def parse(cls, ref: str, **options: Any) -> ModelSpec:
        """Parse ``"provider:model"``; anything else is a Bedrock model ID.

        Bedrock IDs such as ``global.anthropic.claude-haiku-4-5-20251001-v1:0``
        contain colons, so only a known provider name before the first colon
        selects a provider (``"ollama:llama3.2:3b"`` -> ollama / ``llama3.2:3b``).
        """
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("model reference must be a nonempty string")
        head, sep, rest = ref.strip().partition(":")
        if sep and rest:
            try:
                provider = normalize_provider(head)
            except ValueError:
                provider = None
            if provider is not None:
                return cls(provider, rest, options)
        return cls(DEFAULT_PROVIDER, ref.strip(), options)

    @property
    def info(self) -> ProviderInfo:
        return PROVIDERS[self.provider]

    @property
    def region_name(self) -> str | None:
        value = self.options.get("region_name")
        return value if isinstance(value, str) else None

    def with_options(self, **options: Any) -> ModelSpec:
        """Return a copy whose options are updated (later values win)."""
        return ModelSpec(self.provider, self.model_id, {**self.options, **options})

    def with_model_id(self, model_id: str) -> ModelSpec:
        return ModelSpec(self.provider, model_id, self.options)

    def ref(self) -> str:
        """Canonical ``provider:model`` reference (Bedrock IDs stay bare)."""
        if self.provider == DEFAULT_PROVIDER:
            return self.model_id
        return f"{self.provider}:{self.model_id}"


def as_model_spec(value: ModelSpec | str, *, region: str | None = None) -> ModelSpec:
    """Coerce a routing value: plain strings remain Bedrock model IDs (legacy)."""
    if isinstance(value, ModelSpec):
        if value.provider == DEFAULT_PROVIDER and region and "region_name" not in value.options:
            return value.with_options(region_name=region)
        return value
    if isinstance(value, str):
        return ModelSpec.bedrock(value, region_name=region)
    raise TypeError("model must be a ModelSpec or a Bedrock model ID string")


__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "ModelSpec",
    "ProviderInfo",
    "as_model_spec",
    "normalize_provider",
    "redact_options",
]
