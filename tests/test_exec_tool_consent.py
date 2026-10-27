"""#362: exec-class tools run only inside a human approval of the exact call.

Before #362 the consent bridge covered ``file_write`` only: an approved ``shell``/``cron``
call reached the upstream terminal prompt and failed (``Command execution cancelled by
user. Input: ``), and the real ``BYPASS_TOOL_CONSENT`` / ``STRANDS_NON_INTERACTIVE`` let
any caller skip it. Here every exec tool is behind ``ExecGatedTool`` (gate mark, bridge
re-check, grant per step) and the gate mark needs a verified HITL binding. The headless
process E2E is ``tests/test_headless_exec_tools_e2e.py``.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import Mock

import pytest
from strands import Agent
from strands.types._events import ToolResultEvent
from test_subagent import ScriptedModel

from stiracore.agent.checkpoint_hooks import ApprovalBindingVerifier
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig
from stiracore.core.approval import HITL_BINDINGS_KEY, ApprovalBinding, args_hash
from stiracore.tools import exec_gate, tool_consent, tool_registry
from stiracore.tools.exec_gate import ExecGatedTool
from stiracore.tools.os_protection import OsProtectionGuardTool
from stiracore.tools.tool_consent import GATE_PASSED_KEY, GateMark, mark_gate_passed

pytest.importorskip("strands_tools.shell")
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="upstream shell uses a PTY")
EXEC_TOOLS = sorted(tool_consent.EXEC_TOOLS)
NO_GATE = "no Stira gate approval"
NO_BRIDGE = "cannot route the upstream terminal confirmation"
OUTSIDE_GATE = "requested terminal confirmation outside Stira's approval gate"
OUTSIDE_STEP = "called outside an approved step of Stira's gate"
NO_HUMAN = "run only after a human approved this exact call"
CONSENT_ENV = ("BYPASS_TOOL_CONSENT", "STRANDS_NON_INTERACTIVE")


@pytest.fixture
def consent_env(monkeypatch):
    """Both upstream switches set in the real environment: Stira must ignore them."""
    for name in CONSENT_ENV:
        monkeypatch.setenv(name, "true")


@pytest.fixture
def terminal(monkeypatch):
    """Fail the test if anything reads the terminal prompt."""
    read = Mock(side_effect=AssertionError("terminal read"))
    monkeypatch.setattr("strands_tools.utils.user_input.get_user_input", read)
    monkeypatch.setattr("strands_tools.utils.user_input.get_user_input_async", read)
    return read


def _tool(name: str):
    assert name in tool_registry.WRITE_TOOLS, f"{name} is installed but not registered"
    return tool_registry.WRITE_TOOLS[name]


def _module(name: str):
    return sys.modules[tool_consent.CONSENT_TOOL_MODULES[name]]


def _use(name: str, tool_input: dict, use_id: str = "t1") -> dict:
    return {"toolUseId": use_id, "name": name, "input": tool_input}


async def _collect(tool, use: dict, state: dict | None = None) -> dict:
    state = {} if state is None else state
    state.setdefault("agent", None)
    events = [event async for event in tool.stream(use, state)]
    event = events[-1]
    return getattr(event, "tool_result", None) or event["tool_result"]


def _text(result: dict) -> str:
    return " ".join(c.get("text", "") for c in result.get("content", []))


def _shell_args(tmp_path, name: str = "ran.txt") -> dict:
    return {"command": f"printf ran > {name}", "work_dir": str(tmp_path)}


# --- Bridge and registry -------------------------------------------------------


def test_every_exec_tool_is_bridged_gated_and_registered_unchanged():
    covered = tool_consent.install_consent_bridge()
    assert set(EXEC_TOOLS) <= covered and set(EXEC_TOOLS) <= tool_consent.GATED_TOOLS
    assert tool_consent.HUMAN_APPROVAL_TOOLS == tool_consent.EXEC_TOOLS
    assert tool_registry.EXEC_TOOL_NAMES == tool_consent.EXEC_TOOLS
    for name in EXEC_TOOLS:
        assert tool_consent.bridge_refusal(name) is None
        tool = _tool(name)
        assert isinstance(tool, ExecGatedTool)
        # Same name and spec as the upstream tool: the cached prefix is unchanged (#254).
        assert tool.tool_name == tool.wrapped.tool_name == name
        assert tool.tool_spec == tool.wrapped.tool_spec
    for name in ("shell", "cron"):  # the two upstream modules with a terminal prompt
        assert _module(name).get_user_input.stira_consent_bridge == name
        assert isinstance(_module(name).os, tool_consent._ScopedConsentOs)


def test_get_tools_exposes_gated_exec_tools_only_when_enabled(monkeypatch):
    monkeypatch.delenv("STIRA_ENABLE_EXEC_TOOLS", raising=False)
    names = {t.tool_name for t in tool_registry.get_tools()}
    assert not names & set(EXEC_TOOLS)
    tools = {t.tool_name: t for t in tool_registry.get_tools(include_exec=True)}
    assert set(EXEC_TOOLS) <= set(tools)
    for name in EXEC_TOOLS:
        tool = tools[name]
        while isinstance(tool, OsProtectionGuardTool):
            tool = tool.wrapped
        assert isinstance(tool, ExecGatedTool), name


def test_scoped_environment_ignores_the_process_environment(consent_env):
    shell_env, cron_env = _module("shell").os.environ, _module("cron").os.environ
    for name in CONSENT_ENV:
        assert name not in shell_env and shell_env.get(name) is None
    assert "BYPASS_TOOL_CONSENT" not in cron_env
    with tool_consent.consent_granted("shell"):
        assert shell_env["BYPASS_TOOL_CONSENT"] == shell_env["STRANDS_NON_INTERACTIVE"] == "true"
        assert "BYPASS_TOOL_CONSENT" not in cron_env  # one tool's grant opens only that tool
    with tool_consent.consent_granted("batch"):
        assert "BYPASS_TOOL_CONSENT" not in shell_env  # an approved batch is not a shell grant
    with pytest.raises(tool_consent.ToolConsentError):
        shell_env["BYPASS_TOOL_CONSENT"] = "true"


def _break_missing_prompt(monkeypatch, module):
    monkeypatch.delattr(module, "get_user_input")


def _break_environ_alias(monkeypatch, module):
    import os

    monkeypatch.setattr(module, "environ", os.environ, raising=False)


def _break_os_alias(monkeypatch, module):
    import os

    monkeypatch.setattr(module, "_real_os", os, raising=False)


def _break_async_prompt(monkeypatch, module):
    from strands_tools.utils import user_input

    monkeypatch.setattr(module, "ask", user_input.get_user_input_async, raising=False)


@pytest.mark.parametrize(
    ("name", "breakage"),
    [
        ("shell", _break_missing_prompt),
        ("cron", _break_missing_prompt),
        ("shell", _break_environ_alias),
        ("batch", _break_os_alias),
        ("think", _break_async_prompt),
    ],
)
def test_unverifiable_bridge_drops_and_refuses(
    tmp_path, monkeypatch, caplog, consent_env, terminal, name, breakage
):
    """Fail closed: a module that could reach the terminal or the real environment."""
    breakage(monkeypatch, _module(name))
    assert name not in tool_consent.install_consent_bridge()
    assert NO_BRIDGE in tool_consent.bridge_refusal(name)
    tools = {name: object(), "current_time": object()}
    with caplog.at_level("ERROR", logger=tool_registry.__name__):
        assert tool_registry._drop_unbridged(tools) == {name}
    assert f"Not registering {name}" in caplog.text
    assert name not in {t.tool_name for t in tool_registry.get_tools(include_exec=True)}
    args = _shell_args(tmp_path) if name == "shell" else {}
    use = _use(name, args)
    state: dict = {}
    assert mark_gate_passed(state, use)  # even with the gate's mark
    result = asyncio.run(_collect(_tool(name), use, state))
    assert result["status"] == "error" and NO_BRIDGE in _text(result)
    assert not (tmp_path / "ran.txt").exists()
    terminal.assert_not_called()


# --- The gate mark: the wrapper alone is not an approval ------------------------


@pytest.mark.parametrize("name", EXEC_TOOLS)
def test_direct_stream_without_gate_mark_is_refused(tmp_path, consent_env, terminal, name):
    tool = _tool(name)
    upstream = Mock(side_effect=AssertionError("upstream ran"))
    tool_stream = tool.wrapped.stream
    tool.wrapped.__dict__["stream"] = upstream  # instance attribute: this object only
    try:
        result = asyncio.run(_collect(tool, _use(name, {"x": 1})))
    finally:
        del tool.wrapped.__dict__["stream"]
    assert tool.wrapped.stream == tool_stream
    assert result["status"] == "error" and NO_GATE in _text(result)
    upstream.assert_not_called()
    terminal.assert_not_called()


@POSIX_ONLY
def test_gated_shell_runs_once_without_the_terminal(tmp_path, monkeypatch, terminal):
    for name in CONSENT_ENV:
        monkeypatch.delenv(name, raising=False)
    tool, use = _tool("shell"), _use("shell", _shell_args(tmp_path))
    state: dict = {}
    assert isinstance(mark_gate_passed(state, use), GateMark)
    result = asyncio.run(_collect(tool, use, state))
    assert result["status"] == "success", result
    assert (tmp_path / "ran.txt").read_text("utf-8") == "ran"
    (tmp_path / "ran.txt").unlink()
    replay = asyncio.run(_collect(tool, use, state))  # the same call again
    assert replay["status"] == "error" and NO_GATE in _text(replay)
    assert not (tmp_path / "ran.txt").exists()
    terminal.assert_not_called()


@POSIX_ONLY
def test_mark_is_bound_to_id_tool_and_arguments(tmp_path, consent_env, terminal):
    tool = _tool("shell")
    args = _shell_args(tmp_path)
    state: dict = {}
    mark_gate_passed(state, _use("shell", args, "other-id"))
    assert NO_GATE in _text(asyncio.run(_collect(tool, _use("shell", args), state)))
    state = {}
    mark_gate_passed(state, _use("shell", _shell_args(tmp_path, "approved.txt")))
    changed = asyncio.run(_collect(tool, _use("shell", args), state))
    assert "arguments changed after Stira's gate" in _text(changed)
    state = {}
    mark_gate_passed(state, _use("cron", args))  # a mark for another tool
    assert "approved a different call" in _text(
        asyncio.run(_collect(tool, _use("shell", args), state))
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == []


@POSIX_ONLY
def test_raw_upstream_shell_outside_a_grant_names_the_cause(tmp_path, consent_env, terminal):
    """The upstream tool without Stira's wrapper: never the terminal, never empty Input:."""
    raw = _tool("shell").wrapped
    result = asyncio.run(_collect(raw, _use("shell", _shell_args(tmp_path))))
    assert result["status"] == "error"
    # Refused at the upstream entry point, before its body (or its prompt) runs.
    assert OUTSIDE_STEP in _text(result) and "Input: " not in _text(result)
    assert not (tmp_path / "ran.txt").exists()
    terminal.assert_not_called()


