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
_GUARD_CODE = re.compile(r"^[a-z_]{1,40}$")
_GUARD_UNREADABLE = {
    "guard": "unverified",
    "guard_code": "guard_unreadable",
    "axis": "axis_unreadable",
    "measured_age_s": None,
}


def _page_guard_row(read: Callable[[], Any]) -> dict[str, Any]:
    """Three fixed codes and whole seconds, read now from memory; anything else (free
    text, a path, an error) is shown as unreadable (fail closed)."""
    try:
        row = read()
        codes = {key: row[key] for key in ("guard", "guard_code", "axis")}
        if not all(isinstance(v, str) and _GUARD_CODE.match(v) for v in codes.values()):
            return dict(_GUARD_UNREADABLE)
        age = row.get("measured_age_s")
        ok = isinstance(age, int) and not isinstance(age, bool) and age >= 0
        return {**codes, "measured_age_s": age if ok else None}
    except Exception:  # noqa: BLE001 - the row never fails the report
        return dict(_GUARD_UNREADABLE)


def create_diagnostics_blueprint(
    board: WriteFailureBoard,
    *,
    doctor_report: Callable[[], dict[str, Any] | None],
    run_doctor: Callable[[], dict[str, Any]],
    page_guard: Callable[[], Any] | None = None,
) -> Blueprint:
    """``page_guard`` (ADR 0015 Addendum rev 6, S1): a reader of the page tools' guard
    state. Its row is added to the doctor report as ``page_guard`` — read-only, display
    only; no route here accepts a value for it."""
    bp = Blueprint("diagnostics", __name__)

    def shown(report: dict[str, Any]) -> dict[str, Any]:
        if page_guard is None or not isinstance(report, dict):
            return report
        return {**report, "page_guard": _page_guard_row(page_guard)}

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
        return jsonify(shown(report)), 200, _NO_STORE

    @bp.post("/api/diagnostics/doctor")
    def doctor_rerun():
        return jsonify(shown(run_doctor())), 200, _NO_STORE

    return bp
