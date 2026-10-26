"""#145: safety contract per tool execution path (standard / MCP / delegation).

Every case runs a real NXPAgent turn against the offline FakeModel, so the
Strands tool loop, NXP filtering, CheckpointHooks (HITL) and IdentityGuard all
take part. Expected outcomes were measured, then fixed here. Gaps that are not
fixed yet are strict xfails; they are listed in docs/tool-execution-paths.md.

Set STIRA_CONTRACT_REPORT=<file> to write the measured matrix as JSON (CI
uploads it as an artifact).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from provider_fakes import FakeModel, text_turn, tool_turn
from strands.tools.registry import ToolProvider
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool

from stiracore.agent.delegation import register_subagent_tool
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.subagent import create_nxp_subagent
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig
from stiracore.middleware.identity_guard import IdentityGuard
from stiracore.tools import tool_registry

LEVELS = {"normal": 0.9, "warning": 0.4, "critical": 0.1}
_REPORT: list[dict[str, Any]] = []


class RecordingTool(AgentTool):
    """Writes ``injected`` to input.path and records the call."""

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
        path = Path(tool_use["input"]["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("injected", encoding="utf-8")
        self.calls.append((self._name, str(path)))
        yield ToolResultEvent(
            {"toolUseId": tool_use["toolUseId"], "status": "success", "content": [{"text": "ok"}]}
        )


class FakeMcp(ToolProvider):
    def __init__(self, tools: list[AgentTool]) -> None:
        self.tools = tools

    async def load_tools(self, **kwargs: Any) -> list[AgentTool]:
        return list(self.tools)

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        pass

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    monkeypatch.delenv(tool_registry.EXEC_ENV_VAR, raising=False)
    snapshot = (
        dict(tool_registry.READ_TOOLS),
        dict(tool_registry.WRITE_TOOLS),
        dict(tool_registry._MCP_REGISTRIES),
    )
    yield
    for target, saved in zip(
        (tool_registry.READ_TOOLS, tool_registry.WRITE_TOOLS, tool_registry._MCP_REGISTRIES),
        snapshot,
        strict=True,
    ):
        target.clear()
        target.update(saved)


@pytest.fixture(scope="module", autouse=True)
def _write_report():
    yield
    target = os.environ.get("STIRA_CONTRACT_REPORT")
    if target:
        Path(target).write_text(
            json.dumps({"issue": 145, "cases": _REPORT}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _quiet() -> Mock:
    return Mock(classify=lambda **kwargs: None)


def _outcome(agent: NXPAgent, model: FakeModel, calls: list, name: str, hitl: Mock | None):
    results = [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    text = results[0]["content"][0].get("text", "") if results else ""
    offered = name in model.requests[0]["tool_names"]
    if calls:
        verdict = "executed"
    elif not offered:
        verdict = "filtered"
    elif "read-only" in text:
        verdict = "blocked:identity"
    elif "HITL" in text:
        verdict = "blocked:hitl"
    elif "circuit breaker" in text or "confirmation" in text:
        verdict = "blocked:breaker"
    else:
        verdict = f"other:{text[:40]}"
    return {
        "verdict": verdict,
        "offered": offered,
        "hitl_asked": bool(hitl and hitl.called),
        "message": text[:80],
        # A denial must be a well-formed error toolResult the model can read.
        "result_status": results[0].get("status") if results else None,
    }


def _run(level, tools, name, path, *, hitl=None, critical_action="halt", read_only=()):
    model = FakeModel([tool_turn(name, {"path": str(path)}), text_turn("done")])
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=LEVELS[level]),
        config=StiraConfig(critical_action=critical_action),
        tools=tools,
        on_hitl_request=hitl,
        read_only_paths=read_only,
        stimulus_classifier=_quiet(),
    )
    agent("go")
    return agent, model


def _approve() -> Mock:
    return Mock(return_value=HITLDecision(HITLAction.APPROVE))


def _record(case: str, path: str, level: str, outcome: dict, expected: str) -> None:
    _REPORT.append({"case": case, "path": path, "level": level, "expected": expected, **outcome})


# --- Standard tools (tool_registry.get_tools + IdentityProtectedTool) --------

STANDARD = [
    # case, level, target, hitl, critical_action, expected verdict, HITL asked
    ("std-normal-write", "normal", "notes/x.md", None, "halt", "executed", False),
    ("std-normal-soul", "normal", "SOUL.md", None, "halt", "blocked:identity", False),
    (
        "std-normal-procedural",
        "normal",
        "procedural/rule.md",
        None,
        "halt",
        "blocked:identity",
        False,
    ),
    ("std-warning-write", "warning", "notes/x.md", None, "halt", "filtered", False),
    ("std-critical-halt-nohuman", "critical", "notes/x.md", None, "halt", "blocked:hitl", False),
    ("std-critical-halt-approved", "critical", "notes/x.md", "approve", "halt", "executed", True),
    (
        "std-critical-approved-soul",
        "critical",
        "SOUL.md",
        "approve",
        "halt",
        "blocked:identity",
        False,  # #243: the HITL gate shares the path decision; no futile prompt
    ),
    ("std-critical-readonly", "critical", "notes/x.md", None, "readonly", "filtered", False),
]


@pytest.mark.parametrize(
    ("case", "level", "target", "hitl", "action", "expected", "asked"),
    STANDARD,
    ids=[row[0] for row in STANDARD],
)
def test_standard_path(monkeypatch, tmp_path, case, level, target, hitl, action, expected, asked):
    calls: list = []
    monkeypatch.setitem(tool_registry.WRITE_TOOLS, "file_write", RecordingTool("file_write", calls))
    # chat server wiring (#144): procedural subtree protected at the execution boundary.
    tools = tool_registry.get_tools(protected_dirs=[str(tmp_path / "procedural")])
    callback = _approve() if hitl else None
    agent, model = _run(level, tools, "file_write", tmp_path / target, hitl=callback,
                        critical_action=action)  # fmt: skip
    outcome = _outcome(agent, model, calls, "file_write", callback)
    _record(case, "standard", level, outcome, expected)
    assert outcome["verdict"] == expected, outcome
    assert outcome["hitl_asked"] is asked
    if expected != "executed":
        assert not (tmp_path / target).exists()
    if expected.startswith("blocked"):
        assert outcome["result_status"] == "error"


# --- MCP tools (register_mcp_tools; unknown names default to WRITE) ---------

MCP = [
    ("mcp-normal-write", "normal", "put_object", "notes/x.md", None, "executed"),
    # Fixed in #145: IdentityGuard now treats live WRITE_TOOLS (incl. MCP) as writes.
    ("mcp-normal-soul", "normal", "put_object", "SOUL.md", None, "blocked:identity"),
    ("mcp-warning-write", "warning", "put_object", "notes/x.md", None, "filtered"),
    ("mcp-critical-nohuman", "critical", "put_object", "notes/x.md", None, "blocked:hitl"),
    ("mcp-critical-approved", "critical", "put_object", "notes/x.md", "approve", "executed"),
    ("mcp-critical-declared-read", "critical", "get_object", "notes/x.md", None, "executed"),
]


@pytest.mark.parametrize(
    ("case", "level", "name", "target", "hitl", "expected"), MCP, ids=[r[0] for r in MCP]
)
def test_mcp_path(tmp_path, case, level, name, target, hitl, expected):
    calls: list = []
    provider = FakeMcp([RecordingTool("put_object", calls), RecordingTool("get_object", calls)])
    registered = tool_registry.register_mcp_tools(provider, read_tool_names={"get_object"})
    assert set(registered) == {"put_object", "get_object"}
    callback = _approve() if hitl else None
    try:
        agent, model = _run(
            level, tool_registry.get_tools(), name, tmp_path / target, hitl=callback
        )
    finally:
        tool_registry.close_mcp_tools(provider)
    outcome = _outcome(agent, model, calls, name, callback)
    _record(case, "mcp", level, outcome, expected)
    assert outcome["verdict"] == expected, outcome


# --- Delegation (register_subagent_tool: child has its own engine/breaker) ---

DELEGATION = [
    # case, parent level, child level, target, expected child-tool verdict
    ("deleg-normal-normal", "normal", "normal", "notes/x.md", "executed"),
    ("deleg-child-soul", "normal", "normal", "SOUL.md", "blocked:identity"),
    ("deleg-child-warning", "normal", "warning", "notes/x.md", "blocked:breaker"),
    ("deleg-child-critical", "normal", "critical", "notes/x.md", "blocked:halted-child"),
    ("deleg-parent-critical", "critical", "normal", "notes/x.md", "blocked:hitl"),
]


@pytest.mark.parametrize(
    ("case", "parent_level", "child_level", "target", "expected"),
    DELEGATION,
    ids=[r[0] for r in DELEGATION],
)
def test_delegation_path(tmp_path, case, parent_level, child_level, target, expected):
    calls: list = []
    path = tmp_path / target
    script = [tool_turn("worker", {"input": "do it"}, "d1")]
    if parent_level == "normal" and child_level != "critical":
        script += [tool_turn("file_write", {"path": str(path)}, "c1"), text_turn("child done")]
    script.append(text_turn("parent done"))
    model = FakeModel(script)
    parent = NXPAgent(
        model=model,
        state=NeuralState(stability=LEVELS[parent_level]),
        config=StiraConfig(critical_action="halt"),
        stimulus_classifier=_quiet(),
    )
    child = create_nxp_subagent(
        parent,
        "task",
        name="worker",
        state=NeuralState(stability=LEVELS[child_level]),
        tools=[RecordingTool("file_write", calls)],
        stimulus_classifier=_quiet(),
    )
    register_subagent_tool(parent, child)
    assert "worker" in parent.config.blocked_tool_names  # delegation is write-capable
    parent("go")

    def results(agent):
        return {
            block["toolResult"]["toolUseId"]: block["toolResult"]
            for message in agent.agent.messages
            for block in message["content"]
            if "toolResult" in block
        }

    delegate = results(parent)["d1"]
    delegate_text = delegate["content"][0].get("text", "")
    child_text = (results(child).get("c1") or {"content": [{"text": ""}]})["content"][0]["text"]
    if calls:
        verdict = "executed"
    elif "halted this child" in delegate_text:
        verdict = "blocked:halted-child"
    elif "HITL" in delegate_text:
        verdict = "blocked:hitl"
    elif "read-only" in child_text:
        verdict = "blocked:identity"
    elif "confirmation" in child_text or "circuit breaker" in child_text:
        verdict = "blocked:breaker"
    else:
        verdict = f"other:{delegate_text[:40]}|{child_text[:40]}"
    outcome = {"verdict": verdict, "message": (child_text or delegate_text)[:80]}
    _record(case, "delegation", f"{parent_level}/{child_level}", outcome, expected)
    assert verdict == expected, outcome
    if expected != "executed":
        assert not path.exists()


# --- Unknown / unclassified tools -------------------------------------------


def test_write_shaped_unregistered_tool_is_denied_at_warning(tmp_path):
    """Fixed in #145: heuristic writes no longer run unconfirmed at WARNING."""
    calls: list = []
    tool = RecordingTool("notes_append", calls)
    agent, model = _run("warning", [tool], "notes_append", tmp_path / "x.md")
    outcome = _outcome(agent, model, calls, "notes_append", None)
    _record("unknown-writeshaped-warning", "unknown", "warning", outcome, "blocked:breaker")
    assert outcome["verdict"] == "blocked:breaker" and outcome["offered"]


