"""Local Stira observer. Run with PYTHONPATH=../src python3 dashboard/app.py."""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request

from stiracore.core import EMOTION_MATRIX, NeuralState, StiraEngine, bc_measurement
from stiracore.core.turn_lock import TurnLock
from stiracore.integrations.webhook import WebhookReceiver
from stiracore.observability.access_log import install_polling_filter
from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.observability.i18n import install_i18n
from stiracore.observability.neural_history import (
    HISTORY_LIMIT,
    create_neural_history_blueprint,
    history_payload,
)
from stiracore.observability.security import install_security, resolve_bind_host
from stiracore.observability.shell import install_shell, observer_nav
from stiracore.yolo.own_dashboard import OBSERVER_PORT


class StateStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.state = NeuralState()
        self.engine = StiraEngine(self.state)
        self.extra = {}
        self.history = []
        self.signature = None
        self.refresh()

    def refresh(self):
        if not self.path.exists():
            if self.signature is not None:
                raise ValueError("State file disappeared; refusing to display stale safety status.")
            return
        stat = self.path.stat()
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if signature == self.signature:
            return
        # Read a single snapshot, then validate before publishing to memory.
        data = json.loads(self.path.read_text())
        fields = NeuralState.__dataclass_fields__
        if not isinstance(data, dict):
            raise ValueError("State must be a JSON object.")  # noqa: TRY004 - preserve the public ValueError validation contract
        candidate = NeuralState(**{k: v for k, v in data.items() if k in fields})
        for key in (
            "trust",
            "curiosity",
            "vigilance",
            "attachment",
            "stability",
            "satisfaction",
            "bc_score",
        ):
            value = getattr(candidate, key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"Invalid state dimension: {key}")
        for key in ("last_decay_check", "last_stimulus_time"):
            value = getattr(candidate, key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"Invalid timestamp: {key}")
        if not isinstance(candidate.emotion_transcription, str):
            raise ValueError("Invalid emotion transcription.")  # noqa: TRY004 - preserve the public ValueError validation contract
        if not isinstance(candidate.bc_window, list) or any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            or not 0 <= v <= 1
            for v in candidate.bc_window
        ):
            raise ValueError("Invalid coherence window.")
        history = data.get("dashboard_history", [])
        if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
            raise ValueError("Invalid dashboard history.")
        self.state = candidate
        self.engine.state = candidate
        self.extra = {k: v for k, v in data.items() if k not in fields and k != "dashboard_history"}
        self.history = history[-HISTORY_LIMIT:]
        self.signature = signature

    def snapshot(self):
        # The core currently exposes only a private read-only breaker check.
        # Do NOT call pre_turn during polling: it mutates decay and bc_window.
        level = self.engine._check_breaker().value
        config = self.engine.config
        snapshot = self.state.to_dict() | {
            "breaker": level,
            "signal": {"normal": "GO", "warning": "HOLD", "coherence": "HOLD", "critical": "NO-GO"}[
                level
            ],
            "thresholds": {
                "stability_warning": config.stability_warning,
                "stability_critical": config.stability_critical,
                "bc_threshold": config.bc_threshold,
            },
            "protected_layers": sorted(config.protected_layers),
            "observed_at": datetime.now(UTC).isoformat(),
            "source": str(self.path),
            "source_exists": self.path.exists(),
            "history_count": len(self.history),
        }
        # #337: "not measured yet" vs "measured, no movement" (both bc_score 1.0).
        return snapshot | bc_measurement(len(self.state.bc_window))

    def apply(self, stimulus, intensity):
        candidate = copy.deepcopy(self.state)
        engine = StiraEngine(candidate, config=self.engine.config)
        # This endpoint records an operator stimulus, not an autonomous agent turn.
        # Stimuli remain recordable even under CRITICAL, e.g. recovery/connection.
        before = candidate.snapshot()
        pre_level = engine.pre_turn()
        result = engine.post_turn(stimulus, intensity)
        turn = int(self.history[-1].get("turn", 0)) + 1 if self.history else 1
        result.update(
            turn=turn,
            state=candidate.snapshot(),
            stimulus=stimulus,
            intensity=intensity,
            timestamp=datetime.now(UTC).isoformat(),
            source="operator",
            pre_breaker=pre_level.value,
            net_deltas={
                key: round(getattr(candidate, key) - value, 6)
                for key, value in before.items()
                if key != "bc_score" and abs(getattr(candidate, key) - value) > 1e-6
            },
        )
        history = (self.history + [result])[-HISTORY_LIMIT:]
        payload = self.extra | candidate.to_dict() | {"dashboard_history": history}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.signature = None
        self.refresh()
        return result


