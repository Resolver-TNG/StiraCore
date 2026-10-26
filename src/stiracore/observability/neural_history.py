"""Read-only neural visualization payloads shared by observer and live gateway."""

from __future__ import annotations

import copy
import math
import time
from collections import deque
from collections.abc import Callable
from threading import RLock

from flask import Blueprint, jsonify, request

from stiracore.core.neural_state import DIMS, EMOTION_MATRIX, NeuralState

HISTORY_LIMIT = 500


def history_payload(state: NeuralState, config, rows: list[dict], limit: int) -> dict:
    """Project core decay on copies; never advance the live engine/clock."""
    now = time.time()
    predictions = []
    for hours in range(25):
        predicted = copy.deepcopy(state)
        if hours:
            predicted.apply_decay(now_fn=lambda h=hours: now + h * 3600, min_interval_sec=0)
        predictions.append({"timestamp": now + hours * 3600, "state": predicted.snapshot()})
    valid = []
    for row in rows:
        values = row.get("state", {})
        if not isinstance(values, dict) or not all(
            type(values.get(dim)) in (int, float)
            and math.isfinite(values[dim])
            and 0 <= values[dim] <= 1
            for dim in DIMS
        ):
            continue
        valid.append(row)
    return {
        "history": valid[-limit:],
        "count": min(len(valid), limit),
        "limit": limit,
        "capacity": HISTORY_LIMIT,
        "dimensions": list(DIMS),
        "current": {"timestamp": now, "state": state.snapshot()},
        "prediction": predictions,
        "prediction_note": "24h without new stimuli; core linear, baseline-clamped lazy decay.",
        "thresholds": {"warning": config.stability_warning, "critical": config.stability_critical},
        "emotion_matrix": {
            "stimuli": list(EMOTION_MATRIX),
            "dimensions": list(DIMS),
            "values": [list(values) for values in EMOTION_MATRIX.values()],
        },
    }


class NeuralHistory:
    """Bounded process-lifetime history; recording does not require browser polling."""

    def __init__(self):
        self.lock = RLock()
        self.rows = deque(maxlen=HISTORY_LIMIT)

    def record(self, state: NeuralState, *, stimulus: str | None = None):
        with self.lock:
            self.rows.append(
                {"timestamp": time.time(), "state": state.snapshot(), "stimulus": stimulus}
            )

    def record_snapshot(self, snapshot: dict, *, stimulus: str | None = None):
        """Record a heartbeat tick snapshot as taken, not a later re-read (#238)."""
        values = {k: v for k, v in snapshot.items() if k not in ("turn", "timestamp")}
        timestamp = snapshot.get("timestamp")
        with self.lock:
            self.rows.append(
                {
                    "timestamp": timestamp if timestamp is not None else time.time(),
                    "state": copy.deepcopy(values),
                    "stimulus": stimulus,
                }
            )

    def snapshot(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(list(self.rows))


def create_neural_history_blueprint(provider: Callable[[int], dict]) -> Blueprint:
    """Provider must capture state/history using its application's synchronization."""
    bp = Blueprint("neural_history", __name__)

    @bp.get("/api/state/history")
    def history():
        try:
            limit = int(request.args.get("limit", "100"))
            if not 1 <= limit <= HISTORY_LIMIT:
                raise ValueError
        except ValueError:
            return jsonify(error=f"limit must be an integer from 1 to {HISTORY_LIMIT}"), 400
        try:
            response = jsonify(provider(limit))
            response.headers["Cache-Control"] = "no-store"
            return response
        except (OSError, ValueError, TypeError):
            return jsonify(error="Neural history unavailable"), 503

    return bp
