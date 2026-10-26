"""#242: upstream file_write/editor consent is decided by Stira's policy, never the terminal."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import threading
import types
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from strands import Agent
from strands.tools.tools import AgentTool
from strands.types._events import ToolResultEvent
from test_subagent import ScriptedModel

from stiracore.agent.checkpoint_hooks import ApprovalBindingVerifier
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig
from stiracore.identity_paths import WORKSPACE_ROOT, WritePathPolicy
from stiracore.tools import tool_consent, tool_registry
from stiracore.tools.identity_tools import IdentityProtectedTool
from stiracore.tools.tool_config import ToolConfig
from stiracore.tools.tool_consent import GATE_PASSED_KEY, GateMark, mark_gate_passed

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_CANCEL = "cancelled by the user"
EXPLICIT = "requested terminal confirmation outside Stira's approval gate"
NO_GATE = "no Stira gate approval"
NO_BRIDGE = "cannot route the upstream terminal confirmation"
EDITOR_OPS = [
    {"command": "create", "file_text": "one\ntwo\n"},
    {"command": "str_replace", "old_str": "two", "new_str": "TWO"},
    {"command": "insert", "insert_line": 1, "new_str": "inserted"},
]
TOOLS = ("file_write", "editor")
# File tools with an upstream terminal prompt; ``editor`` is Stira's own since #245.
# The exec tools in CONSENT_TOOL_MODULES (#362): tests/test_exec_tool_consent.py.
# ``http_request`` (#547, a prompt for modifying methods only): tests/test_http_request_consent.py.
CONSENT_TOOLS = tuple(
    name
    for name in tool_consent.CONSENT_TOOL_MODULES
    if name not in tool_consent.EXEC_TOOLS and name != tool_consent.HTTP_REQUEST_TOOL
)
NOT_APPROVED = "not inside a step Stira's approval gate granted"


def _installed(name: str) -> bool:
    """False only if the module does not exist; an existing module must import (or fail)."""
    if name not in tool_consent.CONSENT_TOOL_MODULES:
        return True  # Stira's own tool (editor, #245)
    module_name = tool_consent.CONSENT_TOOL_MODULES[name]
    if find_spec("strands_tools") is None or find_spec(module_name) is None:
        return False
    import_module(module_name)  # a broken install (missing dependency) fails the test
    return True


def _tool(name: str) -> AgentTool:
    """Skip only if the upstream module is absent; an installed but broken tool fails."""
    if not _installed(name):
        pytest.skip(f"strands_tools.{name} is not installed")
    assert name in tool_registry.WRITE_TOOLS, f"{name} is installed but not registered"
    return tool_registry.WRITE_TOOLS[name]


def _available() -> list[str]:
    return [name for name in TOOLS if _installed(name)]


def _module(name: str):
    _tool(name)
    return sys.modules[tool_consent.CONSENT_TOOL_MODULES[name]]


def _use(name: str, tool_input: dict, use_id: str = "t1") -> dict:
    return {"toolUseId": use_id, "name": name, "input": tool_input}


async def _collect(tool, use: dict, state: dict | None = None) -> dict:
    state = {} if state is None else state
    state.setdefault("agent", None)  # the SDK always supplies it; decorated tools need it
    events = [event async for event in tool.stream(use, state)]
    event = events[-1]
    return getattr(event, "tool_result", None) or event["tool_result"]


def _gated(tool, tool_input: dict, use_id: str = "t1") -> dict:
    """Run the wrapper as if Stira's hooks approved this exact call."""
    use = _use(tool.tool_name, tool_input, use_id)
    state: dict = {}
    assert mark_gate_passed(state, use)
    return asyncio.run(_collect(tool, use, state))


def _raw(name: str, tool_input: dict) -> dict:
    """The upstream tool outside Stira's wrapper; the SDK turns exceptions into errors."""
    try:
        return asyncio.run(_collect(_tool(name).wrapped, _use(name, tool_input)))
    except tool_consent.ToolConsentError as error:
        return {"status": "error", "content": [{"text": f"Error: {error}"}]}


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _editor_input(op: dict, path: Path) -> dict:
    return {**op, "path": str(path)}


@pytest.mark.parametrize("name", CONSENT_TOOLS)
def test_bridge_covers_installed_tools(name):
    _tool(name)
    assert name in tool_consent.install_consent_bridge()
    module = _module(name)
    assert module.get_user_input.stira_consent_bridge == name
    assert isinstance(module.os.environ, tool_consent._ScopedConsentEnviron)
    assert tool_consent.bridge_refusal(name) is None


# --- Terminal input is never read (separate process, real stdin, real hooks) ---

