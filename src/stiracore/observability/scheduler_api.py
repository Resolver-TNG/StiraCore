"""HTTP API for the operator-defined scheduler (#239, ADR 0008 §10).

Reads are open to the dashboard (Host check). Every POST goes through
``install_security`` first (loopback Host, same-origin, CSRF token, #185); the
scheduler itself then requires a human principal, ``"confirm": true`` for widening
changes and a NORMAL breaker. Refusals are logged without the request body.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from flask import Blueprint, jsonify, request

from stiracore.scheduler import Scheduler, SchedulerRefused
from stiracore.yolo import runtime as yolo_runtime
from stiracore.yolo.host_ops import CODES as HOST_CODES

__all__ = ["create_scheduler_blueprint"]

logger = logging.getLogger(__name__)
STATUS_CODES = {
    "invalid": 400,
    "scope": 403,
    "not_found": 404,
    "conflict": 409,
    "breaker": 409,
    "confirmation_required": 428,
    "unavailable": 503,
    # Only with STIRA_YOLO_ALLOW (ADR 0012 §8b): a loosening while an agent tool call runs,
    # and the host confirmation of a loosening (phase 2; host_ops.CODES).
    "tool_call_running": 409,
    **{code: status for code, status in HOST_CODES.items()},
}
# Job fields that change without anyone touching the job (not part of its generation).
_VOLATILE_JOB_FIELDS = frozenset(
    {"next_run_at", "usage_today", "held_since", "last_run_id", "consecutive_failures"}
)
_ACTOR = re.compile(r"[A-Za-z0-9._@-]{1,48}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _safe_target(target: Any) -> str:
    """A path id for the log: only if it is a plain id that masking leaves unchanged."""
    from stiracore.observability.masking import mask_secrets

    if isinstance(target, str) and _ID.fullmatch(target) and mask_secrets(target) == target:
        return target
    return "-"


def _principal(body: dict[str, Any]) -> str:
    actor = body.get("actor")
    if isinstance(actor, str) and _ACTOR.fullmatch(actor.strip()):
        return f"local:{actor.strip()}"
    return "local:operator"


def create_scheduler_blueprint(
    scheduler_provider: Callable[[], Scheduler | None],
    status_provider: Callable[[], dict[str, Any]],
) -> Blueprint:
    bp = Blueprint("scheduler", __name__)

    def _body() -> dict[str, Any]:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def _refused(operation: str, target: str | None, error: SchedulerRefused):
        logger.warning(
            "scheduler: refused %s target=%s code=%s", operation, _safe_target(target), error.code
        )
        return jsonify(error=str(error), code=error.code), STATUS_CODES.get(error.code, 400)

    def _call(
        operation: str,
        target: str | None,
        fn: Callable[[Scheduler], Any],
        *,
        loosening: bool = False,
    ):
        if loosening:
            # Only with STIRA_YOLO_ALLOW (ADR 0012 §8b): not while an agent tool call runs.
            running = yolo_runtime.tool_call_refusal()
            if running is not None:
                return _refused(
                    operation, target, SchedulerRefused(running["code"], running["error"])
                )
        scheduler = scheduler_provider()
        if scheduler is None:
            return _refused(
                operation, target, SchedulerRefused("unavailable", "the scheduler is not enabled")
            )
        if loosening and yolo_runtime.enabled():
            # Only with STIRA_YOLO_ALLOW (ADR 0012 §8b, phase 2): applies only after a
            # person confirmed exactly this request. Reading the job, checking the
            # confirmed generation, consuming the operation and the change are one step
            # under the scheduler's lock (review of phase 2: no change can slip in between).
            with scheduler.exclusive():
                gate = yolo_runtime.host_gate(
                    "scheduler_loosen",
                    route=request.path,
                    body=request.get_json(silent=True),
                    target=_job_target(scheduler, target),
                    summary={"operation": operation, "job": _job_label(target)},
                    header=request.headers.get(yolo_runtime.HEADER),
                )
                if gate is None:
                    return _apply(operation, target, fn, scheduler)
            body, status = gate
            logger.warning(
                "scheduler: %s target=%s needs the host (%s)",
                operation,
                _safe_target(target),
                body["code"],
            )
            return jsonify(body), status
        return _apply(operation, target, fn, scheduler)

    def _apply(operation, target, fn, scheduler):
        try:
            result = fn(scheduler)
        except SchedulerRefused as error:
            return _refused(operation, target, error)
        logger.info("scheduler: %s target=%s accepted", operation, _safe_target(target))
        return jsonify(result)

    def _job_target(scheduler: Scheduler, job_id: str | None) -> dict:
        jobs = scheduler.jobs()
        current = next((j for j in jobs if j.get("job_id") == job_id), None)
        if current is not None:
            current = {k: v for k, v in current.items() if k not in _VOLATILE_JOB_FIELDS}
        return {"job": current, "ids": sorted(str(j.get("job_id")) for j in jobs)}

    def _job_label(job_id: str | None) -> str:
        # A job id is typed text (maybe the agent's): only a short digest on the host.
        return "-" if job_id is None else "#" + yolo_runtime.target_digest(str(job_id))[:8]

    @bp.get("/api/scheduler")
    def scheduler_state():
        scheduler = scheduler_provider()
        if scheduler is None:
            return jsonify(status=status_provider(), jobs=[], runs=[], notifications=[])
        return jsonify(
            status=scheduler.status(),
            jobs=scheduler.jobs(),
            runs=scheduler.runs(50),
            notifications=scheduler.notifications(50),
        )

    @bp.post("/api/scheduler/jobs")
    def create_job():
        body = _body()
        return _call(
            "create",
            None,
            lambda s: s.create_job(
                body.get("job"), by=_principal(body), confirm=body.get("confirm") is True
            ),
            loosening=True,
        )

    @bp.post("/api/scheduler/jobs/<job_id>")
    def update_job(job_id):
        body = _body()
        return _call(
            "update",
            job_id,
            lambda s: s.update_job(
                job_id,
                body.get("changes"),
                by=_principal(body),
                confirm=body.get("confirm") is True,
            ),
            loosening=body.get("confirm") is True,
        )

    @bp.post("/api/scheduler/jobs/<job_id>/pause")
    def pause_job(job_id):
        body = _body()
        return _call(
            "pause",
            job_id,
            lambda s: s.pause_job(job_id, by=_principal(body), reason=body.get("reason")),
        )

    @bp.post("/api/scheduler/jobs/<job_id>/resume")
    def resume_job(job_id):
        body = _body()
        return _call(
            "resume",
            job_id,
            lambda s: s.resume_job(
                job_id, by=_principal(body), confirm=body.get("confirm") is True
            ),
            loosening=True,
        )

    @bp.post("/api/scheduler/runs/<run_id>/cancel")
    def cancel_run(run_id):
        body = _body()
        return _call("cancel", run_id, lambda s: s.cancel_run(run_id, by=_principal(body)))

    return bp
