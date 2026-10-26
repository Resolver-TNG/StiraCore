"""Capability table and usage normalisation (#206)."""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from stiracore.agent import model_factory, nxp_agent
from stiracore.agent.stimulus_classifier import RuleBasedClassifier
from stiracore.providers import ModelSpec, create_model
from stiracore.providers.capabilities import (
    UNKNOWN,
    ModelCapabilities,
    capabilities_for,
    lookup_capabilities,
)
from stiracore.providers.usage import (
    NormalizedUsage,
    Pricing,
    estimate_cost,
    format_cost,
    normalize_raw_usage,
    normalize_strands_usage,
)
from tests.provider_fakes import FakeModel, text_turn

# --- capabilities ----------------------------------------------------------


def test_unknown_models_get_conservative_defaults():
    caps = capabilities_for("openai", "some-new-model")
    assert caps is UNKNOWN
    assert caps.as_dict() == {
        "vision": False,
        "tools": False,
        "thinking": False,
        "prompt_cache": False,
        "thinking_uses_output_budget": False,
        "max_output": None,
        "thinking_reserve": None,
        "context_window": None,
        "known": False,
        "source": "unknown model: conservative defaults",
    }


@pytest.mark.parametrize(
    ("role", "known", "vision", "thinking"),
    [
        (model_factory.ModelRole.ORCHESTRATOR, True, True, True),  # Opus 5.5
        (model_factory.ModelRole.REVIEWER, True, True, True),  # Sonnet 5
        (model_factory.ModelRole.FAST_GATE, True, True, True),  # Haiku 4.5
        (model_factory.ModelRole.CODER, False, False, False),  # not documented here
    ],
)
def test_default_bedrock_routes(role, known, vision, thinking):
    caps = lookup_capabilities("bedrock", model_factory.DEFAULT_MODELS[role])
    assert (caps.known, caps.vision, caps.thinking) == (known, vision, thinking)


def test_thinking_budget_flag_and_provider_scoping():
    opus = lookup_capabilities("bedrock", "global.anthropic.claude-opus-5-5")
    assert opus.thinking_uses_output_budget and opus.prompt_cache and opus.tools
    assert lookup_capabilities("anthropic", "claude-sonnet-4-5").known
    # Patterns are scoped to providers that serve the model.
    assert lookup_capabilities("ollama", "claude-sonnet-4-5") is UNKNOWN
    assert lookup_capabilities("bedrock", "us.amazon.nova-micro-v1:0").vision is False
    assert lookup_capabilities("bedrock", "us.amazon.nova-pro-v1:0").vision is True


def test_local_models_never_claim_prompt_cache_and_record_observations():
    qwen = lookup_capabilities("llamacpp", "qwen2.5-0.5b-instruct")
    assert (qwen.known, qwen.tools, qwen.vision, qwen.prompt_cache) == (True, True, False, False)
    assert qwen.source.startswith("observed")
    assert lookup_capabilities("ollama", "gpt-oss:20b").prompt_cache is False


def test_spec_override_and_validation():
    spec = ModelSpec(
        "ollama", "my-llava", {"capabilities": {"vision": True, "context_window": 8192}}
    )
    caps = capabilities_for(spec)
    assert (caps.known, caps.vision, caps.context_window, caps.source) == (
        True,
        True,
        8192,
        "configured override",
    )
    with pytest.raises(ValueError, match="Unknown capability"):
        capabilities_for(ModelSpec("ollama", "m", {"capabilities": {"telepathy": True}}))
    with pytest.raises(ValueError, match="invalid value"):
        capabilities_for(ModelSpec("ollama", "m", {"capabilities": {"vision": "yes"}}))
    # The override is Stira metadata, never a provider constructor argument.
    built = create_model(spec, model_class=lambda *a, **kw: kw)
    assert "capabilities" not in built


