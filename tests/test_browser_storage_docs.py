"""Browser storage keys (#481): the pages' keys are listed in docs/configuration*.md.

What a page keeps in this browser (``localStorage`` / ``sessionStorage``) is not a server
setting: the table says so, and no server code names a key.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
CONFIG_DOCS = (ROOT / "docs" / "configuration.md", ROOT / "docs" / "configuration_ja.md")
MARKER = "browser-storage"
_CALL = re.compile(r"\b(?:getItem|setItem|removeItem)\(\s*([^,)]+)")
_QUOTED = re.compile(r"""\A(['"`])(.*)\1\Z""")
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}")
# The one key the pages send back (a one-time token in a request header): named in the table.
SENT_KEYS = {"stira.grant.view.<id>"}


def _page_sources() -> list[Path]:
    """The pages' own scripts and templates (not the bundled three.js)."""
    return sorted(
        [
            *(DASHBOARD / "static" / "js").glob("*.js"),
            *(DASHBOARD / "stage" / "js").glob("*.js"),
            *(DASHBOARD / "templates").glob("*.html"),
        ]
    )


def _constant(name: str, text: str) -> str | None:
    found = re.search(rf"""\b(?:const|var|let)\s+{re.escape(name)}\s*=\s*(['"])([^'"]+)\1""", text)
    return found.group(2) if found else None


def _code_keys() -> set[str]:
    """Every key passed to getItem / setItem / removeItem: a literal or a string constant."""
    texts = {path: path.read_text(encoding="utf-8") for path in _page_sources()}
    keys: set[str] = set()
    for path, text in texts.items():
        for argument in _CALL.findall(text):
            argument = argument.strip()
            quoted = _QUOTED.match(argument)
            if quoted:
                keys.add(_PLACEHOLDER.sub("<id>", quoted.group(2)))
                continue
            # A constant of this file, or one imported from another (IDLE_KEY, FPS_KEY).
            values = {_constant(argument, text)} - {None} or {
                value for other in texts.values() if (value := _constant(argument, other))
            }
            assert len(values) == 1, f"{path.name}: storage key {argument!r} is not one constant"
            keys |= values
    return keys


def _doc_rows(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8")
    begin, end = f"<!-- {MARKER}:begin -->", f"<!-- {MARKER}:end -->"
    if begin not in text or end not in text:
        pytest.fail(f"{path.name}: markers {begin} / {end} not found")
    rows = []
    for line in text.split(begin, 1)[1].split(end, 1)[0].splitlines():
        line = line.strip()
        if line.startswith("|") and not set(line) <= {"|", "-", " "}:
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:]  # drop the header


def _doc_keys(path: Path) -> list[str]:
    return [key for row in _doc_rows(path) for key in re.findall(r"`([^`]+)`", row[0])]


def test_the_storage_keys_are_found_in_the_page_scripts() -> None:
    keys = _code_keys()
    assert all(key.startswith("stira.") for key in keys), keys  # one namespace
    assert "stira.lang" in keys and "stira.stage.fps" in keys
    assert "stira.settings.showInternal" in keys


@pytest.mark.parametrize("path", CONFIG_DOCS, ids=lambda path: path.name)
def test_every_storage_key_is_listed_in_the_configuration_reference(path: Path) -> None:
    listed = _doc_keys(path)
    assert len(listed) == len(set(listed)), f"{path.name}: a key is listed twice"
    assert set(listed) == _code_keys()


@pytest.mark.parametrize("path", CONFIG_DOCS, ids=lambda path: path.name)
def test_the_table_says_which_key_is_sent(path: Path) -> None:
    # Column 5: "no" / "いいえ" for everything but the keys in SENT_KEYS.
    for row in _doc_rows(path):
        key = re.findall(r"`([^`]+)`", row[0])[0]
        sent = not row[4].lower().startswith(("no", "いいえ"))
        assert sent == (key in SENT_KEYS), f"{path.name}: {key}"


def test_no_server_code_names_a_storage_key() -> None:
    """The server has no route, field or template value for them: nothing to receive."""
    sources = [*(ROOT / "src" / "stiracore").rglob("*.py"), *DASHBOARD.glob("*.py")]
    names = {key.removesuffix("<id>") for key in _code_keys()}
    assert names
    for path in sources:
        text = path.read_text(encoding="utf-8")
        found = sorted(name for name in names if name in text)
        assert not found, f"{path.relative_to(ROOT)} names {found}"
