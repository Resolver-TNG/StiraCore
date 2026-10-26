"""Stage (#341, stage 2): the opt-in /stage page on the chat server.

Off by default (no route, no nav item); STIRA_STAGE=1 adds three read-only page routes
that serve an allow-list of the files under dashboard/stage/. No API route is added: the
page reads the existing masked, same-origin APIs and decides approvals through the existing
decision API (tests/stage_adapter_test.cjs covers the client side).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from stiracore.observability.config_api import ConfigStore
from stiracore.observability.security import CSP
from stiracore.observability.stage import stage_asset_allowed, stage_enabled

ROOT = Path(__file__).resolve().parents[1]
STAGE_ROUTES = {("/stage", "GET"), ("/stage/", "GET"), ("/stage/<path:asset>", "GET")}


def _app(tmp_path, monkeypatch, enabled: bool):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    if enabled:
        monkeypatch.setenv("STIRA_STAGE", "1")
    else:
        monkeypatch.delenv("STIRA_STAGE", raising=False)
    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
    return stira_server.create_app(config_store=store)


def _routes(app) -> set:
    return {
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in rule.methods - {"HEAD", "OPTIONS"}
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("0", False), ("no", False), ("1", True), ("true", True)],
)
def test_stage_is_opt_in(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("STIRA_STAGE", raising=False)
    else:
        monkeypatch.setenv("STIRA_STAGE", value)
    assert stage_enabled() is expected


def test_off_by_default_no_route_no_nav(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, enabled=False)
    assert not _routes(app) & STAGE_ROUTES
    client = app.test_client()
    assert client.get("/stage/").status_code == 404
    assert 'href="/stage/"' not in client.get("/chat").get_data(as_text=True)


def test_enabled_adds_only_three_page_routes_and_the_nav_item(tmp_path, monkeypatch):
    off = _routes(_app(tmp_path / "off", monkeypatch, enabled=False))
    app = _app(tmp_path / "on", monkeypatch, enabled=True)
    on = _routes(app)
    assert on - off == STAGE_ROUTES  # no API route, no POST
    assert all(not rule.startswith("/api/") for rule, _ in on - off)
    assert 'href="/stage/"' in app.test_client().get("/chat").get_data(as_text=True)


def test_page_is_the_live_template_with_strict_csp(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch, enabled=True).test_client()
    redirect = client.get("/stage")
    assert redirect.status_code in (301, 302, 308)
    assert redirect.headers["Location"].endswith("/stage/")
    page = client.get("/stage/")
    assert page.status_code == 200
    assert page.headers["Content-Security-Policy"] == CSP  # unchanged, not loosened
    html = page.get_data(as_text=True)
    assert 'data-stage="live"' in html
    assert re.search(r'<meta name="csrf-token" content="[^"]+">', html)
    assert '"stage.say_idle"' in html  # the dashboard catalog is embedded (ja + en)
    scripts = re.findall(r"<script\b([^>]*)>", html)
    assert all("src=" in attrs or 'type="application/json"' in attrs for attrs in scripts)
    assert not re.search(r"\son[a-z]+=", html, re.IGNORECASE)
    assert not re.search(r"\sstyle=", html, re.IGNORECASE)
    assert not re.search(r"https?://", html)


@pytest.mark.parametrize(
    ("path", "status", "mimetype"),
    [
        ("js/main.js", 200, "text/javascript"),
        ("js/adapter.js", 200, "text/javascript"),
        # #350 phase 2: the board, its renderer and the still fallback
        ("js/markdown.js", 200, "text/javascript"),
        ("js/md_render.js", 200, "text/javascript"),
        ("js/board.js", 200, "text/javascript"),
        ("js/still_scene.js", 200, "text/javascript"),
        ("stage.css", 200, "text/css"),
        ("vendor/three.stage.min.js", 200, "text/javascript"),
        ("assets/char/base.png", 200, "image/png"),
        ("assets/bg/bg.json", 200, "application/json"),
        # not served: the source icon, the offline tools, docs, the standalone demo page
        ("assets/source/resolver-icon.jpg", 404, None),
        ("assets/source/resolver-mask.png", 404, None),
        ("tools/build_char.py", 404, None),
        ("README.md", 404, None),
        ("index.html", 404, None),
        ("js/../tools/build_bg.py", 404, None),
        ("js/%2e%2e/README.md", 404, None),
        ("assets/char/../../tools/build_bg.py", 404, None),
        ("js/missing.js", 404, None),
    ],
)
def test_assets_are_an_allow_list(tmp_path, monkeypatch, path, status, mimetype):
    client = _app(tmp_path, monkeypatch, enabled=True).test_client()
    response = client.get(f"/stage/{path}")
    assert response.status_code == status, path
    if mimetype:
        assert response.mimetype == mimetype
        assert response.headers["Content-Security-Policy"] == CSP


def test_allow_list_rules():
    assert stage_asset_allowed("js/live_ui.js")
    assert stage_asset_allowed("assets/gadgets/laptop.png")  # later packs: generated images
    assert not stage_asset_allowed("assets/source/x.png")
    assert not stage_asset_allowed("js/../x.js")
    assert not stage_asset_allowed("tools/build_bg.py")
    assert not stage_asset_allowed("js/Main.JS")


def test_api_reads_used_by_stage_stay_same_origin(tmp_path, monkeypatch):
    """The page adds no permission: its reads are refused cross-origin like any API read."""
    client = _app(tmp_path, monkeypatch, enabled=True).test_client()
    for url in ("/api/gateway/status", "/api/turns?limit=1", "/api/hitl/pending"):
        assert client.get(url).status_code == 200, url
        refused = client.get(url, headers={"Origin": "https://evil.example"})
        assert refused.status_code == 403, url
    # pages may be linked from elsewhere (only API reads are origin-checked)
    assert client.get("/stage/", headers={"Origin": "https://evil.example"}).status_code == 200


# --- #350 phase 1: the Stage inside the shell, the composer, ui.home ----------------------


def _app_home(tmp_path, monkeypatch, enabled: bool, home: str | None):
    import json

    if home is not None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "gw.json").write_text(json.dumps({"ui": {"home": home}}), encoding="utf-8")
    return _app(tmp_path, monkeypatch, enabled=enabled)


def test_page_is_in_the_shell_with_the_composer(tmp_path, monkeypatch):
    html = (
        _app(tmp_path, monkeypatch, enabled=True)
        .test_client()
        .get("/stage/")
        .get_data(as_text=True)
    )
    assert 'class="app-sidebar"' in html and 'id="app-shell"' in html  # the side menu
    assert re.search(r'<a class="app-nav-link" href="/stage/"\s+aria-current="page"', html)
    assert 'class="app stage-page"' in html
    # composer in the text window; the shared chat core is loaded before the module
    for element_id in (
        "stage-form",
        "stage-input",
        "stage-send",
        "stage-stop",
        "stage-attach",
        "backlog",
        "backlog-list",
        "btn-log",
        "line-live",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert html.index("js/chat_core.js") < html.index('src="js/main.js"')
    assert "<noscript>" in html and 'href="/chat"' in html


def test_stage_adds_no_route_for_the_composer(tmp_path, monkeypatch):
    """Sending from the Stage uses the existing POST /api/chat (CSRF, Idempotency-Key)."""
    off = _routes(_app(tmp_path / "off", monkeypatch, enabled=False))
    on = _routes(_app(tmp_path / "on", monkeypatch, enabled=True))
    assert ("/api/chat", "POST") in off and ("/api/chat", "POST") in on
    assert on - off == STAGE_ROUTES


def test_chat_post_from_the_stage_needs_csrf(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch, enabled=True).test_client()
    client.get("/stage/")
    # the root conftest attaches the token unless told not to
    refused = client.post(
        "/api/chat", json={"message": "hi"}, headers={"Idempotency-Key": "k", "X-Test-No-Csrf": "1"}
    )
    assert refused.status_code == 403


@pytest.mark.parametrize(
    ("enabled", "home", "location", "first"),
    [
        (False, None, "/chat", "chat"),  # default: unchanged
        (True, None, "/chat", "chat"),  # Stage on, ui.home default "chat": unchanged
        (True, "chat", "/chat", "chat"),
        (True, "stage", "/stage/", "stage"),  # opt-in: / is the Stage, listed first
        (False, "stage", "/chat", "chat"),  # Stage off: falls back to the chat
    ],
)
def test_ui_home(tmp_path, monkeypatch, capsys, enabled, home, location, first):
    app = _app_home(tmp_path, monkeypatch, enabled, home)
    response = app.test_client().get("/")
    assert response.status_code in (301, 302, 308)
    assert response.headers["Location"].endswith(location)
    nav = [key for key, *_ in app.extensions["stira_shell"]["nav"]]
    assert nav[0] == first
    assert ("stage" in nav) is enabled
    warned = "ui.home" in capsys.readouterr().out
    assert warned is (home == "stage" and not enabled)


def test_ui_home_validation():
    from stiracore.config import GatewayConfig

    assert GatewayConfig().ui == {"home": "chat"}
    assert GatewayConfig(ui={"home": "stage"}).ui == {"home": "stage"}
    assert GatewayConfig(ui={}).ui == {"home": "chat"}
    for bad in ({"home": "observer"}, {"home": "stage", "x": 1}, "stage", None):
        with pytest.raises(ValueError):
            GatewayConfig(ui=bad)


def test_invalid_ui_home_in_the_file_falls_back_to_the_chat(tmp_path, monkeypatch):
    app = _app_home(tmp_path, monkeypatch, True, "elsewhere")
    assert app.test_client().get("/").headers["Location"].endswith("/chat")


def test_phase2_board_room_canvas_gauge_in_the_page(tmp_path, monkeypatch):
    """#350 phase 2: the whiteboard between the two canvases, the gauge and the still note."""
    html = (
        _app(tmp_path, monkeypatch, enabled=True)
        .test_client()
        .get("/stage/")
        .get_data(as_text=True)
    )
    for element_id in (
        "view-room",
        "view",
        "board",
        "board-body",
        "board-pin",
        "board-copy",
        "board-close",
        "ctx-gauge",
        "stage-still",
    ):
        assert f'id="{element_id}"' in html, element_id
    # back to front in the DOM: room canvas, character canvas, board (z-order is CSS)
    assert html.index('id="view-room"') < html.index('id="view"') < html.index('id="board"')
    assert re.search(r'<section id="board"[^>]*\shidden', html)  # rolled up until a reply
    assert re.search(r'<canvas id="view-room"[^>]*aria-hidden="true"[^>]*hidden', html)
    assert 'aria-pressed="false"' in html


def test_phase3_adds_exactly_the_new_session_route(tmp_path, monkeypatch):
    """The board and the gauge add no route; phase 3 adds only POST /api/chat/session/new.

    It belongs to the chat API (registered with or without the Stage).
    """
    on = _routes(_app(tmp_path / "on", monkeypatch, enabled=True))
    off = _routes(_app(tmp_path / "off", monkeypatch, enabled=False))
    session = {(rule, m) for rule, m in on if "session" in rule or rule.endswith("/new")}
    assert session == {("/api/chat/session/new", "POST")}
    assert ("/api/chat/session/new", "POST") in off
