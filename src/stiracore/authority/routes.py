"""Flask routes of the authority service (ADR 0011 §5, §12; W2 of #296).

* ``GET /api/authority/status`` — read only: healthy, the reason code, killed, the epoch
  and the persisted epoch, settlement, the latches (codes), the latch store's health, the
  outbox's unread count, the watchdog's last pass. Changes nothing.
* ``POST /api/authority/kill`` — the kill switch: one press, no confirmation (a narrowing
  needs none), **always available** — also when the service or a store is unhealthy.
  The answer says whether this narrowing's cancellations still run (``settlement``:
  ``settled`` / ``cancelling``) and whether the latch was persisted.
* ``GET /api/authority/outbox`` (``?unread=1``) — the notifications, newest first.
* ``POST /api/authority/outbox/ack {event_id}`` — acknowledge one notification
  (idempotent: a second ack answers ``already``).

Every state-changing route is behind the server's Host / Origin / CSRF checks
(``install_security``); none is a model-visible tool. **Nothing here clears a latch**:
that is the host command ``stira authority unlatch`` with the server stopped.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Blueprint, jsonify, request

__all__ = ["create_authority_blueprint"]


def create_authority_blueprint(
    service: Callable[[], Any],
    recorder: Callable[[], Any],
    watchdog: Callable[[], Any],
) -> Blueprint:
    blueprint = Blueprint("authority", __name__)

    @blueprint.get("/api/authority/status")
    def authority_status():
        svc, rec, dog = service(), recorder(), watchdog()
        status = svc.status()
        latches = svc.latches
        view = latches.view() if latches is not None else {"healthy": False, "latches": []}
        return jsonify(
            {
                "healthy": status["healthy"],
                "unhealthy_reason": status["unhealthy_reason"],
                "killed": status["killed"],
                "epoch": status["epoch"],
                "persisted_epoch": status["persisted_epoch"],
                "settle_pending": status["settle_pending"],
                "cancel_overdue": status["cancel_overdue"],
                "in_flight": status["in_flight"],
                "latches": view["latches"],
                "latch_store_healthy": view["healthy"],
                "outbox_unread": rec.unread_count() if rec is not None else None,
                "lifecycle_healthy": rec is not None and rec.unhealthy_reason is None,
                "watchdog": dog.status() if dog is not None else None,
            }
        )

    @blueprint.post("/api/authority/kill")
    def authority_kill():
        svc, rec = service(), recorder()
        try:
            report = svc.kill_switch("kill_switch")
        except Exception:  # noqa: BLE001 - the flag is set first; report, never 500
            svc.report_unhealthy("epoch_settle_failed")
            report = {"epoch": svc.epoch, "settlement": "error", "latch": "unknown"}
        recorded = rec.drain() if rec is not None else "unhealthy"
        return jsonify(
            {
                "killed": svc.killed,
                "epoch": report["epoch"],
                "settlement": report["settlement"],
                "latch": report["latch"],
                "recorded": recorded,
                "healthy": svc.unhealthy_reason is None,
            }
        )

    @blueprint.get("/api/authority/outbox")
    def authority_outbox():
        rec = recorder()
        if rec is None:
            return jsonify({"items": [], "unread": 0, "healthy": False})
        unread = request.args.get("unread") in ("1", "true")
        return jsonify(
            {
                "items": rec.outbox(unread_only=unread),
                "unread": rec.unread_count(),
                "healthy": rec.unhealthy_reason is None,
            }
        )

    @blueprint.post("/api/authority/outbox/ack")
    def authority_outbox_ack():
        rec = recorder()
        data = request.get_json(silent=True)
        event_id = data.get("event_id") if type(data) is dict else None
        if rec is None:
            return jsonify({"acked": False, "code": "outbox_unavailable"}), 503
        result = rec.ack(event_id)
        if result.get("acked"):
            return jsonify(result)
        status = {"outbox_unknown": 404, "outbox_stale": 409}.get(result.get("code"), 503)
        return jsonify(result), status

    return blueprint
