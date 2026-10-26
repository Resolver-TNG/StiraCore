"""``GET /api/containment``: the containment observation (ADR 0023 phase 0, #460-0). Read only.

The answer holds the switch, the fixed tables (stages, weights, codes, thresholds) and —
when observation is on — the window now (score, ``would_be``, counts by signal and code).
Codes and numbers only: no path, argument, tool result or text. There is no write route:
phase 0 never enters a stage, and a person's release / raise are phase 1 (ADR 0023 §5).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Blueprint, jsonify

from stiracore.core import containment

__all__ = ["containment_view", "create_containment_blueprint"]


def containment_view(settings: Any, observer: Any) -> dict[str, Any]:
    status = settings.status() if settings is not None else {"enabled": False}
    window = None
    if observer is not None and status.get("enabled"):
        window = observer.snapshot()
    return {
        **status,
        "observe_only": True,
        "enforced": False,
        "tables": containment.describe(),
        "window": window,
    }


def create_containment_blueprint(provider: Callable[[], dict[str, Any]]) -> Blueprint:
    bp = Blueprint("containment", __name__)

    @bp.get("/api/containment")
    def containment_status():
        try:
            response = jsonify(provider())
        except Exception:  # noqa: BLE001 - a view, never a reason to fail the page
            response = jsonify(error="containment observation unavailable")
            response.status_code = 503
        response.headers["Cache-Control"] = "no-store"
        return response

    return bp