_CHILD = textwrap.dedent(
    """
    import asyncio, json, os, sys
    sys.path.insert(0, sys.argv[3])
    from test_subagent import ScriptedModel
    from stiracore.agent.nxp_agent import NXPAgent
    from stiracore.tools import tool_consent, tool_registry as registry

    work, names = sys.argv[1], json.loads(sys.argv[2])
    calls = [("file_write", {"path": f"{work}/a.md", "content": "hello"})] if "file_write" in names else []
    if "editor" in names:
        calls += [("editor", {**op, "path": f"{work}/b.md"}) for op in json.loads(sys.argv[4])]
    model = ScriptedModel(
        [[{"toolUse": {"toolUseId": f"w{i}", "name": n, "input": a}}] for i, (n, a) in enumerate(calls)]
        + [[{"text": "done"}]]
    )
    class Classifier:
        def classify(self, **kwargs):
            return None
    agent = NXPAgent(model=model, tools=[registry.WRITE_TOOLS[n] for n in names],
                     stimulus_classifier=Classifier(), agent_id="child")
    agent("write")
    gated = [b["toolResult"] for m in agent.agent.messages for b in m["content"] if "toolResult" in b]

    def stream(tool, name, tool_input):
        async def collect():
            use = {"toolUseId": "x", "name": name, "input": tool_input}
            return [event async for event in tool.stream(use, {"agent": None})]
        try:
            event = asyncio.run(collect())[-1]
        except Exception as error:  # the SDK executor turns this into an error result
            return {"status": "error", "content": [{"text": f"Error: {error}"}]}
        return getattr(event, "tool_result", None) or event["tool_result"]

    raw, direct = [], []
    for name in names:
        tool = registry.WRITE_TOOLS[name]
        args = ({"path": f"{work}/raw-{name}.md", "content": "x"} if name == "file_write"
                else {"command": "create", "path": f"{work}/raw-{name}.md", "file_text": "x"})
        raw.append(stream(tool.wrapped, name, args))
        direct.append(stream(tool, name, {**args, "path": f"{work}/direct-{name}.md"}))
    print("RESULT " + json.dumps({"gated": gated, "raw": raw, "direct": direct}))
    """
)


@pytest.mark.parametrize("stdin_kind", ["devnull", "open_pipe"])
@pytest.mark.parametrize("bypass", [None, "true"])
def test_headless_process_never_reads_terminal(tmp_path, stdin_kind, bypass):
    """stdin=/dev/null would cancel at once; an open pipe would block if read."""
    names = _available()
    if not names:
        pytest.skip("strands_tools file tools are not installed")
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "BYPASS_TOOL_CONSENT"}
    if bypass is not None:
        env["BYPASS_TOOL_CONSENT"] = bypass
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    stdout_path = tmp_path / "out.txt"
    argv = [str(tmp_path), json.dumps(names), str(ROOT / "tests"), json.dumps(EDITOR_OPS)]
    with open(stdout_path, "w", encoding="utf-8") as stdout:
        proc = subprocess.Popen(
            [sys.executable, str(script), *argv],
            stdin=subprocess.DEVNULL if stdin_kind == "devnull" else subprocess.PIPE,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=tmp_path,
        )
        try:
            # The pipe stays open and empty: any terminal read would block here.
            assert proc.wait(timeout=60) == 0, stdout_path.read_text(encoding="utf-8")
        finally:
            if proc.poll() is None:
                proc.kill()
            if proc.stdin is not None:
                proc.stdin.close()
    line = next(
        row for row in stdout_path.read_text(encoding="utf-8").splitlines() if "RESULT " in row
    )
    out = json.loads(line.split("RESULT ", 1)[1])

    expected = (1 if "file_write" in names else 0) + (3 if "editor" in names else 0)
    assert [r["status"] for r in out["gated"]] == ["success"] * expected, out["gated"]
    if "file_write" in names:
        assert (tmp_path / "a.md").read_text(encoding="utf-8") == "hello"
    if "editor" in names:
        edited = (tmp_path / "b.md").read_text(encoding="utf-8")
        assert "inserted" in edited and "TWO" in edited
    # Outside Stira's gate, with or without BYPASS_TOOL_CONSENT=true: refused with the cause.
    for name, result in zip(names, out["raw"], strict=True):
        assert result["status"] == "error"
        assert UPSTREAM_CANCEL not in _text(result)
        if name == "editor":  # Stira's editor: its sandbox refuses writes outside a grant
            assert NOT_APPROVED in _text(result)
            continue
        assert EXPLICIT in _text(result) and "stdin is not a terminal" in _text(result)
    for result in out["direct"]:
        assert result["status"] == "error" and NO_GATE in _text(result)
    for name in names:
        assert not (tmp_path / f"raw-{name}.md").exists()
        assert not (tmp_path / f"direct-{name}.md").exists()


# --- The gate mark: the wrapper alone is not an approval ------------------------


