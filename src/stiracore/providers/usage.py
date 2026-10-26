"""Provider-neutral token usage and cost (#206).

Two inputs are normalised into :class:`NormalizedUsage`:

* Strands ``Usage`` (``inputTokens`` / ``outputTokens`` / ``cacheReadInputTokens`` /
  ``cacheWriteInputTokens``), as found in ``AgentResult.metrics.accumulated_usage``.
  Its meaning differs by provider: OpenAI-style providers (OpenAI, Responses,
  Gemini, LiteLLM, llama.cpp) copy ``prompt_tokens`` into ``inputTokens``, which
  *includes* cached tokens, while Bedrock Converse and the Anthropic API report
  cache reads/writes *separately* from ``inputTokens`` (strands-agents 1.57 source).
* Raw provider response ``usage`` objects (for callers that talk to an API or an
  external agent directly).

``input_tokens`` in the result is always the *uncached* input, so that
``input + cache_read + cache_write`` is the prompt size and each bucket can be
priced separately. Fields a provider does not report stay ``None`` — never 0 —
and :func:`estimate_cost` returns ``None`` when a needed count or price is
unknown, so unknown cost is never displayed as free.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

# Strands inputTokens includes cache reads/writes for these providers.
INPUT_INCLUDES_CACHE = frozenset({"openai", "openai_responses", "gemini", "litellm", "llamacpp"})


@dataclass(frozen=True)
class NormalizedUsage:
    input_tokens: int | None = None  # uncached input
    output_tokens: int | None = None  # includes reasoning/thinking tokens
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None  # subset of output_tokens, when reported

    @property
    def prompt_tokens(self) -> int | None:
        if self.input_tokens is None:
            return None
        return self.input_tokens + (self.cache_read_tokens or 0) + (self.cache_write_tokens or 0)

    @property
    def total_tokens(self) -> int | None:
        prompt = self.prompt_tokens
        if prompt is None or self.output_tokens is None:
            return None
        return prompt + self.output_tokens

    def as_dict(self) -> dict[str, int | None]:
        data = {item.name: getattr(self, item.name) for item in fields(self)}
        data["total_tokens"] = self.total_tokens
        return data


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _get(source: Any, *path: str) -> Any:
    for key in path:
        if source is None:
            return None
        source = source.get(key) if isinstance(source, Mapping) else getattr(source, key, None)
    return source


def _uncached(prompt: int | None, *cached: int | None) -> int | None:
    if prompt is None:
        return None
    return max(0, prompt - sum(value or 0 for value in cached))


def normalize_strands_usage(usage: Mapping[str, Any] | None, provider: str) -> NormalizedUsage:
    """Normalise Strands ``Usage`` from a provider's adapter (see module docstring)."""
    if not usage:
        return NormalizedUsage()
    raw_input = _count(_get(usage, "inputTokens"))
    read = _count(_get(usage, "cacheReadInputTokens"))
    write = _count(_get(usage, "cacheWriteInputTokens"))
    if provider in INPUT_INCLUDES_CACHE:
        raw_input = _uncached(raw_input, read, write)
    return NormalizedUsage(
        input_tokens=raw_input,
        output_tokens=_count(_get(usage, "outputTokens")),
        cache_read_tokens=read,
        cache_write_tokens=write,
    )


def _openai_chat(raw: Any) -> NormalizedUsage:
    read = _count(_get(raw, "prompt_tokens_details", "cached_tokens"))
    write = _count(_get(raw, "prompt_tokens_details", "cache_write_tokens"))
    return NormalizedUsage(
        input_tokens=_uncached(_count(_get(raw, "prompt_tokens")), read, write),
        output_tokens=_count(_get(raw, "completion_tokens")),
        cache_read_tokens=read,
        cache_write_tokens=write,
        reasoning_tokens=_count(_get(raw, "completion_tokens_details", "reasoning_tokens")),
    )


