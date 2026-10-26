"""Model capability table (#206).

What a model can do (images, tools, reasoning/thinking, prompt caching) is a
property of the *model*, not of the provider adapter: every Strands 1.57
provider adapter transports the same ``{"image": {"format", "source": {"bytes"}}}``
content block, but a text-only model still rejects it. Unknown models get
conservative defaults (everything False, limits None, ``known=False``).

The table encodes public provider documentation as of 2026-10; rows whose
behaviour was observed on a live endpoint say so in ``source``. Override per
model with ``ModelSpec.options["capabilities"]`` (e.g. a local vision model).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from fnmatch import fnmatchcase
from typing import Any

from .spec import ModelSpec, normalize_provider


@dataclass(frozen=True)
class ModelCapabilities:
    vision: bool = False
    tools: bool = False
    thinking: bool = False
    prompt_cache: bool = False
    # Reasoning/thinking tokens are billed as output and count toward max_tokens
    # (Claude extended/adaptive thinking, OpenAI reasoning models).
    thinking_uses_output_budget: bool = False
    max_output: int | None = None
    # Room the automatic cap adds for thinking on top of the answer's share (#416), used only
    # when ``thinking_uses_output_budget`` and ``max_output`` are set; never above max_output.
    thinking_reserve: int | None = None
    context_window: int | None = None
    known: bool = False
    source: str = "unknown model: conservative defaults"

    def as_dict(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


UNKNOWN = ModelCapabilities()

_DOCS = "provider documentation (2026-10)"
_CLAUDE_REASONING = ModelCapabilities(
    vision=True,
    tools=True,
    thinking=True,
    prompt_cache=True,
    thinking_uses_output_budget=True,
    thinking_reserve=32_000,
    known=True,
    source=_DOCS,
)
_CLAUDE_BASIC = replace(
    _CLAUDE_REASONING, thinking=False, thinking_uses_output_budget=False, thinking_reserve=None
)
# Synchronous Messages API output limits (#363). The 5.x families document 128K; the 4.x rows
# carry the lowest limit that family has shipped with (Opus 4/4.1: 32K; Sonnet/Haiku 4: 64K),
# so a row never claims more than every model it matches accepts.
_CLAUDE_5 = replace(_CLAUDE_REASONING, max_output=128_000)
_CLAUDE_OPUS_4 = replace(_CLAUDE_REASONING, max_output=32_000)
_CLAUDE_64K = replace(_CLAUDE_REASONING, max_output=64_000)
_OPENAI_REASONING = replace(_CLAUDE_REASONING)
_OPENAI_CHAT = replace(_CLAUDE_BASIC)
# Model pages (2026-10, #416): GPT-5 / mini / nano 128K "reasoning and output"; o3, o3-mini
# and o4-mini 100K; GPT-4.1 / mini 32,768. Gemini 2.5 Pro / Flash 65,536. Only exact IDs and
# dated snapshots: a family glob would also match variants with lower limits
# (gpt-5-chat-latest: 16,384; Gemini TTS/image models).
_OPENAI_128K = replace(_OPENAI_REASONING, max_output=128_000)
_OPENAI_100K = replace(_OPENAI_REASONING, max_output=100_000)
_GEMINI_64K = replace(_OPENAI_REASONING, max_output=65_536)
_VISION_TOOLS = ModelCapabilities(vision=True, tools=True, known=True, source=_DOCS)
_TOOLS_ONLY = ModelCapabilities(tools=True, known=True, source=_DOCS)
_REASONING_TOOLS = ModelCapabilities(
    tools=True,
    thinking=True,
    thinking_uses_output_budget=True,
    thinking_reserve=32_000,
    known=True,
    source=_DOCS,
)

# (providers, lowercase model-id glob, capabilities); first match wins.
# Bedrock IDs carry a vendor prefix (and optionally a geo/global prefix), so
# their patterns start with "*".
_ANY = ("*",)
_HOSTED_ANTHROPIC = ("bedrock", "anthropic", "litellm")
_HOSTED_OPENAI = ("openai", "openai_responses", "litellm")
_LOCAL = ("ollama", "llamacpp")
CAPABILITY_TABLE: tuple[tuple[tuple[str, ...], str, ModelCapabilities], ...] = (
    # Anthropic Claude (direct API, Bedrock "…anthropic.claude-…", LiteLLM).
    (_HOSTED_ANTHROPIC, "*claude-opus-5*", _CLAUDE_5),
    (_HOSTED_ANTHROPIC, "*claude-sonnet-5*", _CLAUDE_5),
    (_HOSTED_ANTHROPIC, "*claude-fable-5*", _CLAUDE_5),
    (_HOSTED_ANTHROPIC, "*claude-haiku-5*", _CLAUDE_5),
    (_HOSTED_ANTHROPIC, "*claude-opus-4*", _CLAUDE_OPUS_4),
    (_HOSTED_ANTHROPIC, "*claude-sonnet-4*", _CLAUDE_64K),
    (_HOSTED_ANTHROPIC, "*claude-haiku-4*", _CLAUDE_64K),
    (_HOSTED_ANTHROPIC, "*claude-3-7-sonnet*", _CLAUDE_64K),
    (_HOSTED_ANTHROPIC, "*claude-3-5-*", _CLAUDE_BASIC),
    # OpenAI.
    *(
        (_HOSTED_OPENAI, pattern, _OPENAI_128K)
        for name in ("gpt-5", "gpt-5-mini", "gpt-5-nano")
        for pattern in (name, f"{name}-20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]")
    ),
    (_HOSTED_OPENAI, "gpt-5*", _OPENAI_REASONING),
    (_HOSTED_OPENAI, "o3", _OPENAI_100K),
    (_HOSTED_OPENAI, "o3-mini", _OPENAI_100K),
    (_HOSTED_OPENAI, "o4-mini", _OPENAI_100K),
    (_HOSTED_OPENAI, "o3*", _OPENAI_REASONING),
    (_HOSTED_OPENAI, "o4-mini*", _OPENAI_REASONING),
    (_HOSTED_OPENAI, "gpt-4.1", replace(_OPENAI_CHAT, max_output=32_768)),
    (_HOSTED_OPENAI, "gpt-4.1-mini", replace(_OPENAI_CHAT, max_output=32_768)),
    (_HOSTED_OPENAI, "gpt-4.1*", _OPENAI_CHAT),
    (_HOSTED_OPENAI, "gpt-4o*", _OPENAI_CHAT),
    (("bedrock", "openai", "ollama", "llamacpp"), "*gpt-oss*", _REASONING_TOOLS),
    # Google Gemini.
    (("gemini", "litellm"), "*gemini-2.5-pro", _GEMINI_64K),
    (("gemini", "litellm"), "*gemini-2.5-flash", _GEMINI_64K),
    (("gemini", "litellm"), "*gemini-3*", replace(_OPENAI_REASONING)),
    (("gemini", "litellm"), "*gemini-2.5*", replace(_OPENAI_REASONING)),
    (("gemini", "litellm"), "*gemini-2.0*", _VISION_TOOLS),
    # Amazon Nova and Meta Llama on Bedrock.
    (("bedrock",), "*amazon.nova-micro*", _TOOLS_ONLY),
    (("bedrock",), "*amazon.nova-*", _VISION_TOOLS),
    (("bedrock",), "*llama3-2-11b*", _VISION_TOOLS),
    (("bedrock",), "*llama3-2-90b*", _VISION_TOOLS),
    (("bedrock",), "*llama4*", _VISION_TOOLS),
    # Mistral.
    (("mistral", "litellm"), "pixtral*", _VISION_TOOLS),
    (("mistral",), "mistral-large*", _TOOLS_ONLY),
    # Local models (capabilities depend on the weights and server flags).
    (
        _LOCAL,
        "qwen2.5*instruct*",
        replace(_TOOLS_ONLY, source="observed: llama.cpp --jinja tool call (2026-10-12)"),
    ),
    (_LOCAL, "qwen3*", _TOOLS_ONLY),
    (_LOCAL, "llama3.1*", _TOOLS_ONLY),
    (_LOCAL, "llama3.2-vision*", _VISION_TOOLS),
    (_LOCAL, "llama3.2*", _TOOLS_ONLY),
    (_LOCAL, "llava*", ModelCapabilities(vision=True, known=True, source=_DOCS)),
    (_LOCAL, "gemma3*", ModelCapabilities(vision=True, known=True, source=_DOCS)),
)

# Providers whose Strands adapter ignores prompt-cache settings (1.57 source).
_NO_PROMPT_CACHE_PROVIDERS = frozenset({"ollama", "llamacpp"})


def _override(base: ModelCapabilities, overrides: Mapping[str, Any]) -> ModelCapabilities:
    names = {item.name for item in fields(ModelCapabilities)} - {"known", "source"}
    unknown = set(overrides) - names
    if unknown:
        raise ValueError("Unknown capability keys: " + ", ".join(sorted(unknown)))
    for key, value in overrides.items():
        if key in ("max_output", "context_window", "thinking_reserve"):
            valid = value is None or (type(value) is int and value > 0)
        else:
            valid = type(value) is bool
        if not valid:
            raise ValueError(f"capability {key} has an invalid value")
    return replace(base, **overrides, known=True, source="configured override")


def lookup_capabilities(provider: str, model_id: str) -> ModelCapabilities:
    """Table lookup only (no overrides)."""
    provider = normalize_provider(provider)
    key = model_id.strip().lower()
    for providers, pattern, capabilities in CAPABILITY_TABLE:
        if (providers == _ANY or provider in providers) and fnmatchcase(key, pattern):
            if provider in _NO_PROMPT_CACHE_PROVIDERS and capabilities.prompt_cache:
                return replace(capabilities, prompt_cache=False)
            return capabilities
    return UNKNOWN


def capabilities_for(
    spec_or_provider: ModelSpec | str,
    model_id: str | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> ModelCapabilities:
    """Capabilities for a spec (honouring ``options["capabilities"]``) or provider/model."""
    if isinstance(spec_or_provider, ModelSpec):
        spec = spec_or_provider
        base = lookup_capabilities(spec.provider, spec.model_id)
        configured = spec.options.get("capabilities")
        if configured is not None:
            if not isinstance(configured, Mapping):
                raise ValueError("options['capabilities'] must be a mapping")
            base = _override(base, configured)
    else:
        if model_id is None:
            raise TypeError("model_id is required with a provider name")
        base = lookup_capabilities(spec_or_provider, model_id)
    return _override(base, overrides) if overrides else base


# Default per-call output cap (#363). The old fixed 8192 cut a single ``file_write`` of a few
# hundred lines off mid-argument. When the model's documented limit is known the default is
# min(this [+ the row's thinking_reserve when thinking shares the cap, #416], limit).
# A cap is a ceiling, not a purchase: output is billed for the tokens actually produced.
LEGACY_MAX_OUTPUT_TOKENS = 8192
AUTO_MAX_OUTPUT_TOKENS = 32_000

# The automatic cap when the model's limit is not recorded (unknown models, rows without
# ``max_output``), per provider (#416). Hosted APIs (Bedrock, Anthropic, OpenAI and OpenAI-
# compatible servers, Gemini, ...) reject a request whose cap is above the model's own limit,
# so the safe value is a floor the models they serve accept: 8192 stays. A local server
# (llama.cpp ``n_predict``, Ollama ``num_predict``) does not reject a large cap; generation
# ends at its context window, and there is no bill.
_UNKNOWN_LIMIT_MAX_OUTPUT = {"llamacpp": AUTO_MAX_OUTPUT_TOKENS, "ollama": AUTO_MAX_OUTPUT_TOKENS}


def unknown_limit_max_output(provider: str) -> int:
    """The automatic cap for ``provider`` when the model's own limit is not recorded."""
    try:
        name = normalize_provider(provider)
    except (TypeError, ValueError):
        return LEGACY_MAX_OUTPUT_TOKENS
    return _UNKNOWN_LIMIT_MAX_OUTPUT.get(name, LEGACY_MAX_OUTPUT_TOKENS)