@pytest.mark.parametrize("bypass", [None, "true"])
@pytest.mark.parametrize("name", TOOLS)
def test_wrapper_without_gate_mark_is_refused(tmp_path, monkeypatch, bypass, name):
    tool = _tool(name)
    if bypass is None:
        monkeypatch.delenv("BYPASS_TOOL_CONSENT", raising=False)
    else:
        monkeypatch.setenv("BYPASS_TOOL_CONSENT", bypass)
    target = tmp_path / "x.md"
    args = (
        {"path": str(target), "content": "x"}
        if name == "file_write"
        else {"command": "create", "path": str(target), "file_text": "x"}
    )
    unmarked = asyncio.run(_collect(tool, _use(name, args)))
    assert unmarked["status"] == "error" and NO_GATE in _text(unmarked)
    # A mark is bound to the toolUseId, the tool and the exact arguments.
    state: dict = {}
    mark_gate_passed(state, _use(name, args, "other-id"))
    assert asyncio.run(_collect(tool, _use(name, args), state))["status"] == "error"
    state = {}
    mark_gate_passed(state, _use(name, {**args, "path": str(tmp_path / "approved.md")}))
    changed = asyncio.run(_collect(tool, _use(name, args), state))
    assert "arguments changed after Stira's gate" in _text(changed)
    assert not target.exists() and not (tmp_path / "approved.md").exists()


def test_inner_agent_without_stira_hooks_is_refused(tmp_path, monkeypatch):
    """Like think/use_agent inner agents (#145): no Stira hooks, so no approval."""
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    target = tmp_path / "inner.md"
    model = ScriptedModel(
        [
            [
                {
                    "toolUse": {
                        "toolUseId": "inner-1",
                        "name": "file_write",
                        "input": {"path": str(target), "content": "x"},
                    }
                }
            ],
            [{"text": "done"}],
        ]
    )
    agent = Agent(model=model, tools=[_tool("file_write")], callback_handler=None)
    agent("write")
    results = [b["toolResult"] for m in agent.messages for b in m["content"] if "toolResult" in b]
    assert [r["status"] for r in results] == ["error"]
    assert NO_GATE in _text(results[0])
    assert not target.exists()


def _file_write_use(tmp_path, use_id="t1", name="a.md"):
    return _use("file_write", {"path": str(tmp_path / name), "content": "x"}, use_id)


def test_mark_is_single_use_sequential_replay(tmp_path):
    tool = _tool("file_write")
    use = _file_write_use(tmp_path)
    state: dict = {}
    assert isinstance(mark_gate_passed(state, use), GateMark)
    assert asyncio.run(_collect(tool, use, state))["status"] == "success"
    (tmp_path / "a.md").unlink()
    replay = asyncio.run(_collect(tool, use, state))
    assert replay["status"] == "error" and NO_GATE in _text(replay)
    assert not (tmp_path / "a.md").exists()


def test_mark_is_single_use_editor_insert_replay(tmp_path):
    tool = _tool("editor")
    target = tmp_path / "e.md"
    target.write_text("one\n", encoding="utf-8")
    use = _use("editor", _editor_input(EDITOR_OPS[2], target))
    state: dict = {}
    mark_gate_passed(state, use)
    statuses = [asyncio.run(_collect(tool, use, state))["status"] for _ in range(2)]
    assert statuses == ["success", "error"]
    assert target.read_text(encoding="utf-8").count("inserted") == 1


def test_mark_is_single_use_concurrent_replay(tmp_path):
    tool = _tool("file_write")
    use = _file_write_use(tmp_path)
    state: dict = {}
    mark_gate_passed(state, use)

    async def both():
        return await asyncio.gather(*(_collect(tool, use, state) for _ in range(5)))

    statuses = sorted(r["status"] for r in asyncio.run(both()))
    assert statuses == ["error"] * 4 + ["success"]


def test_hand_built_or_unbindable_marks_are_refused(tmp_path):
    tool = _tool("file_write")
    use = _file_write_use(tmp_path)
    digest = tool_consent.input_digest(use["input"])
    for forged in [("file_write", digest), ("file_write", None), {"digest": digest}]:
        state = {GATE_PASSED_KEY: {"t1": forged}}
        assert NO_GATE in _text(asyncio.run(_collect(tool, use, state)))
    # Empty toolUseId or unhashable input: nothing is issued, nothing accepted.
    empty = {**use, "toolUseId": ""}
    assert mark_gate_passed({}, empty) is None
    assert "no toolUseId" in _text(asyncio.run(_collect(tool, empty, {GATE_PASSED_KEY: {}})))
    assert mark_gate_passed({}, {**use, "input": {"path": object()}}) is None
    # A mark for another tool does not open this one.
    state: dict = {}
    mark_gate_passed(state, {**use, "name": "editor"})
    assert "approved a different call" in _text(asyncio.run(_collect(tool, use, state)))
    assert not (tmp_path / "a.md").exists()


