"""Small-window context profiles and the usable-tools-only filter (#368)."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from strands import tool

from dashboard import stira_server
from stiracore.agent import context_profile
from stiracore.agent.context_assembler import ContextAssembler
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.config.models import GatewayConfig
from stiracore.core import NeuralState, StiraConfig
from stiracore.observability import settings_hub
from stiracore.tools.memory_store import MemoryLayer, MemoryStore
from tests.provider_fakes import FakeModel, text_turn, tool_turn


class SpecModel(FakeModel):
    """FakeModel that also keeps the full tool definitions of every request."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.specs: list[list[dict]] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.specs.append(json.loads(json.dumps(tool_specs or [])))
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


@tool
def read_note(name: str) -> str:
    """Read one note by name."""
    return f"note {name}"


@tool
def save_note(name: str, text: str) -> str:
    """Save one note."""
    return "saved"


@tool
def drop_note(name: str) -> str:
    """Delete one note."""
    return "deleted"


class Unnamed:
    """A tool object whose name cannot be resolved."""

    @property
    def tool_spec(self) -> dict:
        return {"description": "no name"}


def _agent(rules: dict, *, send_only: bool, script=None, tools=None) -> NXPAgent:
    def policy(name: str) -> str:
        if name not in rules:
            raise KeyError(name)  # an unreadable rule: Never at the gate
        return rules[name]

    return NXPAgent(
        model=SpecModel(script or [text_turn("hi")]),
        state=NeuralState(stability=0.9),
        config=StiraConfig(),
        tools=tools if tools is not None else (read_note, save_note, drop_note),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=policy,
        send_allowed_tools_only=send_only,
        callback_handler=None,
    )


# --- the profile table ---------------------------------------------------------------


def test_default_sets_nothing_and_small_tiny_set_the_table():
    assert context_profile.assembler_budget("default") == {}
    assert context_profile.assembler_budget("small") == {
        "max_tokens": 3000,
        "episodic_limit": 2,
        "procedural_limit": 3,
    }
    assert context_profile.assembler_budget("tiny") == {
        "max_tokens": 1800,
        "episodic_limit": 1,
        "procedural_limit": 2,
    }
    assert not context_profile.send_allowed_tools_only("default")
    assert context_profile.send_allowed_tools_only("default", True)
    assert context_profile.send_allowed_tools_only("small")
    assert context_profile.send_allowed_tools_only("tiny")


def test_an_explicit_value_wins_over_the_profile():
    budget = context_profile.assembler_budget("tiny", max_tokens=5000, episodic_limit=None)
    assert budget == {"max_tokens": 5000, "episodic_limit": 1, "procedural_limit": 2}
    assert context_profile.assembler_budget("default", procedural_limit=7) == {
        "procedural_limit": 7
    }
    with pytest.raises(TypeError):
        context_profile.assembler_budget("small", soul=1)


def test_the_reserved_columns_are_not_decided_here():
    for profile in context_profile.PROFILES.values():
        assert profile.memory_map is None and profile.handoff is None


def test_the_variable_wins_and_an_unknown_name_is_ignored(caplog):
    assert context_profile.effective_name("small", {}) == "small"
    assert context_profile.effective_name("small", {"STIRA_CONTEXT_PROFILE": "tiny"}) == "tiny"
    assert context_profile.effective_name("small", {"STIRA_CONTEXT_PROFILE": " "}) == "small"
    assert context_profile.effective_name("small", {"STIRA_CONTEXT_PROFILE": "huge"}) == "small"
    assert "huge" not in caplog.text  # the value is never logged
    assert context_profile.effective_name("bogus", {}) == "default"


# --- gateway config ------------------------------------------------------------------


def test_gateway_default_writes_neither_key(tmp_path):
    assert "context_profile" not in GatewayConfig().as_dict()
    assert "tools_send_allowed_only" not in GatewayConfig().as_dict()
    path = tmp_path / "gateway-config.json"
    GatewayConfig(context_profile="tiny", tools_send_allowed_only=True, path=path).save()
    data = json.loads(path.read_text("utf-8"))
    assert data["context_profile"] == "tiny" and data["tools_send_allowed_only"] is True
    loaded = GatewayConfig.load(path)
    assert loaded.context_profile == "tiny" and loaded.tools_send_allowed_only is True


@pytest.mark.parametrize(
    "fields",
    [{"context_profile": "huge"}, {"context_profile": 3}, {"tools_send_allowed_only": 1}],
)
def test_gateway_refuses_bad_values(fields):
    with pytest.raises(ValueError):
        GatewayConfig(**fields)


def test_hub_facts_name_the_variable_never_its_value():
    gateway = GatewayConfig(context_profile="small")
    facts = settings_hub.effective_context_profile(gateway, {})
    assert facts == {
        "name": "small",
        "saved": "small",
        "pinned_by": None,
        "max_tokens": 3000,
        "episodic_limit": 2,
        "procedural_limit": 3,
        "send_allowed_tools_only": True,
        "short_tool_schemas": True,
    }
    pinned = settings_hub.effective_context_profile(gateway, {"STIRA_CONTEXT_PROFILE": "tiny"})
    assert pinned["name"] == "tiny" and pinned["pinned_by"] == "STIRA_CONTEXT_PROFILE"
    plain = settings_hub.effective_context_profile(GatewayConfig(), {})
    assert plain["name"] == "default" and plain["send_allowed_tools_only"] is False
    assert plain["max_tokens"] is None
    switched = settings_hub.effective_context_profile(
        GatewayConfig(tools_send_allowed_only=True), {}
    )
    assert switched["name"] == "default" and switched["send_allowed_tools_only"] is True


