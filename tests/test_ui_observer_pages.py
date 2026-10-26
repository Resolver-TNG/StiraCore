"""Observer/stira pages on the shared shell (#183 / #184 stage 2).

- each app passes its own nav (install_shell); every nav link is served by that app
- observer pages extend base.html, have no inline style/script/colours
- rendered text is catalog-keyed (no hard-coded UI strings), JS has no CJK literals
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock

import pytest

flask = pytest.importorskip("flask")

from stiracore.core import EMOTION_MATRIX, NeuralState
from stiracore.observability.shell import (
    DEFAULT_NAV,
    NAV_ITEMS,
    install_shell,
    nav_entries,
    observer_nav,
)

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
STATIC = DASH / "static"
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
CJK = re.compile("[぀-ヿ㐀-鿿]")

# Observer-dashboard pages migrated in stage 2: path -> (template, page scripts, page css)
OBSERVER_PAGES = {
    "/": ("index.html", ("app.js",), "style.css"),
    "/observability": (
        "observability.html",
        ("js/observability.js", "js/neural_viz.js"),
        "css/observability.css",
    ),
}
# stira pages (stage-1 pages are re-checked here with the stricter rules): path -> same tuple
STIRA_PAGES = {
    "/memory": ("forest.html", ("js/forest.js",), "css/forest.css"),
    "/chat": ("chat.html", ("js/chat.js",), "css/chat.css"),
    "/settings": ("settings.html", ("js/settings.js", "js/settings_hub.js"), "css/settings.css"),
    "/artifacts": ("artifacts.html", ("js/artifacts.js",), "css/artifacts.css"),  # #454
}
# Templates migrated to the shell but not routed by either server (no API registered).
UNROUTED = {
    "memory.html": (("js/memory.js",), "css/memory.css"),
}
ALL_ASSETS = [
    *OBSERVER_PAGES.values(),
    *STIRA_PAGES.values(),
    ("sparkline", ("js/sparkline.js",), "css/chat.css"),
]
ALL_ASSETS += [(name, *assets) for name, assets in UNROUTED.items()]
# Text that is intentionally not translated (brand / symbols / language names).
UNKEYED_ALLOWED = {
    *("STIRACORE", "StiraCore NXP", "日本語", "English", "—", "$0.00"),
    # observer signal names / identifiers shown verbatim
    *("GO", "HOLD", "NO-GO", "BC", "🔒 L3", "bcScore / 1.00", "emotion_transcription"),
    *("STIRA ENGINE", "0 / 10", "slug", "markdown", "json", "html", "svg", "text"),
}


class _Page(HTMLParser):
    """Collect nav links, inline style/script and text nodes without a data-i18n ancestor."""

    VOID = frozenset({"meta", "link", "input", "br", "img", "hr", "source", "area", "col", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.nav: list[tuple[str | None, str | None]] = []
        self.ids: set[str] = set()
        self.style_attrs = 0
        self.style_blocks = 0
        self.inline_scripts: list[str] = []
        self.handlers: list[str] = []
        self.unkeyed: list[str] = []
        self.scripts: list[str] = []
        self._script_type: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if "style" in a:
            self.style_attrs += 1
        self.handlers += [k for k in a if k.startswith("on")]
        if tag == "style":
            self.style_blocks += 1
        if tag == "script":
            self._script_type = a.get("type") or "text/javascript"
            if a.get("src"):
                self.scripts.append(a["src"])
        if tag == "a" and "app-nav-link" in (a.get("class") or "").split():
            self.nav.append((a.get("href"), a.get("aria-current")))
        if tag in self.VOID:
            return
        # data-user-text: operator-written content shown as written (identity anchors,
        # #152), not UI text, so it has no catalog key.
        keyed = "data-i18n" in a or "data-user-text" in a or (self.stack and self.stack[-1][1])
        self.stack.append((tag, bool(keyed)))

    def handle_endtag(self, tag):
        if tag == "script":
            self._script_type = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text:
            return
        if self._script_type is not None:
            if self._script_type != "application/json":
                self.inline_scripts.append(text[:60])
            return
        if self.stack and self.stack[-1][0] in ("style", "title", "noscript"):
            return
        keyed = bool(self.stack and self.stack[-1][1])
        if not keyed and text not in UNKEYED_ALLOWED and re.search(r"[A-Za-z぀-鿿]", text):
            self.unkeyed.append(text)


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    return page


@pytest.fixture
def observer(tmp_path):
    from dashboard.app import create_app

    path = tmp_path / "state.json"
    NeuralState(stability=0.9).save(path)
    return create_app(
        path, tmp_path / "gateway.json", tmp_path / "agent.json", tmp_path / "tools.json"
    )


@pytest.fixture
def stira(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))

    def build_agent(**kwargs):
        return Mock(
            return_value="Hello",
            messages=list(kwargs.get("messages") or []),
            system_prompt=kwargs.get("system_prompt", ""),
        )

    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=build_agent))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    return stira_server.create_app()


def _nav_of(app, path):
    response = app.test_client().get(path)
    assert response.status_code == 200, path
    return _parse(response.get_data(as_text=True)), response.get_data(as_text=True)


def test_nav_entries_validate_keys():
    assert [href for _, href, _ in nav_entries(DEFAULT_NAV)] == [
        "/chat",
        "/settings",
        "/observability",
    ]
    with pytest.raises(ValueError):
        nav_entries(["nope"])
    assert all(label.startswith("base.") for _, label in NAV_ITEMS.values())


def test_observer_nav_has_no_chat_without_agent(observer):
    page, html = _nav_of(observer, "/observability")
    assert [href for href, _ in page.nav] == ["/", "/observability", "/settings"]
    assert [href for href, cur in page.nav if cur == "page"] == ["/observability"]
    # No HITL broker: no approvals chip, breaker comes from the observer state file.
    assert 'id="nav-approvals"' not in html
    assert 'data-status-url="/api/state"' in html
    assert 'data-approvals-url=""' in html


def test_observer_nav_with_chat_agent(tmp_path):
    from dashboard.app import create_app

    path = tmp_path / "state.json"
    NeuralState().save(path)
    app = create_app(path, tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json", Mock())
    page, html = _nav_of(app, "/observability")
    assert [href for href, _ in page.nav] == ["/", "/chat", "/observability", "/settings"]
    assert 'id="nav-approvals"' in html
    assert observer_nav(True)[1] == "chat"


@pytest.mark.parametrize("fixture", ["observer", "stira"])
def test_every_nav_link_is_served(fixture, request):
    app = request.getfixturevalue(fixture)
    client = app.test_client()
    page, _ = _nav_of(app, "/observability")
    assert page.nav
    for href, _ in page.nav:
        response = client.get(href, follow_redirects=True)
        assert response.status_code == 200, f"{fixture}: {href} -> {response.status_code}"
        assert b'id="app-shell"' in response.data, f"{fixture}: {href} is not on the shell"


def test_stira_nav_and_status(stira):
    page, html = _nav_of(stira, "/observability")
    assert [href for href, _ in page.nav] == [
        "/chat",
        "/approvals",
        "/schedules",
        "/memory",
        "/artifacts",  # #454
        "/observability",
        "/settings",
    ]
    assert 'data-status-url="/api/gateway/status"' in html
    # The pending-approvals chip links to the approvals page (#189).
    assert 'href="/approvals" id="nav-approvals"' in html


def test_install_shell_overrides_default_nav():
    app = flask.Flask(__name__, template_folder=str(DASH / "templates"))
    install_shell(app, ["chat"], status_url="/s", approvals_url=None)
    with app.test_request_context("/"):
        html = flask.render_template_string(
            '{% extends "base.html" %}{% block content %}x{% endblock %}'
        )
    page = _parse(html)
    assert [href for href, _ in page.nav] == ["/chat"]
    assert 'data-status-url="/s"' in html


def _render_unrouted(observer, template):
    with observer.test_request_context("/"):
        return flask.render_template(template)


def _pages(observer, stira):
    """Yield (name, parsed page, html, template, scripts) for every shell page."""
    for apps, pages in ((observer, OBSERVER_PAGES), (stira, STIRA_PAGES)):
        for path, (template, scripts, _css) in pages.items():
            page, html = _nav_of(apps, path)
            yield path, page, html, template, scripts
    for template, (scripts, _css) in UNROUTED.items():
        html = _render_unrouted(observer, template)
        yield template, _parse(html), html, template, scripts


def test_pages_use_shell_without_inline_code(observer, stira):
    seen = 0
    for name, page, html, template, scripts in _pages(observer, stira):
        source = (DASH / "templates" / template).read_text(encoding="utf-8")
        assert source.startswith('{% extends "base.html" %}'), name
        assert page.style_attrs == 0, name
        assert page.style_blocks == 0, name
        assert page.handlers == [], name
        assert page.inline_scripts == [], name
        for script in scripts:
            assert any(src.endswith(script) for src in page.scripts), (name, script)
        assert html.count("<main") == 1, name
        seen += 1
    assert seen == len(OBSERVER_PAGES) + len(STIRA_PAGES) + len(UNROUTED)


def test_page_text_is_catalog_keyed(observer, stira):
    for name, page, _html, _template, _scripts in _pages(observer, stira):
        assert page.unkeyed == [], f"{name}: hard-coded text {page.unkeyed[:5]}"


def test_js_ids_exist_in_pages(observer, stira):
    pattern = re.compile(
        r"""getElementById\(\s*["']([^"']+)["']\s*\)|\$\(\s*["']([^"']+)["']\s*\)"""
    )
    for name, page, _html, _template, scripts in _pages(observer, stira):
        for script in scripts:
            source = (STATIC / script).read_text(encoding="utf-8")
            ids = {a or b for a, b in pattern.findall(source)}
            missing = sorted(ids - page.ids)
            assert not missing, f"{name}: {script} references missing ids {missing}"


@pytest.mark.parametrize("assets", ALL_ASSETS, ids=lambda a: a[0])
def test_page_css_and_js_have_no_colour_literals(assets):
    _template, scripts, css = assets
    sheet = re.sub(r"/\*.*?\*/", "", (STATIC / css).read_text(encoding="utf-8"), flags=re.DOTALL)
    assert not HEX_COLOR.findall(sheet), css
    for script in scripts:
        code = re.sub(
            r"/\*.*?\*/|//[^\n]*", "", (STATIC / script).read_text("utf-8"), flags=re.DOTALL
        )
        assert not HEX_COLOR.findall(code), script


@pytest.mark.parametrize("assets", ALL_ASSETS, ids=lambda a: a[0])
def test_page_js_has_no_hardcoded_japanese(assets):
    for script in assets[1]:
        for n, line in enumerate((STATIC / script).read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("//", 1)[0]
            assert not CJK.search(code), f"{script}:{n}: {line.strip()[:80]}"


ENGLISH_PHRASE = re.compile(r"""["'`]([A-Z][a-z]+(?: [A-Za-z]+)+[.…!?]?)["'`]""")


@pytest.mark.parametrize("assets", ALL_ASSETS, ids=lambda a: a[0])
def test_page_js_has_no_english_ui_phrases(assets):
    """English UI text must come from the catalog too (thrown internal errors excepted)."""
    for script in assets[1]:
        for n, line in enumerate((STATIC / script).read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("//", 1)[0]
            if "Error(" in code:
                continue
            found = ENGLISH_PHRASE.findall(code)
            assert not found, f"{script}:{n}: {found}"


def test_dimension_and_stimulus_labels_exist():
    import json

    for lang in ("ja", "en"):
        catalog = json.loads((STATIC / "i18n" / f"catalog.{lang}.json").read_text("utf-8"))
        for dim in ("trust", "curiosity", "vigilance", "attachment", "stability", "satisfaction"):
            assert f"dim.{dim}" in catalog
        for stimulus in EMOTION_MATRIX:
            assert f"stimulus.{stimulus}" in catalog, stimulus