def test_wrapper_runs_a_snapshot_of_the_approved_arguments(tmp_path):
    stepping = _SteppingTool("file_write")
    use = _file_write_use(tmp_path)
    state: dict = {}
    mark_gate_passed(state, use)

    async def scenario():
        events = IdentityProtectedTool(stepping).stream(use, state)
        await anext(events)
        use["input"]["path"] = str(tmp_path / "swapped.md")  # caller mutates after the gate
        await anext(events)
        await events.aclose()

    asyncio.run(scenario())
    assert stepping.inputs[0] is not use["input"]
    assert stepping.inputs[0]["path"] == str(tmp_path / "a.md")


def test_nested_wrappers_share_one_claim(tmp_path):
    """get_tools() wraps the registry wrapper again; one mark serves the nested pair."""
    nested = tool_registry.get_tools(protected_dirs=[str(tmp_path / "protected")])
    tool = next(t for t in nested if t.tool_name == "file_write")
    assert isinstance(tool.wrapped, IdentityProtectedTool)
    assert _gated(tool, {"path": str(tmp_path / "n.md"), "content": "n"})["status"] == "success"


# --- Mark ownership in ApprovalBindingVerifier ----------------------------------


def _before(verifier, state, use, cancel=False):
    event = types.SimpleNamespace(tool_use=use, invocation_state=state, cancel_tool=cancel)
    verifier.before_tool(event)
    return event


def _after(verifier, state, use):
    verifier.after_tool(types.SimpleNamespace(tool_use=use, invocation_state=state, result={}))


def test_verifier_marks_only_file_tools():
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    _before(verifier, state, _use("current_time", {}))
    assert GATE_PASSED_KEY not in state
    _before(verifier, state, _use("editor", {"command": "view", "path": "x"}))
    assert isinstance(state[GATE_PASSED_KEY]["t1"], GateMark)


def test_verifier_cancel_revokes_existing_mark(tmp_path):
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    use = _file_write_use(tmp_path)
    _before(verifier, state, use)
    _before(verifier, state, use, cancel="HITL rejected")
    assert state[GATE_PASSED_KEY] == {}


def test_verifier_issue_failure_cancels_the_call(tmp_path):
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    event = _before(verifier, state, _use("file_write", {"path": object()}))
    assert "could not bind its approval" in event.cancel_tool
    event = _before(verifier, state, {**_file_write_use(tmp_path), "toolUseId": ""})
    assert "could not bind its approval" in event.cancel_tool
    assert not state.get(GATE_PASSED_KEY)


def test_verifier_duplicate_id_in_flight_refuses_both(tmp_path):
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    use = _file_write_use(tmp_path)
    assert not _before(verifier, state, use).cancel_tool
    assert "already in" in _before(verifier, state, dict(use)).cancel_tool
    assert state[GATE_PASSED_KEY] == {}  # the first, unclaimed mark is revoked too


def test_verifier_after_tool_drops_only_its_own_mark(tmp_path):
    verifier, state = ApprovalBindingVerifier(Mock()), {}
    use = _file_write_use(tmp_path)
    _before(verifier, state, use)
    first = state[GATE_PASSED_KEY]["t1"]
    claimed = tool_consent.claim_gate("file_write", state, "t1", dict(use["input"]))
    assert claimed is first  # A is running; B (same id) passes the gate afresh
    use_b = dict(use)  # B: another call (own tool_use object), same id (#255)
    _before(verifier, state, use_b)
    second = state[GATE_PASSED_KEY]["t1"]
    _after(verifier, state, use)  # A finishes: B's mark stays
    assert state[GATE_PASSED_KEY]["t1"] is second
    _after(verifier, state, use_b)  # B finishes without claiming: its own mark goes
    assert state[GATE_PASSED_KEY] == {}


# --- Registry fail-closed ------------------------------------------------------


@pytest.mark.parametrize("name", CONSENT_TOOLS)
def test_registry_drops_tool_whose_bridge_cannot_be_verified(monkeypatch, caplog, name):
    _tool(name)
    monkeypatch.delattr(_module(name), "get_user_input")
    tools = {name: object(), "current_time": object()}
    with caplog.at_level("ERROR", logger=tool_registry.__name__):
        assert tool_registry._drop_unbridged(tools) == {name}
    assert set(tools) == {"current_time"}
    assert f"Not registering {name}" in caplog.text and NO_BRIDGE in caplog.text
    assert name not in {t.tool_name for t in tool_registry.get_tools()}


# --- Fail closed when the bridge cannot be verified ----------------------------


