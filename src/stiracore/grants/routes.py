"""Flask routes of the grant control plane (ADR 0011 §8, §9, §12; W4 of #296).

Registered **only** when ``autonomy.stage_c`` is on (:func:`install`); with it off there is
no route, no file and no thread. Every state-changing route is behind the server's Host /
Origin / CSRF checks (``install_security``) and none is a model-visible tool; the
operation confirmation additionally needs a same-origin browser fetch and the armed
own-dashboard guard (the agent's own HTTP tools cannot reach this server).

* ``GET /api/grants`` / ``GET /api/grants/<id>`` — **structured state only**: ids, codes,
  counters, TTL left, budget left. Never the goal text (nor the workspace path).
* ``GET /api/grants/<id>/goal?confirm=SHOW`` + header ``X-Stira-Goal-View`` — the goal, for
  the issuer only (the one-time view token of the issue answer). ``Cache-Control:
  no-store``.
* ``POST /api/grants/issue`` — typed confirmation (``confirm: "ISSUE"``) and a host-confirmed
  operation: without the header ``X-Stira-Host-Operation`` the answer is 428
  ``grant_confirmation_required`` with the operation (and its one-time confirmation token);
  ``POST /api/grants/operations/<op_id>/confirm {token, acknowledged: true, instance}``
  confirms it (browser channel; with ``STIRA_YOLO_HOST_CONFIRM=command|dialog`` the person
  confirms on the host instead); the same request is then sent again with the header.
* ``POST /api/grants/<id>/revoke`` (one press, always available), ``/pause`` (one press),
  ``/resume`` (the operation; a halt also ``confirm: "RESUME"``), ``/verify``
  (``{"decision": "confirm" | "reject"}``, the operation).
* ``GET /api/grants/operations/<op_id>`` — an operation's state (the host command / dialog
  channel: the page polls until the person decided on the host);
  ``POST /api/grants/operations/<op_id>/cancel`` — cancel an open operation (a narrowing).
* W5, only with ``autonomy.stage_c_exec``: ``POST /api/grants/<id>/step`` — a person's one
  press starts exactly one goal iteration (no typed word, no host operation: the grant was
  host-confirmed at issue; refused ``grant_iteration_refused`` with a ``reason`` code); the
  views carry ``iteration`` (``running`` / ``yielded`` / ``idle``, codes and counters).

Every operation step (request, consume, browser confirmation) is refused while an agent
tool call runs, and the browser confirmation is refused when the effective channel is the
host command or dialog (``host_confirm_on_host``); both are checked in the service.

Refusals are ``{"code": <fixed code>, "error_code": …}`` with the status of
:data:`.schema.CODES`; an operation refusal adds ``host_code`` (a key of
``stiracore.yolo.host_ops.CODES``). No free text.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Blueprint, Flask, jsonify, request

from stiracore.yolo.host_ops import HEADER

from .schema import GrantRefused
from .service import GrantConfirmation, GrantService

__all__ = ["GOAL_VIEW_HEADER", "create_grants_blueprint", "install"]

GOAL_VIEW_HEADER = "X-Stira-Goal-View"
_OP_KEYS = ("id", "short", "kind", "code", "expires_in", "channel", "instance", "summary")


def _refused(error: GrantRefused):
    body = {"code": error.code, "error_code": error.code}
    host_code = error.extra.get("host_code")
    if isinstance(host_code, str):
        body["host_code"] = host_code
    reason = error.extra.get("reason")
    if isinstance(reason, str):
        body["reason"] = reason  # W5: the admission's fixed refusal code
    hint = error.extra.get("hint")
    if isinstance(hint, str):
        body["hint"] = hint  # a fixed sentence (which setting to change), never input
    return jsonify(body), error.status


def _confirmation(error: GrantConfirmation):
    view = error.view
    operation = {key: view[key] for key in _OP_KEYS if key in view}
    operation["confirm_token"] = view.get("confirm_token")
    code = "grant_confirmation_required"
    return jsonify({"code": code, "error_code": code, "operation": operation}), 428


def create_grants_blueprint(
    service: Callable[[], GrantService],
    *,
    browser_ok: Callable[[], bool] = lambda: True,
    executor: Any = None,
    ticker: Any = None,
) -> Blueprint:
    """``executor`` (W5, only with ``autonomy.stage_c_exec``): the goal iteration
    executor — adds ``POST /api/grants/<id>/step`` and the iteration state (codes) to the
    views. ``None``: the W4 routes only. ``ticker`` (W6d, only with
    ``autonomy.heartbeat_start``): a grant issued with ``heartbeat_start`` gets its
    ``tick`` block (skip counts, starts, cost rows; codes and numbers) in the views."""
    blueprint = Blueprint("grants", __name__)

    def with_tick(item: dict) -> dict:
        if ticker is not None and item.get("heartbeat_start") is True:
            item["tick"] = ticker.view(item["grant_id"])
        return item

    def with_iteration(view: dict) -> dict:
        if executor is None:
            return view
        if "grants" in view:
            for item in view["grants"]:
                item["iteration"] = executor.view(item["grant_id"])
                with_tick(item)
            view["executor"] = executor.status()
        return view

    def run(action: Callable[[GrantService], Any]):
        try:
            return jsonify(action(service()))
        except GrantConfirmation as error:
            return _confirmation(error)
        except GrantRefused as error:
            return _refused(error)

    def body() -> Any:
        data = request.get_json(silent=True)
        return {} if data is None else data

    def op_header() -> str | None:
        return request.headers.get(HEADER)

    @blueprint.get("/api/grants")
    def grants_list():
        return run(lambda svc: with_iteration(svc.list_view()))

    @blueprint.get("/api/grants/<grant_id>")
    def grants_one(grant_id):
        if executor is None:
            return run(lambda svc: {"grant": svc.view(grant_id)})
        return run(
            lambda svc: {
                "grant": with_tick(svc.view(grant_id)),
                "iteration": executor.view(grant_id),
            }
        )

    @blueprint.get("/api/grants/<grant_id>/goal")
    def grants_goal(grant_id):
        if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
            return _refused(GrantRefused("grant_view_refused"))
        try:
            data = service().goal_view(
                grant_id,
                confirm=request.args.get("confirm"),
                token=request.headers.get(GOAL_VIEW_HEADER),
            )
        except GrantRefused as error:
            response, code = _refused(error)
            response.headers["Cache-Control"] = "no-store"
            return response, code
        response = jsonify(data)
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post("/api/grants/issue")
    def grants_issue():
        return run(lambda svc: svc.issue(body(), op_id=op_header()))

    @blueprint.post("/api/grants/<grant_id>/revoke")
    def grants_revoke(grant_id):
        return run(lambda svc: svc.revoke(grant_id))

    @blueprint.post("/api/grants/<grant_id>/pause")
    def grants_pause(grant_id):
        return run(lambda svc: svc.pause(grant_id))

    @blueprint.post("/api/grants/<grant_id>/resume")
    def grants_resume(grant_id):
        return run(lambda svc: svc.resume(grant_id, body(), op_id=op_header()))

    @blueprint.post("/api/grants/<grant_id>/verify")
    def grants_verify(grant_id):
        return run(lambda svc: svc.verify(grant_id, body(), op_id=op_header()))

    if executor is not None:

        @blueprint.post("/api/grants/<grant_id>/step")
        def grants_step(grant_id):
            # W5: a person's one press starts exactly one iteration (behind the server's
            # Host / Origin / CSRF checks; no typed word, no host operation: the grant was
            # host-confirmed at issue). Returns at once; never a model-visible tool.
            if body() not in ({}, None):
                return _refused(GrantRefused("grant_body_invalid"))

            def press(svc):
                svc.require_no_tool_call()  # never while an agent tool call runs
                return {"iteration": executor.step(grant_id), "grant": svc.view(grant_id)}

            return run(press)

    @blueprint.post("/api/grants/operations/<op_id>/confirm")
    def grants_confirm(op_id):
        if not browser_ok():
            return _refused(
                GrantRefused("grant_operation_refused", host_code="host_channel_unavailable")
            )
        if (
            request.headers.get("Sec-Fetch-Site") != "same-origin"
            or request.headers.get("Origin") is None
        ):
            return _refused(
                GrantRefused("grant_operation_refused", host_code="host_browser_refused")
            )
        data = body()
        data = data if type(data) is dict else {}
        return run(
            lambda svc: {
                "operation": svc.confirm_operation(
                    op_id,
                    token=data.get("token"),
                    acknowledged=data.get("acknowledged"),
                    instance=data.get("instance"),
                )
            }
        )

    @blueprint.get("/api/grants/operations/<op_id>")
    def grants_operation(op_id):
        # The host command / dialog channel: the page polls until the person decided.
        return run(lambda svc: {"operation": svc.operation_status(op_id)})

    @blueprint.post("/api/grants/operations/<op_id>/cancel")
    def grants_cancel(op_id):
        return run(lambda svc: {"operation": svc.cancel_operation(op_id)})

    return blueprint


def install(app: Flask, service: GrantService, **kwargs: Any) -> None:
    """Register the routes and the settings page's grant panel (stage C on only)."""
    app.register_blueprint(create_grants_blueprint(lambda: service, **kwargs))
    app.extensions["grants"] = service
    if kwargs.get("executor") is not None:
        app.extensions["grant_executor"] = kwargs["executor"]
    if kwargs.get("ticker") is not None:
        app.extensions["grant_tick"] = kwargs["ticker"]

    @app.context_processor
    def _grants_ui():
        ui = {"grants_ui": True, "grants_exec_ui": kwargs.get("executor") is not None}
        if kwargs.get("ticker") is not None:
            ui["grants_tick_ui"] = True  # W6d: the issue form's heartbeat-start choice
        return ui
