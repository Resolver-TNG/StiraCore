"""Provider abstraction (#204): ModelSpec, create_model, NXPAgent model injection.

Nothing here needs network access, API keys, or AWS credentials.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import warnings
from unittest.mock import Mock

import pytest
from typing_extensions import get_type_hints

from stiracore.agent import model_factory, nxp_agent
from stiracore.agent.stimulus_classifier import RuleBasedClassifier
from stiracore.agent.subagent import create_nxp_subagent
from stiracore.providers import (
    PROVIDERS,
    ModelSpec,
    ProviderUnavailableError,
    as_model_spec,
    create_model,
    load_provider_class,
    provider_of,
)
from stiracore.providers import factory as provider_factory
from tests.provider_fakes import FakeModel, text_turn


class Recorder:
    """Stands in for a provider class and records constructor arguments."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def _no_bedrock(monkeypatch):
    monkeypatch.setattr(
        nxp_agent, "BedrockModel", Mock(side_effect=AssertionError("Bedrock must not be built"))
    )


# --- ModelSpec -------------------------------------------------------------


def test_spec_defaults_and_validation():
    spec = ModelSpec("OpenAI", "gpt-test", {"temperature": 0.1})
    assert spec.provider == "openai"
    assert spec.options["temperature"] == 0.1
    with pytest.raises(TypeError):
        spec.options["temperature"] = 1.0  # read-only view
    with pytest.raises(ValueError, match="Unknown model provider"):
        ModelSpec("nope", "x")
    with pytest.raises(ValueError, match="model_id"):
        ModelSpec("openai", " ")
    with pytest.raises(ValueError, match="options"):
        ModelSpec("openai", "x", {1: "bad"})


def test_spec_aliases_and_bedrock_shorthand():
    assert ModelSpec("amazon-bedrock", "m").provider == "bedrock"
    assert ModelSpec("openai-responses", "m").provider == "openai_responses"
    assert ModelSpec("llama.cpp", "m").provider == "llamacpp"
    spec = ModelSpec.bedrock("global.anthropic.claude-sonnet-5", region_name="us-east-1")
    assert (spec.provider, spec.region_name) == ("bedrock", "us-east-1")
    assert spec.ref() == "global.anthropic.claude-sonnet-5"


@pytest.mark.parametrize(
    ("ref", "provider", "model_id"),
    [
        ("global.anthropic.claude-haiku-4-5-20251001-v1:0", "bedrock", None),
        ("openai.gpt-oss-120b-1:0", "bedrock", None),
        ("ollama:llama3.2:3b", "ollama", "llama3.2:3b"),
        ("openai:gpt-test", "openai", "gpt-test"),
        ("bedrock:global.anthropic.claude-sonnet-5", "bedrock", "global.anthropic.claude-sonnet-5"),
    ],
)
def test_spec_parse_keeps_bedrock_ids_with_colons(ref, provider, model_id):
    spec = ModelSpec.parse(ref)
    assert spec.provider == provider
    assert spec.model_id == (model_id or ref)


def test_as_model_spec_treats_strings_as_bedrock_ids():
    spec = as_model_spec("ollama:llama3", region="eu-west-1")
    assert (spec.provider, spec.model_id, spec.region_name) == (
        "bedrock",
        "ollama:llama3",
        "eu-west-1",
    )
    other = ModelSpec("ollama", "llama3")
    assert as_model_spec(other, region="eu-west-1") is other


# --- create_model mapping onto the Strands constructors -------------------

