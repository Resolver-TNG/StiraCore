"""UI audit 2026-10-15: layout/keyboard contracts found broken in real Chrome (390 px).

jsdom-based axe cannot measure layout, so these are static checks of the rules that fixed
what the headless-Chrome audit showed (docs/research/ux-audit-2026-10-15.md).
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
CSS = DASHBOARD / "static" / "css"
TEMPLATES = DASHBOARD / "templates"


def _rule(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"missing rule {selector}"
    return match.group(1)


def test_page_grids_do_not_grow_with_wide_tables():
    # An implicit `auto` grid track takes the table's min-content width, so the whole page
    # scrolled sideways at 390 px (approvals 612 px, schedules 491 px wide).
    for name, selector in (
        ("approvals.css", ".approvals-page"),
        ("approvals.css", ".approval-list"),  # a long one-line brief widened the card
        ("schedules.css", ".schedules-page"),
    ):
        body = _rule((CSS / name).read_text(encoding="utf-8"), selector)
        assert "grid-template-columns: minmax(0, 1fr)" in body, name


def test_hidden_scheduler_facts_are_really_hidden():
    # `.sched-facts { display: grid }` overrode the `hidden` attribute set by the page script.
    css = (CSS / "schedules.css").read_text(encoding="utf-8")
    assert "display: none" in _rule(css, ".sched-facts[hidden]")


def test_sideways_scrolling_regions_are_keyboard_focusable():
    # axe scrollable-region-focusable (serious): a region that scrolls must take focus.
    expected = {
        "approvals.html": ['class="approvals-history-wrap"'],
        "schedules.html": ['class="sched-table-wrap"', 'class="sched-table-wrap"'],
        "observability.html": [
            'id="neural-chart"',
            'id="neural-correlation"',
            'id="neural-emotion"',
        ],
    }
    for name, markers in expected.items():
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        tags = re.findall(r"<div [^>]*>", html)
        for marker in set(markers):
            hits = [tag for tag in tags if marker in tag]
            assert len(hits) == markers.count(marker), (name, marker)
            for tag in hits:
                assert 'tabindex="0"' in tag, (name, tag)
                assert "aria-label" in tag, (name, tag)
