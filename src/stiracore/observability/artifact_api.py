"""API of the workspace artifacts (#454, ADR 0020).

Registered on the chat server over :class:`~stiracore.tools.workspace_artifacts.
WorkspaceArtifacts`. The reads are same-origin (``install_security``); the list, detail and
version routes show only what the registration rules allow *now* (a path that became
protected or a symlink later is not shown). Bodies come only from the registry's snapshot,
never from the live workspace file:

* ``.../versions/<n>`` returns a text kind's text as JSON;
* ``.../versions/<n>/raw`` serves the snapshot as a **sandboxed document**
  (``PREVIEW_CSP``: ``sandbox allow-scripts`` = an opaque origin even when opened
  top-level, no network, framed only by the dashboard) for the page's
  ``<iframe sandbox="allow-scripts">`` and ``<img>`` (ADR 0020 §6, refining ADR 0015 §8).

``POST /api/artifacts/<id>/labels`` is a person's change (CSRF like every dashboard write;
no agent tool reaches it): ``{labels: [...]}`` from a fixed set, one audit row written
before the change (503 ``audit_failed``: nothing changed), refused while an agent tool call
runs (409 ``tool_call_running``, with YOLO's tracker).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from flask import Blueprint, Response, g, jsonify, request

from stiracore.tools.workspace_artifacts import (
    KINDS,
    MAX_LIST,
    WorkspaceArtifacts,
    media_type,
)

# The preview's own policy (replaces the dashboard CSP on that one response, #454).
PREVIEW_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:; "
    "connect-src 'none'; frame-src 'none'; child-src 'none'; worker-src 'none'; "
    "manifest-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'; "
    "frame-ancestors 'self'"
)
_PREVIEW_KINDS = frozenset({"html", "image", "markdown", "code", "text"})

_ID = re.compile(r"a[0-9a-f]{20}")
_TURN = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_TEXT_KINDS = frozenset({"html", "markdown", "code", "text"})


class _AuditFailed(Exception):
    """The label change's audit row could not be written (nothing is changed)."""


def _int_arg(name: str, default: int | None = None) -> int | None:
    value = request.args.get(name)
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number >= 0 else None


def create_artifact_blueprint(
    registry: WorkspaceArtifacts | None,
    policy_provider: Callable[[], Any],
    *,
    audit: Callable[..., Any] | None = None,
    principal: Callable[[], str] = lambda: "local:dashboard",
    tool_call_running: Callable[[], bool] = lambda: False,
) -> Blueprint:
    """``registry=None`` (its directory failed its check at start-up): every route 503.

    ``audit`` is ``AuditChain.record_artifact`` (``None``: labels are refused, 503).
    """
    bp = Blueprint("artifacts", __name__)

    @bp.before_request
    def _available():
        if registry is None:
            return jsonify({"error": "artifacts_unavailable"}), 503
        return None

    def _bad(code: str):
        return jsonify({"error": code}), 400

    def _missing():
        return jsonify({"error": "not_found"}), 404

    @bp.get("/api/artifacts")
    def list_artifacts():
        kind = request.args.get("kind") or None
        if kind is not None and kind not in KINDS:
            return _bad("invalid_kind")
        turn = request.args.get("turn") or None
        if turn is not None and not _TURN.fullmatch(turn):
            return _bad("invalid_turn")
        limit = _int_arg("limit", 50)
        if limit is None or limit < 1:
            return _bad("invalid_limit")
        query = (request.args.get("q") or "").strip() or None
        items = registry.list(
            policy_provider(), kind=kind, turn_id=turn, query=query, limit=min(limit, MAX_LIST)
        )
        return jsonify({"artifacts": items, **registry.status()})

    @bp.get("/api/artifacts/<artifact_id>")
    def get_artifact(artifact_id):
        if not _ID.fullmatch(artifact_id):
            return _missing()
        artifact = registry.get(policy_provider(), artifact_id)
        return jsonify(artifact) if artifact is not None else _missing()

    @bp.get("/api/artifacts/<artifact_id>/versions/<int:version>")
    def get_version(artifact_id, version):
        if not _ID.fullmatch(artifact_id):
            return _missing()
        found = registry.content(policy_provider(), artifact_id, version)
        if found is None:
            return _missing()
        artifact, entry, data = found["artifact"], found["version"], found["data"]
        body: dict[str, Any] = {
            "id": artifact["id"],
            "path": artifact["path"],
            "kind": artifact["kind"],
            "labels": artifact["labels"],
            **entry,
            "text": None,
            "text_reason": None,
        }
        if data is None:
            body["text_reason"] = "not_stored"
        elif artifact["kind"] not in _TEXT_KINDS:
            body["text_reason"] = "binary"
        else:
            try:
                body["text"] = data.decode("utf-8")
            except UnicodeDecodeError:
                body["text_reason"] = "binary"
        return jsonify(body)

    @bp.get("/api/artifacts/<artifact_id>/diff")
    def diff_versions(artifact_id):
        if not _ID.fullmatch(artifact_id):
            return _missing()
        old, new = _int_arg("from"), _int_arg("to")
        if old is None or new is None:
            return _bad("invalid_version")
        result = registry.diff(policy_provider(), artifact_id, old, new)
        return jsonify(result) if result is not None else _missing()

    @bp.get("/api/artifacts/<artifact_id>/versions/<int:version>/raw")
    def raw_version(artifact_id, version):
        if not _ID.fullmatch(artifact_id):
            return _missing()
        found = registry.content(policy_provider(), artifact_id, version)
        if found is None or found["artifact"]["kind"] not in _PREVIEW_KINDS:
            return _missing()
        if found["data"] is None:
            return jsonify({"error": "not_stored"}), 404
        artifact = found["artifact"]
        mime = media_type(artifact["path"], artifact["kind"])
        if mime.startswith("text/"):
            mime += "; charset=utf-8"
        response = Response(found["data"], mimetype=None, content_type=mime)
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Disposition"] = "inline"
        g.stira_csp = PREVIEW_CSP  # install_security applies it instead of the page CSP
        return response

    @bp.post("/api/artifacts/<artifact_id>/labels")
    def set_labels(artifact_id):
        if not _ID.fullmatch(artifact_id):
            return _missing()
        data = request.get_json(silent=True)
        if type(data) is not dict or set(data) != {"labels"}:
            return _bad("invalid_request")
        if tool_call_running():
            return jsonify({"error": "tool_call_running"}), 409
        if audit is None:
            return jsonify({"error": "audit_unavailable"}), 503
        who = principal()

        def write_audit(change: dict) -> None:
            try:
                audit(
                    "labeled",
                    artifact_id=change["id"],
                    labels=change["labels"],
                    previous=change["previous"],
                    version=change["version"],
                    principal=who,
                )
            except Exception:  # noqa: BLE001 - any failure: refuse the change
                raise _AuditFailed from None

        try:
            result = registry.set_labels(
                policy_provider(),
                artifact_id,
                data["labels"],
                principal=who,
                before_write=write_audit,
            )
        except ValueError:
            return _bad("invalid_labels")
        except _AuditFailed:
            return jsonify({"error": "audit_failed"}), 503
        return jsonify(result) if result is not None else _missing()

    return bp
