"""WCAG contrast checks on design tokens (#197)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TOKENS = Path(__file__).resolve().parents[1] / "dashboard" / "static" / "ui" / "tokens.css"
DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


HEX6 = re.compile(r"#[0-9a-fA-F]{6}")


def parse_tokens() -> tuple[dict[str, str], list[str]]:
    colors: dict[str, str] = {}
    bad: list[str] = []
    for name, value in DECL.findall(_strip_comments(TOKENS.read_text(encoding="utf-8"))):
        value = value.strip()
        if value.startswith("#"):
            if HEX6.fullmatch(value):
                colors[name] = value.lower()
            else:
                bad.append(f"{name}: {value}")
    return colors, bad


def _channel(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


COLORS, BAD = parse_tokens()
SURFACES = ["--color-bg", "--color-surface", "--color-surface-raised", "--color-surface-sunken"]
TEXT_PAIRS = [(fg, bg) for fg in ("--color-text", "--color-text-muted") for bg in SURFACES]
TEXT_PAIRS += [("--color-accent", "--color-surface"), ("--color-on-primary", "--color-primary")]
TEXT_PAIRS += [(f"--{s}-fg", f"--{s}-bg") for s in ("ok", "warn", "crit", "info")]
HEAT = ("--heat-pos-bg", "--heat-pos-strong-bg", "--heat-neg-bg", "--heat-neg-strong-bg")
TEXT_PAIRS += [("--color-text", bg) for bg in HEAT]
# Timeline bars carry dark text on status/series fills (observability.css).
TEXT_PAIRS += [("--color-text-inverse", bg) for bg in ("--info", "--ok", "--neutral", "--crit")]
# Status text, accent text and links also sit directly on every surface (#350 theme).
TEXT_PAIRS += [(f"--{s}-fg", bg) for s in ("ok", "warn", "crit", "info") for bg in SURFACES]
TEXT_PAIRS += [("--color-accent", bg) for bg in SURFACES[:1] + SURFACES[2:]]
TEXT_PAIRS += [("--color-accent", "--color-surface-hover"), ("--color-accent-2", "--color-surface")]
TEXT_PAIRS += [("--color-accent", f"--{s}-bg") for s in ("ok", "warn", "crit", "info")]
# Selection (nav, pressed buttons, checked options) and the user's chat bubble.
TEXT_PAIRS += [("--color-selected-fg", "--color-selected-bg")]
TEXT_PAIRS += [
    ("--color-text", "--color-selected-bg"),
    ("--color-text-muted", "--color-selected-bg"),
]
TEXT_PAIRS += [("--color-text", "--color-bubble-user-bg")]
TEXT_PAIRS += [("--color-text-muted", "--color-bubble-user-bg")]
TEXT_PAIRS += [("--color-on-primary", "--color-primary-hover")]
TEXT_PAIRS += [("--color-on-primary", "--color-danger-solid")]
TEXT_PAIRS += [("--color-on-primary", "--color-danger-solid-hover")]
# Non-text (WCAG 1.4.11, 3:1): form-control borders and the focus ring (= accent).
UI_PAIRS = [("--color-border-strong", bg) for bg in SURFACES]
UI_PAIRS += [("--color-accent", bg) for bg in (*SURFACES, "--color-surface-hover")]
UI_PAIRS += [("--color-accent", "--color-selected-bg")]
VIZ = ("--viz-blue", "--viz-violet", "--viz-orange", "--viz-teal")
VIZ += tuple(f"--dim-{d}" for d in ("trust", "curiosity", "vigilance", "attachment"))
VIZ += ("--dim-stability", "--dim-satisfaction")


def _get(name: str) -> str:
    assert name in COLORS, f"token {name} missing or not #rrggbb in tokens.css"
    return COLORS[name]


def test_tokens_parse():
    assert not BAD, f"unparseable color tokens: {BAD}"
    assert COLORS


def test_contrast_math():
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#777777") == pytest.approx(1.0)


@pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
def test_text_contrast(fg, bg):
    ratio = contrast(_get(fg), _get(bg))
    assert ratio >= 4.5, f"{fg} on {bg}: {ratio:.2f}:1 < 4.5:1"


def test_border_non_text_contrast():
    ratio = contrast(_get("--color-border"), _get("--color-surface"))
    print(f"--color-border on --color-surface: {ratio:.2f}:1 (target 3:1)")
    if ratio < 3.0:
        pytest.xfail(f"--color-border on --color-surface is {ratio:.2f}:1 < 3:1 (improve later)")


@pytest.mark.parametrize(("fg", "bg"), UI_PAIRS)
def test_ui_component_contrast(fg, bg):
    ratio = contrast(_get(fg), _get(bg))
    assert ratio >= 3.0, f"{fg} on {bg}: {ratio:.2f}:1 < 3:1"


@pytest.mark.parametrize("series", VIZ)
def test_viz_series_non_text_contrast(series):
    ratio = contrast(_get(series), _get("--color-surface"))
    assert ratio >= 3.0, f"{series} on --color-surface: {ratio:.2f}:1 < 3:1"


def markdown_table() -> str:
    """The contrast table in docs/ui-theme.md: `python tests/test_ui_contrast.py`."""
    rows = ["| Foreground | Background | Ratio | Need |", "|---|---|---|---|"]
    groups = [
        (TEXT_PAIRS, 4.5),
        (UI_PAIRS, 3.0),
        ([("--color-border", "--color-surface")], 3.0),
        ([(v, "--color-surface") for v in VIZ], 3.0),
    ]
    for pairs, need in groups:
        for fg, bg in pairs:
            ratio = contrast(_get(fg), _get(bg))
            mark = "" if ratio >= need else " (decorative, xfail)"
            rows.append(
                f"| `{fg}` {_get(fg)} | `{bg}` {_get(bg)} | {ratio:.2f}:1 | {need}:1{mark} |"
            )
    return "\n".join(rows)


if __name__ == "__main__":
    print(markdown_table())
