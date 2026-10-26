"""``memory_harvest``: rule-only Harvest as a human-defined scheduler job (#251, ADR 0008).

A Gateway action of the scheduler — model-free like every Gateway action (ADR 0008 §6) —
not a timer of its own. Nothing runs unless the operator turned the scheduler on
(``STIRA_SCHEDULER=1``) **and** created a ``memory_harvest`` job through the
authenticated API with ``confirm: true``; Stira has no tool or route that creates one
(ADR 0008 §Constraints; ADR 0011 stays Proposed). One run:

* refuses to start unless this process owns the data directory (``server.lock``,
  #285), the breaker is NORMAL and no other Harvest sweep of the same memory store is
  running in this process (:func:`~stiracore.control.harvest.store_lock`);
* uses the **rule distiller only**: with ``distiller_mode`` ``"llm"`` or ``"jev"`` the job
  cannot be created, updated or resumed (:func:`harvest_job_precondition`) and a run
  fails before anything is scored (``distiller_not_rule``). Model scoring stays a manual
  or OS-timer run of ``distillation_cli`` (docs/memory-maintenance.md). This module
  builds no model and imports no model-calling distiller;
* scores at most ``harvest_job_max_episodes`` episodes (newest first); the rest wait,
  unmarked, for the next run;
* always stages what it promotes (``semantic/staging/``), whatever
  ``harvest_stage_auto`` says (#293). It never applies forgetting (#294) and never
  approves anything;
* stops before the next episode when the breaker leaves NORMAL or the run is cancelled;
* reports counts and reason codes only (never episode text, paths or exception text) in
  the run summary, a notification when a limit stopped it or it failed, and
  :meth:`HarvestJobStatus.status` for ``/api/gateway/status``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from stiracore.config import AgentConfig
from stiracore.control.distillers import RuleBasedDistiller
from stiracore.control.harvest import HarvestScheduler, store_lock
from stiracore.core.amygdala import AmygdalaPass
from stiracore.scheduler.scheduler import NORMAL, RunContext, RunOutcome, normalize_level

__all__ = [
    "ACTION_NAME",
    "HarvestJobStatus",
    "harvest_job_precondition",
    "memory_harvest_action",
]

logger = logging.getLogger(__name__)

ACTION_NAME = "memory_harvest"
_SUMMARY_KEYS = (
    "mode",
    "staged",
    "knowledge",
    "discard",
    "skipped",
    "deferred",
    "errors",
    "staging_pending",
    "episode_limit",
    "stopped_by",
)


class HarvestJobStatus:
    """The newest scheduled Harvest run, for the gateway status (counts and codes only)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, Any] | None = None
        self._running = False

    def _set(self, *, running: bool | None = None, last: dict[str, Any] | None = None) -> None:
        with self._lock:
            if running is not None:
                self._running = running
            if last is not None:
                self._last = dict(last)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "action": ACTION_NAME,
                "running": self._running,
                "last_run": dict(self._last) if self._last is not None else None,
            }


def _config_refusal(
    load_config: Callable[[], AgentConfig],
) -> tuple[AgentConfig | None, str | None]:
    try:
        config = load_config()
    except Exception as error:  # noqa: BLE001 - class name only (messages carry paths)
        return None, f"config_{type(error).__name__}"
    if config.distiller_mode != "rule":
        # Model scoring is not a scheduled job (ADR 0008: Gateway jobs never call a
        # model); run distillation_cli by hand or from an OS timer instead.
        return config, "distiller_not_rule"
    return config, None


def harvest_job_precondition(load_config: Callable[[], AgentConfig]) -> Callable[[], str | None]:
    """Scheduler ``action_checks`` entry: refuse create/update/resume unless rule mode."""

    def check() -> str | None:
        return _config_refusal(load_config)[1]

    return check


def _rule_distiller() -> RuleBasedDistiller:
    return RuleBasedDistiller(AmygdalaPass())