# --- the assembled prompt ------------------------------------------------------------


def _wired(tmp_path, **kwargs):
    agent = _agent({}, send_only=False, tools=())
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    soul = tmp_path / "SOUL.md"
    soul.write_text("I am Resolver.", encoding="utf-8")
    store = MemoryStore(tmp_path / "memory", "stira")
    for n in range(6):
        store.write(MemoryLayer.EPISODIC, f"ep-{n}", f"episode {n} " + "x" * 400)
    return stira_server._wire_system_prompt(agent, store, soul, **kwargs), agent, store, soul


def test_default_profile_builds_the_assembler_as_before(tmp_path):
    assembler, agent, store, soul = _wired(tmp_path)
    assert (assembler.max_tokens, assembler.episodic_limit, assembler.procedural_limit) == (
        4000,
        5,
        5,
    )
    before = ContextAssembler(
        agent_id="stira",
        memory_store=store,
        engine=agent.engine,
        soul_path=soul,
        max_tokens=4000,
        breaker_state_provider=agent.engine.breaker_status,
        include_identity=False,
    )
    # Byte-identical apart from the clock line (none in the assembled prompt).
    assert agent.system_prompt_provider() == before.build_system_prompt()
    explicit, _, _, _ = _wired(tmp_path, profile="default", max_tokens=4000)
    assert explicit.max_tokens == 4000


def test_small_and_tiny_shrink_the_prompt(tmp_path):
    sizes = {}
    for name in ("default", "small", "tiny"):
        assembler, agent, _store, _soul = _wired(tmp_path / name, profile=name)
        sizes[name] = len(agent.system_prompt_provider())
        assert assembler.max_tokens == {"default": 4000, "small": 3000, "tiny": 1800}[name]
    assert sizes["default"] > sizes["small"] > sizes["tiny"]
    explicit, _, _, _ = _wired(tmp_path / "x", profile="tiny", max_tokens=5000)
    assert explicit.max_tokens == 5000 and explicit.episodic_limit == 1


# --- the tool definitions sent -------------------------------------------------------


RULES = {"read_note": "always", "save_note": "ask", "drop_note": "never"}


def test_off_sends_every_tool_exactly_as_before():
    on_rules = _agent(RULES, send_only=False)
    plain = NXPAgent(
        model=SpecModel([text_turn("hi")]),
        state=NeuralState(stability=0.9),
        config=StiraConfig(),
        tools=(read_note, save_note, drop_note),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        callback_handler=None,
    )
    on_rules("hello")
    plain("hello")
    assert on_rules.model.specs == plain.model.specs
    assert [spec["name"] for spec in on_rules.model.specs[0]] == [
        "read_note",
        "save_note",
        "drop_note",
    ]


def test_on_sends_only_always_and_ask_with_the_same_schemas():
    reference = _agent(RULES, send_only=False)
    reference("hello")
    agent = _agent(RULES, send_only=True)
    agent("hello")
    sent = {spec["name"]: spec for spec in agent.model.specs[0]}
    assert list(sent) == ["read_note", "save_note"]
    full = {spec["name"]: spec for spec in reference.model.specs[0]}
    for name, spec in sent.items():
        assert spec == full[name]  # name, description and argument schema unchanged


def test_unreadable_rules_and_unresolvable_names_are_not_sent():
    agent = _agent({"read_note": "always"}, send_only=True, tools=(read_note, save_note, Unnamed()))
    assert [getattr(t, "tool_name", None) for t in agent._allowed_tools(list(agent.tools))] == [
        "read_note"
    ]


def test_a_saved_rule_applies_from_the_next_turn():
    rules = dict(RULES)
    agent = _agent(rules, send_only=True, script=[text_turn("a"), text_turn("b")])
    agent("one")
    rules["drop_note"] = "ask"
    rules["read_note"] = "never"
    agent("two")
    names = [[spec["name"] for spec in specs] for specs in agent.model.specs]
    assert names == [["read_note", "save_note"], ["save_note", "drop_note"]]


def test_without_a_rule_reader_every_named_tool_stays():
    agent = NXPAgent(
        model=SpecModel([text_turn("hi")]),
        state=NeuralState(stability=0.9),
        config=StiraConfig(),
        tools=(read_note, Unnamed()),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        send_allowed_tools_only=True,
        callback_handler=None,
    )
    assert [t.tool_name for t in agent._allowed_tools(list(agent.tools))] == ["read_note"]


def test_the_gate_is_unchanged_a_never_call_is_still_refused():
    # A model that calls the Never tool anyway (it was not listed) never runs it.
    calls = []

    @tool
    def drop_note(name: str) -> str:  # the Never tool, observed
        """Delete one note."""
        calls.append(name)
        return "deleted"

    for send_only in (False, True):
        agent = _agent(
            RULES,
            send_only=send_only,
            tools=(read_note, save_note, drop_note),
            script=[tool_turn("drop_note", {"name": "a"}), text_turn("done")],
        )
        agent("delete a")
        assert calls == []
