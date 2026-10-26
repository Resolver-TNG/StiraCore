"""Flask API over an application's live StiraEngine.

No breaker state is synthesized or cached here: GET reads the engine, and
POST invokes its stimulus operation before reading the resulting state.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from threading import RLock
from typing import TYPE_CHECKING, Any

from flask import Blueprint, Response, current_app, jsonify, request

if TYPE_CHECKING:
    from stiracore.core import StiraEngine


_MISSING = object()
_STATE_FIELDS = (
    "level",
    "actions",
    "blocked_tools",
    "identity_injection",
    "stability",
    "bc_score",
)


def _member(value: Any, name: str) -> Any:
    if value is None or value is _MISSING:
        return _MISSING
    if isinstance(value, Mapping):
        return value.get(name, _MISSING)
    return getattr(value, name, _MISSING)


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return _json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_json_value(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if callable(getattr(value, "model_dump", None)):
        return _json_value(value.model_dump(mode="json"))
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Engine state contains a non-finite number.")
        return value
    raise TypeError(f"Cannot serialize engine state value {type(value).__name__}.")


def _read_state(engine: StiraEngine) -> dict[str, Any]:
    """Read direct breaker snapshots or breaker fields within engine state.

    Supports engines exposing get_breaker_state(), get_state(), or state.
    Breaker fields may reside on that snapshot's breaker/breaker_state or
    on engine.breaker; stability and bc_score may reside on the engine.
    Missing fields are integration errors, never fabricated defaults.
    """
    snapshot: Any = _MISSING
    for method_name in ("get_breaker_state", "get_state"):
        getter = getattr(engine, method_name, None)
        if callable(getter):
            snapshot = getter()
            break
    if snapshot is _MISSING:
        snapshot = _member(engine, "state")

    breaker = _member(engine, "breaker")
    breaker_state = _member(breaker, "state")
    engine_state = _member(engine, "state")

    sources = (
        _member(snapshot, "breaker"),
        _member(snapshot, "breaker_state"),
        snapshot,
        breaker_state,
        breaker,
        _member(engine_state, "breaker"),
        _member(engine_state, "breaker_state"),
        engine_state,
        engine,
    )

    result: dict[str, Any] = {}
    for name in _STATE_FIELDS:
        for source in sources:
            value = _member(source, name)
            if value is not _MISSING:
                result[name] = _json_value(value)
                break
        else:
            raise RuntimeError(f"StiraEngine does not expose breaker field {name!r}.")
    return result


def _submit_stimulus(
    engine: StiraEngine,
    stimulus: str,
    intensity: float,
) -> None:
    """Invoke one real engine operation, without retrying a failed mutation.

    Engine releases may name this operation process_stimulus,
    apply_stimulus, or stimulate. Each supported operation accepts
    stimulus and intensity keyword arguments.
    """
    for method_name in ("process_stimulus", "apply_stimulus", "stimulate"):
        method = getattr(engine, method_name, None)
        if callable(method):
            method(stimulus=stimulus, intensity=intensity)
            return
    raise RuntimeError("StiraEngine does not expose a supported stimulus operation.")


def create_breaker_blueprint(engine: StiraEngine) -> Blueprint:
    """Bind breaker endpoints to the supplied live StiraEngine.

    The local lock prevents these API requests from interleaving a stimulus
    with another API read. Engine users outside this blueprint must follow
    the engine's own synchronization requirements.

    These endpoints expose a state-mutating diagnostic operation; apply the
    application's authentication and authorization before public exposure.
    """
    blueprint = Blueprint("breaker", __name__)
    lock = RLock()

    def state_response(state: dict[str, Any]) -> Response:
        response = jsonify(state)
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/api/breaker")
    def get_breaker() -> Response | tuple[Response, int]:
        try:
            with lock:
                state = _read_state(engine)
            return state_response(state)
        except Exception:
            current_app.logger.exception("Failed to read StiraEngine breaker state.")
            return jsonify(error="Unable to read breaker state."), 500

    @blueprint.post("/api/breaker/stimulus")
    def submit_stimulus() -> Response | tuple[Response, int]:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Request body must be a JSON object."), 400

        stimulus = body.get("stimulus")
        if not isinstance(stimulus, str) or not stimulus.strip():
            return jsonify(error="stimulus must be a nonempty string."), 400

        intensity = body.get("intensity")
        if isinstance(intensity, bool) or not isinstance(intensity, (int, float)):
            return jsonify(error="intensity must be a number between 0 and 1."), 400
        if not 0 <= intensity <= 1:
            return jsonify(error="intensity must be a number between 0 and 1."), 400

        try:
            with lock:
                try:
                    _submit_stimulus(engine, stimulus.strip(), float(intensity))
                except ValueError as exc:
                    return jsonify(error=str(exc)), 400
                state = _read_state(engine)
            return state_response(state)
        except Exception:
            current_app.logger.exception("Failed to submit a stimulus to StiraEngine.")
            return jsonify(error="Unable to submit stimulus or read breaker state."), 500

    return blueprint


__all__ = ["create_breaker_blueprint"]