def test_nxp_agent_exposes_capabilities(monkeypatch):
    monkeypatch.setattr(nxp_agent, "BedrockModel", lambda **kw: FakeModel())
    legacy = nxp_agent.NXPAgent("global.anthropic.claude-sonnet-5", "us-east-1")
    assert legacy.capabilities.vision is True
    assert nxp_agent.NXPAgent(model=FakeModel()).capabilities is UNKNOWN


def _png_payload():
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(buffer, format="PNG")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(buffer.getvalue()).decode(),
        },
    }


@pytest.mark.parametrize(
    ("spec", "accepted"),
    [
        (ModelSpec("llamacpp", "qwen2.5-0.5b-instruct"), False),  # known text-only
        (ModelSpec("llamacpp", "my-vlm", {"capabilities": {"vision": True}}), True),
        (ModelSpec("llamacpp", "unlisted-model"), True),  # unknown: unchanged behaviour
    ],
)
def test_chat_api_rejects_images_only_for_known_text_only_models(
    tmp_path, monkeypatch, spec, accepted
):
    from dashboard.app import create_app

    model = FakeModel([text_turn("seen")])
    monkeypatch.setattr(nxp_agent, "create_model", lambda spec, *, max_tokens=None: model)
    agent = nxp_agent.NXPAgent(model=spec, stimulus_classifier=RuleBasedClassifier())
    app = create_app(state_path=tmp_path / "state.json", chat_agent=agent)

    response = app.test_client().post(
        "/api/chat", json={"message": "what is this?", "images": [_png_payload()]}
    )

    if accepted:
        assert response.status_code == 200 and "seen" in response.text
        assert len(model.requests) == 1
    else:
        assert response.status_code == 400
        assert response.get_json()["capability"] == "vision"
        assert model.requests == []  # rejected before any model call
    # Text-only turns are never affected.
    text_only = app.test_client().post("/api/chat", json={"message": "hi"})
    assert text_only.status_code == 200


# --- usage: raw response shapes --------------------------------------------