def _break_missing_prompt(monkeypatch, module, module_name):
    monkeypatch.delattr(module, "get_user_input")


def _break_import(monkeypatch, module, module_name):
    monkeypatch.setitem(sys.modules, module_name, None)


def _break_foreign_os(monkeypatch, module, module_name):
    foreign = types.SimpleNamespace(environ={"BYPASS_TOOL_CONSENT": "true"}, path=os.path)
    monkeypatch.setattr(module, "os", foreign)


def _break_swapped_bridge(monkeypatch, module, module_name):
    # A bridge for the other tool: it would honour the wrong tool's grant.
    other = "editor" if module_name.endswith("file_write") else "file_write"
    monkeypatch.setattr(module, "get_user_input", tool_consent._terminal_prompt_replacement(other))


@pytest.mark.parametrize(
    "breakage",
    [_break_missing_prompt, _break_import, _break_foreign_os, _break_swapped_bridge],
)
@pytest.mark.parametrize("bypass", [None, "true"])
@pytest.mark.parametrize("name", CONSENT_TOOLS)
def test_unverifiable_bridge_refuses_without_terminal_or_side_effect(
    tmp_path, monkeypatch, breakage, bypass, name
):
    tool = _tool(name)
    if bypass is None:
        monkeypatch.delenv("BYPASS_TOOL_CONSENT", raising=False)
    else:
        monkeypatch.setenv("BYPASS_TOOL_CONSENT", bypass)
    terminal = Mock(side_effect=AssertionError("terminal read"))
    monkeypatch.setattr("strands_tools.utils.user_input.get_user_input_async", terminal)
    monkeypatch.setattr("strands_tools.utils.user_input.get_user_input", terminal)
    module_name = tool_consent.CONSENT_TOOL_MODULES[name]
    breakage(monkeypatch, _module(name), module_name)

    assert name not in tool_consent.install_consent_bridge()
    with (
        pytest.raises(tool_consent.ToolConsentError, match=NO_BRIDGE),
        tool_consent.consent_granted(name),
    ):
        pass
    target = tmp_path / "x.md"
    args = (
        {"path": str(target), "content": "x"}
        if name == "file_write"
        else {"command": "create", "path": str(target), "file_text": "x"}
    )
    result = _gated(tool, args)
    assert result["status"] == "error" and NO_BRIDGE in _text(result)
    assert not target.exists()
    terminal.assert_not_called()


def test_bridge_is_restored_after_a_reload_shape(monkeypatch):
    """A module back in its original shape (e.g. reloaded) is patched again before use."""
    from strands_tools.utils import user_input

    module = _module("file_write")
    monkeypatch.setattr(module, "get_user_input", user_input.get_user_input)
    monkeypatch.setattr(module, "os", os)
    assert tool_consent.bridge_refusal("file_write") is None
    assert module.get_user_input.stira_consent_bridge == "file_write"


# --- The grant: scoped to the upstream step, never across a yield --------------


