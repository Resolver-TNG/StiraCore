"""UI shell tests (#183): chat / tools / settings / observability extend base.html."""

from __future__ import annotations

import importlib
import inspect
import re
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock

import pytest

flask = pytest.importorskip("flask")

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
STATIC = DASHBOARD / "static"

PAGES = {
    "chat": ("chat.html", "chat.js"),
    "settings": ("settings.html", "settings.js"),
    "observability": ("observability.html", "observability.js"),
}
NAV_HREFS = ["/chat", "/settings", "/observability"]
CSRF_VALUE = "test-csrf-token"
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
JS_ID_PATTERNS = [
    re.compile(r'getElementById\(\s*"([^"]+)"\s*\)'),
    re.compile(r'\$\(\s*"([^"]+)"\s*\)'),
]
# chat.js also addresses elements via set("id", value) -> $(id).
CHAT_SET_PATTERN = re.compile(r'\bset\(\s*"([^"]+)"')

# IDs that JS creates at runtime and therefore are not in the server HTML.
# The regexes above only capture string literals, so template-literal ids are
# never captured; these prefixes are listed explicitly to document that and to
# guard against future literal references to generated ids.
DYNAMIC_ID_PREFIXES = {
    # buildBars("gateway-neural") creates gateway-neural-<dim>[-value].
    "chat.js": ("gateway-neural-",),
    # syncOutput builds "<id>-value" via template literal (outputs do exist).
    "settings.js": (),
    "observability.js": (),
}

CONFIG_MODULE_CANDIDATES = (
    "dashboard.config_api",
    "dashboard.stira_server",
    "stira_server.config_api",
    "stira_server",
    "config_api",
)


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.style_attrs = 0
        self.style_blocks: list[str] = []
        self.nav: list[tuple[str | None, str | None]] = []
        self.metas: dict[str, str | None] = {}
        self.scripts: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if "style" in a:
            self.style_attrs += 1
        if tag == "style":
            self._in_style = True
            self.style_blocks.append("")
        classes = (a.get("class") or "").split()
        if tag == "a" and "app-nav-link" in classes:
            self.nav.append((a.get("href"), a.get("aria-current")))
        if tag == "meta" and a.get("name"):
            self.metas[a["name"]] = a.get("content")
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.style_blocks[-1] += data


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


@pytest.fixture(scope="module")
def app():
    factory = _find_blueprint_factory()
    if factory is None:
        pytest.skip("create_config_blueprint not importable")
    application = flask.Flask(
        __name__,
        template_folder=str(DASHBOARD / "templates"),
        static_folder=str(STATIC),
    )
    application.register_blueprint(_build_blueprint(factory))
    application.add_url_rule("/chat", "chat_page", lambda: "")

    @application.context_processor
    def _csrf():
        return {"csrf_token": lambda: CSRF_VALUE}

    return application


@pytest.fixture(scope="module")
def rendered(app):
    out = {}
    with app.test_request_context("/"):
        assert flask.url_for("config_api.tools_page")
        for key, (template, _js) in PAGES.items():
            parser = _Collector()
            parser.feed(flask.render_template(template))
            out[key] = parser
    return out


@pytest.mark.parametrize("page", list(PAGES))
def test_nav_items_and_aria_current(rendered, page):
    nav = rendered[page].nav
    assert [href for href, _ in nav] == NAV_HREFS
    current = [href for href, cur in nav if cur == "page"]
    assert current == [f"/{page}"]


@pytest.mark.parametrize("page", list(PAGES))
def test_csrf_meta_and_api_js(rendered, page):
    parsed = rendered[page]
    assert parsed.metas.get("csrf-token") == CSRF_VALUE
    scripts = parsed.scripts
    api_idx = next(i for i, s in enumerate(scripts) if s.endswith("js/api.js"))
    page_js = PAGES[page][1]
    page_idx = next(i for i, s in enumerate(scripts) if s.endswith(f"js/{page_js}"))
    assert api_idx < page_idx
    if page == "chat":
        spark_idx = next(i for i, s in enumerate(scripts) if s.endswith("js/sparkline.js"))
        assert spark_idx < page_idx


@pytest.mark.parametrize("page", list(PAGES))
def test_poll_js_loads_before_the_shell_and_the_page(rendered, page):
    """#369: every page polls through poll.js, so it must be defined first (defer = in order)."""
    scripts = rendered[page].scripts
    poll_idx = next(i for i, s in enumerate(scripts) if s.endswith("js/poll.js"))
    shell_idx = next(i for i, s in enumerate(scripts) if s.endswith("js/shell.js"))
    page_idx = next(i for i, s in enumerate(scripts) if s.endswith(f"js/{PAGES[page][1]}"))
    assert poll_idx < shell_idx < page_idx


@pytest.mark.parametrize("page", list(PAGES))
def test_no_inline_style_attributes(rendered, page):
    assert rendered[page].style_attrs == 0


@pytest.mark.parametrize("page", list(PAGES))
def test_no_hex_colors_in_style_blocks(rendered, page):
    for block in rendered[page].style_blocks:
        assert not HEX_COLOR.findall(block)


@pytest.mark.parametrize("page", list(PAGES))
def test_js_referenced_ids_exist(rendered, page):
    js_name = PAGES[page][1]
    source = (STATIC / "js" / js_name).read_text(encoding="utf-8")
    patterns = list(JS_ID_PATTERNS)
    if js_name == "chat.js":
        patterns.append(CHAT_SET_PATTERN)
    referenced = {m for pat in patterns for m in pat.findall(source)}
    prefixes = DYNAMIC_ID_PREFIXES[js_name]
    referenced = {i for i in referenced if not i.startswith(prefixes)} if prefixes else referenced
    assert referenced, f"no ids extracted from {js_name}"
    missing = sorted(referenced - rendered[page].ids)
    assert not missing, f"{js_name} ids missing in {page}: {missing}"


def _defined_tokens() -> set[str]:
    text = (STATIC / "ui" / "tokens.css").read_text(encoding="utf-8")
    return set(re.findall(r"(--[\w-]+)\s*:", text))


def test_status_tokens_defined():
    defined = _defined_tokens()
    for name in ("--ok", "--warn", "--crit", "--info"):
        assert name in defined


def test_components_reference_only_defined_tokens():
    defined = _defined_tokens()
    text = (STATIC / "ui" / "components.css").read_text(encoding="utf-8")
    used = set(re.findall(r"var\(\s*(--[\w-]+)", text))
    assert used
    assert not sorted(used - defined)