def _saved_anchors_empty(agent_path) -> bool:
    try:
        data = json.loads(Path(agent_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - any failure: not the "empty list" case
        return False
    return isinstance(data, dict) and data.get("identity_anchors") == []


def _observer_anchors(chat_agent, config_store) -> tuple[list[str] | None, str]:
    """The identity anchors for the panel and where they come from (#152).

    ``running``: the attached chat agent's live ``StiraConfig`` (what its prompt uses).
    ``saved``: agent-config.json, else the neutral defaults; a chat server reads it at
    start-up, so an edit shows here before it is in force (the panel says so).
    ``None``: unreadable, for any error, without the error text. ``[]``: the file sets an
    empty list (invalid; a chat server refuses to start with it), not "unreadable".
    """
    if chat_agent is not None:
        try:
            return list(chat_agent.engine.config.identity_anchors), "running"
        except Exception:  # noqa: BLE001 - shown as a fixed "unavailable" text
            return None, "running"
    try:
        return list(config_store.resolved().identity_anchors), "saved"
    except Exception:  # noqa: BLE001 - shown as a fixed "unavailable" text
        return ([] if _saved_anchors_empty(config_store.agent_path) else None), "saved"


def create_app(
    state_path=None,
    gateway_config_path=None,
    agent_config_path=None,
    tool_config_path=None,
    chat_agent=None,
):
    app = Flask(__name__, root_path=str(Path(__file__).resolve().parent))
    app.config["MAX_CONTENT_LENGTH"] = 4096
    app.json.ensure_ascii = False
    turn_lock = TurnLock()
    app.register_blueprint(WebhookReceiver(chat_agent, turn_lock=turn_lock))
    if chat_agent is not None:
        # Reuse the production chat API; the observer does not create an AWS agent.
        app.register_blueprint(create_chat_blueprint(chat_agent, turn_lock=turn_lock))
    store = StateStore(
        state_path or os.environ.get("STIRA_STATE_PATH", "/tmp/stiracore_poc_state.json")
    )
    app.extensions["state_store"] = store
    config_store = ConfigStore(
        gateway_config_path or os.environ.get("STIRA_GATEWAY_CONFIG_PATH"),
        agent_config_path or os.environ.get("STIRA_AGENT_CONFIG_PATH"),
        tool_config_path or os.environ.get("STIRA_TOOL_CONFIG_PATH"),
    )
    app.extensions["config_store"] = config_store

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    install_i18n(app)
    # The observer's breaker truth is its state file; HITL exists only with a chat agent.
    install_shell(
        app,
        observer_nav(has_chat=chat_agent is not None),
        status_url="/api/state",
        approvals_url="/api/hitl/pending" if chat_agent is not None else None,
    )
    install_security(
        app,
        csrf_exempt_endpoints=("webhooks.receive",),
        idempotent_endpoints=(
            "stimulus",
            "/api/chat",
            "/api/hitl/*/decision",
            "/api/config/models*",
        ),
    )

    @app.get("/")
    def index():
        anchors, source = _observer_anchors(chat_agent, config_store)
        return render_template(
            "index.html", stimuli=list(EMOTION_MATRIX), anchors=anchors, anchors_source=source
        )

    if chat_agent is None:

        @app.get("/chat")
        def chat_unavailable():
            return redirect("/")

    @app.get("/api/state")
    def state():
        with store.lock:
            try:
                store.refresh()
                return jsonify(store.snapshot())
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"State unavailable: {error}"), 503

    def neural_history(limit):
        with store.lock:
            store.refresh()
            return history_payload(store.state, store.engine.config, store.history, limit)

    app.register_blueprint(create_neural_history_blueprint(neural_history))

    @app.get("/observability")
    def observability_page():
        # #278: the observer serves no event stream; the page says so instead of
        # reporting a closed connection and asking for a reload.
        return render_template("observability.html", event_source="absent")

    @app.get("/api/history")
    def history():
        with store.lock:
            try:
                store.refresh()
                return jsonify(store.history[-10:])
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"History unavailable: {error}"), 503

    @app.post("/api/stimulus")
    def stimulus():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Expected a JSON object."), 400
        name, intensity = data.get("stimulus"), data.get("intensity", 1.0)
        if not isinstance(name, str) or name not in EMOTION_MATRIX:
            return jsonify(error="Unknown stimulus.", allowed=list(EMOTION_MATRIX)), 400
        if (
            isinstance(intensity, bool)
            or not isinstance(intensity, (int, float))
            or not math.isfinite(intensity)
            or not 0 <= intensity <= 1
        ):
            return jsonify(error="Intensity must be a finite number between 0 and 1."), 400
        with store.lock:
            try:
                store.refresh()
                result = store.apply(name, intensity)
                return jsonify(result=result, state=store.snapshot())
            except (OSError, ValueError, TypeError) as error:
                return jsonify(error=f"Stimulus could not be saved: {error}"), 503

    def breaker_level():
        with store.lock:
            store.refresh()
            return store.engine._check_breaker()

    app.register_blueprint(create_config_blueprint(config_store, breaker_level))

    return app


if __name__ == "__main__":
    bind_host = resolve_bind_host()  # #185: refuse before the app is built
    install_polling_filter()  # #369: successful page polls stay out of the access log
    # The chat server refuses its agent's connections to this port too (#499).
    create_app().run(
        host=bind_host, port=OBSERVER_PORT, debug=False, use_reloader=False, threaded=True
    )