def test_write_shaped_unregistered_tool_at_critical_is_denied_without_hitl(tmp_path):
    """Over-block (fail-closed): CheckpointHooks asks HITL only for registered names,
    IdentityGuard also blocks write-shaped names, so approval is never offered."""
    calls: list = []
    callback = _approve()
    agent, model = _run(
        "critical", [RecordingTool("notes_append", calls)], "notes_append", tmp_path / "x.md",
        hitl=callback,
    )  # fmt: skip
    outcome = _outcome(agent, model, calls, "notes_append", callback)
    _record("unknown-writeshaped-critical-hitl", "unknown", "critical", outcome, "blocked:breaker")
    assert outcome["verdict"] == "blocked:breaker"
    assert outcome["hitl_asked"] is False


def _unknown_at_critical(tmp_path):
    calls: list = []
    agent, model = _run("critical", [RecordingTool("do_thing", calls)], "do_thing", tmp_path / "x")
    return _outcome(agent, model, calls, "do_thing", None)


def test_unclassified_tool_runs_at_critical_measured(tmp_path):
    """Measured current behaviour, recorded for the CI report (fail-open)."""
    outcome = _unknown_at_critical(tmp_path)
    _record("unknown-unclassified-critical", "unknown", "critical", outcome, "UNPROTECTED")
    assert outcome["verdict"] == "executed"