def memory_harvest_action(
    store,
    *,
    agent_id: str,
    load_config: Callable[[], AgentConfig],
    breaker_level: Callable[[], Any],
    owner_held: Callable[[], bool],
    status: HarvestJobStatus | None = None,
    on_finish: Callable[[dict[str, Any]], None] | None = None,
    now_fn: Callable[[], float] = time.time,
) -> Callable[[RunContext], RunOutcome]:
    """Build the ``memory_harvest`` Gateway action for one memory store."""
    lock = store_lock(store)  # shared with every Harvest of this store in the process
    status = status if status is not None else HarvestJobStatus()

    def level() -> str:
        try:
            return normalize_level(breaker_level())
        except Exception:  # noqa: BLE001 - unknown is treated as CRITICAL
            return "critical"

    def finish(ctx: RunContext, outcome: RunOutcome, report: dict[str, Any]) -> RunOutcome:
        report = {
            "job_id": ctx.job.job_id,
            "run_id": ctx.run_id,
            "finished_at": now_fn(),
            "status": outcome.status,
            "reason": outcome.reason,
            **report,
        }
        outcome.summary = " ".join(
            f"{key}={report[key]}" for key in _SUMMARY_KEYS if report.get(key) is not None
        ) or (outcome.reason or "")
        status._set(last=report)
        stopped = report.get("stopped_by")
        if outcome.status != "completed" or stopped:
            severity = "info" if outcome.status == "cancelled" else "warning"
            ctx.notify(
                "harvest_" + ("stopped" if outcome.status == "completed" else outcome.status),
                f"memory_harvest {ctx.job.job_id}: {outcome.status} "
                f"({outcome.reason or stopped}); {outcome.summary}",
                severity,
            )
        if on_finish is not None:
            try:
                on_finish(report)
            except Exception as error:  # noqa: BLE001 - status refresh is best effort
                logger.info("memory_harvest: on_finish failed (%s)", type(error).__name__)
        return outcome

    def action(ctx: RunContext) -> RunOutcome:
        if not lock.acquire(blocking=False):
            # Another Harvest sweep of this store is running here (another job, a manual
            # in-process run): never two at once, and the job does not wait for it.
            return finish(ctx, RunOutcome("cancelled", "harvest_in_progress"), {})
        status._set(running=True)
        try:
            return run(ctx)
        finally:
            status._set(running=False)
            lock.release()

    def run(ctx: RunContext) -> RunOutcome:
        if not owner_held():
            # Without server.lock another writer could share the memory (#285).
            return finish(ctx, RunOutcome("failed", "data_dir_not_owned"), {})
        current = level()
        if current != NORMAL:
            return finish(ctx, RunOutcome("cancelled", f"breaker_{current}"), {})
        config, refusal = _config_refusal(load_config)
        if refusal is not None:
            mode = config.distiller_mode if config is not None else None
            return finish(ctx, RunOutcome("failed", refusal), {"mode": mode})
        assert config is not None
        cap = config.harvest_job_max_episodes
        distiller = _rule_distiller()
        state: dict[str, Any] = {"scored": 0, "stopped_by": None}

        def admit(_entry) -> bool:
            # Asked only for an episode that would be scored, so the stop reason is
            # recorded only when an episode was actually left for later.
            if state["stopped_by"] is not None:
                return False
            if ctx.cancelled:
                state["stopped_by"] = "cancelled"
            elif level() != NORMAL:
                state["stopped_by"] = "breaker"
            elif state["scored"] >= cap:
                state["stopped_by"] = "episode_limit"
            else:
                state["scored"] += 1
                return True
            return False

        try:
            result = HarvestScheduler(
                store, None, agent_id, distiller=distiller, stage_auto=True
            ).run_distillation(admit=admit)
        except Exception as error:  # noqa: BLE001 - recorded (class name), never raised
            return finish(ctx, RunOutcome("failed", f"harvest_{type(error).__name__}"), {})
        stopped_by = state["stopped_by"]
        report = {
            "mode": "rule",
            "staged": result.semantic,
            "knowledge": result.knowledge,
            "discard": result.discard,
            "skipped": result.skipped,
            "deferred": result.deferred,
            "errors": result.errors,
            "staging_pending": result.staging_pending,
            "episode_limit": cap,
            "stopped_by": stopped_by,
        }
        if result.errors:
            outcome = RunOutcome("failed", "episode_errors")
        elif stopped_by in ("cancelled", "breaker"):
            outcome = RunOutcome("cancelled", stopped_by)
        else:
            outcome = RunOutcome("completed")
        return finish(ctx, outcome, report)

    action.status = status  # type: ignore[attr-defined]
    return action
