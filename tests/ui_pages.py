"""Render shell pages to standalone HTML strings for a11y checks (#197)."""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
STATIC = DASHBOARD / "static"
CSRF_VALUE = "test-csrf-token"
PAGES = {
    "chat": "chat.html",
    "settings": "settings.html",
    "observability": "observability.html",
    "observer": "index.html",
    "forest": "forest.html",
    "memory": "memory.html",
    "artifacts": "artifacts.html",
    "approvals": "approvals.html",
    "schedules": "schedules.html",
    # Stage (#341, opt-in): its own full-screen page, same catalog
    "stage": "stage.html",
}
# Extra template context for pages whose route passes variables.
CONTEXT = {"observer": {"stimuli": ["joy", "threat"]}}
# ADR 0012 phase 5b: the chat and Stage pages as a server with STIRA_YOLO_ALLOW renders
# them (the YOLO slot, its CSS and script).
VARIANTS = {
    "chat_yolo": ("chat.html", {"yolo_ui": True}),
    "stage_yolo": ("stage.html", {"yolo_ui": True}),
    # ADR 0011 W4: the settings page as a server with autonomy.stage_c renders it.
    "settings_grants": ("settings.html", {"grants_ui": True}),
    # W6d: with autonomy.heartbeat_start the issue form has the heartbeat-start switch.
    "settings_grants_tick": (
        "settings.html",
        {"grants_ui": True, "grants_exec_ui": True, "grants_tick_ui": True},
    ),
}
CONFIG_MODULE_CANDIDATES = (
    "dashboard.config_api",
    "dashboard.stira_server",
    "stira_server.config_api",
    "stira_server",
    "config_api",
)
LINK_RE = re.compile(r'<link\s+rel="stylesheet"\s+href="([^"]+)"\s*/?>')


def _find_blueprint_factory():
    for name in CONFIG_MODULE_CANDIDATES:
        try:
            module = importlib.import_module(name)
        except Exception:  # noqa: BLE001, S112 - optional candidates
            continue
        factory = getattr(module, "create_config_blueprint", None)
        if factory is not None:
            return factory
    return None


def _build_blueprint(factory):
    kwargs = {}
    for pname, param in inspect.signature(factory).parameters.items():
        kinds = (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
        if param.default is param.empty and param.kind in kinds:
            kwargs[pname] = MagicMock()
    return factory(**kwargs)


def _inline_css(html: str) -> str:
    def repl(match: re.Match[str]) -> str:
        href = match.group(1)
        if not href.startswith("/static/"):
            return match.group(0)
        path = STATIC / href[len("/static/") :].split("?", 1)[0]
        if not path.is_file():
            return match.group(0)
        return "<style>\n" + path.read_text(encoding="utf-8") + "\n</style>"

    return LINK_RE.sub(repl, html)


def render_pages(tmp_dir: Path | str) -> dict[str, str]:
    """Return {page: html}. Raises RuntimeError if the blueprint is unavailable."""
    import flask

    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    factory = _find_blueprint_factory()
    if factory is None:
        raise RuntimeError("create_config_blueprint not importable")
    app = flask.Flask(
        __name__,
        template_folder=str(DASHBOARD / "templates"),
        static_folder=str(STATIC),
    )
    from stiracore.observability.i18n import install_i18n

    install_i18n(app)
    app.register_blueprint(_build_blueprint(factory))
    app.add_url_rule("/chat", "chat_page", lambda: "")

    @app.context_processor
    def _csrf():
        return {"csrf_token": lambda: CSRF_VALUE}

    out: dict[str, str] = {}
    with app.test_request_context("/"):
        for key, template in PAGES.items():
            out[key] = _inline_css(flask.render_template(template, **CONTEXT.get(key, {})))
        for key, (template, context) in VARIANTS.items():
            out[key] = _inline_css(flask.render_template(template, **context))
    return out
