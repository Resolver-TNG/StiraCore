"""Shared helpers for the W5 executor tests: a scripted provider, a fake run_checks, an
approval broker double, and an executor over a real grant service and a real NXP parent.
Offline: no model, no Bedrock."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from grants_helpers import confirm_and_issue, issue_body, make_service
from strands.models.model import Model
from strands.tools.tools import AgentTool
from strands.types._events import ToolResultEvent

MARKER = "goalmarker-9f3c2a7e5b1d4c60aa"


class ScriptedProvider(Model):
    """Each call pops one script entry: a list of blocks (``{"text"}`` / ``{"toolUse"}``)
    or a callable ``(messages) -> blocks``. Records the final input of every call."""

    # Its stream is its one request (the executor accepts only such declared models).
    stira_grant_terminal = True

    def __init__(self, script=None, *, max_tokens: int = 50, usage: int | None = 7) -> None:
        self.script = list(script or [])
        self.seen: list[list] = []
        self.max_tokens = max_tokens
        self.usage = usage
        self.calls = 0
        self.on_call = None  # (n) -> None, called before the reply streams

    def get_config(self):
        return {"model_id": "offline", "max_tokens": self.max_tokens}

    def update_config(self, **kwargs):
        pass

    def format_request(
        self, messages, tool_specs=None, system_prompt_content=None, tool_choice=None, **_kw
    ):
        """The request as this provider would send it (what the executor sizes)."""
        request = {
            "modelId": "offline",
            "messages": messages,
            "system": system_prompt_content or [],
            "toolConfig": {"tools": [{"toolSpec": spec} for spec in tool_specs or []]},
        }
        if self.max_tokens is not None:
            request["inferenceConfig"] = {"maxTokens": self.max_tokens}
        return request

    async def structured_output(self, *args, **kwargs):
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls += 1
        self.seen.append(json.loads(json.dumps(messages, default=str)))
        if self.on_call is not None:
            self.on_call(self.calls)
        entry = self.script.pop(0) if self.script else [{"text": "nothing to do"}]
        blocks = entry(messages) if callable(entry) else entry
        yield {"messageStart": {"role": "assistant"}}
        reason = "end_turn"
        for block in blocks:
            if "toolUse" in block:
                reason = "tool_use"
                use = block["toolUse"]
                yield {
                    "contentBlockStart": {
                        "start": {"toolUse": {"name": use["name"], "toolUseId": use["toolUseId"]}}
                    }
                }
                yield {
                    "contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(use["input"])}}}
                }
                yield {"contentBlockStop": {}}
            else:
                yield {"contentBlockDelta": {"delta": {"text": block["text"]}}}
                yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": reason}}
        if self.usage is not None:
            yield {
                "metadata": {
                    "usage": {
                        "inputTokens": self.usage,
                        "outputTokens": 3,
                        "totalTokens": self.usage + 3,
                    },
                    "metrics": {"latencyMs": 1},
                }
            }


def use(name: str, tool_input: dict, n: int = 1) -> dict:
    return {"toolUse": {"name": name, "toolUseId": f"tu-{name}-{n}", "input": tool_input}}


class FakeRunChecksTool(AgentTool):
    """Only the name and spec matter: the executor runs ``run_checks_runner``."""

    @property
    def tool_name(self):
        return "run_checks"

    @property
    def tool_spec(self):
        return {
            "name": "run_checks",
            "description": "run checks",
            "inputSchema": {"json": {"type": "object", "properties": {"path": {"type": "string"}}}},
        }

    @property
    def tool_type(self):
        return "python"

    async def stream(self, tool_use, invocation_state, **kwargs):  # pragma: no cover
        raise AssertionError("the executor must not call the wrapped run_checks")
        yield ToolResultEvent({})


class Host:
    def __init__(self, verified: bool = True) -> None:
        self.verified = verified


class Counter:
    """A request estimator: fixed per call; ``broken`` raises (no usable bound)."""

    def __init__(self, tokens: int = 10) -> None:
        self.tokens = tokens
        self.broken = False
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        if self.broken:
            raise RuntimeError("count")
        return self.tokens


class Broker:
    """The HITL broker double: ``answer(request) -> HITLDecision`` decides each card."""

    def __init__(self, answer=None) -> None:
        self.answer = answer
        self.requests: list[Any] = []
        self.withdrawn: list[str] = []
        self.release = threading.Event()

    def request(self, request, **_kwargs):
        from stiracore.core import hitl_reasons
        from stiracore.core.engine import HITLAction, HITLDecision

        self.requests.append(request)
        if self.answer is not None:
            return self.answer(request)
        # Unanswered: wait (on a signal) until withdrawn or "expired" by the test.
        self.release.wait(10)
        if request.request_id in self.withdrawn:
            return HITLDecision(HITLAction.REJECT, reason="withdrawn")
        return HITLDecision(HITLAction.REJECT, reason=hitl_reasons.EXPIRED)

    def withdraw(self, request_id):
        self.withdrawn.append(request_id)
        self.release.set()


def make_parent(tmp_path: Path, workspace: Path, *, rules: dict | None = None):
    from stiracore.agent.nxp_agent import NXPAgent
    from stiracore.identity_paths import WritePathPolicy

    rules = {
        "editor": "always",
        "file_read": "always",
        "run_checks": "always",
        "current_time": "always",
        **(rules or {}),
    }
    policy = WritePathPolicy(base_dir=workspace, write_roots=[workspace])
    parent = NXPAgent(
        model=ScriptedProvider(),
        tools=[],
        tool_policy=lambda name: rules.get(name, "never"),
        write_policy=policy,
        callback_handler=None,
    )
    return parent, policy, rules


def server_tools(policy, *, run_checks: bool = True):
    from stiracore.tools import tool_registry

    tools = list(tool_registry.get_tools(None, write_policy=policy))
    if run_checks:
        tools.append(FakeRunChecksTool())
    return tools


class Env:
    pass


def make_env(
    tmp_path: Path,
    *,
    script=None,
    tools=("file_read", "editor", "run_checks"),
    rules=None,
    broker=None,
    total=10_000,
    per_tick=2_000,
    probe=None,
    contained=True,
    wall=600,
    max_iterations=10,
    stop_drain_sec=2.0,
    **service_kw,
):
    from stiracore.grants.executor import GoalIterationExecutor
    from stiracore.grants.service import AutonomySettings

    service, authority, recorder, clocks, breaker, ws = make_service(
        tmp_path, contained=contained, **service_kw
    )
    service.settings = AutonomySettings(
        stage_c=True, allow_browser_confirm=True, stage_c_exec=True, iteration_wall_sec=wall
    )
    sub = ws / "proj"
    sub.mkdir(exist_ok=True)
    parent, policy, rules = make_parent(tmp_path, ws, rules=rules)
    env = Env()
    env.service, env.authority, env.recorder, env.clocks, env.breaker = (
        service,
        authority,
        recorder,
        clocks,
        breaker,
    )
    env.ws, env.sub, env.parent, env.policy, env.rules = ws, sub, parent, policy, rules
    env.provider = ScriptedProvider(script)
    env.counter = Counter()
    env.host = Host(True)
    env.checks: list = []
    env.broker = broker or Broker(answer=None)
    env.busy = False
    env.mono = [1000.0]

    def runner(arguments, run_policy, host=None):
        env.checks.append((dict(arguments), host))
        return {"status": "passed", "checks": [], "reason": None}

    env.executor = GoalIterationExecutor(
        service,
        authority,
        parent=lambda: parent,
        tool_provider=lambda names: server_tools(policy),
        broker=env.broker,
        recorder=recorder,
        turn_busy=lambda: env.busy,
        estimator=env.counter,
        inner_model=lambda _parent: env.provider,
        run_checks_runner=runner,
        containment=lambda: env.host,
        run_checks_enabled=lambda: True,
        probe=probe,
        mono=lambda: env.mono[0],
        stop_drain_sec=stop_drain_sec,
    )
    body = issue_body(
        sub,
        goal=f"tidy the notes {MARKER}",
        scope={"tools": list(tools), "workspace_subtree": str(sub)},
        budget={"total_tokens": total, "per_tick_tokens": per_tick},
        max_iterations=max_iterations,
    )
    env.grant_id = confirm_and_issue(service, body)["grant"]["grant_id"]
    return env


def run_one(env, timeout: float = 20.0) -> dict:
    """Press once and wait (on the executor's signal) until the iteration ended."""
    env.executor.step(env.grant_id)
    assert env.executor.wait_idle(timeout), "the iteration did not end"
    return env.executor.view(env.grant_id)


def grant(env):
    return env.service._get(env.grant_id)