def automatic_cap_source(spec_or_provider: ModelSpec | str, model_id: str | None = None) -> str:
    """Where the automatic cap comes from: ``"model"`` (its limit) or ``"provider_default"``."""
    try:
        limit = capabilities_for(spec_or_provider, model_id).max_output
    except (TypeError, ValueError):
        limit = None
    return "provider_default" if limit is None else "model"


def resolve_max_output_tokens(
    configured: int | None,
    spec_or_provider: ModelSpec | str,
    model_id: str | None = None,
) -> int:
    """The per-call output cap: an explicit value wins, else the capability-bound default."""
    if configured is not None:
        return configured
    try:
        capabilities = capabilities_for(spec_or_provider, model_id)
    except (TypeError, ValueError):
        capabilities = None
    if capabilities is None or capabilities.max_output is None:
        provider = (
            spec_or_provider.provider
            if isinstance(spec_or_provider, ModelSpec)
            else spec_or_provider
        )
        return unknown_limit_max_output(provider)
    wanted = AUTO_MAX_OUTPUT_TOKENS
    if capabilities.thinking_uses_output_budget and capabilities.thinking_reserve:
        wanted += capabilities.thinking_reserve
    return min(wanted, capabilities.max_output)


__all__ = [
    "AUTO_MAX_OUTPUT_TOKENS",
    "CAPABILITY_TABLE",
    "LEGACY_MAX_OUTPUT_TOKENS",
    "UNKNOWN",
    "ModelCapabilities",
    "automatic_cap_source",
    "capabilities_for",
    "lookup_capabilities",
    "resolve_max_output_tokens",
    "unknown_limit_max_output",
]
