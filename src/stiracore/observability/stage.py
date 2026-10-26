"""Stage (#341): the opt-in pixel-art view of the agent, served by the chat server.

Off by default. ``STIRA_STAGE=1`` registers three GET page routes:

- ``/stage`` redirects to ``/stage/``;
- ``/stage/`` renders ``templates/stage.html`` inside the dashboard shell (base.html: side
  menu, i18n catalog, CSRF meta tag; no inline script);
- ``/stage/<asset>`` serves only the files the page loads (an allow-list: the
  stylesheet, ``js/*.js``, the bundled three.js and the generated images and metadata
  under ``assets/<dir>/``). The offline build tools and ``assets/source/`` (the source
  icon) are not served.

Stage reads the same masked, same-origin APIs as the other pages (``/api/gateway/status``,
``/api/turns``, ``/api/turns/<id>/events``, ``/api/hitl/pending``, ``/api/chat/history``)
and writes only through two existing routes: approvals through ``/api/hitl/<id>/decision``
(CSRF, ``args_hash``, ``Idempotency-Key``) and chat messages through ``POST /api/chat``
(CSRF, ``Idempotency-Key``; the chat page's own send logic, ``static/js/chat_core.js``,
#350). It adds no API route and no permission. Gateway ``ui.board_svg`` (#453, settings page,
default off) only adds ``data-board-svg="on"`` to the page.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from flask import abort, redirect, render_template, send_from_directory

STAGE_ENV = "STIRA_STAGE"
# Relative paths the page may load. Anything else under dashboard/stage/ is a 404.
_ASSETS = re.compile(
    r"(?:stage\.css"
    r"|js/[a-z0-9_-]+\.js"
    r"|vendor/three\.stage\.min\.js"
    r"|vendor/LICENSE\.three\.txt"
    # generated images/metadata (bg, char, later packs); never the source icon
    r"|assets/(?!source/)[a-z0-9_-]+/[a-z0-9_-]+\.(?:png|json))"
)
_MIMETYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".png": "image/png",
    ".json": "application/json",
    ".txt": "text/plain",
}


def stage_enabled() -> bool:
    """Opt-in only: ``STIRA_STAGE=1`` (or true/yes/on) turns the page on."""
    raw = os.environ.get(STAGE_ENV, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def stage_asset_allowed(path: str) -> bool:
    return bool(_ASSETS.fullmatch(path)) and ".." not in path


def register_stage(app, stage_dir: Path, board_svg: Callable[[], bool] | None = None) -> None:
    """Add the Stage page routes to ``app`` (call only when :func:`stage_enabled`).

    ``board_svg`` (#453): read on every page load; true writes ``data-board-svg="on"`` on the
    page, which turns ```svg blocks on the board into pictures (``js/svg_safe.js``). Any error
    reading it (or none given) is off.
    """
    root = Path(stage_dir).resolve()

    def svg_on() -> bool:
        if board_svg is None:
            return False
        try:
            return board_svg() is True
        except Exception:  # noqa: BLE001 - a broken config never turns pictures on
            return False

    @app.get("/stage")
    def stage_redirect():
        # Relative asset URLs in the page resolve against /stage/.
        return redirect("/stage/")

    @app.get("/stage/")
    def stage_page():
        return render_template("stage.html", board_svg=svg_on())

    @app.get("/stage/<path:asset>")
    def stage_asset(asset: str):
        if not stage_asset_allowed(asset):
            abort(404)
        response = send_from_directory(
            root, asset, mimetype=_MIMETYPES.get(Path(asset).suffix), max_age=0
        )
        response.headers["Cache-Control"] = "no-cache"
        return response


__all__ = ["STAGE_ENV", "register_stage", "stage_asset_allowed", "stage_enabled"]
