#!/usr/bin/env python3
"""Codemod/check for UI i18n (#184).

Converts bilingual literals ("日本語 / English") in the shell-based templates and
scripts into catalog keys (data-i18n / t("key")) and writes the two catalogs.

    python scripts/i18n_extract.py --apply   # rewrite sources + catalogs (idempotent)
    python scripts/i18n_extract.py --check   # fail if bilingual literals remain or keys are missing
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "dashboard"
CATALOG_DIR = ROOT / "static" / "i18n"
TARGETS = [
    "templates/base.html",
    "templates/chat.html",
    "templates/settings.html",
    "templates/observability.html",
    "templates/index.html",
    "templates/forest.html",
    "templates/memory.html",
    "templates/artifacts.html",
    "templates/approvals.html",
    "templates/schedules.html",
    "static/js/shell.js",
    "static/js/chat.js",
    "static/js/turn_ribbon.js",
    "static/js/neural_trend.js",
    "static/js/tool_cards.js",
    "static/js/settings_hub.js",
    "static/js/settings.js",
    "static/js/settings_diagnostics.js",
    "static/js/settings_surface.js",
    "static/js/settings_yolo.js",
    "static/js/settings_page_check_ports.js",
    "static/js/settings_containment.js",
    "static/js/settings_taint.js",
    "static/js/settings_grants.js",
    "static/js/observability.js",
    "static/js/neural_viz.js",
    "static/app.js",
    "static/js/forest.js",
    "static/js/procedural_review.js",
    "static/js/memory.js",
    "static/js/artifacts.js",
    "static/js/approvals.js",
    "static/js/permission_cards.js",
    "static/js/approvals_page.js",
    "static/js/schedules_page.js",
    "static/js/confirm_dialog.js",
    "static/js/api.js",
    "static/js/yolo_ui.js",
    "static/js/allow_ui.js",
    "static/js/progress_ui.js",
    # Stage (#341): strings come from the same catalog (keys stage.*)
    "templates/stage.html",
    "stage/js/main.js",
    "stage/js/ui.js",
    "stage/js/live_ui.js",
    "stage/js/adapter.js",
    "stage/js/approval.js",
    # #350: the conversation, the whiteboard and its renderer
    "stage/js/talk.js",
    "stage/js/backlog.js",
    "stage/js/board.js",
    "stage/js/md_render.js",
    # #446: the board's activity mode
    "stage/js/activity.js",
]
CJK = "぀-ヿ㐀-鿿＀-￯"
JA_EN = rf"[^<>{{}}\"'`]*?[{CJK}][^<>{{}}\"'`]*?"
EN_PART = r"[^<>{}\"'`]*?[A-Za-z][^<>{}\"'`]*?"
ATTRS = ("placeholder", "aria-label", "title", "alt", "aria-valuetext")


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text[:40] or "text"


def split_pair(raw: str) -> tuple[str, str] | None:
    """Split 'JA / EN' at the first ' / ' whose right side is Latin text."""
    for match in re.finditer(r" / ", raw):
        ja, en = raw[: match.start()].strip(), raw[match.end() :].strip()
        if ja and en and re.search(rf"[{CJK}]", ja) and re.search(r"[A-Za-z]", en):
            prefix = re.match(rf"^([^\w\s{CJK}]{{1,3}}\s)", ja)
            if prefix and not re.match(rf"^{re.escape(prefix.group(1))}", en):
                en = prefix.group(1) + en
            return ja, en
    return None


class Catalog:
    def __init__(self) -> None:
        self.ja: dict[str, str] = {}
        self.en: dict[str, str] = {}
        self.by_pair: dict[tuple[str, str], str] = {}
        for lang in ("ja", "en"):
            path = CATALOG_DIR / f"catalog.{lang}.json"
            if path.exists():
                getattr(self, lang).update(json.loads(path.read_text("utf-8")))
        self.by_pair = {(self.ja[k], self.en.get(k, "")): k for k in self.ja}

    def key_for(self, scope: str, ja: str, en: str) -> str:
        known = self.by_pair.get((ja, en))
        if known:
            return known
        base = f"{scope}.{slug(en)}"
        key, n = base, 2
        while key in self.ja:
            key, n = f"{base}_{n}", n + 1
        self.ja[key], self.en[key] = ja, en
        self.by_pair[(ja, en)] = key
        return key

    def write(self) -> None:
        CATALOG_DIR.mkdir(parents=True, exist_ok=True)
        for lang in ("ja", "en"):
            data = dict(sorted(getattr(self, lang).items()))
            (CATALOG_DIR / f"catalog.{lang}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8"
            )


def esc_html(text: str) -> str:
    return text


def convert_html(src: str, scope: str, cat: Catalog, report: list[str]) -> str:
    # Catalog values are applied as textContent, so store them entity-decoded.
    def new_key(scope: str, ja: str, en: str) -> str:
        return cat.key_for(scope, html.unescape(ja), html.unescape(en))

    # 1) JA <span>/ EN</span>, or JA <span class="en" lang="en">EN</span> (no slash)
    def span_pair(m: re.Match) -> str:
        if not m.group(4) and 'lang="en"' not in m.group(3):
            return m.group(0)
        ja, en = m.group(2).strip(), " ".join(m.group(5).split())
        ja = " ".join(ja.split())
        pair = split_pair(f"{ja} / {en}")
        if pair is None:
            return m.group(0)
        key = new_key(scope, *pair)
        return f'{m.group(1)}<span data-i18n="{key}">{pair[0]}</span>'

    src = re.sub(
        rf"(>)\s*({JA_EN})\s*<span([^>]*)>\s*(/\s*)?([^<]*?)</span>",
        span_pair,
        src,
    )

    # 1b) <option ...>JA / EN</option>: spans are not allowed inside <option>
    def option_pair(m: re.Match) -> str:
        pair = split_pair(m.group(2).strip())
        if pair is None or "data-i18n" in m.group(1):
            return m.group(0)
        key = new_key(scope, *pair)
        return f'<option{m.group(1)} data-i18n="{key}">{pair[0]}</option>'

    src = re.sub(r"<option([^>]*)>([^<]*? / [^<]*?)</option>", option_pair, src)

    # 2) attributes: placeholder="JA / EN" -> placeholder="JA" data-i18n-placeholder="key"
    def attr_pair(m: re.Match) -> str:
        pair = split_pair(m.group(2))
        if pair is None:
            return m.group(0)
        key = new_key(scope, *pair)
        return f'{m.group(1)}="{pair[0]}" data-i18n-{m.group(1)}="{key}"'

    for attr in ATTRS:
        src = re.sub(rf'\b({attr})="([^"{{}}]*? / [^"{{}}]*?)"', attr_pair, src)

    # 3) Jinja title block
    def title_pair(m: re.Match) -> str:
        body = m.group(1)
        tail = ""
        if " — " in body:
            body, tail = body.split(" — ", 1)
            tail = " — " + tail
        pair = split_pair(body)
        if pair is None:
            return m.group(0)
        ja, en = pair[0] + tail, pair[1] + tail
        key = new_key("title", ja, en)
        return (
            f"{{% block title %}}{ja}{{% endblock %}}{{% block title_key %}}{key}{{% endblock %}}"
        )

    src = re.sub(r"\{% block title %\}(.*?)\{% endblock %\}", title_pair, src)

    # 4) plain text nodes: >JA / EN< (skip text already keyed by its element)
    def text_pair(m: re.Match) -> str:
        if "data-i18n=" in m.group(1):
            return m.group(0)
        pair = split_pair(m.group(2))
        if pair is None:
            return m.group(0)
        key = new_key(scope, *pair)
        return f'{m.group(1)}<span data-i18n="{key}">{pair[0]}</span>{m.group(3)}<'

    src = re.sub(
        rf"(<[^<>]*>\s*)((?:{JA_EN}) / (?:{EN_PART}))(\s*)<",
        text_pair,
        src,
        flags=re.DOTALL,
    )

    # 5) Jinja tuple labels in loops: ('chat', '/chat', 'JA / EN')
    def tuple_label(m: re.Match) -> str:
        pair = split_pair(m.group(2))
        if pair is None:
            return m.group(0)
        key = new_key(scope, *pair)
        return f"{m.group(1)}'{key}'"

    src = re.sub(r"(\('[a-z_]+', '[^']*', )'([^']*? / [^']*?)'", tuple_label, src)
    return src


def _scan_expr(src: str, i: int) -> int:
    """Return index just after the '}' closing a '${' expression that starts at i."""
    depth = 1
    while i < len(src):
        ch = src[i]
        if ch in "'\"":
            j = i + 1
            while j < len(src) and src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "`":
            i = _scan_template(src, i + 1)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _scan_template(src: str, i: int) -> int:
    """Return index just after the closing backtick of a template literal body at i."""
    while i < len(src):
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            return i + 1
        if ch == "$" and src[i + 1 : i + 2] == "{":
            i = _scan_expr(src, i + 2)
            continue
        i += 1
    return i


def _template_pair(body: str, scope: str, cat: Catalog) -> str | None:
    """Convert a template literal body to t("key", {vars}); None if not a bilingual pair."""
    parts: list[tuple[str, str]] = []
    i, buf = 0, ""
    while i < len(body):
        if body[i] == "$" and body[i + 1 : i + 2] == "{":
            j = _scan_expr(body, i + 2)
            parts.append(("s", buf))
            buf = ""
            parts.append(("e", body[i + 2 : j - 1].strip()))
            i = j
        else:
            buf += body[i]
            i += 1
    parts.append(("s", buf))
    names: dict[str, str] = {}
    text = ""
    for kind, value in parts:
        if kind == "s":
            text += value
            continue
        m = re.search(r"([A-Za-z_]\w*)\s*$", re.sub(r"\(.*\)$", "", value)) if value else None
        name = m.group(1) if m else "v"
        base, n = name, 1
        while name in names and names[name] != value:
            n += 1
            name = f"{base}{n}"
        names[name] = value
        text += "{" + name + "}"
    if "\\" in text:
        return None
    pair = split_pair(text)
    if pair is None:
        return None
    for side in pair:
        if side.count("[") != side.count("]") or side.count("(") != side.count(")"):
            return None
    if not names:
        return f't("{cat.key_for(scope, *pair)}")'
    args = ", ".join(f"{k}: {v}" for k, v in names.items())
    return f't("{cat.key_for(scope, *pair)}", {{ {args} }})'


def convert_js(src: str, scope: str, cat: Catalog, report: list[str]) -> str:
    out, i = [], 0
    while i < len(src):
        ch = src[i]
        if ch == "`":
            end = _scan_template(src, i + 1)
            body = src[i + 1 : end - 1]
            converted = _template_pair(body, scope, cat)
            if converted is None and "${" in body:
                # keep the template, but convert nested bilingual literals inside expressions
                converted = "`" + convert_js(body, scope, cat, report) + "`"
            out.append(converted if converted is not None else src[i:end])
            i = end
            continue
        if ch in "'\"":
            j = i + 1
            while j < len(src) and src[j] != ch and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            lit = src[i : j + 1]
            body = lit[1:-1]
            pair = split_pair(body) if "\\" not in body else None
            if pair and src[j : j + 1] == ch:
                out.append(f't("{cat.key_for(scope, *pair)}")')
            else:
                out.append(lit)
            i = j + 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = len(src) if j == -1 else j
            out.append(src[i:j])
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i)
            j = len(src) if j == -1 else j + 2
            out.append(src[i:j])
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def leftovers(path: Path, text: str) -> list[str]:
    out = []
    pattern = re.compile(rf"[{CJK}][^<>\"'`\n]*? / [A-Za-z][^<>\"'`\n]*")
    for n, line in enumerate(text.splitlines(), 1):
        skip = line.lstrip().startswith(("//", "<!--", "*", "#"))
        if pattern.search(line) and "data-i18n" not in line and 't("' not in line and not skip:
            out.append(f"{path.name}:{n}: {line.strip()[:100]}")
        elif 'lang="en"' in line and path.suffix == ".html" and not skip:
            out.append(f"{path.name}:{n}: always-on English span: {line.strip()[:80]}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    cat = Catalog()
    problems: list[str] = []
    for rel in TARGETS:
        path = ROOT / rel
        text = path.read_text("utf-8")
        scope = path.stem
        if args.apply:
            report: list[str] = []
            new = (convert_html if path.suffix == ".html" else convert_js)(text, scope, cat, report)
            if new != text:
                path.write_text(new, "utf-8")
            problems += [f"{path.name}: {r}" for r in report]
            problems += leftovers(path, new)
        else:
            problems += leftovers(path, text)
            for key in re.findall(
                r'data-i18n(?:-[a-z-]+)?="([^"]+)"|\bt\(\s*["\']([\w.]+)["\']\s*[,)]', text
            ):
                for k in key:
                    if k and "{" not in k and (k not in cat.ja or k not in cat.en):
                        problems.append(f"{path.name}: key missing in catalog: {k}")
    if args.apply:
        cat.write()
    for p in problems:
        print(p)
    if args.check and problems:
        return 1
    print(f"{len(cat.ja)} keys; {len(problems)} items need attention", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
