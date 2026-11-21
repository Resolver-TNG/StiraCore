"""Gate equivalence (#795 S0, finding F4): the same tool x the same rule x the same breaker
level, once in a chat turn and once in a grant iteration.

Both sides run end to end with a scripted provider (offline): the model calls the tool
once, and what happened is read from outside —

* ``ask``: an approval was requested (the chat engine's ``on_hitl_request``; the grant's
  HITL broker). Both answer REJECT, so nothing runs;
* ``allow``: the tool's result came back to the model with status ``success``;
* ``refuse``: neither (the call was cancelled, the tool was not offered, or the grant
  admitted no iteration).

Chat side: a real ``NXPAgent`` (tool list filter, ``CheckpointHooks``, ``IdentityGuard``,
the binding verifier). Grant side: a real ``GoalIterationExecutor`` and its child
(``IterationGuard``, the child's ``CheckpointHooks``, ``GrantTool`` / ``_check_tool``,
the permits), with the grant service's breaker reader at the same level.

A case where the two differ is **not fixed here**: it is listed in ``DIFFERENCES`` and
kept as ``xfail(strict=True)``, so a change on either side that moves it is noticed.
"""

from __future__ import annotations

import pytest
from grants_exec_helpers import Broker, ScriptedProvider, make_env, server_tools, use

from stiracore.core import NeuralState, StiraConfig
from stiracore.core.engine import HITLAction, HITLDecision
from stiracore.grants.schema import GrantRefused

TOOLS = ("file_read", "editor")
RULES = ("always", "ask", "never")
# level -> (the chat agent's stability, its ``critical_action``, the grant's breaker level)
LEVELS = {
    "normal": (0.7, "readonly", "normal"),
    "warning": (0.4, "readonly", "warning"),
    "critical": (0.2, "readonly", "critical"),
    "critical_halt": (0.2, "halt", "critical"),
}

LEVEL_NAMES = tuple(LEVELS)
# What each side decides today, read from the runs below (``test_each_side_decides_as_
# recorded`` pins it, so two sides that both refuse for an unrelated reason do not pass
# as "equal"). Per tool and rule: one verdict per level, in the order of ``LEVELS``.
CHAT = {
    ("file_read", "always"): ("allow", "allow", "allow", "allow"),
    ("file_read", "ask"): ("ask", "ask", "ask", "ask"),
    ("file_read", "never"): ("refuse", "refuse", "refuse", "refuse"),
    ("editor", "always"): ("allow", "refuse", "refuse", "ask"),
    ("editor", "ask"): ("ask", "refuse", "refuse", "ask"),
    ("editor", "never"): ("refuse", "refuse", "refuse", "refuse"),
}
# The grant: the rule decides at NORMAL; at any other level no iteration is admitted.
GRANT = {
    (tool, rule): (verdict, "refuse", "refuse", "refuse")
    for tool in TOOLS
    for rule, verdict in (("always", "allow"), ("ask", "ask"), ("never", "refuse"))
}
# (tool, rule, level) -> why the two gates differ today (found by this test, not fixed).
# In every one the grant is the stricter side: it admits no iteration unless the breaker
# is NORMAL (``GoalIterationExecutor._admissible``), while the chat turn still runs reads
# (WARNING, CRITICAL) and sends writes to a person (CRITICAL with ``halt``).
DIFFERENCES = {
    (tool, rule, level): f"chat={CHAT[tool, rule][n]}, grant=refuse (the grant is stricter)"
    for (tool, rule) in CHAT
    for n, level in enumerate(LEVEL_NAMES)
    if CHAT[tool, rule][n] != GRANT[tool, rule][n]
}


def _call(tool: str, workspace) -> dict:
    target = workspace / "proj" / "notes.md"
    if tool == "editor":
        return use("editor", {"command": "create", "path": str(target), "file_text": "x\n"})
    return use("file_read", {"path": str(target)})


def _result_status(provider: ScriptedProvider, tool: str) -> str | None:
    """The status of the call's result as the model saw it (``None``: no result came)."""
    wanted = f"tu-{tool}-1"
    for messages in provider.seen:
        for message in messages:
            for block in message.get("content", []):
                result = block.get("toolResult") if isinstance(block, dict) else None
                if result and result.get("toolUseId") == wanted:
                    return result.get("status")
    return None