class _SteppingTool(AgentTool):
    """Stands in for an upstream tool streaming two events; records the grant per step."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.seen: list[bool] = []
        self.inputs: list[Any] = []
        self.closed = False

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> Any:
        return {"name": self._name, "description": "fake", "inputSchema": {"json": {}}}

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        try:
            self.inputs.append(tool_use["input"])
            self.seen.append(tool_consent._granted(self._name))
            yield {"step": 1}
            self.seen.append(tool_consent._granted(self._name))
            yield ToolResultEvent(
                {"toolUseId": tool_use["toolUseId"], "status": "success", "content": []}
            )
        finally:
            self.closed = True


def test_grant_is_not_held_across_yield(tmp_path, monkeypatch):
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    environ = _module("file_write").os.environ
    stepping = _SteppingTool("file_write")
    wrapper = IdentityProtectedTool(stepping)
    use = _use("file_write", {"path": str(tmp_path / "s.md"), "content": "x"})
    state: dict = {}
    mark_gate_passed(state, use)
    raw_target = tmp_path / "leak.md"

    async def raw_attempt():
        return await _raw_in_loop(raw_target)

    async def scenario():
        events = wrapper.stream(use, state)
        first = await anext(events)
        # Suspended at the yield: neither this task nor a child task holds the grant.
        same_task = (environ.get("BYPASS_TOOL_CONSENT"), await _raw_in_loop(raw_target))
        child_task = await asyncio.create_task(raw_attempt())
        second = await anext(events)
        with pytest.raises(StopAsyncIteration):
            await anext(events)
        return first, same_task, child_task, second

    first, same_task, child_task, second = asyncio.run(scenario())
    assert first == {"step": 1} and second.tool_result["status"] == "success"
    assert stepping.seen == [True, True]
    assert same_task == (None, "refused")
    assert child_task == "refused"
    assert not raw_target.exists()
    assert stepping.closed


async def _raw_in_loop(target: Path) -> str:
    use = _use("file_write", {"path": str(target), "content": "leak"})
    try:
        result = [e async for e in _tool("file_write").wrapped.stream(use, {})][-1]
    except tool_consent.ToolConsentError:
        return "refused"
    return result.tool_result["status"]


def test_closing_mid_stream_releases_grant_and_closes_upstream(tmp_path):
    stepping = _SteppingTool("file_write")
    use = _use("file_write", {"path": str(tmp_path / "s.md"), "content": "x"})
    state: dict = {}
    mark_gate_passed(state, use)

    async def scenario():
        events = IdentityProtectedTool(stepping).stream(use, state)
        await anext(events)
        await events.aclose()
        return tool_consent._granted("file_write")

    assert asyncio.run(scenario()) is False
    assert stepping.closed and stepping.seen == [True]


def test_upstream_finishes_in_worker_before_anext_returns(tmp_path, monkeypatch):
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    tool = _tool("file_write")
    target = tmp_path / "done.md"
    raw_target = tmp_path / "raw.md"
    use = _use("file_write", {"path": str(target), "content": "done"})
    state: dict = {}
    mark_gate_passed(state, use)

    async def scenario():
        events = tool.stream(use, state)
        event = await anext(events)
        written = target.read_text(encoding="utf-8")  # the worker already finished
        leaked = await _raw_in_loop(raw_target)
        await events.aclose()
        return event.tool_result["status"], written, leaked

    assert asyncio.run(scenario()) == ("success", "done", "refused")
    assert not raw_target.exists()


def test_lost_context_in_worker_fails_closed(tmp_path, monkeypatch):
    """An executor that does not copy the context sees no grant: refused, not prompted."""
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    module = _module("file_write")
    target = tmp_path / "x.md"

    async def scenario():
        with tool_consent.consent_granted("file_write"):
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, module.file_write, _use("file_write", {"path": str(target), "content": "x"})
            )

    with pytest.raises(tool_consent.ToolConsentError, match="did not reach the worker"):
        asyncio.run(scenario())
    assert not target.exists()


# --- Scoping: no process-global switch, no leak across concurrent calls -------


def test_scoped_view_ignores_process_environment(monkeypatch):
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    file_write_os = _module("file_write").os
    assert file_write_os.environ.get("BYPASS_TOOL_CONSENT") is None
    assert "BYPASS_TOOL_CONSENT" not in file_write_os.environ
    assert file_write_os.getenv("BYPASS_TOOL_CONSENT") is None
    with tool_consent.consent_granted("file_write"):
        assert file_write_os.environ.get("BYPASS_TOOL_CONSENT") == "true"
    assert file_write_os.environ.get("BYPASS_TOOL_CONSENT") is None
    assert os.environ["BYPASS_TOOL_CONSENT"] == "true"  # the real environment is untouched
    monkeypatch.setenv("STIRA_CONSENT_PROBE", "1")
    assert file_write_os.environ.get("STIRA_CONSENT_PROBE") == "1"
    assert file_write_os.path is os.path


def test_grant_opens_only_the_named_tool():
    file_write_os = _module("file_write").os
    with tool_consent.consent_granted("editor"):
        assert tool_consent.active_grant("editor") is not None
        assert tool_consent.active_grant("file_write") is None
        assert file_write_os.environ.get("BYPASS_TOOL_CONSENT") is None
    with tool_consent.consent_granted("file_write"):
        assert tool_consent.active_grant("editor") is None
        assert file_write_os.environ.get("BYPASS_TOOL_CONSENT") == "true"


def test_grant_does_not_leak_to_a_concurrent_thread():
    environ = _module("file_write").os.environ
    granted, checked = threading.Event(), threading.Event()
    seen = {}

    def holder():
        with tool_consent.consent_granted("file_write"):
            seen["holder"] = environ.get("BYPASS_TOOL_CONSENT")
            granted.set()
            checked.wait(10)

    def other():
        granted.wait(10)
        seen["other"] = environ.get("BYPASS_TOOL_CONSENT")
        checked.set()

    threads = [threading.Thread(target=holder), threading.Thread(target=other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert seen == {"holder": "true", "other": None}


def test_concurrent_gated_and_raw_calls_in_one_loop(tmp_path, monkeypatch):
    """The SDK runs tools as concurrent tasks; only the gated call gets consent."""
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    wrapped = _tool("file_write")
    use = _use("file_write", {"path": str(tmp_path / "gated.md"), "content": "gated"})
    state: dict = {}
    mark_gate_passed(state, use)

    async def gated():
        return (await _collect(wrapped, use, state))["status"]

    async def both():
        return await asyncio.gather(gated(), _raw_in_loop(tmp_path / "raw.md"))

    assert asyncio.run(both()) == ["success", "refused"]
    assert (tmp_path / "gated.md").read_text(encoding="utf-8") == "gated"
    assert not (tmp_path / "raw.md").exists()


# --- Raw upstream calls: explicit cause, no side effect ------------------------


def test_raw_file_write_error_names_the_cause(tmp_path):
    result = _raw("file_write", {"path": str(tmp_path / "x.md"), "content": "x"})
    assert result["status"] == "error" and EXPLICIT in _text(result)
    assert UPSTREAM_CANCEL not in _text(result)
    assert not (tmp_path / "x.md").exists()


@pytest.mark.parametrize("bypass", [None, "true"])
def test_raw_editor_operations_refused_and_file_unchanged(tmp_path, monkeypatch, bypass):
    _tool("editor")
    if bypass is None:
        monkeypatch.delenv("BYPASS_TOOL_CONSENT", raising=False)
    else:
        monkeypatch.setenv("BYPASS_TOOL_CONSENT", bypass)
    existing = tmp_path / "existing.md"
    existing.write_text("one\ntwo\n", encoding="utf-8")
    created = tmp_path / "created.md"
    for op, path in [
        (EDITOR_OPS[0], created),
        (EDITOR_OPS[1], existing),
        (EDITOR_OPS[2], existing),
    ]:
        result = _raw("editor", _editor_input(op, path))
        assert result["status"] == "error", op
        assert NOT_APPROVED in _text(result) and UPSTREAM_CANCEL not in _text(result)
    assert existing.read_text(encoding="utf-8") == "one\ntwo\n"
    assert not created.exists()


# --- Stira's policy alone decides, with BYPASS_TOOL_CONSENT=true set -----------


def _agent(monkeypatch, tools, calls, *, stability, decision=None, bypass="true"):
    if bypass is None:
        monkeypatch.delenv("BYPASS_TOOL_CONSENT", raising=False)
    else:
        monkeypatch.setenv("BYPASS_TOOL_CONSENT", bypass)
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
        agent_id="consent-test",
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    return agent, callback


def _results(agent) -> list[dict]:
    return [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]


def _calls(tmp_path, names):
    calls = []
    if "file_write" in names:
        calls.append(("file_write", {"path": str(tmp_path / "a.md"), "content": "hello"}))
    if "editor" in names:
        calls += [("editor", _editor_input(op, tmp_path / "b.md")) for op in EDITOR_OPS]
    return calls


def _write_tools():
    names = _available()
    if not names:
        pytest.skip("strands_tools file tools are not installed")
    return [_tool(name) for name in names], names


def _assert_nothing_written(tmp_path):
    assert not (tmp_path / "a.md").exists() and not (tmp_path / "b.md").exists()


@pytest.mark.parametrize("bypass", [None, "true"])
def test_normal_level_writes_without_terminal(monkeypatch, tmp_path, bypass):
    tools, names = _write_tools()
    calls = _calls(tmp_path, names)
    agent, _ = _agent(monkeypatch, tools, calls, stability=0.8, bypass=bypass)
    assert str(agent("write")).strip() == "turn done"
    assert [r["status"] for r in _results(agent)] == ["success"] * len(calls)
    if "editor" in names:
        assert "inserted" in (tmp_path / "b.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("bypass", [None, "true"])
def test_hitl_gate_runs_only_when_approved(monkeypatch, tmp_path, bypass):
    """Today's HITL gate (CRITICAL + halt); per-tool Ask of #190 is not implemented yet."""
    tools, names = _write_tools()
    calls = _calls(tmp_path, names)
    agent, callback = _agent(
        monkeypatch,
        tools,
        calls,
        stability=0.1,
        decision=HITLDecision(HITLAction.APPROVE),
        bypass=bypass,
    )
    agent("write")
    assert [c.args[0].tool_name for c in callback.call_args_list] == [n for n, _ in calls]
    assert [r["status"] for r in _results(agent)] == ["success"] * len(calls)


