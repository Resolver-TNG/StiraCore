"""The operator guides (scheduler/heartbeat, memory routines): links resolve, twins and index."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_docs_deploy_ec2 import _check_links

DOCS = Path(__file__).resolve().parents[1] / "docs"
GUIDES = ("guide-scheduler-heartbeat", "guide-memory-routines")
FILES = [DOCS / f"{name}{suffix}.md" for name in GUIDES for suffix in ("", "_ja")]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_links_and_headings_in_the_guide_resolve(path: Path) -> None:
    assert _check_links(path, path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("name", GUIDES)
def test_the_twins_link_each_other_and_the_index_lists_both(name: str) -> None:
    english = (DOCS / f"{name}.md").read_text(encoding="utf-8")
    japanese = (DOCS / f"{name}_ja.md").read_text(encoding="utf-8")
    assert f"]({name}_ja.md)" in english
    assert f"]({name}.md)" in japanese
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    assert f"[{name}.md]({name}.md) / [{name}_ja.md]({name}_ja.md)" in index


@pytest.mark.parametrize("name", GUIDES)
def test_the_twins_have_the_same_sections(name: str) -> None:
    def numbered(path: Path) -> list[str]:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [line.split()[1] for line in lines if line.startswith(("## ", "### "))]

    assert numbered(DOCS / f"{name}.md") == numbered(DOCS / f"{name}_ja.md")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_example_confirms_its_own_operation(path: Path) -> None:
    """A person confirms a `scheduler_loosen` operation; no command in a guide does it for them."""
    blocks = re.findall(r"```(?:bash|sh)\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL)
    for block in blocks:
        assert "/api/host-operations/" not in block
        assert "Sec-Fetch-Site" not in block
