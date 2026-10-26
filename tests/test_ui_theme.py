"""Dashboard colour theme (#350 follow-up): tokens.css is the only place colours live.

- no colour literal (hex, rgb()/hsl()/…, named colours) in page CSS, JS or templates;
- selection states use the selection tokens, never the semantic "ok" (green = normal);
- the six Neural State dimension colours stay apart (CIEDE2000), also under simulated
  colour-vision deficiency, and info / ok / selection do not read as the accent.

dashboard/stage/ is excluded: the Stage keeps its own pixel-art palette (docs/stage.md).
"""

from __future__ import annotations

import itertools
import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
STATIC = DASHBOARD / "static"
TOKENS = STATIC / "ui" / "tokens.css"

HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
COLOUR_FN = re.compile(r"\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(", re.IGNORECASE)
NAMED = re.compile(
    r"(?:^|[;{\s])(?:color|background(?:-color)?|fill|stroke|outline(?:-color)?|"
    r"border(?:-[a-z]+)*(?:-color)?|box-shadow|text-shadow|accent-color|caret-color)\s*:"
    r"[^;}]*\b(?:white|black|red|green|blue|yellow|orange|purple|gray|grey|silver|"
    r"lime|teal|navy|aqua|cyan|magenta|fuchsia|maroon|olive|pink|violet)\b",
    re.IGNORECASE,
)
DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")


def _strip(text: str, suffix: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    if suffix == ".js":
        text = re.sub(r"(?<![:\"'])//[^\n]*", "", text)
    if suffix == ".html":
        text = re.sub(r"\{#.*?#\}|<!--.*?-->", "", text, flags=re.DOTALL)
    if suffix in (".js", ".html"):
        # issue references in strings ("tool rules changed (#190)") are not colours
        text = re.sub(r"\(#\d{1,4}\)", "", text)
    return text


def _sources() -> list[Path]:
    files = [
        *STATIC.rglob("*.css"),
        *STATIC.rglob("*.js"),
        *(DASHBOARD / "templates").glob("*.html"),
    ]
    return sorted(p for p in files if p != TOKENS)


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(DASHBOARD)))
def test_no_colour_literals_outside_tokens(path):
    text = _strip(path.read_text(encoding="utf-8"), path.suffix)
    found = HEX.findall(text) + COLOUR_FN.findall(text)
    if path.suffix == ".css":
        found += NAMED.findall(re.sub(r"var\(--[\w-]+\)", "", text))
    assert not found, f"{path.relative_to(ROOT)}: colour literals {found[:5]} (use a token)"


SELECTION = re.compile(r'aria-current|aria-pressed="true"|:checked|\.current\b')


def _rules(css: str):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    yield from re.findall(r"([^{}]+)\{([^{}]*)\}", css)


@pytest.mark.parametrize(
    "path", [*sorted(STATIC.rglob("*.css"))], ids=lambda p: str(p.relative_to(DASHBOARD))
)
def test_selection_states_do_not_use_status_ok(path):
    bad = [
        sel.strip()
        for sel, body in _rules(path.read_text(encoding="utf-8"))
        if SELECTION.search(sel) and "var(--ok" in body
    ]
    assert not bad, f"{path.name}: selection uses --ok-* (use --color-selected-*): {bad}"


def _tokens() -> dict[str, str]:
    raw = dict(DECL.findall(re.sub(r"/\*.*?\*/", "", TOKENS.read_text("utf-8"), flags=re.DOTALL)))
    out: dict[str, str] = {}
    for name, value in raw.items():
        value = value.strip()
        for _ in range(5):  # resolve var(--x) aliases
            m = re.fullmatch(r"var\((--[\w-]+)\)", value)
            if not m:
                break
            value = raw[m.group(1)].strip()
        out[name] = value.lower()
    return out


TOK = _tokens()


