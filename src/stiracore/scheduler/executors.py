"""Job executors (#239, ADR 0008 §6, §8).

* Gateway layer: model-free actions (``health_check``).
* Agent layer: :class:`AgentJobExecutor` runs a job prompt on a fresh Strands ``Agent``
  with only the job's read-only tools, a guard hook that stops the run at the first call
  that would need a human approval, and a per-run token cap.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from .scheduler import RunContext, RunOutcome

__all__ = ["UNATTENDED_APPROVAL_MODES", "AgentJobExecutor", "JobToolGuard", "health_check_action"]

logger = logging.getLogger(__name__)

# ADR 0008 Open (b): only "deny" exists. Waiting on the durable approval store (#240)
# and resuming unattended work is not authorized; any other value is a start-up error.
UNATTENDED_APPROVAL_MODES = frozenset({"deny"})
JOB_SYSTEM_PROMPT = (
    "You are running an operator-defined scheduled job without a human present. "
    "Use only the tools provided. If a task needs any write or external action, stop "
    "and report what would be needed instead."
)


def health_check_action(status_providers: dict[str, Callable[[], dict[str, Any]]]):
    """A Gateway action that summarizes component health (no model call, no writes)."""

    def action(ctx: RunContext) -> str:
        parts = []
        for name in sorted(status_providers):
            try:
                status = status_providers[name]() or {}
            except Exception as error:  # noqa: BLE001 - report, never raise
                parts.append(f"{name}=error:{type(error).__name__}")
                continue
            if "running" in status:
                state = (
                    "running"
                    if status.get("running")
                    else ("off" if not status.get("enabled", True) else "stopped")
                )
            else:
                state = str(status.get("level", "unknown"))
            parts.append(f"{name}={state}")
        return ", ".join(parts) or "ok"

    return action


def _tool_name(tool: Any) -> str | None:
    name = getattr(tool, "tool_name", None)
    if isinstance(name, str) and name:
        return name
    spec = getattr(tool, "tool_spec", None)
    if isinstance(spec, dict) and isinstance(spec.get("name"), str):
        return spec["name"]
    return None


class JobToolGuard:
    """Strands hook provider: allow-listed tools only, budget check before each model call."""

    def __init__(self, ctx: RunContext, allowed: frozenset[str], token_cap: int | None) -> None:
        self.ctx = ctx
        self.allowed = allowed
        self.token_cap = token_cap
        self.over_budget = False
        self.refused: list[str] = []

    def register_hooks(self, registry: Any, **_: Any) -> None:
        from strands.hooks import BeforeModelCallEvent, BeforeToolCallEvent

        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(BeforeModelCallEvent, self.before_model)

    def before_tool(self, event: Any) -> None:
        name = str(event.tool_use.get("name", ""))
        if name in self.allowed and not self.ctx.cancelled:
            return
        if name not in self.allowed:
            # The point where a human approval would be needed: unattended = stop.
            self.refused.append(name)
            event.cancel_tool = (
                f"{name} needs a human approval; scheduled jobs do not wait for approvals"
            )
            self.ctx.require_approval(name, event.tool_use.get("input", {}))
        else:
            event.cancel_tool = "the job run was cancelled"
        try:
            event.agent.cancel()
        except Exception as error:  # noqa: BLE001 - best effort; the tool is refused anyway
            logger.info("scheduler job: agent cancel failed (%s)", type(error).__name__)

    def before_model(self, event: Any) -> None:
        # ``cancel`` denies this model call (no request is sent); Agent.cancel() ends
        # the invocation.
        if self.ctx.cancelled:
            event.cancel = "the job run was cancelled"
            event.agent.cancel()
            return
        if self.token_cap is None:
            return
        usage = event.agent.event_loop_metrics.accumulated_usage
        if int(usage.get("totalTokens", 0) or 0) >= self.token_cap:
            self.over_budget = True
            event.cancel = "the job reached its per-run token budget"
            event.agent.cancel()


def _observed_tokens(agent: Any) -> int:
    try:
        usage = agent.event_loop_metrics.accumulated_usage
        return max(0, int(usage.get("totalTokens", 0) or 0))
    except Exception:  # noqa: BLE001 - unknown is handled by the reservation
        return 0


class AgentJobExecutor:
    """Run an Agent-layer job with a budget. Tests pass a fake ``model_factory``."""

    def __init__(
        self,
        model_factory: Callable[[Any], Any],
        tool_provider: Callable[[Iterable[str]], list[Any]],
        *,
        unattended_approval: str = "deny",
        system_prompt: str = JOB_SYSTEM_PROMPT,
    ) -> None:
        if unattended_approval not in UNATTENDED_APPROVAL_MODES:
            raise ValueError(
                "unattended_approval must be 'deny' (waiting for durable approvals is not "
                "authorized, ADR 0008 Open b)"
            )
        self._model_factory = model_factory
        self._tool_provider = tool_provider
        self._system_prompt = system_prompt

    def __call__(self, ctx: RunContext) -> RunOutcome:
        from strands import Agent

        allowed = ctx.allowed_tools()
        tools = [t for t in self._tool_provider(sorted(allowed)) if _tool_name(t) in allowed]
        guard = JobToolGuard(ctx, allowed, ctx.job.budget.max_tokens_per_run)
        agent = Agent(
            model=self._model_factory(ctx.job),
            tools=tools,
            system_prompt=self._system_prompt,
            callback_handler=None,
            hooks=[guard],
        )
        ctx.on_cancel(agent.cancel)
        try:
            result = agent(ctx.job.prompt or "")
        except Exception as error:  # noqa: BLE001 - keep what was observed, then fail
            # The final usage is unknown: report what the agent had accumulated; the
            # scheduler charges at least the reservation (ADR 0008 §6).
            return RunOutcome(
                "failed", type(error).__name__, _observed_tokens(agent), usage_known=False
            )
        usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None) or {}
        tokens = max(int(usage.get("totalTokens", 0) or 0), _observed_tokens(agent))
        if guard.refused:
            return RunOutcome("failed", "approval_required", tokens)
        if guard.over_budget or (
            ctx.job.budget.max_tokens_per_run is not None
            and tokens > ctx.job.budget.max_tokens_per_run
        ):
            return RunOutcome("failed", "budget_exceeded", tokens)
        if ctx.cancelled:
            return RunOutcome("cancelled", ctx.cancel_reason, tokens)
        return RunOutcome("completed", tokens=tokens, summary=str(result))