# Expected constructor calls, written from the strands-agents 1.57 signatures.
EXPECTED = {
    "bedrock": ((), {"model_id": "m", "region_name": "eu-west-1", "max_tokens": 512}),
    "anthropic": ((), {"client_args": None, "model_id": "m", "max_tokens": 512}),
    "openai": ((), {"client_args": None, "model_id": "m", "params": {"max_tokens": 512}}),
    "openai_responses": (
        (),
        {"client_args": None, "model_id": "m", "params": {"max_output_tokens": 512}},
    ),
    "gemini": ((), {"client_args": None, "model_id": "m", "params": {"max_output_tokens": 512}}),
    "litellm": ((), {"client_args": None, "model_id": "m", "params": {"max_tokens": 512}}),
    "llamacpp": (
        (),
        {
            "base_url": "http://localhost:8080",
            "timeout": None,
            "model_id": "m",
            "params": {"max_tokens": 512},
        },
    ),
    "ollama": ((None,), {"ollama_client_args": None, "model_id": "m", "max_tokens": 512}),
    "mistral": ((), {"client_args": None, "model_id": "m", "max_tokens": 512}),
    "writer": ((), {"client_args": None, "model_id": "m", "max_tokens": 512}),
    "llamaapi": ((), {"client_args": None, "model_id": "m", "max_completion_tokens": 512}),
    "sagemaker": (
        (),
        {
            "endpoint_config": {"endpoint_name": "m"},
            "payload_config": {"max_tokens": 512},
            "boto_session": None,
            "boto_client_config": None,
        },
    ),
}


def test_every_known_provider_has_an_expectation():
    assert set(EXPECTED) == set(PROVIDERS)


@pytest.mark.parametrize("provider", sorted(EXPECTED))
def test_create_model_maps_spec_to_provider_constructor(provider):
    options = {"region_name": "eu-west-1"} if provider == "bedrock" else {}
    model = create_model(ModelSpec(provider, "m", options), max_tokens=512, model_class=Recorder)
    args, kwargs = EXPECTED[provider]
    assert (model.args, model.kwargs) == (args, kwargs)


def test_generation_options_move_into_params_and_explicit_budget_wins():
    spec = ModelSpec(
        "openai",
        "m",
        {"temperature": 0.0, "max_tokens": 64, "base_url": "http://127.0.0.1:1234/v1"},
    )
    model = create_model(spec, max_tokens=512, model_class=Recorder)
    assert model.kwargs == {
        "client_args": {"base_url": "http://127.0.0.1:1234/v1"},
        "model_id": "m",
        "params": {"temperature": 0.0, "max_tokens": 64},
    }
    # A provider-specific token key already in params suppresses the default.
    spec = ModelSpec("openai", "m", {"params": {"max_completion_tokens": 99}})
    assert create_model(spec, max_tokens=512, model_class=Recorder).kwargs["params"] == {
        "max_completion_tokens": 99
    }


def test_anthropic_generation_options_go_to_params():
    model = create_model(
        ModelSpec("anthropic", "m", {"temperature": 0.0}), max_tokens=64, model_class=Recorder
    )
    assert model.kwargs == {
        "client_args": None,
        "model_id": "m",
        "max_tokens": 64,
        "params": {"temperature": 0.0},
    }


def test_ollama_base_url_becomes_host():
    spec = ModelSpec("ollama", "llama3", {"base_url": "http://127.0.0.1:11434"})
    model = create_model(spec, model_class=Recorder)
    assert model.args == ("http://127.0.0.1:11434",)
    assert "max_tokens" not in model.kwargs


def test_model_id_in_options_is_rejected():
    with pytest.raises(ValueError, match="ModelSpec.model_id"):
        create_model(ModelSpec("openai", "m", {"model_id": "other"}), model_class=Recorder)


@pytest.mark.parametrize("provider", sorted(EXPECTED))
def test_mapping_matches_installed_strands_signature(provider):
    """Where the provider SDK is installed, bind our kwargs to the real class."""
    try:
        cls = load_provider_class(provider)
    except ProviderUnavailableError:
        pytest.skip(f"{provider} SDK not installed")
    options = {"region_name": "eu-west-1"} if provider == "bedrock" else {}
    recorded = create_model(ModelSpec(provider, "m", options), max_tokens=512, model_class=Recorder)
    signature = inspect.signature(cls.__init__)
    signature.bind(None, *recorded.args, **recorded.kwargs)
    config_class = next(
        (
            value
            for name, value in vars(cls).items()
            if name.endswith("Config") and isinstance(value, type) and name != "BaseModelConfig"
        ),
        None,
    )
    if provider in ("sagemaker",) or config_class is None:
        return
    accepted = set(get_type_hints(config_class))
    explicit = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind is not inspect.Parameter.VAR_KEYWORD
    }
    assert set(recorded.kwargs) - explicit <= accepted


