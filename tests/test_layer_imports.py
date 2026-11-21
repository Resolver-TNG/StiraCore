"""Layer check (#795 S0, module map T2 option B): today's cross-package imports are frozen.

``tests/data/layer_edges.json`` lists every import edge between the top-level parts of
``src/stiracore`` that exists today. A new edge fails this test; nothing else is judged
(the list is the present state, not the wanted one).

How an edge is counted:

* a *part* is the first name under ``stiracore``: a package (``agent``, ``core``, …) or a
  root module (``labels``, ``identity_paths``, …);
* the root ``__init__.py`` (re-exports) is not read; a ``from stiracore import Name`` of a
  re-exported name is an edge to ``__root__``;
* ``top``: an import statement outside any function (class bodies, ``try`` and
  ``if TYPE_CHECKING`` blocks included); ``lazy``: one inside a function. A ``lazy`` edge
  that becomes a ``top`` edge is a new edge;
* imports inside one part are not edges.
"""

from __future__ import annotations

import ast
import json
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "stiracore"
ALLOWED = Path(__file__).resolve().parent / "data" / "layer_edges.json"
KINDS = ("top", "lazy")


def _part(name: str, root: Path = ROOT) -> str:
    """``name`` (the first name under ``stiracore``) if it is a part, else ``__root__``."""
    if (root / name).is_dir() or (root / f"{name}.py").is_file():
        return name
    return "__root__"


def _targets(node: ast.AST, package: tuple[str, ...], root: Path) -> list[str]:
    """The parts one import statement names (``package``: the importing module's package,
    below ``stiracore``)."""
    if isinstance(node, ast.Import):
        dotted = [alias.name.split(".") for alias in node.names]
        return [_part(n[1], root) for n in dotted if n[0] == "stiracore" and len(n) > 1]
    names = tuple(name for name in (node.module or "").split(".") if name)
    if node.level:
        if node.level - 1 > len(package):
            return []
        below = package[: len(package) - (node.level - 1)] + names
    elif names[:1] == ("stiracore",):
        below = names[1:]
    else:
        return []
    if below:
        return [_part(below[0], root)]
    return [_part(alias.name, root) for alias in node.names]  # ``from stiracore import x``


def _walk(node: ast.AST, lazy: bool):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            yield child, lazy
        inside = lazy or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        yield from _walk(child, inside)


def current_edges(root: Path = ROOT) -> dict[str, list[str]]:
    edges: dict[str, set[str]] = {kind: set() for kind in KINDS}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).with_suffix("").parts
        if relative == ("__init__",):
            continue
        source = relative[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, lazy in _walk(tree, False):
            for target in _targets(node, relative[:-1], root):
                if target != source:
                    edges["lazy" if lazy else "top"].add(f"{source} -> {target}")
    return {kind: sorted(edges[kind]) for kind in KINDS}


def _allowed() -> dict[str, list[str]]:
    data = json.loads(ALLOWED.read_text(encoding="utf-8"))
    return {kind: list(data[kind]) for kind in KINDS}


def test_the_counter_reads_top_lazy_relative_and_root_imports(tmp_path):
    for name in ("aa", "bb", "cc"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("from stiracore.aa import x\n", encoding="utf-8")
    (tmp_path / "solo.py").write_text("from . import aa\nfrom .bb import y\n", encoding="utf-8")
    (tmp_path / "aa" / "mod.py").write_text(
        "import os\n"
        "import stiracore.bb.thing\n"
        "from . import sibling\n"
        "from .sibling import z\n"
        "from stiracore.aa import other\n"
        "from stiracore import Name\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from ..solo import s\n"
        "class K:\n"
        "    def m(self):\n"
        "        from stiracore.cc import late\n"
        "        from .. import bb\n",
        encoding="utf-8",
    )
    assert current_edges(tmp_path) == {
        "top": ["aa -> __root__", "aa -> bb", "aa -> solo", "solo -> aa", "solo -> bb"],
        "lazy": ["aa -> bb", "aa -> cc"],
    }


def test_no_new_cross_package_import_edge():
    allowed, current = _allowed(), current_edges()
    new = {kind: sorted(set(current[kind]) - set(allowed[kind])) for kind in KINDS}
    assert not any(new.values()), (
        "New import edge(s) between parts of src/stiracore: "
        f"{json.dumps(new)}. The layer check (#795 S0) freezes the edges that exist. "
        "If this edge is really needed, add it to tests/data/layer_edges.json and write "
        "the reason in the PR description (a `lazy` import moved to module level counts "
        "as a new `top` edge)."
    )


def test_the_allow_list_is_sorted_and_names_existing_parts():
    allowed = _allowed()
    for kind in KINDS:
        assert allowed[kind] == sorted(set(allowed[kind])), kind
        for edge in allowed[kind]:
            source, _, target = edge.partition(" -> ")
            assert source != target and _part(source) == source, edge
            assert target == "__root__" or _part(target) == target, edge


def test_edges_that_are_gone_are_reported_not_failed():
    """An edge that no longer exists does not fail: a warning asks to shrink the list
    (so the removed edge cannot come back unnoticed)."""
    allowed, current = _allowed(), current_edges()
    gone = {kind: sorted(set(allowed[kind]) - set(current[kind])) for kind in KINDS}
    if any(gone.values()):
        warnings.warn(
            "tests/data/layer_edges.json lists import edge(s) that no longer exist: "
            f"{json.dumps(gone)}. Remove them from the list.",
            stacklevel=1,
        )
