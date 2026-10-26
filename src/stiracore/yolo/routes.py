"""Flask wiring of YOLO (only called with ``STIRA_YOLO_ALLOW``; ADR 0012 §1a, §2, §8, §8b).

* ``GET /api/yolo/status`` — the allowance, clock support, the request, why nothing is
  offered yet. It changes nothing. #450 adds ``targets``: the tools a window of each scope
  could answer (the saved Ask rules, :mod:`.targets`), for the start dialog.
* ``POST /api/yolo/request {tier}`` — the press: a request a person confirms (in the page
  by default, or on the host); the confirmation opens the window (phase 3; nothing is
  approved automatically before phase 5). Refused (409 ``tool_call_running``) while an
  agent tool call runs (§8b). ``POST /api/yolo/request/cancel`` — a narrowing.
* ``POST /api/yolo/stop`` — ends everything current (the request, the window, open
  confirmed operations): never refused, no confirmation, no breaker check, names no id.
* ``POST /api/yolo/unlatch {window_id}`` — unlatches exactly one latch after an abnormal
  end, as a confirmed operation (kind ``yolo_unlatch``: 428 first, then the page's or the
  host's confirmation; the target is the latch, the marker digest and the chain head, so
  a change in between is ``stale_operation``). It never opens a window.
* ``GET /api/host-operations/<id>`` / ``POST /api/host-operations/<id>/cancel`` — the state
  of a host-confirmed operation the page started (polled until the host decided), and its
  cancel (a narrowing).
* ``POST /api/host-operations/<id>/confirm {token, acknowledged[, external_acknowledged],
  instance}`` — the browser channel's confirmation (revision 6, the default): the
  operation's or the YOLO request's one-time token, the dialog's checkbox(es) (phase 2c:
  exactly ``true``), this process's instance reference. Refused
  unless the browser channel is configured, the own-dashboard guard is armed, the request
  is a same-origin browser fetch (``Origin`` present, ``Sec-Fetch-Site: same-origin``) and
  no agent tool call runs. None of these proves a person (ADR 0012 §8b); they are layers.
* A ``before_request`` check on every state-changing route under ``/api/config``,
  ``/api/tools`` and ``/api/scheduler``: a JSON body naming ``yolo*`` is refused with
  409 ``yolo_not_settable`` before the view runs (after the CSRF / Origin / Host checks,
  which are installed first).

Only the five ``/api/yolo/*`` routes touch the YOLO state (§8); ``/new`` and the server's
stop end it too. A template context flag ``yolo_ui`` (phase 5b) makes the chat and Stage
pages carry the button / badge slot and ``yolo_ui.js``; it is installed here, so without
the allowance no page has either.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from flask import Flask, current_app, jsonify, request

from . import own_dashboard, runtime
from .host_ops import HEADER, HostOpRefused
from .state import Refused
from .targets import yolo_targets

__all__ = ["SAVE_PATH_PREFIXES", "YOLO_REFUSAL_STATUS", "install"]

SAVE_PATH_PREFIXES = ("/api/config", "/api/tools", "/api/scheduler")
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
YOLO_REFUSAL_STATUS = {
    "yolo_rate_limited": 429,
    "yolo_audit_failed": 503,
    "clock_not_measured": 409,
    "yolo_not_latched": 409,
    "yolo_busy": 409,
    "yolo_clock_failed": 503,
    "stale_operation": 409,
}


_UNLATCH_MESSAGES = {
    "stale_operation": (
        "The YOLO state changed while you confirmed (a window row or the marker); "
        "nothing was unlatched. Check the latch again and retry."
    ),
}


def _desk_or_refusal():
    host = runtime.desk()
    if host is None or not host.available:
        body, status = runtime.host_refusal("host_channel_unavailable")
        return None, (jsonify(body), status)
    return host, None


def _refusal(code: str):
    body, status = runtime.host_refusal(code)
    return jsonify(body), status


logger = logging.getLogger(__name__)


def _targets(tool_rules: Callable[[], Any] | None) -> dict | None:
    """#450: what each scope could answer under the saved rules; ``None`` when the rules
    cannot be read (the dialog says so; the press is not refused for it)."""
    if tool_rules is None:
        return None
    try:
        return yolo_targets(tool_rules())
    except Exception:  # noqa: BLE001 - a display summary never fails the status
        logger.warning("YOLO status: the tool rules could not be read for the targets")
        return None


def install(app: Flask, tool_rules: Callable[[], Any] | None = None) -> None:
    """``tool_rules``: returns the saved ``ToolConfig`` (read only) for ``targets``."""

    @app.context_processor
    def yolo_template_flag():
        # Phase 5b: the templates add the button / badge slot and its script only when
        # this is set — without the allowance this function is never installed, so the
        # pages carry no YOLO element and poll nothing.
        return {"yolo_ui": True}

    @app.get("/api/yolo/status")
    def yolo_status():
        return jsonify({**runtime.status(), "targets": _targets(tool_rules)})

    @app.post("/api/yolo/request")
    def yolo_request():
        host, refused = _desk_or_refusal()
        if refused is not None:
            return refused
        # §8b: the running-tool-call refusal applies to the press too (as to its
        # confirmation and the unlatch) — no request is made, no confirm token is issued.
        running = runtime.tool_call_refusal()
        if running is not None:
            return jsonify({**running, "errors": []}), 409
        data = request.get_json(silent=True)
        tier = data.get("tier") if type(data) is dict else None
        # Phase 7 (§9 option C): ``exec`` absent = no exec; exactly ``true`` asks for a
        # window that also answers shell / batch (refused unless offered).
        with_exec = data.get("exec", False) if type(data) is dict else False
        try:
            view = host.yolo.press(tier, with_exec)
        except Refused as error:
            status = YOLO_REFUSAL_STATUS.get(error.code, 409)
            return jsonify(error=error.code, error_code=error.code, code=error.code), status
        # ADR 0014 phase 1 (#451): the dialog's 「30 分ごとに中間報告する」 tick, bound to the
        # window this very press would open (absent = the settings' default). Every press
        # replaces the last one's; a press that is cancelled, denied or expires never opens
        # its window, so its tick is never used (PR #489 review, major 1).
        tracker = current_app.extensions.get("progress")
        if tracker is not None:
            choice = data.get("progress") if type(data) is dict else None
            tracker.note_press(choice, window=host.yolo.window_short_of(view.get("id")))
        command = f"stira yolo confirm {view['short']}" if host.channel == "command" else None
        token = view.pop("confirm_token", None)
        body = {"request": view, "opening": "on_confirmation", "command": command}
        if token is not None:
            # Browser channel: only in this answer (the page's own press).
            # The dialog shows a second checkbox when ``external`` is true (tier external).
            body["confirm"] = {
                "token": token,
                "external": view["tier"] == "external",
                # Phase 7: a third checkbox ("commands run and can bypass …").
                "exec": view["exec"] is True,
                "instance": host.yolo.instance,
            }
        return jsonify(body), 202

    @app.post("/api/yolo/request/cancel")
    def yolo_request_cancel():
        host, refused = _desk_or_refusal()
        if refused is not None:
            return refused
        data = request.get_json(silent=True)
        request_id = data.get("request_id") if type(data) is dict else None
        try:
            view = host.yolo.cancel(request_id)
        except Refused as error:
            return jsonify(error=error.code, error_code=error.code, code=error.code), 409
        return jsonify(request=view)

    @app.post("/api/yolo/stop")
    def yolo_stop():
        # A narrowing: no confirmation, no breaker check, no running-tool-call refusal.
        # ADR 0025 §5: the YOLO badge stop also ends every session allowance (first).
        from .session_allow import publish_end

        publish_end("stop")
        return jsonify(runtime.stop())

    @app.post("/api/yolo/unlatch")
    def yolo_unlatch():
        host, refused = _desk_or_refusal()
        if refused is not None:
            return refused
        running = runtime.tool_call_refusal()
        if running is not None:
            return jsonify({**running, "errors": []}), 409
        data = request.get_json(silent=True)
        window_id = data.get("window_id") if type(data) is dict else None
        try:
            target = host.yolo.unlatch_target(window_id)
        except Refused as error:
            status = YOLO_REFUSAL_STATUS.get(error.code, 409)
            return jsonify(error=error.code, error_code=error.code, code=error.code), status
        header = request.headers.get(HEADER)
        gated = runtime.host_gate(
            "yolo_unlatch",
            route="/api/yolo/unlatch",
            body=data,
            target=target,
            summary={"action": "unlatch", "operation": target["subject"]},
            header=header,
        )
        if gated is not None:
            body, status = gated
            return jsonify(body), status
        try:
            result = host.yolo.unlatch(window_id, expected=target, op_id=header)
        except Refused as error:
            status = YOLO_REFUSAL_STATUS.get(error.code, 409)
            message = _UNLATCH_MESSAGES.get(error.code, error.code)
            return jsonify(error=message, error_code=error.code, code=error.code), status
        return jsonify(result)

    @app.get("/api/host-operations/<op_id>")
    def host_operation(op_id):
        host, refused = _desk_or_refusal()
        if refused is not None:
            return refused
        try:
            view = host.operations.status(op_id)
        except HostOpRefused as error:
            return jsonify(error=error.code, error_code=error.code, code=error.code), 404
        return jsonify(operation=view)

    @app.post("/api/host-operations/<op_id>/cancel")
    def host_operation_cancel(op_id):
        host, refused = _desk_or_refusal()
        if refused is not None:
            return refused
        try:
            view = host.operations.cancel(op_id)
        except HostOpRefused as error:
            return jsonify(error=error.code, error_code=error.code, code=error.code), 404
        return jsonify(operation=view)

    @app.post("/api/host-operations/<op_id>/confirm")
    def host_operation_confirm(op_id):
        host, refused = _desk_or_refusal()
        if refused is not None:
            return refused
        if host.channel != "browser":
            return _refusal("host_confirm_on_host")
        if own_dashboard.armed() is None:
            # Premise 2 of §8b: the agent's own HTTP tools cannot reach this dashboard.
            return _refusal("host_channel_unavailable")
        if (
            request.headers.get("Sec-Fetch-Site") != "same-origin"
            or request.headers.get("Origin") is None
        ):
            return _refusal("host_browser_refused")
        running = runtime.tool_call_refusal()
        if running is not None:
            return jsonify({**running, "errors": []}), 409
        data = request.get_json(silent=True)
        data = data if type(data) is dict else {}
        try:
            view = host.confirm_in_browser(
                op_id,
                token=data.get("token"),
                acknowledged=data.get("acknowledged"),
                external_acknowledged=data.get("external_acknowledged"),
                instance=data.get("instance"),
                exec_acknowledged=data.get("exec_acknowledged"),
            )
        except HostOpRefused as error:
            body, status = runtime.host_refusal(error.code)
            return jsonify({**body, "state": error.state}), status
        return jsonify(operation=view)

    @app.before_request
    def refuse_reserved_yolo_keys():
        if request.method not in _MUTATING or not request.path.startswith(SAVE_PATH_PREFIXES):
            return None
        refusal = runtime.reserved_key_refusal(request.get_json(silent=True))
        if refusal is None:
            return None
        return jsonify({**refusal, "errors": []}), 409
