"""Isolated NXP children with a native Strands streaming execution surface.

Issues #56/#57: never share a parent's NeuralState, engine, breaker, history,
or prompt-provider closure. Only explicit tools and a copied policy cross the
boundary. Each child owns its SDK ``agent.state`` namespace as well.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from copy import deepcopy
from threading import Lock
from typing import Any
from uuid import uuid4

from strands import Agent
from strands.tools.registry import ToolRegistry

from stiracore.agent.checkpoint_hooks import read_policy
from stiracore.agent.nxp_agent import NXPAgent, _tool_name
from stiracore.agent.state_persistence import _state_dict
from stiracore.core import BreakAction, NeuralState, StiraConfig

_LOG = logging.getLogger(__name__)


class GuardedDelegate(Agent):
    """An actual SDK Agent usable by as_tool, make_subagent, Graph and Swarm.

    Streaming goes through the same NXP turn policy as synchronous execution;
    returning the bare inner Agent would silently bypass the child's breaker.
    SDK orchestration may reset JSON state/history, but cannot reset the live
    NXP engine or breaker. One delegate permits only one in-flight turn.
    """

    def __init__(self, owner: NXPSubagent) -> None:
        self.owner = owner
        self._nxp_lock = Lock()
        super().__init__(
            model=owner.model,
            tools=list(owner.tools),
            system_prompt=owner.system_prompt,
            agent_id=owner.agent_id,
            name=owner.name,
            callback_handler=None,
            hooks=[
                # Checks that must decide before the gate (``delegate``'s limits, #486).
                *owner.pre_hooks,
                owner.checkpoint_hooks,
                owner.identity_guard,
                # Observers (the dashboard's tool rows, the YOLO tracker), as on a parent.
                *owner._extra_hooks,
                owner.checkpoint_hooks.binding_verifier,
            ],
            state={"nxp": _state_dict(owner.state)},
        )

    async def stream_async(self, prompt: Any = None, **kwargs: Any):
        if not self._nxp_lock.acquire(blocking=False):
            raise RuntimeError("NXP child already has an active turn")
        result = None
        breaker = None
        completed = False
        original_registry = self.tool_registry
        try:
            history = self.owner._checkpoint_turn_history(prompt)
            pending = (
                [block["toolUse"] for block in history[-1].get("content", []) if "toolUse" in block]
                if history and history[-1].get("role") == "assistant"
                else []
            )
            breaker = self.owner.engine.pre_turn(
                conversation_history=history,
                pending_tool_calls=pending,
            )
            if breaker.actions & BreakAction.HALT:
                raise RuntimeError("Stira halted this child before model invocation.")
            # Include SDK-injected handoff tools, but pass them through the same
            # policy. Restore the registry afterwards so recovery is reversible.
            original_tools = self.owner.tools
            try:
                self.owner.tools = tuple(original_registry.registry.values())
                tools, system_prompt = self.owner._prepare_turn(breaker)
            finally:
                self.owner.tools = original_tools
            self.tool_registry = ToolRegistry()
            self.tool_registry.process_tools(tools)
            self.system_prompt = system_prompt
            self.state.set("nxp", _state_dict(self.owner.state))
            self.owner._restored_messages = None
            self.owner._snapshot_context(self, system_prompt)
            async for event in super().stream_async(prompt, **kwargs):
                if "result" in event:
                    result = event["result"]
                    completed = result.stop_reason not in ("cancelled", "interrupt")
                else:
                    yield event
        finally:
            try:
                # Classifiers expect text, whereas Swarm supplies content blocks.
                text = prompt if isinstance(prompt, str) else str(prompt or "")
                self.owner._finish_turn(text, result, breaker, completed)
                self.state.set("nxp", _state_dict(self.owner.state))
            finally:
                self.tool_registry = original_registry
                self._nxp_lock.release()
        if completed and self.owner.on_complete is not None:
            self.owner.on_complete(self.owner, result)
        if result is not None:
            yield {"result": result}


class NXPSubagent(NXPAgent):
    """A focused child; call without an argument to execute its assigned task."""

    def __init__(
        self,
        *args: Any,
        task: str,
        name: str,
        on_complete: Callable[[NXPSubagent, Any], None] | None = None,
        pre_hooks: Iterable[Any] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.pre_hooks = tuple(pre_hooks)
        self.task = task
        self.name = name
        self.agent_id = f"child-{uuid4().hex}"
        self.engine.agent_id = self.agent_id
        self.on_complete = on_complete
        self.agent = GuardedDelegate(self)

    def __call__(self, prompt: str | None = None) -> Any:
        if prompt is None and self._restored_messages is not None:
            return self.agent(None)
        return self.agent(self.task if prompt is None else prompt)

    def start_new_conversation(self) -> int:
        """Refused for a child (#601): its delegate is built once, in ``__init__``, and is
        not rebuilt per turn, so a reset would leave it unusable. Nothing changes."""
        raise RuntimeError("start_new_conversation is not supported for a subagent")

    def cancel(self) -> bool:
        """Stop the child's running turn at the SDK's next safe point (thread-safe)."""
        self.agent.cancel()
        return True


def _child_model_kwargs(
    parent_agent: NXPAgent, model_id: str | None, region_name: str | None
) -> dict[str, Any]:
    """Children inherit the parent's provider; Bedrock keeps the legacy pair."""
    if getattr(parent_agent, "_legacy_bedrock", True):
        return {
            "model_id": model_id or parent_agent.model_id,
            "region_name": region_name or parent_agent.region_name,
        }
    spec = parent_agent.model_spec
    if spec is not None:
        if region_name is not None:
            if spec.provider != "bedrock":
                raise ValueError("region_name applies only to Bedrock models")
            spec = spec.with_options(region_name=region_name)
        return {"model": spec.with_model_id(model_id) if model_id else spec}
    if model_id is not None or region_name is not None:
        raise ValueError("A parent built from a Model instance cannot be overridden")
    # A Strands Model holds configuration and a client, not conversation state.
    return {"model": parent_agent.model}


def create_nxp_subagent(
    parent_agent: NXPAgent,
    task: str,
    *,
    name: str = "subagent",
    state: NeuralState | None = None,
    config: StiraConfig | None = None,
    tools: Iterable[Any] = (),
    system_prompt: str = "Complete the delegated task and return a self-contained result.",
    model_id: str | None = None,
    region_name: str | None = None,
    stimulus_classifier: Any = None,
    on_complete: Callable[[NXPSubagent, Any], None] | None = None,
    write_roots: Iterable[str] | None = None,
    pre_hooks: Iterable[Any] = (),
    extra_hooks: Iterable[Any] = (),
    keep_ask_tools: bool = False,
) -> NXPSubagent:
    """Construct, but do not run, a child with independent state and breaker.

    Fresh state is the default; even explicitly supplied state/config are copied.
    No tools, messages, SOUL or mutable classifier are inherited implicitly.
    The callback is invoked only after a successfully completed guarded turn.

    The child inherits the parent's write floor and write roots (#243);
    ``write_roots`` may narrow them, never widen them (``ValueError``). The
    child's file tools are re-wrapped so the narrower roots also hold inside
    hook-less inner agents.

    ``pre_hooks`` run before the child's gate, ``extra_hooks`` after it (observers);
    neither can loosen it: the child's own ``CheckpointHooks`` / ``IdentityGuard`` /
    binding verifier are always registered.

    ``keep_ask_tools`` (ADR 0024 phase 2, a job with ``unattended_approval: wait`` only):
    Ask-rule tools are offered too; the caller connects the child engine's approver.
    """
    # NXPAgent wraps the child's file tools with this policy (execution boundary).
    write_policy = parent_agent.write_policy.narrow(write_roots)
    policy = deepcopy(parent_agent.config if config is None else config)
    # A handoff can cause writes by another specialist. Fail closed under the
    # child's breaker, just as ordinary delegation tools do on the parent.
    # ``delegate`` (#486): depth 1 — a child never starts a child.
    policy.blocked_tool_names |= frozenset({"handoff_to_agent", "subagent", "delegate"})
    rules = parent_agent.checkpoint_hooks.policy_check
    return NXPSubagent(
        **_child_model_kwargs(parent_agent, model_id, region_name),
        max_output_tokens=parent_agent.max_output_tokens,
        task=task,
        name=name,
        state=deepcopy(state) if state is not None else NeuralState(),
        config=policy,
        read_only_paths=parent_agent.read_only_paths,
        write_policy=write_policy,
        tools=_callable_without_approver(tools, rules, keep_ask=keep_ask_tools),
        system_prompt=system_prompt,
        stimulus_classifier=stimulus_classifier,
        on_complete=on_complete,
        pre_hooks=pre_hooks,
        extra_hooks=list(extra_hooks),
        # The parent's Always/Ask/Never reader reaches the child too (#190): a child
        # never runs a governed tool its parent's rules forbid or would ask about.
        tool_policy=rules,
    )


def _callable_without_approver(
    tools: Iterable[Any], rules: Any, *, keep_ask: bool = False
) -> list[Any]:
    """The child's tool list: only tools its parent's rules let it call (#368).

    A child has no approver, so an Ask tool is refused at every call, like a Never
    one; their definitions are not sent. Decided once, when the child is built (a
    rule saved later does not rebuild it; the child's own breaker filter still runs
    each turn, unchanged). The call-time gate still checks every call (defense in
    depth). Fail closed: a tool whose name cannot be resolved (no ``tool_name`` and no
    spec name, e.g. a tool provider) cannot be looked up, so it is not given to the
    child.
    """
    tools = list(tools)
    if rules is None:
        return tools
    kept = []
    for tool in tools:
        name = _tool_name(tool)
        if name is None:
            _LOG.warning(
                "Not giving a child a tool whose name cannot be resolved (%s)", type(tool).__name__
            )
            continue
        rule = read_policy(rules, name)[0]
        if rule == "always" or (keep_ask and rule == "ask"):
            kept.append(tool)
    return kept