def test_new_theme_tokens_exist():
    for name in (
        "--color-accent-2",
        "--color-link",
        "--color-selected-bg",
        "--color-selected-fg",
        "--color-selected-border",
        "--color-bubble-user-bg",
        "--color-border-strong",
        "--color-scrim",
        "--color-stripe",
        "--color-edge-shade",
    ):
        assert name in TOK, name


# --- colour difference (CIEDE2000) and colour-vision deficiency simulation -------------


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb(hex_color: str) -> tuple[float, float, float]:
    return tuple(_lin(int(hex_color[i : i + 2], 16) / 255) for i in (1, 3, 5))


def _lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = rgb
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e2000(c1, c2) -> float:
    (l1, a1, b1), (l2, a2, b2) = c1, c2
    cb = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(cb**7 / (cb**7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360
    dlp, dcp = l2 - l1, c2p - c1p
    dh = 0.0 if c1p * c2p == 0 else (h2p - h1p + 180) % 360 - 180
    dhp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))
    lbp, cbp = (l1 + l2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    else:
        hbp = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hbp - 30))
        + 0.24 * math.cos(math.radians(2 * hbp))
        + 0.32 * math.cos(math.radians(3 * hbp + 6))
        - 0.20 * math.cos(math.radians(4 * hbp - 63))
    )
    dth = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cbp**7 / (cbp**7 + 25**7))
    sl = 1 + 0.015 * (lbp - 50) ** 2 / math.sqrt(20 + (lbp - 50) ** 2)
    sc, sh = 1 + 0.045 * cbp, 1 + 0.015 * cbp * t
    rt = -math.sin(math.radians(2 * dth)) * rc
    return math.sqrt(
        (dlp / sl) ** 2 + (dcp / sc) ** 2 + (dhp / sh) ** 2 + rt * (dcp / sc) * (dhp / sh)
    )


# Machado, Oliveira & Fernandes (2009), severity 1.0, linear RGB.
CVD = {
    "deuteranopia": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "protanopia": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "tritanopia": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}


def lab_of(hex_color: str, vision: str = "normal"):
    rgb = _rgb(hex_color)
    if vision != "normal":
        m = CVD[vision]
        rgb = tuple(max(0.0, min(1.0, sum(m[i][j] * rgb[j] for j in range(3)))) for i in range(3))
    return _lab(rgb)


DIMS = ("trust", "curiosity", "vigilance", "attachment", "stability", "satisfaction")
# Measured on the #350 palette: normal 24.3, deuteranopia 6.5, protanopia 8.6, tritanopia 6.9
# (the previous green palette: 11.9 / 2.5 / 1.9 / 5.0 — trust vs stability, etc.).
MIN_DE = {"normal": 20.0, "deuteranopia": 5.0, "protanopia": 5.0, "tritanopia": 5.0}


@pytest.mark.parametrize("vision", list(MIN_DE))
def test_neural_dimension_colours_are_distinct(vision):
    pairs = [
        (delta_e2000(lab_of(TOK[f"--dim-{a}"], vision), lab_of(TOK[f"--dim-{b}"], vision)), a, b)
        for a, b in itertools.combinations(DIMS, 2)
    ]
    worst = min(pairs)
    assert worst[0] >= MIN_DE[vision], f"{vision}: {worst[1]} vs {worst[2]} dE {worst[0]:.1f}"


@pytest.mark.parametrize(
    ("a", "b", "minimum"),
    [
        ("--color-accent", "--info", 15.0),  # info must not read as the accent
        ("--color-accent", "--ok", 15.0),  # ok (green) stays apart from the cyan accent
        ("--color-selected-bg", "--ok-bg", 15.0),  # selection is not "ok"
        ("--color-selected-bg", "--info-bg", 5.0),
        ("--dim-stability", "--ok", 15.0),
    ],
)
def test_semantic_colours_do_not_read_as_theme(a, b, minimum):
    d = delta_e2000(lab_of(TOK[a]), lab_of(TOK[b]))
    assert d >= minimum, f"{a} vs {b}: dE {d:.1f} < {minimum}"
