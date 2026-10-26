"""Tool definitions sent to the model: only usable ones, shorter upstream descriptions (#368)."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import Mock

import pytest
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.subagent import create_nxp_subagent
from stiracore.core import NeuralState
from stiracore.tools import tool_registry
from stiracore.tools.compact_specs import COMPACT_DESCRIPTIONS, CompactSpecTool
from stiracore.tools.tool_config import ToolConfig
from tests.provider_fakes import FakeModel, text_turn, tool_turn


def _chain(tool) -> list[Any]:
    """The tool and every wrapper inside it (outermost first)."""
    chain = [tool]
    while hasattr(chain[-1], "wrapped"):
        chain.append(chain[-1].wrapped)
    return chain


def _upstream_spec(tool) -> dict:
    """The spec the upstream tool itself declares (inside the compaction, if any)."""
    compact = [t for t in _chain(tool) if isinstance(t, CompactSpecTool)]
    return compact[0].wrapped.tool_spec if compact else tool.tool_spec


# --- (2) shorter descriptions, same tool otherwise -----------------------------------


def test_only_the_listed_descriptions_change_and_schemas_are_identical():
    tools = tool_registry.get_tools(None, include_exec=True)
    sent = {t.tool_name: t.tool_spec for t in tools}
    compacted = set(COMPACT_DESCRIPTIONS) & set(sent)
    assert {"current_time", "shell", "think", "use_agent", "swarm", "graph", "cron"} <= compacted
    for tool in tools:
        name, spec, upstream = tool.tool_name, tool.tool_spec, _upstream_spec(tool)
        if name in compacted:
            assert spec["description"] == COMPACT_DESCRIPTIONS[name]
            assert len(spec["description"]) < len(upstream["description"])
            # The argument schema is the upstream one, byte for byte.
            assert json.dumps(spec["inputSchema"], sort_keys=True) == json.dumps(
                upstream["inputSchema"], sort_keys=True
            )
            assert {k: v for k, v in spec.items() if k != "description"} == {
                k: v for k, v in upstream.items() if k != "description"
            }
            assert tool.tool_type == _chain(tool)[-1].tool_type
        else:
            assert spec == upstream


def test_the_tool_order_is_the_registry_order():
    # Compaction replaces a registry entry in place: positions (the cached prefix's
    # order, #254) are the registry's, reads then writes.
    names = [t.tool_name for t in tool_registry.get_tools(None, include_exec=True)]
    registry = [*tool_registry.READ_TOOLS, *tool_registry.WRITE_TOOLS]
    assert names == [n for n in registry if n in names]
    standard = [n for _target, group in tool_registry._STANDARD for n in group]
    assert [n for n in registry if n in standard] == [n for n in standard if n in registry]


def test_the_compaction_sits_innermost_inside_every_gate():
    tools = {t.tool_name: t for t in tool_registry.get_tools(None, include_exec=True)}
    shell = tools.get("shell")
    if shell is None:
        pytest.skip("strands_tools.shell is not installed")
    chain = _chain(shell)
    kinds = [type(t).__name__ for t in chain]
    assert kinds[0] == "OsProtectionGuardTool"
    assert kinds.count("CompactSpecTool") == 1
    # The exec gate (#362) and the guard wrap the compact tool, never the other way.
    assert kinds.index("ExecGatedTool") < kinds.index("CompactSpecTool") == len(chain) - 2
    again = {t.tool_name: t for t in tool_registry.get_tools(None, include_exec=True)}
    assert again["shell"].tool_spec == shell.tool_spec


@pytest.mark.asyncio
async def test_the_guard_still_refuses_a_protected_command_through_the_compact_shell():
    shell = {t.tool_name: t for t in tool_registry.get_tools(None, include_exec=True)}.get("shell")
    if shell is None:
        pytest.skip("strands_tools.shell is not installed")
    events = [
        event
        async for event in shell.stream(
            {"toolUseId": "t1", "name": "shell", "input": {"command": "sudo true"}}, {}
        )
    ]
    result = events[-1].tool_result
    assert result["status"] == "error"


def test_a_compacted_tool_is_called_with_the_same_arguments_and_runs_unchanged():
    tools = [t for t in tool_registry.get_tools(None) if t.tool_name == "current_time"]
    if not tools:
        pytest.skip("strands_tools.current_time is not installed")
    assert isinstance(tools[0], CompactSpecTool)
    model = FakeModel([tool_turn("current_time", {"timezone": "UTC"}), text_turn("done")])
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=0.9),
        tools=tools,
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    agent("what time is it?")
    results = [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    assert results and results[0]["status"] == "success"
    assert "+00:00" in results[0]["content"][0]["text"]
    assert model.requests[0]["tool_names"] == ["current_time"]


# --- (1) only usable definitions are sent ---------------------------------------------


def test_stira_sends_no_never_and_no_hidden_exec_definition(tmp_path):
    """Measured shape of the default install: Never and hidden exec are not listed."""
    from dashboard import stira_server
    from stiracore.observability.settings_hub import filter_tools_by_config

    config = ToolConfig.first_run_defaults(tmp_path / "tool-config.json")
    tools = stira_server._stira_tools((), set())
    sent = {
        stira_server._registered_tool_name(t)
        for t in filter_tools_by_config(tools, config, stira_server._registered_tool_name)
    }
    for name in sent:
        assert config.policy_of(name) != "never", name
    assert not sent & tool_registry.EXEC_TOOL_NAMES
    assert not sent & {"http_request", "webhook_send", "oauth_api_call"}


class _Recording(AgentTool):
    def __init__(self, name: str, calls: list) -> None:
        super().__init__()
        self._name = name
        self.calls = calls

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> Any:
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        return {"name": self._name, "description": "records", "inputSchema": {"json": schema}}

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        self.calls.append(tool_use["name"])
        yield ToolResultEvent(
            {"toolUseId": tool_use["toolUseId"], "status": "success", "content": [{"text": "ok"}]}
        )


def _parent(rules) -> NXPAgent:
    return NXPAgent(
        model=FakeModel([text_turn("p")]),
        state=NeuralState(stability=0.9),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=rules,
    )


def test_a_child_is_sent_only_the_tools_it_can_call_without_an_approver():
    calls: list = []
    rules = {"t_always": ("always", 1), "t_ask": ("ask", 1), "t_never": ("never", 1)}
    tools = [_Recording(name, calls) for name in rules] + [_Recording("t_broken", calls)]

    def reader(name):
        if name == "t_broken":
            raise RuntimeError("unreadable rule")
        return rules[name]

    child = create_nxp_subagent(_parent(reader), "do it", tools=tools)
    assert [t.tool_name for t in child.tools] == ["t_always"]
    # The call-time gate is unchanged: a call to a tool it was not given never runs.
    child.model = FakeModel(
        [tool_turn("t_ask", {"path": "x"}), tool_turn("t_always", {"path": "x"}), text_turn("ok")]
    )
    child.agent.model = child.model
    child()
    assert calls == ["t_always"]
    assert child.model.requests[0]["tool_names"] == ["t_always"]


def test_a_child_without_rules_keeps_every_tool():
    calls: list = []
    tools = [_Recording(name, calls) for name in ("a", "b")]
    child = create_nxp_subagent(_parent(None), "do it", tools=tools)
    assert [t.tool_name for t in child.tools] == ["a", "b"]


class _Nameless:
    """No ``tool_name`` and no spec name: the rules cannot be asked about it."""

    @property
    def tool_spec(self) -> dict:
        return {"description": "no name"}


@pytest.mark.parametrize("rule", ["always", "never"])
def test_a_tool_whose_name_cannot_be_resolved_is_not_given_to_a_child(rule):
    calls: list = []
    nameless = _Nameless()
    tools = [nameless, _Recording("named", calls)]
    child = create_nxp_subagent(_parent(lambda _name: (rule, 1)), "do it", tools=tools)
    assert nameless not in child.tools
    assert [t.tool_name for t in child.tools] == (["named"] if rule == "always" else [])


def _run_child(child, script):
    child.model = FakeModel(script)
    child.agent.model = child.model
    child()
    return child.model


@pytest.mark.parametrize("later", ["ask", "never", "unreadable"])
def test_a_rule_tightened_after_the_child_was_built_refuses_its_kept_tool(later):
    calls: list = []
    rule = {"value": "always"}

    def reader(_name):
        if rule["value"] == "unreadable":
            raise RuntimeError("rules unreadable")
        return (rule["value"], 1)

    child = create_nxp_subagent(_parent(reader), "do it", tools=[_Recording("kept", calls)])
    assert [t.tool_name for t in child.tools] == ["kept"]
    rule["value"] = later
    _run_child(child, [tool_turn("kept", {"path": "x"}), text_turn("ok")])
    # Still listed (built once), but the call-time gate refuses it: no approver.
    assert calls == []


@pytest.mark.parametrize("relaxed", [False, True], ids=["rules-kept", "rules-relaxed-to-always"])
def test_breaker_transitions_never_bring_back_a_tool_excluded_at_build_time(relaxed):
    calls: list = []
    rules = {"kept": ("always", 1), "asked": ("ask", 1), "denied": ("never", 1)}
    tools = [_Recording(name, calls) for name in rules]
    child = create_nxp_subagent(
        _parent(lambda name: rules[name]),
        "do it",
        tools=tools,
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    if relaxed:
        # Loosened after the child was built: the list was decided at build time.
        rules.update(asked=("always", 2), denied=("always", 2))
    seen: list[tuple[str, list[str]]] = []
    # NORMAL -> WARNING -> CRITICAL -> recovered (past the cooldown); every turn the
    # model tries to call both excluded tools.
    for turn, stability in enumerate((0.9, 0.4, 0.1, 0.95, 0.95, 0.95, 0.95, 0.95)):
        child.state.stability = stability
        script = [
            tool_turn("asked", {"path": "x"}, f"asked-{turn}"),
            tool_turn("denied", {"path": "x"}, f"denied-{turn}"),
            text_turn("ok"),
        ]
        model = _run_child(child, script)
        level = child.engine.breaker_status().level.value
        seen += [(level, request["tool_names"]) for request in model.requests]
    levels = [level for level, _ in seen]
    assert {"normal", "warning", "critical"} <= set(levels)
    first = {level: levels.index(level) for level in ("warning", "critical")}
    assert levels[0] == "normal"
    assert 0 < first["warning"] < first["critical"] < len(levels) - 1
    # Recovered: NORMAL, or COHERENCE after the swings (no tool restriction either way).
    assert levels[-1] in ("normal", "coherence")
    for _level, names in seen:
        assert "asked" not in names and "denied" not in names
    assert seen[-1][1] == ["kept"]
    assert [t.tool_name for t in child.tools] == ["kept"]
    assert calls == []  # an excluded tool never ran, at any level, relaxed or not


# --- the real server's request (fake Bedrock endpoint, no cloud call) -------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX process semantics")
@pytest.mark.parametrize("condition", ["default", "exec"])
def test_the_real_request_lists_only_usable_tools_with_short_descriptions(condition):
    from scripts.tool_payload_probe import measure_request

    body = measure_request(condition)
    specs = {t["toolSpec"]["name"]: t["toolSpec"] for t in body["toolConfig"]["tools"]}
    if condition == "default":
        assert not set(specs) & tool_registry.EXEC_TOOL_NAMES
        assert not set(specs) & {"http_request", "webhook_send", "oauth_api_call"}
    else:
        assert set(tool_registry.EXEC_TOOL_NAMES) & set(tool_registry.WRITE_TOOLS) <= set(specs)
    for name in set(COMPACT_DESCRIPTIONS) & set(specs):
        assert specs[name]["description"] == COMPACT_DESCRIPTIONS[name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process semantics")
def test_the_probe_passes_no_caller_secret_or_setting_to_the_measured_server(monkeypatch):
    from scripts.tool_payload_probe import measure_request

    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-not-a-real-key")
    monkeypatch.setenv("STIRA_ENABLE_EXEC_TOOLS", "1")
    body = measure_request("default")
    names = {t["toolSpec"]["name"] for t in body["toolConfig"]["tools"]}
    # Neither the key (it would add perplexity_search) nor the STIRA_* setting got through.
    assert "perplexity_search" not in names
    assert not names & tool_registry.EXEC_TOOL_NAMES


def test_the_probe_shows_the_server_output_only_masked(tmp_path):
    from scripts.tool_payload_probe import _log_tail

    log = tmp_path / "server.log"
    log.write_text(
        "boot failed: Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123\n"
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY\n",
        "utf-8",
    )
    shown = _log_tail(log)
    assert "abcdefghijklmnopqrstuvwxyz0123" not in shown
    assert "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY" not in shown
    assert _log_tail(tmp_path / "missing.log") == "(no server output)"


def test_the_probe_cuts_after_masking_so_a_split_header_never_leaks(tmp_path):
    from scripts.tool_payload_probe import _log_tail

    value = "dXNlcjpwYXNz"  # a dummy credential (base64 of "user:pass")
    # The last 3,000 characters start inside the header: "...Basi" | "c <value>".
    tail = f"c {value}\n" + "y" * (3000 - len(f"c {value}\n"))
    log = tmp_path / "server.log"
    log.write_text("x" * 5000 + "\nAuthorization: Basi" + tail, "utf-8")
    shown = _log_tail(log)
    assert value not in shown
    assert len(shown) <= 3000


@pytest.mark.skipif(os.name == "nt", reason="POSIX process semantics")
def test_the_probe_never_writes_the_server_output_unmasked(tmp_path):
    import sys
    import time

    from scripts.tool_payload_probe import _start_masked, _stop

    secret = "dummy-SeCrEt-0123456789abcdef"
    code = (
        "import sys, time\n"
        f"print('AWS_SECRET_ACCESS_KEY={secret}', flush=True)\n"
        f"print('Authorization: Basic {secret}', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n"
    )
    log = tmp_path / "server.log"
    process, pump = _start_masked(
        [sys.executable, "-c", code], cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"}, log=log
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            text = log.read_text("utf-8") if log.exists() else ""
            if "AWS_SECRET_ACCESS_KEY" in text and "Authorization" in text:
                break
            time.sleep(0.05)
        # The child is still running and the file is still there: no plain value in it.
        assert process.poll() is None
        text = log.read_text("utf-8")
        assert "AWS_SECRET_ACCESS_KEY" in text and "Authorization" in text
        assert secret not in text
    finally:
        _stop(process, pump)
    assert secret not in log.read_text("utf-8")


# --- #397's exec gate and consent bridge with the compact descriptions ------------------


def _compact_shell():
    pytest.importorskip("strands_tools.shell")
    tools = {t.tool_name: t for t in tool_registry.get_tools(None, include_exec=True)}
    shell = tools["shell"]
    assert shell.tool_spec["description"] == COMPACT_DESCRIPTIONS["shell"]
    return shell


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
@pytest.mark.parametrize("approve", [True, False])
def test_the_compact_shell_runs_only_after_approval_through_gate_and_bridge(
    monkeypatch, tmp_path, approve
):
    from stiracore.core import HITLAction, HITLDecision, StiraConfig

    for name in ("BYPASS_TOOL_CONSENT", "STRANDS_NON_INTERACTIVE"):
        monkeypatch.setenv(name, "true")  # ignored by the bridge (#362)
    terminal = Mock(side_effect=AssertionError("terminal read"))
    monkeypatch.setattr("strands_tools.utils.user_input.get_user_input", terminal)
    monkeypatch.setattr("strands_tools.utils.user_input.get_user_input_async", terminal)
    shell = _compact_shell()
    args = {"command": "printf ran > ran.txt", "work_dir": str(tmp_path)}
    decision = HITLDecision(HITLAction.APPROVE if approve else HITLAction.REJECT)
    approver = Mock(return_value=decision)
    model = FakeModel([tool_turn("shell", args, "s1"), text_turn("done")])
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=0.8),
        config=StiraConfig(critical_action="halt"),
        on_hitl_request=approver,
        tools=[shell],
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=lambda _name: "ask",
    )
    agent("run")
    results = [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    assert approver.call_count == 1  # the HITL gate was asked for this exact call
    assert (tmp_path / "ran.txt").exists() is approve
    assert results[0]["status"] == ("success" if approve else "error")
    terminal.assert_not_called()  # the consent bridge answered, never the terminal
    assert model.requests[0]["tool_names"] == ["shell"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell")
def test_the_compact_shell_called_outside_the_gate_never_runs(tmp_path):
    import asyncio

    shell = _compact_shell()
    use = {
        "toolUseId": "direct",
        "name": "shell",
        "input": {"command": "printf ran > ran.txt", "work_dir": str(tmp_path)},
    }

    async def run():
        return [event async for event in shell.stream(use, {"agent": None})]

    result = asyncio.run(run())[-1].tool_result
    assert result["status"] == "error"
    assert not (tmp_path / "ran.txt").exists()