@pytest.mark.parametrize("decision", [HITLDecision(HITLAction.REJECT), None])
def test_hitl_gate_without_approval_never_writes(monkeypatch, tmp_path, decision):
    tools, names = _write_tools()
    calls = _calls(tmp_path, names)
    agent, _ = _agent(monkeypatch, tools, calls, stability=0.1, decision=decision)
    agent("write")
    results = _results(agent)
    assert [r["status"] for r in results] == ["error"] * len(calls)
    assert all("HITL rejected" in _text(r) for r in results)
    _assert_nothing_written(tmp_path)


def test_breaker_warning_blocks_writes(monkeypatch, tmp_path):
    tools, names = _write_tools()
    calls = _calls(tmp_path, names)
    agent, _ = _agent(monkeypatch, tools, calls, stability=0.4)
    agent("write")
    assert [r["status"] for r in _results(agent)] == ["error"] * len(calls)
    _assert_nothing_written(tmp_path)


def test_tool_switched_off_blocks_writes(monkeypatch, tmp_path):
    _, names = _write_tools()
    allowed = ToolConfig(
        mode="custom", enabled_tools={"current_time"}, path=tmp_path / "tc.json"
    ).get_allowed_tools()
    assert all(t.tool_name not in TOOLS for t in allowed)
    calls = _calls(tmp_path, names)
    agent, _ = _agent(monkeypatch, allowed, calls, stability=0.8)
    agent("write")
    assert [r["status"] for r in _results(agent)] == ["error"] * len(calls)
    _assert_nothing_written(tmp_path)