@pytest.mark.xfail(
    strict=True,
    reason="#145: an unclassified tool (no registry entry, no write-shaped name) is "
    "treated as read and runs at CRITICAL; fail-closed needs a read allowlist "
    "(memory_read/search etc. are not in READ_TOOLS) — policy decision pending",
)
def test_unclassified_tool_is_not_executed_at_critical(tmp_path):
    assert _unknown_at_critical(tmp_path)["verdict"] != "executed"


def test_think_is_exec_class_and_hidden_by_default():
    """think re-runs parent tools in a bare inner Agent (no NXP hooks): exec-gated."""
    assert "think" in tool_registry.EXEC_TOOL_NAMES
    assert "think" not in {getattr(t, "tool_name", None) for t in tool_registry.get_tools()}


def _think_bypass(tmp_path, *, approved: bool = True) -> dict:
    if "think" not in tool_registry.WRITE_TOOLS:
        pytest.skip("strands_tools.think unavailable")
    calls: list = []
    soul = tmp_path / "SOUL.md"
    model = FakeModel(
        [
            tool_turn("think", {"thought": "x", "cycle_count": 1, "system_prompt": "s"}),
            tool_turn("notes_append", {"path": str(soul)}, "inner-1"),
            text_turn("inner"),
            text_turn("done"),
        ]
    )
    tools = [tool_registry.WRITE_TOOLS["think"], RecordingTool("notes_append", calls)]
    # Since #362 an exec tool runs only inside a human approval of the call: approve
    # ``think`` itself (Ask rule) to measure what its inner agent does.
    agent = NXPAgent(
        model=model,
        tools=tools,
        stimulus_classifier=_quiet(),
        tool_policy=lambda name: "ask" if name == "think" else "always",
        on_hitl_request=Mock(return_value=HITLDecision(HITLAction.APPROVE)) if approved else None,
    )
    agent("go")
    outcome = {"verdict": "executed" if calls else "blocked", "soul_written": soul.exists()}
    if approved:
        _record("think-inner-agent-soul", "delegation(think)", "normal", outcome, "UNPROTECTED")
    return outcome


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_think_inner_agent_bypasses_identity_guard_measured(tmp_path):
    """Only reachable with STIRA_ENABLE_EXEC_TOOLS and an approved ``think``: the guard
    hook does not run inside its inner agent."""
    assert _think_bypass(tmp_path) == {"verdict": "executed", "soul_written": True}


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_think_without_a_human_approval_does_not_run(tmp_path):
    """#362: no approver connected, so ``think`` (and its inner agent) never runs."""
    assert _think_bypass(tmp_path, approved=False) == {"verdict": "blocked", "soul_written": False}