# Shapes checked against the SDK types / service model (see the *_shape tests).
RAW = {
    "openai": {
        "prompt_tokens": 2006,
        "completion_tokens": 300,
        "total_tokens": 2306,
        "prompt_tokens_details": {"cached_tokens": 1920, "audio_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 192, "audio_tokens": 0},
    },
    "openai_responses": {
        "input_tokens": 1200,
        "input_tokens_details": {"cached_tokens": 1024, "cache_write_tokens": 0},
        "output_tokens": 350,
        "output_tokens_details": {"reasoning_tokens": 128},
        "total_tokens": 1550,
    },
    "anthropic": {
        "input_tokens": 12,
        "output_tokens": 210,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 11500,
    },
    # Observed live (Bedrock Converse via stira_server, 2026-10-12).
    "bedrock": {"inputTokens": 11936, "outputTokens": 18, "totalTokens": 11954},
    "gemini": {
        "prompt_token_count": 4200,
        "cached_content_token_count": 4096,
        "candidates_token_count": 120,
        "thoughts_token_count": 300,
        "total_token_count": 4620,
    },
    "ollama": {"prompt_eval_count": 26, "eval_count": 8},
    # Observed live (llama.cpp b10809 /v1/chat/completions, repeated prompt).
    "llamacpp": {
        "completion_tokens": 8,
        "prompt_tokens": 19,
        "total_tokens": 27,
        "prompt_tokens_details": {"cached_tokens": 18},
    },
}

EXPECTED = {
    "openai": NormalizedUsage(86, 300, 1920, None, 192),
    "openai_responses": NormalizedUsage(176, 350, 1024, 0, 128),
    "anthropic": NormalizedUsage(12, 210, 11500, 0, None),
    "bedrock": NormalizedUsage(11936, 18, None, None, None),
    "gemini": NormalizedUsage(104, 420, 4096, None, 300),
    "ollama": NormalizedUsage(26, 8, None, None, None),
    "llamacpp": NormalizedUsage(1, 8, 18, None, None),
}


@pytest.mark.parametrize("provider", sorted(RAW))
def test_raw_usage_normalisation(provider):
    usage = normalize_raw_usage(provider, RAW[provider])
    assert usage == EXPECTED[provider]


def test_prompt_and_total_add_up_across_semantics():
    openai = normalize_raw_usage("openai", RAW["openai"])
    assert (openai.prompt_tokens, openai.total_tokens) == (2006, 2306)
    anthropic = normalize_raw_usage("anthropic", RAW["anthropic"])
    assert (anthropic.prompt_tokens, anthropic.total_tokens) == (11512, 11722)
    gemini = normalize_raw_usage("gemini", RAW["gemini"])
    assert gemini.total_tokens == RAW["gemini"]["total_token_count"]


def test_bedrock_raw_keys_match_the_service_model():
    import botocore.session

    shape = botocore.session.get_session().get_service_model("bedrock-runtime")
    members = set(shape.shape_for("TokenUsage").members)
    assert {"inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"} <= (
        members
    )
    assert set(RAW["bedrock"]) <= members


@pytest.mark.parametrize(
    ("module", "path", "fixture"),
    [
        ("openai", "openai.types.CompletionUsage", "openai"),
        ("openai", "openai.types.responses.ResponseUsage", "openai_responses"),
        ("anthropic", "anthropic.types.Usage", "anthropic"),
        ("google.genai", "google.genai.types.GenerateContentResponseUsageMetadata", "gemini"),
    ],
)
def test_raw_fixtures_match_sdk_types(module, path, fixture):
    pytest.importorskip(module)
    import importlib

    module_name, _, name = path.rpartition(".")
    cls = getattr(importlib.import_module(module_name), name)
    model = cls.model_validate(RAW[fixture])
    assert set(RAW[fixture]) <= set(type(model).model_fields)
    # SDK objects (attribute access) normalise the same as dicts.
    assert normalize_raw_usage(fixture, model) == EXPECTED[fixture]


# --- usage: Strands Usage from each adapter --------------------------------


def _same_buckets(a: NormalizedUsage, b: NormalizedUsage) -> bool:
    return (
        a.input_tokens,
        a.output_tokens,
        a.cache_read_tokens or 0,
        a.cache_write_tokens or 0,
    ) == (
        b.input_tokens,
        b.output_tokens,
        b.cache_read_tokens or 0,
        b.cache_write_tokens or 0,
    )


def _strands_usage(provider):
    """Run the provider's own Strands adapter on the raw fixture."""
    raw = RAW[provider]
    if provider == "llamacpp":
        from strands.models.llamacpp import LlamaCppModel

        data = SimpleNamespace(
            prompt_tokens=raw["prompt_tokens"],
            completion_tokens=raw["completion_tokens"],
            total_tokens=raw["total_tokens"],
            cached_tokens=raw["prompt_tokens_details"]["cached_tokens"],
        )
        model = LlamaCppModel(model_id="m")
        return model._format_chunk({"chunk_type": "metadata", "data": data})
    if provider == "openai":
        pytest.importorskip("openai")
        from openai.types import CompletionUsage
        from strands.models.openai import OpenAIModel

        model = OpenAIModel(client_args={"api_key": "test-not-a-secret"}, model_id="m")
        data = CompletionUsage.model_validate(raw)
        return model.format_chunk({"chunk_type": "metadata", "data": data})
    if provider == "openai_responses":
        pytest.importorskip("openai")
        from openai.types.responses import ResponseUsage
        from strands.models.openai_responses import OpenAIResponsesModel

        model = OpenAIResponsesModel(client_args={"api_key": "test-not-a-secret"}, model_id="m")
        data = ResponseUsage.model_validate(raw)
        return model._format_chunk({"chunk_type": "metadata", "data": data})
    if provider == "anthropic":
        pytest.importorskip("anthropic")
        from strands.models.anthropic import AnthropicModel

        model = AnthropicModel(
            client_args={"api_key": "test-not-a-secret"}, model_id="m", max_tokens=10
        )
        return model.format_chunk({"type": "metadata", "usage": raw})
    if provider == "gemini":
        pytest.importorskip("google.genai")
        from google.genai.types import GenerateContentResponseUsageMetadata
        from strands.models.gemini import GeminiModel

        model = GeminiModel(client_args={"api_key": "test-not-a-secret"}, model_id="m")
        data = GenerateContentResponseUsageMetadata.model_validate(raw)
        return model._format_chunk({"chunk_type": "metadata", "data": data})
    if provider == "ollama":
        pytest.importorskip("ollama")
        from strands.models.ollama import OllamaModel

        model = OllamaModel(None, model_id="m")
        data = SimpleNamespace(**raw, total_duration=1_000_000)
        return model.format_chunk({"chunk_type": "metadata", "data": data})
    raise AssertionError(provider)


@pytest.mark.parametrize(
    "provider", ["llamacpp", "openai", "openai_responses", "anthropic", "gemini", "ollama"]
)
def test_strands_usage_normalises_like_the_raw_response(provider):
    chunk = _strands_usage(provider)
    from_strands = normalize_strands_usage(chunk["metadata"]["usage"], provider)
    assert _same_buckets(from_strands, EXPECTED[provider])


def test_strands_usage_for_bedrock_and_custom_keeps_input_as_is():
    usage = {"inputTokens": 30, "outputTokens": 5, "cacheReadInputTokens": 900}
    assert normalize_strands_usage(usage, "bedrock").input_tokens == 30
    assert normalize_strands_usage(usage, "custom").input_tokens == 30
    assert normalize_strands_usage(usage, "openai").input_tokens == 0  # inclusive semantics
    assert normalize_strands_usage({}, "openai") == NormalizedUsage()
    bad = {"inputTokens": -1, "outputTokens": True}
    assert normalize_strands_usage(bad, "bedrock") == NormalizedUsage()


# --- cost: unknown is never zero -------------------------------------------


def test_cost_needs_every_used_price():
    usage = NormalizedUsage(1_000_000, 100_000, 2_000_000, 0)
    full = Pricing(3.0, 15.0, cache_read_per_mtok=0.3, cache_write_per_mtok=3.75)
    assert estimate_cost(usage, full) == pytest.approx(3.0 + 1.5 + 0.6)
    assert estimate_cost(usage, Pricing(3.0, 15.0)) is None  # cache reads unpriced
    assert estimate_cost(usage, None) is None
    assert estimate_cost(NormalizedUsage(10, None), full) is None
    assert estimate_cost(NormalizedUsage(1_000_000, 0), Pricing(3.0, None)) == pytest.approx(3.0)
    assert format_cost(None) == "unknown"
    assert format_cost(0.0) == "$0.0000"


def test_capabilities_dataclass_is_immutable():
    with pytest.raises(AttributeError):
        ModelCapabilities().vision = True  # type: ignore[misc]


def test_chat_turn_reports_cache_buckets_when_the_provider_does(tmp_path, monkeypatch):
    import json

    from dashboard.app import create_app

    usage = {"inputTokens": 40, "outputTokens": 5, "totalTokens": 45, "cacheReadInputTokens": 900}
    agent = nxp_agent.NXPAgent(
        model=FakeModel([text_turn("cached", usage=usage), text_turn("plain")]),
        stimulus_classifier=RuleBasedClassifier(),
    )
    client = create_app(state_path=tmp_path / "state.json", chat_agent=agent).test_client()

    def turn_event(text):
        block = next(b for b in text.split("\n\n") if b.startswith("event: turn"))
        return json.loads(block.split("data: ", 1)[1])

    first = turn_event(client.post("/api/chat", json={"message": "a"}).text)
    assert (first["input_tokens"], first["output_tokens"], first["cache_read_tokens"]) == (
        40,
        5,
        900,
    )
    assert "cache_write_tokens" not in first
    second = turn_event(client.post("/api/chat", json={"message": "b"}).text)
    assert "cache_read_tokens" not in second  # FakeModel default usage has no cache
