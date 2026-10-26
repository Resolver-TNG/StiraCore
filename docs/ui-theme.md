# Dashboard colour theme

English. The dashboard (both Flask apps, every page on `templates/base.html`) takes all of
its colours from **`dashboard/static/ui/tokens.css`**. Since the #350 Stage work the palette is
blue / violet so the dashboard and the Stage scene (`dashboard/stage/`) look like one product:
the deep navy night of the lab, the cyan neon, the lavender of her hair, amber for approvals
and red for CRITICAL.

## Rules

- **One source.** Page CSS (`static/css/*.css`, `static/style.css`), `ui/components.css`, the
  JS and the templates use `var(--token)` only. `tests/test_ui_theme.py` fails on any colour
  literal outside `tokens.css` (hex, `rgb()`/`hsl()`/`oklch()`…, named colours in CSS colour
  properties). Translucent tints are `color-mix()` on tokens.
- **Swap the theme by editing values, not names.** Token names are a stable API (pages,
  tests and the legacy aliases `--bg`, `--accent`, … depend on them). Change the values in
  `tokens.css`, run the contrast and theme tests, look at the pages.
- **Semantic colours keep their meaning.** `--ok` is green (normal / connected / GO),
  `--warn` amber, `--crit` red, `--info` periwinkle. They are not theme colours: a theme
  change must not turn "normal" cyan or "info" into the accent. The theme test keeps
  `--info` and `--ok` at least ΔE2000 15 away from `--color-accent`.
- **Selection is not "ok".** The current nav item, pressed toggle buttons, checked
  options and the current model use `--color-selected-bg/-fg/-border`. A rule on
  `aria-current`, `aria-pressed="true"`, `:checked` or `.current` that uses `--ok-*` fails
  the theme test.
- **Form controls** (inputs, selects, textareas, toggles, the Always/Ask/Never segmented
  control) use `--color-border-strong` (≥ 3:1 on every surface, WCAG 1.4.11). Card and table
  separators use `--color-border`, which is decorative and stays below 3:1 (reported as an
  xfail in `tests/test_ui_contrast.py`).
- **Excluded:** `dashboard/stage/` keeps its own pixel-art palette (`stage.css`,
  `tools/build_*.py`, baked into the PNG assets; see [stage.md](stage.md)). It does not load
  `tokens.css`; the dashboard palette was chosen to match it, not the other way round.

## Palette

| Role | Token | Value | Stage counterpart |
|---|---|---|---|
| Page background | `--color-bg` | `#0b0d1e` | night `k1` / `sky0` |
| Background glow (top left) | `--color-bg-glow` | `#1d1f48` | `k4` / `sky2` |
| Card / sidebar | `--color-surface` | `#11142a` | text window fill |
| Raised / sunken / hover | `--color-surface-raised` / `-sunken` / `-hover` | `#181c38` / `#0d1023` / `#222749` | `k3`–`k5` |
| Separators (decorative) | `--color-border` / `--color-border-subtle` | `#3a4170` / `#252a4c` | `k6` |
| Form-control border | `--color-border-strong` | `#60679f` | `k8` |
| Text / muted | `--color-text` / `--color-text-muted` | `#e8e9f7` / `#a6abd0` | `--ink` / `--dim-ink` |
| Accent (links, focus ring, highlights) | `--color-accent` (`--color-link`) | `#6fd8f2` | cyan neon (NORMAL) |
| Secondary accent (brand) | `--color-accent-2` | `#b9a6ff` | lavender hair |
| Primary button | `--color-primary` / `-hover` / `-border` | `#4150b0` / `#4d5dc4` / `#7d8ae6` | indigo |
| Danger button | `--color-danger-solid` / `-hover` | `#7a2e3a` / `#8f3645` | red (CRITICAL) |
| Selection | `--color-selected-bg` / `-fg` / `-border` | `#15324f` / `#bdeeff` / accent | — |
| User chat bubble | `--color-bubble-user-bg` | `#1b2550` | — |
| ok / warn / crit / info | `--ok` `--warn` `--crit` `--info` (+ `-fg` `-bg` `-border`) | `#7fd69a` / `#f2c96b` / `#ff8d98` / `#94b2ff` | green / amber (approval) / red / — |
| Overlays | `--color-scrim` / `--color-stripe` / `--color-edge-shade` | translucent | — |

Neural State dimensions (one colour per dimension on every page; chosen for six separate
hues instead of the previous two greens, a teal and a blue):

