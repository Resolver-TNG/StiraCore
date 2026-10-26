"""Shorter property descriptions in long upstream tool schemas, opt-in (#368 proposal 2)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from strands.types.tools import AgentTool

from stiracore.agent import context_profile
from stiracore.config.models import GatewayConfig
from stiracore.observability import settings_hub
from stiracore.tools import compact_specs, tool_registry
from stiracore.tools.compact_specs import (
    SHORT_PROPERTY_DESCRIPTIONS,
    CompactSpecTool,
    set_short_schemas,
    shorten_schema,
)


@pytest.fixture(autouse=True)
def _switch_off_after():
    set_short_schemas(False)
    yield
    set_short_schemas(False)


def _chain(tool) -> list[Any]:
    chain = [tool]
    while hasattr(chain[-1], "wrapped"):
        chain.append(chain[-1].wrapped)
    return chain


def _upstream(tool) -> AgentTool:
    compact = [t for t in _chain(tool) if isinstance(t, CompactSpecTool)]
    assert len(compact) == 1
    return compact[0].wrapped


def _strip(value: Any) -> Any:
    """The schema without any ``description`` string: names, types, enums, defaults,
    required and nesting."""
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != "description"}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _description_paths(value: Any, path: tuple = ()) -> set[tuple]:
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "description" and isinstance(item, str):
                found.add(path)
            else:
                found |= _description_paths(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found |= _description_paths(item, (*path, index))
    return found


def _listed() -> dict[str, Any]:
    tools = {t.tool_name: t for t in tool_registry.get_tools(None, include_exec=True)}
    listed = {name: tools[name] for name in SHORT_PROPERTY_DESCRIPTIONS if name in tools}
    if not listed:
        pytest.skip("no listed upstream tool is installed")
    return listed


def test_off_by_default_the_schema_is_the_upstream_object_itself():
    assert compact_specs.short_schemas_enabled() is False
    for tool in _listed().values():
        assert tool.tool_spec["inputSchema"] is _upstream(tool).tool_spec["inputSchema"]
        # Only the listed top-level descriptions differ (#398), nothing else.
        upstream = _upstream(tool).tool_spec
        assert {k: v for k, v in tool.tool_spec.items() if k != "description"} == {
            k: v for k, v in upstream.items() if k != "description"
        }


def test_on_only_property_descriptions_change_and_the_argument_structure_is_identical():
    listed = _listed()
    before = {name: json.dumps(t.tool_spec, sort_keys=True) for name, t in listed.items()}
    set_short_schemas(True)
    for name, tool in listed.items():
        spec, upstream = tool.tool_spec, _upstream(tool).tool_spec
        schema, original = spec["inputSchema"], upstream["inputSchema"]
        # Names, types, enums, defaults, required, nesting: byte for byte.
        assert json.dumps(_strip(schema), sort_keys=True) == json.dumps(
            _strip(original), sort_keys=True
        )
        # No description added or removed anywhere; only the listed ones changed.
        assert _description_paths(schema) == _description_paths(original)
        properties = schema["json"]["properties"]
        upstream_properties = original["json"]["properties"]
        table = SHORT_PROPERTY_DESCRIPTIONS[name]
        # Every listed property exists upstream (the table does not rot silently).
        assert set(table) <= set(upstream_properties), name
        for prop, text in table.items():
            if "description" in upstream_properties[prop]:
                assert properties[prop]["description"] == text
                assert len(text) <= len(upstream_properties[prop]["description"]), (name, prop)
        for prop in set(upstream_properties) - set(table):
            assert properties[prop] == upstream_properties[prop]
        assert len(json.dumps(schema)) < len(json.dumps(original)), name
        # The rest of the spec (name, top-level description) is unchanged by the switch.
        assert {k: v for k, v in spec.items() if k != "inputSchema"} == {
            k: v for k, v in json.loads(before[name]).items() if k != "inputSchema"
        }
        # The upstream object is never modified.
        assert original is _upstream(tool).tool_spec["inputSchema"]
    set_short_schemas(False)
    assert {n: json.dumps(t.tool_spec, sort_keys=True) for n, t in listed.items()} == before


def test_the_required_cautions_are_kept():
    table = SHORT_PROPERTY_DESCRIPTIONS
    assert "HTTP_REQUEST_TOKEN_CONFIG" in table["http_request"]["auth_env_var"]
    assert "may be restricted" in table["http_request"]["verify_ssl"]
    assert "1-10" in table["think"]["cycle_count"]
    swarm = table["swarm"]
    for prop, default in (
        ("max_handoffs", "20"),
        ("max_iterations", "20"),
        ("execution_timeout", "900"),
        ("node_timeout", "300"),
        ("repetitive_handoff_detection_window", "8"),
        ("repetitive_handoff_min_unique_agents", "3"),
    ):
        assert default in swarm[prop]
    for key in ('"nodes"', '"edges"', '"entry_points"', '"system_prompt"'):
        assert key in table["graph"]["topology"]
    assert "SHELL_DEFAULT_TIMEOUT" in table["shell"]["timeout"]


def test_shorten_schema_never_adds_a_description_or_touches_the_input():
    schema = {"json": {"type": "object", "properties": {"a": {"type": "string"}, "b": {}}}}
    out = shorten_schema(schema, {"a": "x", "c": "y"})
    assert out == schema and out is not schema
    with_text = {"json": {"properties": {"a": {"type": "string", "description": "long text"}}}}
    out = shorten_schema(with_text, {"a": "short"})
    assert out["json"]["properties"]["a"]["description"] == "short"
    assert with_text["json"]["properties"]["a"]["description"] == "long text"


class _Recorder(AgentTool):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []

    @property
    def tool_name(self) -> str:
        return "think"

    @property
    def tool_spec(self) -> Any:
        return {
            "name": "think",
            "description": "upstream",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"thought": {"type": "string", "description": "a long one"}},
                    "required": ["thought"],
                }
            },
        }

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use, invocation_state, **kwargs):
        self.calls.append(tool_use)
        yield {"toolUseId": tool_use["toolUseId"], "status": "success", "content": []}


def test_a_call_is_passed_through_unchanged_with_the_switch_on():
    recorder = _Recorder()
    tool = CompactSpecTool(recorder, None, {"thought": "short"})
    set_short_schemas(True)
    assert tool.tool_spec["description"] == "upstream"
    assert tool.tool_spec["inputSchema"]["json"]["properties"]["thought"]["description"] == "short"
    use = {"toolUseId": "t1", "name": "think", "input": {"thought": "hi"}}

    async def run():
        return [event async for event in tool.stream(use, {})]

    events = asyncio.run(run())
    assert recorder.calls == [use] and events[-1]["status"] == "success"


def test_the_compaction_still_sits_innermost_inside_every_gate():
    for name, tool in _listed().items():
        kinds = [type(t).__name__ for t in _chain(tool)]
        assert kinds.count("CompactSpecTool") == 1, name
        assert kinds.index("CompactSpecTool") == len(kinds) - 2, name


# --- the switch ---------------------------------------------------------------------


def test_gateway_key_is_off_by_default_written_only_when_on_and_type_checked(tmp_path):
    assert GatewayConfig().tools_short_schemas is False
    assert "tools_short_schemas" not in GatewayConfig().as_dict()
    path = tmp_path / "gateway-config.json"
    GatewayConfig(tools_short_schemas=True, path=path).save()
    assert json.loads(path.read_text("utf-8"))["tools_short_schemas"] is True
    assert GatewayConfig.load(path).tools_short_schemas is True
    with pytest.raises(ValueError):
        GatewayConfig(tools_short_schemas=1)


def test_small_and_tiny_turn_it_on_default_only_with_the_key():
    assert not context_profile.short_tool_schemas("default")
    assert context_profile.short_tool_schemas("default", True)
    assert context_profile.short_tool_schemas("small")
    assert context_profile.short_tool_schemas("tiny")
    facts = settings_hub.effective_context_profile
    assert facts(GatewayConfig(), {})["short_tool_schemas"] is False
    assert facts(GatewayConfig(tools_short_schemas=True), {})["short_tool_schemas"] is True
    assert facts(GatewayConfig(context_profile="tiny"), {})["short_tool_schemas"] is True


# --- the real server's request (fake Bedrock endpoint, no cloud call) -------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX process semantics")
def test_the_real_request_uses_the_short_schemas_only_with_the_key():
    from scripts.tool_payload_probe import measure_request

    def specs(gateway):
        body = measure_request("exec", gateway=gateway)
        return {t["toolSpec"]["name"]: t["toolSpec"] for t in body["toolConfig"]["tools"]}

    plain, short = specs(None), specs({"tools_short_schemas": True})
    assert set(plain) == set(short)
    names = set(SHORT_PROPERTY_DESCRIPTIONS) & set(plain)
    if not names:
        pytest.skip("no listed upstream tool is installed")
    for name in set(plain):
        if name not in names:
            assert short[name] == plain[name]
            continue
        assert _strip(short[name]) == _strip(plain[name])
        properties = short[name]["inputSchema"]["json"]["properties"]
        for prop, text in SHORT_PROPERTY_DESCRIPTIONS[name].items():
            if "description" in plain[name]["inputSchema"]["json"]["properties"][prop]:
                assert properties[prop]["description"] == text
    assert len(json.dumps(short)) < len(json.dumps(plain))