# --- Name-based classification inventory (over-blocking) --------------------

# Every registered write tool needs a reason. "over-block" marks candidates that
# do not mutate anything; they are blocked at WARNING/CRITICAL by name only.
WRITE_INVENTORY = {
    "file_write": "filesystem write",
    "editor": "filesystem write (view/find_line are exempted by editor_read_only)",
    "shell": "exec",
    "batch": "exec (runs other tools)",
    "cron": "exec (system crontab)",
    "use_agent": "delegation (tool-using agent)",
    "swarm": "delegation (tool-using agents)",
    "graph": "delegation (tool-using agents)",
    "think": "delegation (re-runs parent tools without NXP hooks)",
    "http_request": "network egress",
    "web_fetch": "network egress",
    "web_search": "network egress",
    "webhook_send": "network egress",
    "oauth_api_call": "network egress",
    "memory": "remote knowledge-base write",
    "journal": "local persistence",
    "rss": "network + local persistence",
    "diagram": "writes image files",
    "page_check": "runs the agent's page in a closed browser, writes a screenshot (ADR 0015)",
    "run_checks": "runs the project's checks in a throwaway copy (ADR 0019; opt-in)",
    "speak": "audio subprocess / Polly network",
    "lsp_diagnostics": "over-block: read-only analysis (pyright/ruff subprocess)",
    "lsp_hover": "over-block: read-only analysis (jedi/AST)",
    "memory_write": "memory layer write",
    "propose_procedure": "memory layer write (procedural/staging only, #463)",
    "memory_delete": "memory layer write",
}


def test_every_registered_write_tool_is_classified():
    unclassified = set(tool_registry.WRITE_TOOLS) - set(WRITE_INVENTORY)
    assert not unclassified, f"classify these in WRITE_INVENTORY and the docs: {unclassified}"
    over = sorted(n for n, why in WRITE_INVENTORY.items() if why.startswith("over-block"))
    _REPORT.append({"case": "inventory", "over_block_candidates": over})
    assert over == ["lsp_diagnostics", "lsp_hover"]


@pytest.mark.parametrize(
    ("name", "write_shaped"),
    [
        # Reads correctly left alone by the name heuristic.
        ("file_read", False),
        ("memory_search", False),
        ("lsp_hover", False),
        ("think", False),
        ("current_time", False),
        ("fetch_page", False),
        # Writes caught by name.
        ("write_file", True),
        ("apply_patch", True),
        ("deleteRecord", True),
        ("run_bash", True),
        # Over-blocks: pure functions whose names contain a write verb.
        ("edit_distance", True),
        ("create_summary", True),
        ("remove_stopwords", True),
        ("shell_quote", True),
        ("move_cursor_hint", True),
    ],
)
def test_name_heuristic_inventory(name, write_shaped):
    assert IdentityGuard._is_write_tool(name) is write_shaped