| Dimension | Token | Value |
|---|---|---|
| trust | `--dim-trust` | `#6f9cff` (blue) |
| curiosity | `--dim-curiosity` | `#ffa45c` (orange) |
| vigilance | `--dim-vigilance` | `#f4e06a` (yellow) |
| attachment | `--dim-attachment` | `#ff8fd2` (pink) |
| stability | `--dim-stability` | `#5fe2ee` (cyan, as the Stage's NORMAL neon) |
| satisfaction | `--dim-satisfaction` | `#7fd98a` (green) |

Smallest colour difference (CIEDE2000) between any two of the six dimension colours, with
normal vision and with simulated dichromacy (Machado et al. 2009, severity 1.0). The test
requires ≥ 20 (normal) and ≥ 5 (each simulation):

| Vision | Before: closest pair | Before ΔE | After: closest pair | After ΔE |
|---|---|---|---|---|
| normal | trust / stability | 11.9 | vigilance / satisfaction | 24.3 |
| deuteranopia | curiosity / attachment | 2.5 | attachment / stability | 6.5 |
| protanopia | vigilance / satisfaction | 1.9 | curiosity / satisfaction | 8.6 |
| tritanopia | trust / stability | 5.0 | curiosity / attachment | 6.9 |


## Contrast (WCAG 2.x)

Computed by `tests/test_ui_contrast.py` (the CI accessibility gate); `python
tests/test_ui_contrast.py` prints the current values. Normal text needs 4.5:1, large text and
UI components 3:1. "Before" is the green palette that this theme replaced (— = the token did
not exist).

| Foreground | Background | Before (green) | After (blue/violet) | Need |
|---|---|---|---|---|
| `--color-text` | `--color-bg` | 15.96 | **15.98** | 4.5 |
| `--color-text` | `--color-surface` | 14.45 | **15.06** | 4.5 |
| `--color-text` | `--color-surface-raised` | 12.86 | **13.81** | 4.5 |
| `--color-text` | `--color-surface-sunken` | 15.26 | **15.63** | 4.5 |
| `--color-text-muted` | `--color-bg` | 8.63 | **8.58** | 4.5 |
| `--color-text-muted` | `--color-surface` | 7.81 | **8.09** | 4.5 |
| `--color-text-muted` | `--color-surface-raised` | 6.95 | **7.41** | 4.5 |
| `--color-text-muted` | `--color-surface-sunken` | 8.25 | **8.39** | 4.5 |
| `--color-accent` | `--color-surface` | 8.93 | **11.04** | 4.5 |
| `--color-on-primary` | `--color-primary` | 6.60 | **7.00** | 4.5 |
| `--ok-fg` | `--ok-bg` | 8.15 | **9.52** | 4.5 |
| `--warn-fg` | `--warn-bg` | 9.43 | **9.99** | 4.5 |
| `--crit-fg` | `--crit-bg` | 8.29 | **8.79** | 4.5 |
| `--info-fg` | `--info-bg` | 8.75 | **8.96** | 4.5 |
| `--color-text` | `--heat-pos-bg` | 9.94 | **9.94** | 4.5 |
| `--color-text` | `--heat-pos-strong-bg` | 6.17 | **6.57** | 4.5 |
| `--color-text` | `--heat-neg-bg` | 11.28 | **11.10** | 4.5 |
| `--color-text` | `--heat-neg-strong-bg` | 7.37 | **7.16** | 4.5 |
| `--color-text-inverse` | `--info` | 9.03 | **9.24** | 4.5 |
| `--color-text-inverse` | `--ok` | 9.86 | **10.99** | 4.5 |
| `--color-text-inverse` | `--neutral` | 8.63 | **8.58** | 4.5 |
| `--color-text-inverse` | `--crit` | 7.91 | **8.73** | 4.5 |
| `--ok-fg` | `--color-bg` | 11.58 | **12.85** | 4.5 |
| `--ok-fg` | `--color-surface` | 10.48 | **12.11** | 4.5 |
| `--ok-fg` | `--color-surface-raised` | 9.33 | **11.11** | 4.5 |
| `--ok-fg` | `--color-surface-sunken` | 11.07 | **12.56** | 4.5 |
| `--warn-fg` | `--color-bg` | 13.35 | **13.79** | 4.5 |
| `--warn-fg` | `--color-surface` | 12.08 | **13.00** | 4.5 |
| `--warn-fg` | `--color-surface-raised` | 10.76 | **11.92** | 4.5 |
| `--warn-fg` | `--color-surface-sunken` | 12.76 | **13.48** | 4.5 |
| `--crit-fg` | `--color-bg` | 9.81 | **10.75** | 4.5 |
| `--crit-fg` | `--color-surface` | 8.88 | **10.13** | 4.5 |
| `--crit-fg` | `--color-surface-raised` | 7.91 | **9.29** | 4.5 |
| `--crit-fg` | `--color-surface-sunken` | 9.38 | **10.51** | 4.5 |
| `--info-fg` | `--color-bg` | 11.21 | **11.57** | 4.5 |
| `--info-fg` | `--color-surface` | 10.15 | **10.91** | 4.5 |
| `--info-fg` | `--color-surface-raised` | 9.04 | **10.00** | 4.5 |
| `--info-fg` | `--color-surface-sunken` | 10.72 | **11.32** | 4.5 |
| `--color-accent` | `--color-bg` | 9.86 | **11.72** | 4.5 |
| `--color-accent` | `--color-surface-raised` | 7.95 | **10.13** | 4.5 |
| `--color-accent` | `--color-surface-sunken` | 9.43 | **11.46** | 4.5 |
| `--color-accent` | `--color-surface-hover` | 7.03 | **8.79** | 4.5 |
| `--color-accent-2` | `--color-surface` | — | **8.61** | 4.5 |
| `--color-accent` | `--ok-bg` | 6.94 | **8.68** | 4.5 |
| `--color-accent` | `--warn-bg` | 6.97 | **8.48** | 4.5 |
| `--color-accent` | `--crit-bg` | 8.33 | **9.58** | 4.5 |
| `--color-accent` | `--info-bg` | 7.70 | **9.07** | 4.5 |
| `--color-selected-fg` | `--color-selected-bg` | — | **10.52** | 4.5 |
| `--color-text` | `--color-selected-bg` | — | **10.89** | 4.5 |
| `--color-text-muted` | `--color-selected-bg` | — | **5.84** | 4.5 |
| `--color-text` | `--color-bubble-user-bg` | — | **12.21** | 4.5 |
| `--color-text-muted` | `--color-bubble-user-bg` | — | **6.56** | 4.5 |
| `--color-on-primary` | `--color-primary-hover` | 4.97 | **5.71** | 4.5 |
| `--color-on-primary` | `--color-danger-solid` | 9.81 | **9.21** | 4.5 |
| `--color-on-primary` | `--color-danger-solid-hover` | 8.11 | **7.55** | 4.5 |
| `--color-border-strong` | `--color-bg` | — | **3.61** | 3.0 |
| `--color-border-strong` | `--color-surface` | — | **3.40** | 3.0 |
| `--color-border-strong` | `--color-surface-raised` | — | **3.12** | 3.0 |
| `--color-border-strong` | `--color-surface-sunken` | — | **3.53** | 3.0 |
| `--color-accent` | `--color-selected-bg` | — | **7.98** | 3.0 |
| `--color-border` | `--color-surface` | 1.61 | **1.88** ¹ | 3.0 |
| `--viz-blue` | `--color-surface` | 6.90 | **7.67** | 3.0 |
| `--viz-violet` | `--color-surface` | 6.34 | **8.24** | 3.0 |
| `--viz-orange` | `--color-surface` | 7.54 | **8.73** | 3.0 |
| `--viz-teal` | `--color-surface` | 8.00 | **9.69** | 3.0 |
| `--dim-trust` | `--color-surface` | 8.93 | **6.80** | 3.0 |
| `--dim-curiosity` | `--color-surface` | 8.17 | **9.26** | 3.0 |
| `--dim-vigilance` | `--color-surface` | 10.79 | **13.58** | 3.0 |
| `--dim-attachment` | `--color-surface` | 6.92 | **8.74** | 3.0 |
| `--dim-stability` | `--color-surface` | 8.36 | **11.75** | 3.0 |
| `--dim-satisfaction` | `--color-surface` | 10.80 | **10.54** | 3.0 |

¹ `--color-border` separates cards and table rows only; it is decorative (form controls use
`--color-border-strong`).

## Hard-coded colours replaced

| Where | Before | Now |
|---|---|---|
| `ui/components.css` sidebar scrim, confirm dialog backdrop | `rgb(0 0 0 / .55)`, `rgb(0 0 0 / .6)` | `--color-scrim` |
| `css/observability.css` timeline bar edge, unfinished-bar stripes | `rgb(0 0 0 / .07)`, `rgb(255 255 255 / .15)` | `--color-edge-shade`, `--color-stripe` |
| Selection states using the semantic ok green (`--ok-bg` / `--ok-fg`): nav current item, observability pressed buttons, settings current model / hub nav / checked choice / preset / Always-Ask-Never, Turn Inspector view buttons | `--ok-*` | `--color-selected-*` |
| User chat bubble | `--ok-bg` | `--color-bubble-user-bg` |
| Chat breaker label at NORMAL | `--accent` (was green) | `--ok-fg` (keeps "normal = green") |
| Links | browser default | `--color-link` (`:where(a)`, no specificity) |
| Form-control borders | `--color-border` (1.6:1) | `--color-border-strong` (≥ 3:1) |

JS (`static/js/*.js`, `static/app.js`) and the templates had no colour literals: charts
(`neural_viz.js`, `sparkline.js`, `neural_trend.js`) and the memory forest set classes or
`currentColor`, and the CSS maps them to `--dim-*` / `--viz-*`.

Screenshots (before / after, ja / en, 1300 / 390 px): research/ui-theme-2026-10-17/.
