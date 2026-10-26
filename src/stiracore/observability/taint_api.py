"""``GET /api/taint``: the taint observation (ADR 0022 phase 0, #458). Read only.

The switch, the fixed vocabulary (sources, flow codes) and — when observation is on — the
last turn's row and the totals since start. Codes and numbers only: no text, argument, URL
or host. There is no write route: phase 0 enforces nothing and no destination is listed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint, jsonify

from stiracore import labels

__all__ = ["clean_summary", "create_taint_blueprint", "taint_view"]


def taint_view(settings: Any, observer: Any) -> dict[str, Any]:
    enabled = bool(getattr(settings, "enabled", False))
    return {
        "enabled": enabled,
        "forced_off": bool(getattr(settings, "forced_off", False)),
        "env_var": labels.ENV_VAR,
        "observe_only": True,
        "enforced": False,
        "frame_version": labels.FRAME_VERSION,
        "sources": list(labels.SOURCES),
        "trusted_sources": sorted(labels.TRUSTED_SOURCES),
        "flow_codes": list(labels.FLOW_CODES),
        "observation": observer.snapshot() if enabled and observer is not None else None,
    }


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value < 10**9


def clean_summary(value: Any) -> dict[str, Any] | None:
    """The ``taint`` object of ``turn.summary`` (ADR 0022 §8): fixed keys, codes, counts."""
    if not isinstance(value, Mapping):
        return None
    trust = value.get("context_trust")
    if trust not in ("trusted", "untrusted") or type(value.get("context_sensitive")) is not bool:
        return None
    inputs = value.get("input_labels")
    held = value.get("flow_would_hold")
    if not isinstance(inputs, Mapping) or not isinstance(held, Mapping):
        return None
    inputs = {k: v for k, v in inputs.items() if k in labels.SOURCES and _count(v)}
    held = {k: v for k, v in held.items() if k in labels.FLOW_CODES and _count(v)}
    return {
        "context_trust": trust,
        "context_sensitive": value["context_sensitive"],
        "input_labels": inputs,
        "untrusted_recall_only": value.get("untrusted_recall_only") is True,
        "frame_version": labels.FRAME_VERSION,
        "flow_would_hold": held,
        "observe_only": True,
    }


def create_taint_blueprint(provider: Callable[[], dict[str, Any]]) -> Blueprint:
    bp = Blueprint("taint", __name__)

    @bp.get("/api/taint")
    def taint_status():
        try:
            response = jsonify(provider())
        except Exception:  # noqa: BLE001 - a view, never a reason to fail the page
            response = jsonify(error="taint observation unavailable")
            response.status_code = 503
        response.headers["Cache-Control"] = "no-store"
        return response

    return bp