@pytest.mark.parametrize("provider", sorted(EXPECTED))
def test_installed_providers_construct_without_config_warnings(provider, monkeypatch):
    """Real constructors (offline): our options must not trip Strands key validation."""
    if provider in ("bedrock", "sagemaker"):
        pytest.skip("tests never construct AWS clients; covered by the signature check")
    try:
        load_provider_class(provider)
    except ProviderUnavailableError:
        pytest.skip(f"{provider} SDK not installed")
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MISTRAL_API_KEY", "WRITER_API_KEY"):
        monkeypatch.setenv(name, "test-value-not-a-secret")
    monkeypatch.setenv("LLAMA_API_KEY", "test-value-not-a-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-value-not-a-secret")
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        model = create_model(ModelSpec(provider, "m", {"temperature": 0.2}), max_tokens=100)
    assert provider_of(model) == provider


def test_installed_core_providers_build_offline():
    # llama.cpp needs no optional SDK and creates no connection on construction.
    model = create_model(ModelSpec("llamacpp", "local"), max_tokens=256)
    assert provider_of(model) == "llamacpp"
    assert model.get_config()["params"] == {"max_tokens": 256}


def test_missing_sdk_names_the_extra(monkeypatch):
    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "strands.models.openai":
            raise ModuleNotFoundError("No module named 'openai'", name="openai")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(provider_factory.importlib, "import_module", fake_import)
    with pytest.raises(ProviderUnavailableError) as caught:
        create_model(ModelSpec("openai", "gpt-test"))
    message = str(caught.value)
    assert "pip install 'stiracore[openai]'" in message
    assert "'openai'" in message
    assert caught.value.extra == "openai"
    assert isinstance(caught.value, ImportError)


@pytest.mark.skipif(importlib.util.find_spec("openai") is not None, reason="openai installed")
def test_real_missing_openai_sdk_raises_clear_error():
    with pytest.raises(ProviderUnavailableError, match=r"stiracore\[openai\]"):
        create_model(ModelSpec("openai_responses", "gpt-test"))


# --- NXPAgent with an injected model --------------------------------------


def test_nxp_agent_runs_a_turn_with_injected_model(monkeypatch):
    _no_bedrock(monkeypatch)
    fake = FakeModel([text_turn("hello from a fake provider")], model_id="fake-1")
    agent = nxp_agent.NXPAgent(
        model=fake,
        system_prompt="You are offline.",
        stimulus_classifier=RuleBasedClassifier(),
    )

    result = agent("hi")

    assert str(result).strip() == "hello from a fake provider"
    assert agent.model is fake
    assert (agent.provider, agent.model_id, agent.region_name) == ("custom", "fake-1", None)
    assert agent.model_spec is None
    assert fake.requests[0]["system_prompt"] == "You are offline."
    assert fake.requests[0]["messages"][-1] == {"role": "user", "content": [{"text": "hi"}]}
    assert agent.last_turn_summary is not None  # post_turn ran exactly as for Bedrock


def test_nxp_agent_builds_model_from_spec(monkeypatch):
    _no_bedrock(monkeypatch)
    built = []

    def fake_create(spec, *, max_tokens=None):
        built.append((spec, max_tokens))
        return FakeModel([text_turn("spec ok")])

    monkeypatch.setattr(nxp_agent, "create_model", fake_create)
    spec = ModelSpec("ollama", "llama3.2:3b", {"base_url": "http://127.0.0.1:11434"})
    agent = nxp_agent.NXPAgent(
        model=spec, max_output_tokens=1024, stimulus_classifier=RuleBasedClassifier()
    )

    assert str(agent("hi")).strip() == "spec ok"
    assert built == [(spec, 1024)]
    assert (agent.provider, agent.model_id, agent.model_spec) == ("ollama", "llama3.2:3b", spec)


