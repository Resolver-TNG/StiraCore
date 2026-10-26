"""#508: the agent is told which tools are disabled, why, and how its calls are permitted.

Acceptance, one test (group) each:
1. every Never registry tool is in the section with a reason code (= ``/api/config/tools``);
2. a save Never -> Ask removes the tool from the section and puts it in the tool list;
3. the section is the same bytes across turns; a save or a YOLO window start/end changes it;
4. a Never call is refused as before (``rule_never``) and the refusal says who can enable it;
5. no tool schema reaches the prompt (and the added size is measured);
6. offline model: asked "can you check a page in a browser?", it answers from the section
   that ``page_check`` is disabled, not that it does not exist;
7. the #145 contract tests are unchanged (they run in the normal suite).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("flask")

from strands.models import Model

from stiracore.agent.checkpoint_hooks import NEVER_REFUSAL
from stiracore.config import GatewayConfig
from stiracore.observability.config_api import ConfigStore
from stiracore.tools import tool_availability as ta
from stiracore.tools import tool_policy, tool_registry
from stiracore.yolo import runtime as yolo_runtime

SECTION = "## " + ta.TITLE
_LINE = re.compile(r"^- ([a-z_]+): .* — ([a-z_, ]+?)(?: \[.*\])?$")


def _section_of(prompt: str) -> str:
    start = prompt.index(SECTION)
    rest = prompt[start:]
    following = rest.find("\n## ", len(SECTION))
    return rest if following < 0 else rest[:following]


def _listed(section: str) -> dict[str, tuple[str, ...]]:
    found = {}
    for line in section.splitlines():
        match = _LINE.match(line)
        if match:
            found[match.group(1)] = tuple(code.strip() for code in match.group(2).split(","))
    return found


def _row(name, policy="always", *, exposed=True, reason=None):
    return {
        "name": name,
        "policy": policy,
        "exposed": exposed,
        "stack": {"rule": {"reason": reason or f"rule_{policy}"}},
    }


# --- the pure builder ---------------------------------------------------------------------


def test_section_lists_never_tools_with_codes_and_one_legend():
    rows = [
        _row("file_read"),
        _row("page_check", "never"),
        _row("shell", "ask", exposed=False),
        _row("cron", "never", exposed=False),
        _row("run_checks", "always", reason="rule_always_capped"),
    ]
    text = ta.section(rows, absent={"memory_rate": ta.NOT_CONFIGURED}, yolo_allowed=True)
    listed = _listed(text)
    assert listed == {
        "cron": (ta.POLICY_NEVER, ta.EXEC_NOT_ENABLED),
        "memory_rate": (ta.NOT_CONFIGURED,),
        "page_check": (ta.POLICY_NEVER,),
        "shell": (ta.EXEC_NOT_ENABLED,),
    }
    assert "file_read" not in text and "run_checks" not in text
    assert "You cannot use them. You can ask the user to enable one" in text
    legend = next(line for line in text.splitlines() if line.startswith("Who can enable"))
    assert "policy_never = Settings → Tools" in legend
    assert ta.DEPENDENCY_MISSING not in legend  # only the codes in use
    # A capped Always is Ask for the gate, and the counts say so.
    assert "Always 1 / Ask 2 / Never 2." in text
    assert "YOLO: no window is open." in text
    assert "YOLO only approves Ask calls automatically" in text


def test_locked_rules_say_so():
    text = ta.section([_row("file_read", "always", reason="rule_locked")])
    assert _listed(text) == {"file_read": (ta.RULES_LOCKED,)}
    assert "Never 1." in text
    # ADR 0012: without the allowance (and no window) the prompt never mentions YOLO.
    assert "yolo" not in text.lower()
    allowed = ta.section([_row("file_read")], yolo_allowed=True)
    assert allowed.splitlines()[-2:] == ["YOLO: no window is open.", ta.YOLO_SCOPE]


def test_section_is_deterministic_and_has_no_clock_or_count():
    rows = [_row("b", "never"), _row("a", "never"), _row("c")]
    window = {"id": "abcd1234", "tier": "internal", "exec": False, "ends_at": 1_800_000_000}
    first = ta.section(rows, yolo_allowed=True, yolo_window=window)
    assert first == ta.section(list(reversed(rows)), yolo_allowed=True, yolo_window=window)
    assert list(_listed(first)) == ["a", "b"]
    assert "a window is open (internal only) until " in first
    assert "remaining" not in first and "used" not in first


def test_purpose_is_one_line_from_the_description_never_the_schema():
    tool = SimpleNamespace(
        tool_spec={
            "description": "Fetch things from far away. Arguments: url (str), depth (int).",
            "inputSchema": {"json": {"type": "object", "properties": {"url": {}}}},
        }
    )
    assert ta.purpose_of("far_fetch", tool) == "Fetch things from far away"
    assert ta.purpose_of("page_check", tool) == ta.PURPOSE["page_check"]
    long = SimpleNamespace(tool_spec={"description": "word " * 60})
    assert len(ta.purpose_of("x", long)) <= 81


def test_yolo_window_facts_ignore_counts_and_pauses(monkeypatch):
    view = {
        "id": "abcd1234",
        "state": "active",
        "tier": "external",
        "exec": True,
        "ends_at": 1_800_000_000,
        "remaining_sec": 100,
        "used": 3,
    }
    desk = SimpleNamespace(yolo=SimpleNamespace(status=lambda: {"window": dict(view)}))
    monkeypatch.setattr(yolo_runtime, "_DESK", desk)
    facts = yolo_runtime.window_facts()
    assert facts == {"id": "abcd1234", "tier": "external", "exec": True, "ends_at": 1_800_000_000}
    view.update(state="paused", remaining_sec=50, used=4)
    assert yolo_runtime.window_facts() == facts  # a pause or a count changes nothing
    view.update(state="ended")
    assert yolo_runtime.window_facts() is None
    monkeypatch.setattr(yolo_runtime, "_DESK", None)
    assert yolo_runtime.window_facts() is None


# --- 4. the refusal -----------------------------------------------------------------------


def test_never_refusal_keeps_rule_never_and_adds_the_way_to_enable():
    from stiracore.agent.checkpoint_hooks import CheckpointHooks
    from stiracore.core import StiraEngine

    hooks = CheckpointHooks(StiraEngine(), policy_check=lambda name: "never")
    event = SimpleNamespace(
        tool_use={"name": "page_check", "toolUseId": "t1", "input": {}}, cancel_tool=False
    )
    hooks.before_tool(event)
    assert event.cancel_tool == NEVER_REFUSAL
    assert NEVER_REFUSAL.startswith("Tool policy is Never for this tool; not executed.")
    assert "rule_never" in NEVER_REFUSAL and "Settings → Tools" in NEVER_REFUSAL
    # The settings page's reason for that rule is unchanged.
    stack = tool_policy.tool_stack(
        SimpleNamespace(mode="custom", enabled_tools=set(), ask_tools=set()),
        "page_check",
        "normal",
        exposed=True,
    )
    assert stack["stack"]["rule"]["reason"] == "rule_never"


# --- the chat server, with an offline model -----------------------------------------------


class ReadingModel(Model):
    """Answers from what it is sent: the system prompt and the tool list (no network).

    "browser": the first time, try ``page_check`` if it is offered; else answer from the
    Tool Availability section — disabled (with its codes), or not there at all.
    """

    def __init__(self):
        self.requests: list[dict] = []
        self.config = {"model_id": "reading-offline"}

    def get_config(self):
        return dict(self.config)

    def update_config(self, **kwargs):
        self.config.update(kwargs)

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError
        yield  # pragma: no cover

    def _answer(self, question: str, system: str, tools: list[str]) -> str:
        if "browser" not in question:
            return "ok"
        if "page_check" in tools:
            return "Yes: page_check is in my tool list."
        listed = _listed(_section_of(system)) if SECTION in system else {}
        if "page_check" in listed:
            return (
                f"page_check exists but is disabled ({', '.join(listed['page_check'])}). "
                "Please enable it in Settings → Tools."
            )
        return "I have no browser tool."

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        tools = [spec["name"] for spec in tool_specs or []]
        self.requests.append(
            {
                "system_prompt": system_prompt or "",
                "tool_names": tools,
                "tool_specs_json": json.dumps(tool_specs or [], default=str),
            }
        )
        last = messages[-1] if messages else {}
        texts = [block["text"] for block in last.get("content", []) if "text" in block]
        results = [b["toolResult"] for b in last.get("content", []) if "toolResult" in b]
        question = " ".join(texts)
        yield {"messageStart": {"role": "assistant"}}
        if "call page_check" in question and not results:
            use = {"name": "page_check", "toolUseId": "pc-1"}
            yield {"contentBlockStart": {"start": {"toolUse": use}}}
            arguments = json.dumps({"target": "index.html"})
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": arguments}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        if results:
            refusal = " ".join(c.get("text", "") for r in results for c in r.get("content", []))
            answer = f"refused: {refusal}"
        else:
            answer = self._answer(question, system_prompt or "", tools)
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": answer}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        usage = {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}
        yield {"metadata": {"usage": usage, "metrics": {"latencyMs": 0}}}


@pytest.fixture
def chat(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.core import NeuralState
    from stiracore.tools import page_check as page_check_tool

    data_dir = tmp_path / "stira-data"
    data_dir.mkdir()
    NeuralState(stability=0.9).save(data_dir / "neural-state.json")
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "SOUL_PATH", tmp_path / "SOUL.md")
    monkeypatch.setattr(stira_server, "GATEWAY_CONFIG", GatewayConfig(path=tmp_path / "gw.json"))
    monkeypatch.setattr(stira_server, "MODEL_ID", GatewayConfig().model_id)
    monkeypatch.setattr(stira_server, "REGION", os.environ.get("AWS_REGION", "ap-northeast-1"))
    # Hosts without a browser (CI cells) still register page_check here: these tests are
    # about its rule. It is never run (Never refuses before the tool).
    if "page_check" not in tool_registry.WRITE_TOOLS:
        monkeypatch.setitem(tool_registry.WRITE_TOOLS, "page_check", page_check_tool.page_check)
    model = ReadingModel()
    monkeypatch.setattr(nxp_agent, "BedrockModel", lambda **kw: model)
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(model_id=None, classify=Mock(return_value=None)),
    )
    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    client = stira_server.create_app(config_store=store).test_client()
    return SimpleNamespace(client=client, model=model, store=store)


def _say(chat, text: str) -> str:
    response = chat.client.post("/api/chat", json={"message": text})
    assert response.status_code == 200, response.text
    return response.text


def _rule(chat, name: str, policy: str):
    body = {"name": name, "policy": policy, **({"confirm": True} if policy != "never" else {})}
    saved = chat.client.post("/api/config/tools", json=body)
    assert saved.status_code == 200, saved.get_json()


def test_never_tools_match_the_settings_api_and_a_save_to_ask_moves_the_tool(chat):
    _rule(chat, "page_check", "never")
    _rule(chat, "propose_procedure", "never")
    _say(chat, "hello")
    first = chat.model.requests[-1]
    section = _section_of(first["system_prompt"])
    listed = _listed(section)
    rows = chat.client.get("/api/config/tools").get_json()["tools"]
    never = {row["name"] for row in rows if row["policy"] == "never"}
    assert {"page_check", "propose_procedure"} <= never
    # 1: every Never registry tool, with its code; and nothing Never is missing.
    assert {name for name, codes in listed.items() if ta.POLICY_NEVER in codes} == never
    hidden = {row["name"] for row in rows if row["exposed"] is False}
    assert {n for n, codes in listed.items() if ta.EXEC_NOT_ENABLED in codes} == hidden
    assert not (set(listed) & set(first["tool_names"]))  # disabled = not offered
    assert f"Never {len(never)}." in section

    # 2: Never -> Ask: gone from the section, in the tool list from the next turn.
    _rule(chat, "propose_procedure", "ask")
    _say(chat, "hello again")
    after = chat.model.requests[-1]
    assert "propose_procedure" not in _listed(_section_of(after["system_prompt"]))
    assert "propose_procedure" in after["tool_names"]
    assert "page_check" in _listed(_section_of(after["system_prompt"]))


def test_section_bytes_are_stable_across_turns_and_change_only_on_save_or_window(chat, monkeypatch):
    _rule(chat, "page_check", "never")
    _say(chat, "one")
    _say(chat, "two")
    one, two = (_section_of(r["system_prompt"]) for r in chat.model.requests[-2:])
    assert one == two
    assert one.encode("utf-8") == two.encode("utf-8")

    window = {"id": "abcd1234", "tier": "internal", "exec": False, "ends_at": 1_800_000_000}
    monkeypatch.setattr(yolo_runtime, "window_facts", lambda: dict(window))
    _say(chat, "three")
    opened = _section_of(chat.model.requests[-1]["system_prompt"])
    assert opened != one and "a window is open (internal only)" in opened
    _say(chat, "four")
    assert _section_of(chat.model.requests[-1]["system_prompt"]) == opened
    monkeypatch.setattr(yolo_runtime, "window_facts", lambda: None)
    _say(chat, "five")
    assert _section_of(chat.model.requests[-1]["system_prompt"]) == one  # ended: as before

    _rule(chat, "page_check", "ask")
    _say(chat, "six")
    assert _section_of(chat.model.requests[-1]["system_prompt"]) != one


def test_split_prompt_puts_the_section_in_the_static_part(tmp_path, monkeypatch):
    """With prompt caching (#254) the section is the end of the cached static part: the
    same bytes every turn, so the cache prefix holds."""
    from dashboard import stira_server
    from stiracore import MemoryStore, NeuralState, NXPAgent, StiraConfig

    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    applied = {"config": None}
    provider = stira_server._tool_availability_provider(store, applied, memory_rate_bound=False)
    agent = NXPAgent(
        model=ReadingModel(),
        state=NeuralState(stability=0.9),
        config=StiraConfig(),
        tools=(),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    (tmp_path / "memory").mkdir()
    soul = tmp_path / "SOUL.md"
    soul.write_text("I am Resolver.", encoding="utf-8")
    memory = MemoryStore(tmp_path / "memory", "stira")
    stira_server._wire_system_prompt(agent, memory, soul, tools_section=provider)
    first = agent.prompt_parts_provider()
    agent.engine.state.stability = 0.5  # a turn moves the state
    second = agent.prompt_parts_provider()
    assert first.static == second.static and first.volatile != second.volatile
    assert first.static.startswith("## SOUL") and first.static.endswith(_section_of(first.static))
    assert SECTION not in first.volatile and SECTION not in first.recalled
    # The legacy prompt carries the same text, at its end.
    assert agent.system_prompt_provider().endswith(_section_of(first.static))


def test_no_schema_in_the_prompt_and_the_size_is_small(chat):
    _rule(chat, "page_check", "never")
    _say(chat, "hello")
    request = chat.model.requests[-1]
    section = _section_of(request["system_prompt"])
    for marker in ('"type"', "inputSchema", '"properties"', '"required"'):
        assert marker not in section
    specs = json.loads(request["tool_specs_json"])
    spec_sizes = {spec["name"]: len(json.dumps(spec)) for spec in specs}
    # Measured on a fresh install (default rules): the section stays a small fraction of
    # one turn's tool definitions, which it never repeats.
    assert len(section) < 2600
    assert len(section) < sum(spec_sizes.values()) / 4


def test_offline_model_answers_from_the_section_that_page_check_is_disabled(chat):
    _rule(chat, "page_check", "never")
    answer = _say(chat, "Can you check a page in a real browser?")
    assert "page_check exists but is disabled (policy_never)" in answer
    assert "no browser tool" not in answer
    # Without the section the same model would say it has none (the #508 report).
    assert "page_check" not in chat.model.requests[-1]["tool_names"]

    # Calling it anyway is refused as before, now with who can enable it.
    refused = _say(chat, "call page_check please")
    assert "Tool policy is Never for this tool; not executed." in refused
    assert "rule_never" in refused and "Settings → Tools" in refused

    _rule(chat, "page_check", "ask")
    assert "in my tool list" in _say(chat, "And now, a browser check?")


def test_server_reason_codes_for_the_host_bound_and_optional_tools(tmp_path, monkeypatch):
    from dashboard import stira_server

    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    monkeypatch.delitem(tool_registry.WRITE_TOOLS, "page_check", raising=False)
    monkeypatch.delitem(tool_registry.WRITE_TOOLS, "run_checks", raising=False)
    monkeypatch.setattr(
        tool_registry, "PAGE_CHECK_STATUS", SimpleNamespace(code="page_check_no_browser")
    )
    monkeypatch.setitem(tool_registry.RUN_CHECKS_STATE, "code", "run_checks_off")
    text = stira_server._tool_availability_provider(
        store, {"config": None}, memory_rate_bound=False
    )()
    listed = _listed(text)
    assert listed["page_check"] == (ta.DEPENDENCY_MISSING,)
    assert listed["run_checks"] == (ta.NOT_CONFIGURED,)
    assert listed["memory_rate"] == (ta.NOT_CONFIGURED,)
    assert 'gateway-config.json "run_checks": {"phase": 1}' in text
    bound = stira_server._tool_availability_provider(
        store, {"config": None}, memory_rate_bound=True
    )()
    assert "memory_rate" not in _listed(bound)
    monkeypatch.setattr(tool_registry, "PAGE_CHECK_STATUS", SimpleNamespace(code="page_check_root"))
    monkeypatch.setitem(tool_registry.RUN_CHECKS_STATE, "code", "run_checks_root")
    text = stira_server._tool_availability_provider(
        store, {"config": None}, memory_rate_bound=True
    )()
    assert _listed(text)["page_check"] == (ta.UNSUPPORTED_HOST,)
    assert _listed(text)["run_checks"] == (ta.UNSUPPORTED_HOST,)


def test_a_broken_build_leaves_the_prompt_without_the_section(tmp_path, monkeypatch):
    from dashboard import stira_server

    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    monkeypatch.setattr(stira_server, "config_tool_rows", Mock(side_effect=RuntimeError))
    build = stira_server._tool_availability_provider(
        store, {"config": None}, memory_rate_bound=False
    )
    assert build() == ""
    assert ta.with_section("prompt", "") == "prompt"


def test_unreadable_rules_are_reported_locked(tmp_path):
    from dashboard import stira_server

    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    Path(store.tool_path).write_text("{not json", encoding="utf-8")
    text = stira_server._tool_availability_provider(
        store, {"config": None}, memory_rate_bound=True
    )()
    listed = _listed(text)
    registered = set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)
    assert registered and registered <= set(listed)
    assert all(ta.RULES_LOCKED in listed[name] for name in registered)
    assert "Never 0." not in text
