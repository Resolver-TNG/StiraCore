"""Tool settings routes; use the dashboard's existing authentication boundary."""

from flask import Blueprint, jsonify, redirect, request

from stiracore.tools import tool_registry as registry
from stiracore.tools.tool_config import ALL_BLOCKED, WRITE_BLOCKED, ToolConfig, level_name


def _hub_tools_url(query_string: bytes) -> str:
    """/tools now lives in the settings hub (#227); keep e.g. ``?lang=en``."""
    query = query_string.decode("latin-1")
    return f"/settings?{query}#sec-tools" if query else "/settings#sec-tools"


def create_tool_config_blueprint(config: ToolConfig, get_breaker_level=lambda: None):
    """Share config with the agent; get_breaker_level must read live state without pre_turn."""
    bp = Blueprint("tool_config", __name__)

    @bp.get("/tools")
    def page():
        # Tool permissions live in the settings hub (#227).
        return redirect(_hub_tools_url(request.query_string), code=302)

    @bp.get("/api/tools/config")
    def get_config():
        return jsonify(config.as_dict())

    @bp.post("/api/tools/config")
    def update_config():
        if request.headers.get("Origin") not in (None, request.host_url.rstrip("/")):
            return jsonify(error="Cross-origin updates are not permitted"), 403
        if not request.is_json:
            return jsonify(error="Content-Type must be application/json"), 415
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - {"mode", "enabled_tools", "shell_allowlist"}:
            return jsonify(error="Invalid configuration fields"), 400
        with config.lock:
            try:
                candidate = ToolConfig(**{**config.as_dict(), **data}, path=config.path)
            except (TypeError, ValueError) as exc:
                return jsonify(error=str(exc)), 400
            try:
                candidate.save()
            except OSError:
                return jsonify(error="Could not persist tool configuration"), 500
            config.mode, config.enabled_tools, config.shell_allowlist = (
                candidate.mode,
                candidate.enabled_tools,
                candidate.shell_allowlist,
            )
            return jsonify(config.as_dict())

    @bp.get("/api/tools/list")
    def list_tools():
        with config.lock:
            level = level_name(get_breaker_level())
            rows = []
            for name in sorted(set(registry.READ_TOOLS) | set(registry.WRITE_TOOLS)):
                write = name in registry.WRITE_TOOLS
                configured = config.mode == "trust_all" or name in config.enabled_tools
                blocked = level in ALL_BLOCKED or (write and level in WRITE_BLOCKED)
                rows.append(
                    {
                        "name": name,
                        "category": "write" if write else "read",
                        "configured": configured,
                        "enabled": configured and not blocked,
                        "blocked_by_breaker": blocked,
                    }
                )
            return jsonify(tools=rows, breaker_level=level)

    return bp