# --- Integration with the write-path policy of #243 (resolved paths, re-wrapping) ---


def _policy_agent(monkeypatch, ws, calls, *, stability=0.8, decision=None):
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    policy = WritePathPolicy(base_dir=ws, write_roots=[WORKSPACE_ROOT])
    # Registry wrapper -> get_tools() wrapper -> NXPAgent's policy wrapper: three layers.
    tools = [
        t
        for t in tool_registry.get_tools(write_policy=policy)
        if getattr(t, "tool_name", None) in TOOLS
    ]
    responses = [
        [{"toolUse": {"toolUseId": f"w{i}", "name": name, "input": tool_input}}]
        for i, (name, tool_input) in enumerate(calls)
    ]
    agent = NXPAgent(
        model=ScriptedModel([*responses, [{"text": "turn done"}]]),
        state=NeuralState(stability=stability),
        config=StiraConfig(critical_action="halt"),
        on_hitl_request=None if decision is None else Mock(return_value=decision),
        tools=tools,
        write_policy=policy,
        agent_id="consent-policy-test",
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    return agent


def test_resolved_relative_paths_run_once_through_nested_wrappers(monkeypatch, tmp_path):
    """#243 hands the tool the resolved path after the gate hashed the original call."""
    _, names = _write_tools()
    ws = tmp_path / "ws"
    ws.mkdir()
    calls = []
    if "file_write" in names:
        calls.append(("file_write", {"path": "rel/a.md", "content": "hello"}))
    if "editor" in names:
        calls += [("editor", {**op, "path": "rel/b.md"}) for op in EDITOR_OPS]
    monkeypatch.chdir(tmp_path)  # not the workspace: relative paths must not land here
    agent = _policy_agent(monkeypatch, ws, calls)
    agent("write")
    assert [r["status"] for r in _results(agent)] == ["success"] * len(calls)
    if "file_write" in names:
        assert (ws / "rel" / "a.md").read_text(encoding="utf-8") == "hello"
    if "editor" in names:
        assert "inserted" in (ws / "rel" / "b.md").read_text(encoding="utf-8")
    assert not (tmp_path / "rel").exists()
    nested = next(t for t in agent.tools if t.tool_name == names[0])
    assert isinstance(nested.wrapped, IdentityProtectedTool)
    assert isinstance(nested.wrapped.wrapped, IdentityProtectedTool)


def test_path_denial_at_hitl_gate_leaves_no_mark(monkeypatch, tmp_path):
    """Outside the write roots at CRITICAL: denied before asking a human, no mark issued."""
    _tool("file_write")
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.md"
    agent = _policy_agent(
        monkeypatch,
        ws,
        [("file_write", {"path": str(outside), "content": "x"})],
        stability=0.1,
        decision=HITLDecision(HITLAction.APPROVE),
    )
    agent("write")
    [result] = _results(agent)
    assert result["status"] == "error" and "write roots" in _text(result)
    assert not outside.exists()


def test_policy_wrapper_refuses_unmarked_and_replayed_calls(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    agent = _policy_agent(monkeypatch, ws, [])
    tool = next(t for t in agent.tools if t.tool_name == "file_write")
    use = _use("file_write", {"path": "x.md", "content": "x"})
    assert NO_GATE in _text(asyncio.run(_collect(tool, use)))
    state: dict = {}
    mark_gate_passed(state, use)
    assert asyncio.run(_collect(tool, use, state))["status"] == "success"
    (ws / "x.md").unlink()
    assert NO_GATE in _text(asyncio.run(_collect(tool, use, state)))
    assert not (ws / "x.md").exists()


def test_registry_fail_closed_with_write_policy(monkeypatch, tmp_path):
    _tool("file_write")
    monkeypatch.delattr(_module("file_write"), "get_user_input")
    policy = WritePathPolicy(base_dir=tmp_path, write_roots=[WORKSPACE_ROOT])
    names = {t.tool_name for t in tool_registry.get_tools(write_policy=policy)}
    assert "file_write" not in names
