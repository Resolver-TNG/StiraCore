"""Review screen routes for procedural-memory proposals (#463).

    GET  /api/memory/procedural/review                       -> proposals, rules, reviews
    POST /api/memory/procedural/proposals/<key>/approve      {sha256, confirm: true[, replace]}
    POST /api/memory/procedural/proposals/<key>/reject       {sha256, confirm: true[, reason]}

Same conventions as the other admin mutations (``docs/admin-api-permissions.md``): the
security layer checks Host / Origin / ``Sec-Fetch-Site`` / the CSRF token before the view
runs; the body must carry ``confirm: true`` (428 ``confirmation_required`` without it) and
the SHA-256 of the proposal the page showed (409 ``E_STALE_PROPOSAL`` when it changed).

**Approve** is a loosening (it adds a rule to the system prompt): refused while a chat turn
holds the turn lock (409 ``turn_running`` — the agent's tools only run inside a turn) or an
agent tool call runs (409 ``tool_call_running``, with YOLO's tracker), and done **under**
the turn lock, so the prompt refresh lands between two turns: the rule applies from the
start of the next turn. The approver is the server's OS account (``local:<account>``,
``procedural_cli.operator_principal``) — the request names nobody. Approve also needs
the breaker at **NORMAL** (operator decision 2026-10-20): any other level is refused with
409 ``E_BREAKER_NOT_NORMAL``, an unreadable level — or a service built without a breaker —
with 503 ``E_BREAKER_UNAVAILABLE``; checked under the turn lock, before anything is read or
written, and audited as ``audit.procedural.refused`` (key and code only). **Reject** only
narrows (nothing reaches the prompt) and is allowed at every breaker level and while a
turn runs.

Responses never echo a reason or a body back; refusals carry a fixed code.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Blueprint, jsonify, request

from stiracore.control.procedural_proposals import ProcedureProposals, ProposalRefused
from stiracore.core.turn_lock import TurnLock

REVIEW_ROUTE = "/api/memory/procedural/review"
_BASE = "/api/memory/procedural/proposals/<key>"
_APPROVE_KEYS = frozenset({"sha256", "confirm", "replace"})
_REJECT_KEYS = frozenset({"sha256", "confirm", "reason"})
_STATUS = {
    "E_INVALID_KEY": 400,
    "E_REASON_INVALID": 400,
    "E_NO_PROPOSAL": 404,
    "E_STALE_PROPOSAL": 409,
    "E_TARGET_EXISTS": 409,
    "E_REVISION_STALE": 409,
    "E_FORGED_RESERVED_KEY": 409,
    "E_SOURCE_TURN_REQUIRED": 409,
    "E_EMPTY_CONTENT": 409,
    "E_SECRET_IN_CONTENT": 422,
    "E_BREAKER_NOT_NORMAL": 409,
    "E_BREAKER_UNAVAILABLE": 503,
    "E_APPROVER_UNAVAILABLE": 503,
    "E_REVIEW_LOG": 503,
}


def _refuse(code: str, status: int):
    return jsonify({"error": code, "error_code": code, "code": code}), status


def create_procedural_review_blueprint(
    proposals: ProcedureProposals,
    *,
    turn_lock: Callable[[], Any],
    tool_call_running: Callable[[], bool] = lambda: False,
) -> Blueprint:
    """``turn_lock()`` returns the server's turn lock (``None``: refused, fail closed)."""
    bp = Blueprint("procedural_review", __name__)

    def body(allowed: frozenset[str]) -> dict | None:
        data = request.get_json(silent=True)
        if type(data) is not dict or not set(data) <= allowed:
            return None
        if not isinstance(data.get("sha256"), str):
            return None
        return data

    @bp.get(REVIEW_ROUTE)
    def review_state():
        return jsonify(proposals.overview())

    @bp.post(_BASE + "/approve")
    def approve(key: str):
        data = body(_APPROVE_KEYS)
        if data is None or type(data.get("replace", False)) is not bool:
            return _refuse("invalid_request", 400)
        if data.get("confirm") is not True:
            return _refuse("confirmation_required", 428)
        if tool_call_running():
            return _refuse("tool_call_running", 409)
        if proposals.breaker_level is None:
            # No live breaker wired: the panel never approves ungated (fail closed).
            return _refuse("E_BREAKER_UNAVAILABLE", 503)
        held = turn_lock()
        if held is None:
            return _refuse("turn_lock_unavailable", 503)
        lock = TurnLock.of(held)  # ADR 0011 W6b: pre-empts a goal iteration first
        if not lock.acquire(blocking=False):
            return _refuse("turn_running", 409)
        try:
            result = proposals.approve(
                key, sha256=data["sha256"], replace=data.get("replace", False)
            )
        except ProposalRefused as error:
            return _refuse(error.code, _STATUS.get(error.code, 409))
        finally:
            lock.release()
        return jsonify(result)

    @bp.post(_BASE + "/reject")
    def reject(key: str):
        data = body(_REJECT_KEYS)
        if data is None:
            return _refuse("invalid_request", 400)
        if data.get("confirm") is not True:
            return _refuse("confirmation_required", 428)
        try:
            result = proposals.reject(key, sha256=data["sha256"], reason=data.get("reason", ""))
        except ProposalRefused as error:
            return _refuse(error.code, _STATUS.get(error.code, 409))
        return jsonify(result)

    return bp


__all__ = ["REVIEW_ROUTE", "create_procedural_review_blueprint"]
