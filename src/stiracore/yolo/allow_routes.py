"""Flask routes of the session allowance list (ADR 0025 §5, §7; PR 2).

* ``GET /api/allowances`` — the live allowances of this session (tool, class in clear,
  time left, uses) and the recent automatic answers (the chat's log lines). Changes
  nothing; the class text goes to the page only.
* ``POST /api/allowances/<allow_id>/revoke`` — the person revokes one (its own
  generation + 1, ``revoked``). A narrowing: no confirmation, no breaker check.
* ``POST /api/allowances/revoke-all`` — 「すべて取り消し」: fence + 1 (``stop``, Q5).

Installed on every chat server (with or without ``STIRA_YOLO_ALLOW``): the list is empty
until PR 3 turns the card buttons on. CSRF / Origin / Host checks are the server's
``install_security`` (installed before every route).
"""

from __future__ import annotations

from flask import Flask, jsonify

from . import allow_runtime
from .session_allow import publish_end, table

__all__ = ["install"]

_NO_STORE = {"Cache-Control": "no-store"}


def install(app: Flask) -> None:
    @app.get("/api/allowances")
    def allowances_status():
        return jsonify(allow_runtime.status()), 200, _NO_STORE

    @app.post("/api/allowances/<allow_id>/revoke")
    def allowances_revoke(allow_id):
        if not isinstance(allow_id, str) or not 1 <= len(allow_id) <= 64:
            return jsonify(error="unknown allowance", code="not_found"), 404
        ended = table().revoke(allow_id)
        body = allow_runtime.status()
        if not ended:
            # Already ended (or unknown): the list the page draws is the current one.
            return jsonify({**body, "code": "not_found"}), 404, _NO_STORE
        return jsonify({**body, "revoked": allow_id[:8]}), 200, _NO_STORE

    @app.post("/api/allowances/revoke-all")
    def allowances_revoke_all():
        publish_end("stop")  # every allowance and every view / save token ends first
        return jsonify({**allow_runtime.status(), "revoked": "all"}), 200, _NO_STORE
