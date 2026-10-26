"""#60: keep the chat template, stylesheet and JS DOM contract in sync."""

import re
from html.parser import HTMLParser
from pathlib import Path

from flask import Flask

from stiracore.observability.chat_api import create_chat_blueprint

ROOT = Path(__file__).resolve().parents[1] / "dashboard"


class ChatMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = []
        self.classes = set()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        self.classes.update(attrs.get("class", "").split())


def test_chat_template_css_and_js_contract():
    app = Flask(
        __name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static")
    )
    app.register_blueprint(create_chat_blueprint(object(), runner=lambda message: "unused"))
    response = app.test_client().get("/chat")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    markup = ChatMarkup(html)
    assert len(markup.ids) == len(set(markup.ids)), "Duplicate DOM IDs"
    assert "<style>" not in html, "Keep layout rules together in chat.css"
    # Shell/components live in the shared UI stylesheet (#183); chat.css keeps chat rules.
    css = "".join(
        (ROOT / "static" / name).read_text(encoding="utf-8")
        for name in ("css/chat.css", "css/approvals.css", "ui/components.css", "ui/allow.css")
    )
    selectors = set(re.findall(r"\.([a-z][\w-]*)", css))
    assert markup.classes <= selectors, markup.classes - selectors
    js = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    literal_ids = set(re.findall(r'\$\("([\w-]+)"\)', js))
    assert literal_ids <= set(markup.ids)
    assert {"dashboard", "chat-panel", "conversation", "composer", "sidebar"} <= markup.classes


def test_chat_retains_sidebar_and_sparkline_targets():
    markup = ChatMarkup((ROOT / "templates/chat.html").read_text(encoding="utf-8"))
    assert {
        "gateway-health",
        "gateway-neural",
        "breaker-level",
        "breaker-indicator",
        "breaker-changed",
        "episodic-count",
        "semantic-count",
        "procedural-count",
        "soul-status",
        "soul-path",
        "bc-score",
        "input-tokens",
        "output-tokens",
        "estimated-cost",
    } <= set(markup.ids)
    for dimension in ("trust", "curiosity", "vigilance", "attachment", "stability", "satisfaction"):
        assert f"spark-{dimension}" in markup.ids
        assert f"sv-{dimension}" in markup.ids


class _TrendMarkup(HTMLParser):
    """Collect attributes by id and the ids nested inside #neural-body."""

    VOID = frozenset({"br", "img", "input", "meta", "link", "hr", "source", "wbr"})

    def __init__(self, html):
        super().__init__()
        self.attrs = {}
        self.stack = []
        self.body_ids = set()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.attrs[attrs["id"]] = {"tag": tag, **attrs}
            if "neural-body" in self.stack:
                self.body_ids.add(attrs["id"])
        if tag not in self.VOID:
            self.stack.append(attrs.get("id"))

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def test_neural_trend_is_collapsed_by_default_with_summary():
    """#303: the trend starts collapsed; summary outside, disclaimer + sparklines inside."""
    m = _TrendMarkup((ROOT / "templates/chat.html").read_text(encoding="utf-8"))
    toggle = m.attrs["neural-toggle"]
    assert toggle["tag"] == "button"
    assert toggle["type"] == "button"
    assert toggle["aria-expanded"] == "false"
    assert toggle["aria-controls"] == "neural-body"
    assert toggle["aria-describedby"] == "neural-summary"
    assert "hidden" in m.attrs["neural-body"]
    assert m.attrs["neural-trend"]["data-expanded"] == "false"
    assert {"neural-disclaimer", "turn-neural", "spark-stability", "sv-stability"} <= m.body_ids
    assert not {"neural-summary", "neural-summary-stability", "neural-summary-badge"} & m.body_ids
    assert "hidden" in m.attrs["neural-summary-badge"]
    html = (ROOT / "templates/chat.html").read_text(encoding="utf-8")
    assert html.index("js/neural_trend.js") < html.index("js/chat.js")


def test_neural_trend_collapse_does_not_touch_observability():
    """#303: /observability keeps its own trend; no chat toggle markup or script there."""
    for name in ("observability.html", "index.html"):
        html = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "neural_trend.js" not in html
        assert "neural-toggle" not in html
    obs = (ROOT / "static/js/observability.js").read_text(encoding="utf-8")
    assert "StiraTrend" not in obs
    css = (ROOT / "static/css/chat.css").read_text(encoding="utf-8")
    assert ".turn-state-badge[hidden] { display: none; }" in css
    # Must outrank `button:hover:not(:disabled)` (primary green) or the label fails contrast.
    assert ".turn-state-toggle:hover:not(:disabled) {" in css