def test_cron_raw_outside_a_grant_never_writes_the_crontab(monkeypatch, consent_env, terminal):
    module = _module("cron")
    run = Mock(return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    popen = Mock(side_effect=AssertionError("crontab written"))
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    use = _use("cron", {"action": "add", "schedule": "0 0 1 1 *", "command": "true"})
    result = asyncio.run(_collect(_tool("cron").wrapped, use))
    assert result["status"] == "error" and OUTSIDE_STEP in _text(result)
    popen.assert_not_called()
    run.assert_not_called()  # not even ``crontab -l``: the entry point refused first
    terminal.assert_not_called()


def test_inner_agent_without_stira_hooks_is_refused(tmp_path, consent_env, terminal):
    """think/use_agent/swarm/graph children: the parent's tools, none of its hooks (#145)."""
    model = ScriptedModel(
        [
            [{"toolUse": {"toolUseId": "i1", "name": "shell", "input": _shell_args(tmp_path)}}],
            [{"text": "done"}],
        ]
    )
    agent = Agent(model=model, tools=[_tool("shell")], callback_handler=None)
    agent("run")
    results = [b["toolResult"] for m in agent.messages for b in m["content"] if "toolResult" in b]
    assert [r["status"] for r in results] == ["error"] and NO_GATE in _text(results[0])
    assert not (tmp_path / "ran.txt").exists()
    terminal.assert_not_called()


def test_upstream_cancel_with_an_empty_reason_is_explained():
    event = ToolResultEvent(
        {
            "toolUseId": "t1",
            "status": "error",
            "content": [{"text": "Command execution cancelled by user. Input: "}],
        }
    )
    text = _text(exec_gate._explained("shell", event).tool_result)
    assert "consent bridge did not answer its prompt" in text
    assert "cannot confirm whether any part of the call completed" in text
    ok = ToolResultEvent({"toolUseId": "t1", "status": "success", "content": []})
    assert exec_gate._explained("shell", ok) is ok


def test_wrapper_refuses_a_non_exec_tool():
    with pytest.raises(ValueError):
        ExecGatedTool(_tool("file_write"))


# --- The gate: a human approval of this exact call ------------------------------


def _before(verifier, state: dict, use: dict, cancel: Any = False):
    event = types.SimpleNamespace(tool_use=use, invocation_state=state, cancel_tool=cancel)
    verifier.before_tool(event)
    return event


def _engine(now: float = 100.0):
    engine = Mock(agent_id="a1", _now_fn=lambda: now)
    engine.operation_ledger.mark_started.return_value = True
    return engine


@pytest.mark.parametrize("name", EXEC_TOOLS)
def test_verifier_gives_exec_tools_no_mark_without_a_human_approval(name):
    verifier, state = ApprovalBindingVerifier(_engine()), {}
    event = _before(verifier, state, _use(name, {"x": 1}))
    assert NO_HUMAN in event.cancel_tool
    assert not state.get(GATE_PASSED_KEY)


@pytest.mark.parametrize("name", EXEC_TOOLS)
def test_verifier_marks_an_exec_call_with_a_verified_binding(name):
    verifier, state = ApprovalBindingVerifier(_engine()), {}
    use = _use(name, {"x": 1})
    binding = ApprovalBinding("r1", "a1", name, args_hash(use["input"]), 200.0)
    state[HITL_BINDINGS_KEY] = {"t1": binding}
    event = _before(verifier, state, use)
    assert not event.cancel_tool
    assert isinstance(state[GATE_PASSED_KEY]["t1"], GateMark)
    # The binding is for this call only: other arguments, an expired one, a replay.
    state = {HITL_BINDINGS_KEY: {"t1": binding}}
    assert "HITL rejected" in _before(verifier, state, _use(name, {"x": 2})).cancel_tool
    assert not state.get(GATE_PASSED_KEY)
    late = ApprovalBindingVerifier(_engine(now=300.0))
    state = {HITL_BINDINGS_KEY: {"t1": binding}}
    assert _before(late, state, use).cancel_tool and not state.get(GATE_PASSED_KEY)
    engine = _engine()
    engine.operation_ledger.mark_started.return_value = False
    state = {HITL_BINDINGS_KEY: {"t1": binding}}
    assert "already recorded" in _before(ApprovalBindingVerifier(engine), state, use).cancel_tool
    assert not state.get(GATE_PASSED_KEY)


def _agent(monkeypatch, tools, calls, *, stability, decision=None, policy=None):
    responses = [
        [{"toolUse": {"toolUseId": f"w{i}", "name": name, "input": tool_input}}]
        for i, (name, tool_input) in enumerate(calls)
    ]
    model = ScriptedModel([*responses, [{"text": "turn done"}]])
    monkeypatch.setattr("stiracore.agent.nxp_agent.BedrockModel", lambda **kwargs: model)
    callback = None if decision is None else Mock(return_value=decision)
    agent = NXPAgent(
        "offline",
        "us-east-1",
        state=NeuralState(stability=stability),
        config=StiraConfig(critical_action="halt"),
        on_hitl_request=callback,
        tools=tools,
        agent_id="exec-consent-test",
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=policy,
    )
    return agent, callback


def _results(agent) -> list[dict]:
    return [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]


@POSIX_ONLY
@pytest.mark.parametrize(
    ("policy", "decision", "expected"),
    [
        ("ask", HITLDecision(HITLAction.APPROVE), "success"),
        ("ask", HITLDecision(HITLAction.REJECT), "HITL rejected"),
        ("ask", None, "HITL rejected"),  # no approver connected
        # A rule reader that answers Always (exec tools cannot be Always, ADR 0010 §5)
        # or no rule reader at all: no human approved it, so it never runs.
        ("always", HITLDecision(HITLAction.APPROVE), NO_HUMAN),
        (None, HITLDecision(HITLAction.APPROVE), NO_HUMAN),
    ],
)
def test_nxp_agent_runs_shell_only_when_a_human_approves(
    monkeypatch, tmp_path, consent_env, terminal, policy, decision, expected
):
    reader = None if policy is None else (lambda name: policy)
    calls = [("shell", _shell_args(tmp_path))]
    agent, _ = _agent(
        monkeypatch, [_tool("shell")], calls, stability=0.8, decision=decision, policy=reader
    )
    agent("run")
    (result,) = _results(agent)
    ran = (tmp_path / "ran.txt").exists()
    if expected == "success":
        assert result["status"] == "success" and ran, result
    else:
        assert result["status"] == "error" and expected in _text(result) and not ran, result
    terminal.assert_not_called()


@POSIX_ONLY
def test_breaker_warning_refuses_exec_without_asking(monkeypatch, tmp_path, consent_env):
    calls = [("shell", _shell_args(tmp_path))]
    agent, callback = _agent(
        monkeypatch,
        [_tool("shell")],
        calls,
        stability=0.4,
        decision=HITLDecision(HITLAction.APPROVE),
        policy=lambda name: "ask",
    )
    agent("run")
    (result,) = _results(agent)
    assert result["status"] == "error" and not (tmp_path / "ran.txt").exists()
    callback.assert_not_called()


def test_upstream_prompt_replacement_refuses_outside_a_grant():
    """Second layer under the entry guard: the prompt itself never answers unasked."""
    for name in ("shell", "cron"):
        with pytest.raises(tool_consent.ToolConsentError, match=OUTSIDE_GATE):
            _module(name).get_user_input("[y/*]")
        with tool_consent.consent_granted(name):
            assert _module(name).get_user_input("[y/*]") == "y"


# --- Upstream release pin and the source audit behind it (review M1) -----------

# What the audited release's exec modules reach, by AST (module source only; the
# libraries they call are out of scope). ``environ``/``getenv`` are read through the
# module-level ``os`` (replaced by the scoped view); the prompt is the module-level
# ``get_user_input`` (replaced); ``sys.stdin`` is used by ``shell`` only in its
# interactive mode, which the grant turns off (STRANDS_NON_INTERACTIVE). No function
# imports anything, and nothing aliases ``os``/``environ``/the prompt.
AUDITED_SOURCE = {
    "shell": [
        "key:BYPASS_TOOL_CONSENT",
        "key:STRANDS_NON_INTERACTIVE",
        "name:get_user_input",
        "os.environ",
        "sys.stdin",
    ],
    "cron": ["key:BYPASS_TOOL_CONSENT", "name:get_user_input", "os.environ", "proc.stdin"],
    "batch": [],
    "use_agent": ["os.getenv"],
    "swarm": [],
    "graph": [],
    "think": ["os.getenv"],
}
_WATCHED_ATTRIBUTES = {"environ", "environb", "getenv", "putenv", "stdin", "system", "popen"}
_WATCHED_NAMES = {"input", "get_user_input", "get_user_input_async", "environ", "getenv"}


def _source_audit(name: str) -> list[str]:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_module(name)))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _WATCHED_ATTRIBUTES:
            base = node.value.id if isinstance(node.value, ast.Name) else "?"
            found.add(f"{base}.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in _WATCHED_NAMES:
            found.add(f"name:{node.id}")
        elif isinstance(node, ast.Constant) and node.value in CONSENT_ENV:
            found.add(f"key:{node.value}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    found.add(f"local-import:{getattr(inner, 'module', None)}")
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) in (
            "__import__",
            "exec",
            "eval",
        ):
            found.add(f"call:{node.func.id}")
    return sorted(found)


def test_pinned_upstream_source_audit():
    installed = tool_consent.upstream_version()
    if installed not in tool_consent.VERIFIED_EXEC_UPSTREAM:
        # Fail closed on an unaudited release: no exec tool is bridged or registered.
        assert not tool_consent.install_consent_bridge() & tool_consent.EXEC_TOOLS
        pytest.skip(f"strands-agents-tools {installed} is not audited; exec tools dropped")
    assert {name: _source_audit(name) for name in EXEC_TOOLS} == AUDITED_SOURCE


def test_pyproject_does_not_admit_a_tools_release_newer_than_the_audited_one():
    # A fresh install resolves to the newest admitted release: it must be one the exec
    # bridge accepts, and nothing newer than the audited releases may be admitted (#296).
    import tomllib
    from pathlib import Path

    from packaging.requirements import Requirement
    from packaging.version import Version

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    dependencies = tomllib.loads(pyproject.read_text("utf-8"))["project"]["dependencies"]
    (requirement,) = [
        Requirement(text)
        for text in dependencies
        if Requirement(text).name == tool_consent.UPSTREAM_DISTRIBUTION
    ]
    newest = max(Version(v) for v in tool_consent.VERIFIED_EXEC_UPSTREAM)
    assert requirement.specifier.contains(newest)
    major, minor, micro = newest.release
    newer = [
        f"{major}.{minor}.{micro + 1}",
        f"{major}.{minor + 1}.0",
        f"{major + 1}.0.0",
        f"{major}.{minor}.{micro + 1}rc1",
    ]
    assert [v for v in newer if requirement.specifier.contains(v, prereleases=True)] == []


def test_unaudited_upstream_release_drops_every_exec_tool(monkeypatch, caplog):
    monkeypatch.setattr(tool_consent, "upstream_version", lambda: "0.9.0")
    covered = tool_consent.install_consent_bridge()
    assert "file_write" in covered and not covered & tool_consent.EXEC_TOOLS
    assert "not a release the exec bridge was audited against" in tool_consent.bridge_refusal(
        "shell"
    )
    names = {t.tool_name for t in tool_registry.get_tools(include_exec=True)}
    assert "file_write" in names and not names & set(EXEC_TOOLS)


# --- The entry guard: an upstream exec function outside its own grant -----------


def _raw_entry(name: str):
    module = _module(name)
    value = module.__dict__[name]
    return getattr(value, "_tool_func", value)


@pytest.mark.parametrize("name", EXEC_TOOLS)
def test_upstream_entry_runs_only_inside_its_own_grant(name, consent_env, terminal):
    entry = _raw_entry(name)
    assert entry.stira_consent_bridge == name
    with pytest.raises(tool_consent.ToolConsentError, match=OUTSIDE_STEP):
        entry()
    other = "think" if name != "think" else "use_agent"
    with tool_consent.consent_granted(other), pytest.raises(tool_consent.ToolConsentError):
        entry()  # an approved parent step is not this tool's approval
    terminal.assert_not_called()


class _RecordingModel(ScriptedModel):
    """ScriptedModel that keeps the system prompt of every request (who was started)."""

    def __init__(self, responses):
        super().__init__(responses)
        self.systems: list[str] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.systems.append(system_prompt or "")
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


@POSIX_ONLY
@pytest.mark.parametrize("parent", ["think", "use_agent", "swarm", "graph"])
def test_child_that_loads_upstream_tools_itself_runs_nothing(
    tmp_path, consent_env, terminal, parent
):
    """Inside an approved think/use_agent/... step, a hook-less child agent that loaded the
    raw upstream modules (not Stira's registry) still cannot run an exec tool or start a
    grandchild agent: each upstream entry point needs a grant for itself."""
    from importlib import import_module

    calls = [
        ("shell", _shell_args(tmp_path)),
        ("use_agent", {"prompt": "hi", "system_prompt": "M362-grandchild"}),
        ("batch", {"invocations": [{"name": "shell", "arguments": _shell_args(tmp_path)}]}),
        ("cron", {"action": "add", "schedule": "0 0 1 1 *", "command": "true"}),
        ("think", {"thought": "hi", "cycle_count": 1, "system_prompt": "M362-grandchild"}),
        ("swarm", {"task": "hi", "agents": [{"name": "g", "system_prompt": "M362-grandchild"}]}),
    ]
    model = _RecordingModel(
        [
            [{"toolUse": {"toolUseId": f"c{i}", "name": n, "input": a}}]
            for i, (n, a) in enumerate(calls)
        ]
        + [[{"text": "child done"}]] * 10  # spare answers: a grandchild would show up
    )
    raw_tools = [import_module(f"strands_tools.{name}") for name, _ in calls]
    child = Agent(model=model, tools=raw_tools, callback_handler=None, system_prompt="child")
    cron = _module("cron")
    # The parent's approved step: its grant, and its upstream body (entered once) runs
    # the child, as think/use_agent/swarm/graph do.
    parent_body = tool_consent._entry_guard(parent, lambda: child("go"))
    original_run, original_popen = cron.subprocess.run, cron.subprocess.Popen
    cron.subprocess.run = Mock(side_effect=AssertionError("crontab read"))
    cron.subprocess.Popen = Mock(side_effect=AssertionError("crontab written"))
    try:
        with tool_consent.consent_granted(parent):
            parent_body()
    finally:
        cron.subprocess.run, cron.subprocess.Popen = original_run, original_popen
    results = [b["toolResult"] for m in child.messages for b in m["content"] if "toolResult" in b]
    assert [r["status"] for r in results] == ["error"] * len(calls), results
    assert all(OUTSIDE_STEP in _text(r) for r in results), results
    assert model.systems == ["child"] * (len(calls) + 1)  # no grandchild ever started
    assert not (tmp_path / "ran.txt").exists()
    terminal.assert_not_called()


# --- One approval, one run: concurrent consumers of the same mark (review M2) ---


def _append_args(tmp_path) -> dict:
    return {"command": "printf x >> count.txt", "work_dir": str(tmp_path)}


@POSIX_ONLY
def test_concurrent_streams_of_one_mark_run_exactly_once(tmp_path, monkeypatch, terminal):
    for name in CONSENT_ENV:
        monkeypatch.delenv(name, raising=False)
    tool, use = _tool("shell"), _use("shell", _append_args(tmp_path))
    state: dict = {"agent": None}
    assert mark_gate_passed(state, use)

    async def both():
        return await asyncio.gather(*(_collect(tool, use, state) for _ in range(8)))

    results = asyncio.run(both())
    assert sorted(r["status"] for r in results) == ["error"] * 7 + ["success"]
    assert all(NO_GATE in _text(r) for r in results if r["status"] == "error")
    assert (tmp_path / "count.txt").read_text("utf-8") == "x"


@POSIX_ONLY
def test_threads_racing_for_one_mark_run_exactly_once(tmp_path, monkeypatch, terminal):
    import threading

    for name in CONSENT_ENV:
        monkeypatch.delenv(name, raising=False)
    tool, use = _tool("shell"), _use("shell", _append_args(tmp_path))
    state: dict = {"agent": None}
    assert mark_gate_passed(state, use)
    start, results = threading.Barrier(8), []

    def race():
        start.wait()
        results.append(asyncio.run(_collect(tool, use, state)))

    threads = [threading.Thread(target=race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert sorted(r["status"] for r in results) == ["error"] * 7 + ["success"]
    assert (tmp_path / "count.txt").read_text("utf-8") == "x"
