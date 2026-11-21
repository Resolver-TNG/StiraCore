"""#197: the UI quality gate, the parts a test can hold (no browser, no network).

Next to the axe-core gate (``test_a11y_axe.py``) and the contrast gate
(``test_ui_contrast.py``):

- 200 % zoom: every page keeps one viewport ``meta`` that does not forbid zooming;
- keyboard: the first link of every page is the skip link, and its target takes focus;
- IME: the chat composer asks ``chat_core.isSendKey`` with the composition state;
- text entry is at least 16 px (iOS Safari zooms the page on a smaller field);
- the disconnect / conflict / expiry regression tests stay in the suite, and run here
  when ``node`` is at hand (CI runs them with ``node --test tests/*_test.cjs``).

The font-size check reads the stylesheets, not a layout: a field's size is the largest
plain ``px`` (or token) size any of its rules sets, else the page's body size. Fields
below 16 px today are ``xfail(strict=True)``: they fail the day the stylesheet is fixed,
so the list cannot go stale. A real-device check (iOS Safari / Android Chrome) and the
first-time-user measure of #197 are outside what a test can show.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")

from tests.ui_pages import PAGES, render_pages

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
MIN_INPUT_PX = 16.0


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    try:
        return render_pages(tmp_path_factory.mktemp("ui-quality"))
    except RuntimeError as exc:
        pytest.skip(str(exc))


# --- zoom and keyboard entry point ---------------------------------------------------

VIEWPORT = re.compile(r'<meta\s+name="viewport"\s+content="([^"]*)"\s*/?>')


@pytest.mark.parametrize("page", sorted(PAGES))
def test_viewport_follows_the_device_and_allows_zoom(pages, page):
    found = VIEWPORT.findall(pages[page])
    assert len(found) == 1, f"{page}: one viewport meta, found {found}"
    settings = {part.strip().lower() for part in found[0].split(",")}
    assert {"width=device-width", "initial-scale=1"} <= settings
    blocked = [s for s in settings if s.startswith(("user-scalable", "maximum-scale"))]
    assert blocked == [], f"{page}: zoom must stay available (200 %): {blocked}"


@pytest.mark.parametrize("page", sorted(PAGES))
def test_first_link_is_the_skip_link_and_its_target_takes_focus(pages, page):
    body = pages[page].split("<body", 1)[1]
    first_link = re.search(r"<a\s[^>]*>", body).group(0)
    assert 'class="skip-link"' in first_link, f"{page}: {first_link}"
    target = re.search(r'href="#([\w-]+)"', first_link).group(1)
    targets = re.findall(rf'<[a-z]+\s[^>]*\bid="{target}"[^>]*>', body)
    assert len(targets) == 1, f"{page}: #{target} found {len(targets)} times"
    # Not focusable by Tab, focusable by the link: the next Tab starts in the content.
    assert 'tabindex="-1"' in targets[0]


def test_chat_composer_never_sends_on_an_ime_enter():
    """The rule itself is tested in chat_core_test.cjs; this pins that the page uses it."""
    js = (DASHBOARD / "static/js/chat.js").read_text(encoding="utf-8")
    assert '$("message").addEventListener("compositionstart", () => { composing = true; });' in js
    assert '$("message").addEventListener("compositionend", () => { composing = false; });' in js
    assert "core.isSendKey(event, composing)" in js
    # No second send path on Enter that skips the rule.
    assert len(re.findall(r'===\s*"Enter"', js)) == 0, "Enter is judged by chat_core only"


# --- text entry is at least 16 px ----------------------------------------------------

RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
SIZE = re.compile(r"^(?:(\d+(?:\.\d+)?)px|var\((--[\w-]+)(?:\s*,[^)]*)?\))(?=$|[\s/])")


def _css(relative: str) -> str:
    text = (DASHBOARD / relative).read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _tokens() -> dict[str, str]:
    declarations = re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", _css("static/ui/tokens.css"))
    return {name: value.strip() for name, value in declarations}


def _px(value: str, tokens: dict[str, str]) -> float | None:
    """A plain ``Npx`` or a token that is one; ``inherit``, ``clamp()``, ``em`` -> None."""
    match = SIZE.match(value.strip())
    if match is None:
        return None
    if match.group(1):
        return float(match.group(1))
    return _px(tokens.get(match.group(2), ""), tokens)


def _declared_sizes(css: str, selector: str, tokens: dict[str, str]) -> list[float]:
    sizes = []
    for selectors, block in RULE.findall(css):
        if not any(re.search(selector, s.strip()) for s in selectors.split(",")):
            continue
        for value in re.findall(r"(?:^|;)\s*(?:font-size|font)\s*:\s*([^;]+)", block):
            size = _px(value, tokens)
            if size is not None:
                sizes.append(size)
    return sizes


def _body_size(tokens: dict[str, str]) -> float:
    sizes = _declared_sizes(_css("static/ui/components.css"), r"body\.app\)?$", tokens)
    assert len(sizes) == 1, sizes
    return sizes[0]


def _field_size(stylesheet: str, selector: str, *, inherits_body: bool = True) -> float:
    tokens = _tokens()
    sizes = _declared_sizes(_css(stylesheet), selector, tokens)
    if sizes:
        return max(sizes)
    assert inherits_body, f"{stylesheet}: no size for {selector}"
    return _body_size(tokens)


def test_the_size_reader_reads_what_the_stylesheets_say():
    """Controls for the check below: tokens, the ``font`` shorthand, rules in ``@media``."""
    tokens = {"--font-size-md": "14px", "--alias": "var(--font-size-md)"}
    assert _px("16px", tokens) == 16
    assert _px("var(--font-size-md) / 1.5 sans-serif", tokens) == 14
    assert _px("var(--alias, 1rem)", tokens) == 14
    assert _px("inherit", tokens) is None
    assert _px("clamp(14px, 2vw, 20px)", tokens) is None
    css = "a { font: inherit; } @media (max-width: 540px) { .x a, b { font-size: 16px; } }"
    assert _declared_sizes(css, r"(^|\s)a$", tokens) == [16]
    assert _declared_sizes(css, r"^c$", tokens) == []
    assert _body_size(_tokens()) == 14, "the body size the fields below inherit"


BELOW_16 = "below 16 px today (font: inherit from the 14 px body): #197, not fixed here"
FIELDS = [
    # Stage composer in the phone layout (stage.css sets 16 px for iOS).
    pytest.param("stage/stage.css", r"\.composer-input$", False, id="stage-composer"),
    pytest.param(
        "static/css/chat.css",
        r"(^|\s)textarea$|#message$",
        True,
        id="chat-composer",
        marks=pytest.mark.xfail(strict=True, reason=BELOW_16),
    ),
    pytest.param(
        "static/ui/components.css",
        r"^\.(input|select|textarea)$",
        True,
        id="shared-input-select-textarea",
        marks=pytest.mark.xfail(strict=True, reason=BELOW_16),
    ),
]


@pytest.mark.parametrize(("stylesheet", "selector", "inherits_body"), FIELDS)
def test_text_entry_is_at_least_16px(stylesheet, selector, inherits_body):
    size = _field_size(stylesheet, selector, inherits_body=inherits_body)
    assert size >= MIN_INPUT_PX, f"{stylesheet} {selector}: {size}px < {MIN_INPUT_PX}px"


# --- disconnect / conflict / expiry regressions --------------------------------------

REGRESSIONS = {
    "chat_resend_test.cjs": [
        "network failure: no automatic re-send; same message reuses the key",
    ],
    "approvals_test.cjs": [
        "server refusals are explained and keep the card; network retries reuse the same key",
        "binding mismatch and expiry messages; an expired request cannot be approved",
    ],
    "chat_core_test.cjs": [
        "IME: Enter while composing never sends; Enter sends; Shift+Enter is a new line",
    ],
}


@pytest.mark.parametrize("name", sorted(REGRESSIONS))
def test_the_named_regression_tests_are_still_in_the_suite(name):
    source = (ROOT / "tests" / name).read_text(encoding="utf-8")
    for title in REGRESSIONS[name]:
        assert f"test('{title}" in source, f"{name}: missing test {title!r}"


@pytest.mark.parametrize("name", sorted(REGRESSIONS))
def test_the_regression_tests_pass_under_node(name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not found on PATH")
    proc = subprocess.run(
        [node, "--test", str(ROOT / "tests" / name)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-6000:]