def _openai_responses(raw: Any) -> NormalizedUsage:
    read = _count(_get(raw, "input_tokens_details", "cached_tokens"))
    write = _count(_get(raw, "input_tokens_details", "cache_write_tokens"))
    return NormalizedUsage(
        input_tokens=_uncached(_count(_get(raw, "input_tokens")), read, write),
        output_tokens=_count(_get(raw, "output_tokens")),
        cache_read_tokens=read,
        cache_write_tokens=write,
        reasoning_tokens=_count(_get(raw, "output_tokens_details", "reasoning_tokens")),
    )


def _anthropic(raw: Any) -> NormalizedUsage:
    return NormalizedUsage(
        input_tokens=_count(_get(raw, "input_tokens")),
        output_tokens=_count(_get(raw, "output_tokens")),
        cache_read_tokens=_count(_get(raw, "cache_read_input_tokens")),
        cache_write_tokens=_count(_get(raw, "cache_creation_input_tokens")),
    )


def _bedrock(raw: Any) -> NormalizedUsage:
    return NormalizedUsage(
        input_tokens=_count(_get(raw, "inputTokens")),
        output_tokens=_count(_get(raw, "outputTokens")),
        cache_read_tokens=_count(_get(raw, "cacheReadInputTokens")),
        cache_write_tokens=_count(_get(raw, "cacheWriteInputTokens")),
    )


def _gemini(raw: Any) -> NormalizedUsage:
    prompt = _count(_get(raw, "prompt_token_count"))
    tool_prompt = _count(_get(raw, "tool_use_prompt_token_count")) or 0
    read = _count(_get(raw, "cached_content_token_count"))
    candidates = _count(_get(raw, "candidates_token_count"))
    thoughts = _count(_get(raw, "thoughts_token_count"))
    output = None if candidates is None else candidates + (thoughts or 0)
    return NormalizedUsage(
        input_tokens=_uncached(None if prompt is None else prompt + tool_prompt, read),
        output_tokens=output,
        cache_read_tokens=read,
        reasoning_tokens=thoughts,
    )


def _ollama(raw: Any) -> NormalizedUsage:
    return NormalizedUsage(
        input_tokens=_count(_get(raw, "prompt_eval_count")),
        output_tokens=_count(_get(raw, "eval_count")),
    )


_RAW = {
    "openai": _openai_chat,
    "litellm": _openai_chat,
    "llamacpp": _openai_chat,
    "mistral": _openai_chat,
    "openai_responses": _openai_responses,
    "anthropic": _anthropic,
    "bedrock": _bedrock,
    "gemini": _gemini,
    "ollama": _ollama,
}


def normalize_raw_usage(provider: str, raw: Any) -> NormalizedUsage:
    """Normalise a provider's own response ``usage`` (dict or SDK object)."""
    try:
        parser = _RAW[provider]
    except KeyError:
        raise ValueError(f"No raw usage parser for provider '{provider}'") from None
    return parser(raw) if raw is not None else NormalizedUsage()


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens. ``None`` = unknown (never treated as free)."""

    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None


def estimate_cost(usage: NormalizedUsage, pricing: Pricing | None) -> float | None:
    """Cost in USD, or ``None`` when any needed count or price is unknown."""
    if pricing is None:
        return None
    buckets = (
        (usage.input_tokens, pricing.input_per_mtok, True),
        (usage.output_tokens, pricing.output_per_mtok, True),
        (usage.cache_read_tokens, pricing.cache_read_per_mtok, False),
        (usage.cache_write_tokens, pricing.cache_write_per_mtok, False),
    )
    total = 0.0
    for tokens, price, required in buckets:
        if tokens is None:
            if required:
                return None
            continue
        if tokens == 0:
            continue
        if price is None:
            return None
        total += tokens * price / 1_000_000
    return total


def format_cost(cost: float | None) -> str:
    """Display helper: unknown is "unknown", never "$0"."""
    return "unknown" if cost is None else f"${cost:.4f}"


__all__ = [
    "INPUT_INCLUDES_CACHE",
    "NormalizedUsage",
    "Pricing",
    "estimate_cost",
    "format_cost",
    "normalize_raw_usage",
    "normalize_strands_usage",
]
