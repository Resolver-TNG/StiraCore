"""The person-only route for ``page_check``'s localhost ports (ADR 0015 Addendum rev 4, #449).

``GET /api/tools/page-check-ports`` shows the list; ``POST`` replaces it. The list lives
in ``tool-config.json`` (``page_check_ports``, part of the checked tool rules: file, head,
anchor) and no other route sets it: the tool APIs and the hub keep the recorded value
(:meth:`ToolConfig.save`), ``page_check`` has no argument for it, and file tools cannot
write the data directory (the floor). Adding a port is a loosening: it needs the port(s)
typed in the page's confirmation (``confirm`` + ``confirm_ports``), a NORMAL breaker, no
agent tool call running and — with the YOLO allowance — the host confirmation (428, as
every loosening save). The dashboard's own port and ports below 1024 are refused. Every
change is a settings-history row (group ``page_check``; not restorable by a rollback, so a
rollback can never add a port). Removing a port needs none of that.
"""

from __future__ import annotations

from typing import Any

from flask import jsonify, request

from stiracore.tools import page_check as page_check_tool
from stiracore.tools import tool_policy
from stiracore.tools.tool_config import (
    PAGE_CHECK_PORT_MIN,
    PAGE_CHECK_PORTS_MAX,
    ToolConfig,
    ToolRulesLocked,
    checked_page_check_ports,
    level_name,
)
from stiracore.yolo import runtime as yolo_runtime

ROUTE = "/api/tools/page-check-ports"
SOURCE = "api:page_check_ports"
FIELD = "page_check.localhost_ports"
BODY_KEYS = frozenset({"ports", "confirm", "confirm_ports"})


def _error(code: str, message: str, status: int, **extra: Any):
    return jsonify(error=message, error_code=code, errors=[], **extra), status


def _own_ports() -> set[int]:
    ports = set(page_check_tool.own_dashboard_ports())
    try:
        port = int(request.host.rsplit(":", 1)[1]) if ":" in request.host else None
    except ValueError:
        port = None
    if port is not None:
        ports.add(port)
    return ports


def history_entry(before: list[int], after: list[int]) -> dict:
    """One settings-history row: ports only (numbers), its own group."""
    import uuid
    from datetime import UTC, datetime

    return {
        "id": uuid.uuid4().hex[:12],
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SOURCE,
        "changes": [
            {
                "field": FIELD,
                "added": sorted(set(after) - set(before)),
                "removed": sorted(set(before) - set(after)),
            }
        ],
        "before": {"page_check": {"localhost_ports": sorted(before)}},
        "after": {"page_check": {"localhost_ports": sorted(after)}},
        "redacted_fields": [],
    }


def register(
    blueprint,
    config_store,
    *,
    breaker_level,
    host_confirmation,
    commit_audited,
    tool_rules_saved,
    locked_body,
) -> None:
    """Add the two routes to the config blueprint (its helpers passed in)."""

    @blueprint.get(ROUTE)
    def get_page_check_ports():
        with config_store.lock:
            try:
                config = config_store.load_tools()
            except (OSError, ValueError, TypeError):
                return _error("tool_rules_unavailable", "Tool rules unavailable.", 503)
        locked = bool(config.integrity_error)
        return jsonify(
            ports=[] if locked else list(config.page_check_ports or []),
            locked=locked,
            max_ports=PAGE_CHECK_PORTS_MAX,
            min_port=PAGE_CHECK_PORT_MIN,
            refused_ports=sorted(_own_ports()),
        )

    @blueprint.post(ROUTE)
    def post_page_check_ports():
        if not request.is_json:
            return jsonify(error="Content-Type must be application/json"), 415
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - BODY_KEYS or "ports" not in data:
            return _error("invalid_fields", "Send ports (and confirm, confirm_ports).", 400)
        raw = data["ports"]
        if isinstance(raw, list) and any(
            type(p) is int and 0 <= p < PAGE_CHECK_PORT_MIN for p in raw
        ):
            return _error("port_refused", "Ports below 1024 are not allowed.", 400)
        try:
            ports = checked_page_check_ports(raw)
        except ValueError as error:
            return _error("invalid_ports", str(error), 400)
        own = sorted(set(ports) & _own_ports())
        if own:
            return _error("own_dashboard", "The dashboard's own port is never allowed.", 400)
        with config_store.lock:
            try:
                current = config_store.load_tools()
            except (OSError, ValueError, TypeError):
                return _error("tool_rules_unavailable", "Tool rules unavailable.", 503)
            if current.integrity_error:
                return jsonify(locked_body(current.integrity_error)), 409
            before = list(current.page_check_ports or [])
            added = sorted(set(ports) - set(before))
            if ports == before:
                return jsonify(ports=before, saved=False)
            if added:
                level = level_name(breaker_level())
                if level not in tool_policy.LOOSENING_LEVELS:
                    return _error(
                        "breaker_active",
                        "Ports cannot be added while the breaker is active.",
                        403,
                        breaker_level=level,
                    )
                typed = data.get("confirm_ports")
                if data.get("confirm") is not True or not (
                    isinstance(typed, list)
                    and all(type(p) is int for p in typed)
                    and sorted(set(typed)) == added
                    and len(typed) == len(added)
                ):
                    return _error(
                        "needs_confirmation",
                        "Type the port(s) being added to confirm.",
                        400,
                        needs_confirmation=["page_check_ports"],
                        needs_ports=added,
                    )
                running = yolo_runtime.tool_call_refusal()
                if running is not None:
                    return jsonify({**running, "errors": []}), 409
                gate = host_confirmation(
                    False,
                    data,
                    {"page_check_ports": before},
                    SOURCE,
                    ["page_check_ports"],
                )
                if gate is not None:
                    return gate
            candidate = ToolConfig(**current.as_dict(), page_check_ports=ports, path=current.path)
            committed = []

            def commit(precommit):
                candidate.save(precommit=precommit)
                committed.append(True)

            try:
                response = commit_audited(
                    history_entry(before, ports),
                    bool(added),
                    commit,
                    lambda _outcome, _entry: jsonify(ports=ports, saved=True),
                )
            except ToolRulesLocked as error:
                return jsonify(locked_body(error)), 409
            except OSError:
                return _error("save_failed", "Could not persist the tool rules.", 500)
            if committed:
                failure = tool_rules_saved()
                if failure is not None:
                    return failure
            return response