def test_nxp_agent_model_argument_validation(monkeypatch):
    _no_bedrock(monkeypatch)
    with pytest.raises(ValueError, match="model_id is required"):
        nxp_agent.NXPAgent()
    with pytest.raises(TypeError, match="ModelSpec"):
        nxp_agent.NXPAgent(model="global.anthropic.claude-sonnet-5")
    with pytest.raises(ValueError, match="region_name"):
        nxp_agent.NXPAgent(model=FakeModel(), region_name="us-east-1")
    with pytest.raises(ValueError, match="conflicts"):
        nxp_agent.NXPAgent(model_id="a", model=ModelSpec("ollama", "b"))


def test_legacy_bedrock_arguments_are_unchanged(monkeypatch):
    constructor = Mock(return_value=FakeModel())
    monkeypatch.setattr(nxp_agent, "BedrockModel", constructor)

    agent = nxp_agent.NXPAgent("offline-model", "us-east-1", max_output_tokens=2048)

    constructor.assert_called_once_with(
        model_id="offline-model", region_name="us-east-1", max_tokens=2048
    )
    assert (agent.provider, agent.model_id, agent.region_name) == (
        "bedrock",
        "offline-model",
        "us-east-1",
    )
    assert agent.model_spec == ModelSpec.bedrock("offline-model", region_name="us-east-1")


def test_subagent_inherits_parent_provider(monkeypatch):
    _no_bedrock(monkeypatch)
    monkeypatch.setattr(nxp_agent, "create_model", lambda spec, *, max_tokens=None: FakeModel())
    parent = nxp_agent.NXPAgent(model=ModelSpec("ollama", "llama3"))

    child = create_nxp_subagent(parent, "task")
    assert (child.provider, child.model_id) == ("ollama", "llama3")
    other = create_nxp_subagent(parent, "task", model_id="qwen3")
    assert (other.provider, other.model_id) == ("ollama", "qwen3")
    with pytest.raises(ValueError, match="Bedrock"):
        create_nxp_subagent(parent, "task", region_name="us-east-1")

    shared = FakeModel()
    instance_parent = nxp_agent.NXPAgent(model=shared)
    assert create_nxp_subagent(instance_parent, "task").model is shared


# --- provider-neutral role routing ----------------------------------------


def test_routing_accepts_model_specs(monkeypatch):
    spec = ModelSpec("ollama", "llama3")
    calls = []
    monkeypatch.setattr(
        model_factory,
        "create_model_from_spec",
        lambda value: calls.append(value) or "built",
    )
    monkeypatch.setattr(
        model_factory, "BedrockModel", Mock(side_effect=AssertionError("not Bedrock"))
    )
    role = model_factory.ModelRole.CODER
    context = model_factory.ExecutionContext.NORMAL

    result = model_factory.create_model_for_context(
        context, role, custom_routing={context: {role: spec}}, temperature=0.0
    )

    assert result == "built"
    assert calls == [spec.with_options(temperature=0.0)]
    assert model_factory.model_ref_for_context(context, role, {context: {role: spec}}) is spec


def test_bedrock_spec_route_gets_the_region(monkeypatch):
    calls = []
    monkeypatch.setattr(model_factory, "create_model_from_spec", calls.append)
    model_factory.create_model(ModelSpec("bedrock", "m"), region="eu-west-1")
    assert calls == [ModelSpec.bedrock("m", region_name="eu-west-1")]


def test_string_routes_remain_bedrock_ids(monkeypatch):
    constructor = Mock(return_value="bedrock")
    monkeypatch.setattr(model_factory, "BedrockModel", constructor)
    assert model_factory.create_model("openai:gpt-test", region="eu-west-1") == "bedrock"
    constructor.assert_called_once_with(model_id="openai:gpt-test", region_name="eu-west-1")
