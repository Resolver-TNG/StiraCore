"""Permission cards and ``stira doctor`` over HTTP (#244).

Read routes return masked data only (home → ``~``, user name → ``<user>``,
secrets masked, #185). The state-changing routes are CSRF-protected like every
other POST (``install_security``). None of them writes a file for the agent,
changes a setting or calls the model: ``retry`` consumes the card's single
retry and re-checks the cause read-only; the actual retry is a chat turn the
page sends, through every gate. ``doctor`` re-runs the read-only check.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from flask import Blueprint, jsonify

from stiracore.diagnostics.write_errors import WriteFailureBoard

_CARD_ID = re.compile(r"^[0-9a-f]{16}$")
_RETRY_STATUS = {"ok": 200, "not_found": 404, "not_retryable": 409, "already_retried": 409}
_NO_STORE = {"Cache-Control": "no-store"}


def create_diagnostics_blueprint(
    board: WriteFailureBoard,
    *,
    doctor_report: Callable[[], dict[str, Any] | None],
    run_doctor: Callable[[], dict[str, Any]],
) -> Blueprint:
    bp = Blueprint("diagnostics", __name__)

    @bp.get("/api/diagnostics/write-failures")
    def write_failures():
        return jsonify(cards=board.cards(), server_time=time.time()), 200, _NO_STORE

    @bp.post("/api/diagnostics/write-failures/<card_id>/retry")
    def retry(card_id: str):
        if not _CARD_ID.match(card_id):
            return jsonify(error="unknown card", code="not_found"), 404
        outcome, card = board.retry(card_id)
        if outcome != "ok":
            return jsonify(error=outcome, code=outcome, card=card), _RETRY_STATUS[outcome]
        return jsonify(card=card), 200, _NO_STORE

    @bp.post("/api/diagnostics/write-failures/<card_id>/dismiss")
    def dismiss(card_id: str):
        if not _CARD_ID.match(card_id) or not board.dismiss(card_id):
            return jsonify(error="unknown card", code="not_found"), 404
        return jsonify(status="dismissed"), 200

    @bp.get("/api/diagnostics/doctor")
    def doctor():
        report = doctor_report()
        if report is None:
            return jsonify(error="the doctor has not run", code="not_run"), 404
        return jsonify(report), 200, _NO_STORE

    @bp.post("/api/diagnostics/doctor")
    def doctor_rerun():
        return jsonify(run_doctor()), 200, _NO_STORE

    return bp