def _verdict(asked: bool, status: str | None) -> str:
    if asked:
        return "ask"
    return "allow" if status == "success" else "refuse"


def chat_verdict(tmp_path, tool: str, rule: str, level: str) -> str:
    from stiracore.agent.nxp_agent import NXPAgent
    from stiracore.agent.stimulus_classifier import RuleBasedClassifier
    from stiracore.identity_paths import WritePathPolicy

    stability, critical_action, _ = LEVELS[level]
    workspace = tmp_path / "chat-ws"
    (workspace / "proj").mkdir(parents=True)
    (workspace / "proj" / "seed.md").write_text("seed\n", encoding="utf-8")
    if tool == "file_read":
        (workspace / "proj" / "notes.md").write_text("x\n", encoding="utf-8")
    policy = WritePathPolicy(base_dir=workspace, write_roots=[workspace])
    provider = ScriptedProvider([[_call(tool, workspace)], [{"text": "ok"}]])
    asked = []

    def hitl(request):
        asked.append(request.tool_name)
        return HITLDecision(HITLAction.REJECT, reason="test")

    agent = NXPAgent(
        model=provider,
        state=NeuralState(stability=stability),
        config=StiraConfig(critical_action=critical_action),
        tools=server_tools(policy, run_checks=False),
        tool_policy=lambda name: rule if name == tool else "never",
        write_policy=policy,
        on_hitl_request=hitl,
        stimulus_classifier=RuleBasedClassifier(),
        callback_handler=None,
    )
    agent("go")
    return _verdict(bool(asked), _result_status(provider, tool))


def grant_verdict(tmp_path, tool: str, rule: str, level: str) -> str:
    def answer(request):
        return HITLDecision(HITLAction.REJECT, request_id=request.request_id, reason="test")

    env = make_env(
        tmp_path, tools=("file_read", "editor"), rules={tool: rule}, broker=Broker(answer=answer)
    )
    if tool == "file_read":
        (env.sub / "notes.md").write_text("x\n", encoding="utf-8")
    env.provider.script = [[_call(tool, env.ws)], [{"text": "ok"}]]
    env.breaker.level = LEVELS[level][2]
    try:
        env.executor.step(env.grant_id)
    except GrantRefused:
        return "refuse"  # no iteration: no model call, no tool call
    assert env.executor.wait_idle(20), "the iteration did not end"
    return _verdict(bool(env.broker.requests), _result_status(env.provider, tool))


def _cases():
    for tool in TOOLS:
        for rule in RULES:
            for level in LEVELS:
                why = DIFFERENCES.get((tool, rule, level))
                marks = [pytest.mark.xfail(strict=True, reason=why)] if why else []
                yield pytest.param(tool, rule, level, marks=marks, id=f"{tool}-{rule}-{level}")


@pytest.mark.parametrize(("tool", "rule", "level"), list(_cases()))
def test_the_chat_gate_and_the_grant_gate_decide_alike(tmp_path, tool, rule, level):
    chat = chat_verdict(tmp_path, tool, rule, level)
    granted = grant_verdict(tmp_path, tool, rule, level)
    assert chat == granted, f"{tool} x {rule} x {level}: chat={chat} grant={granted}"


@pytest.mark.parametrize(("tool", "rule"), sorted(CHAT))
@pytest.mark.parametrize("level", LEVEL_NAMES)
def test_each_side_decides_as_recorded(tmp_path, tool, rule, level):
    n = LEVEL_NAMES.index(level)
    assert chat_verdict(tmp_path, tool, rule, level) == CHAT[tool, rule][n]
    assert grant_verdict(tmp_path, tool, rule, level) == GRANT[tool, rule][n]


def test_the_recorded_differences_are_only_ones_where_the_grant_is_stricter():
    assert len(DIFFERENCES) == 8
    for tool, rule, level in DIFFERENCES:
        n = LEVEL_NAMES.index(level)
        assert GRANT[tool, rule][n] == "refuse" and CHAT[tool, rule][n] in ("allow", "ask")
