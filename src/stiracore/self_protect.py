"""Paths a grant or an automatic approval must not write (#791, ADR 0029 Q3 / B6 / B6b).

Closed lists, one place, no setting and no tool argument changes them:

* :data:`GRANT_RUN_LATER_PATTERNS` (+ :data:`RUN_LATER_SUFFIXES`) — the **run-later files**
  (they run later on the person's machine: git's own directory and configuration, CI
  definitions, editor / agent / shell-hook settings). A grant's ``editor`` never writes
  one, in any workspace. :data:`PERSON_WRITE_PATTERNS` is the same list: in a chat, a
  write to one by ``editor`` / ``file_write`` (or ``speak``'s ``output_path``) is never
  approved automatically (:func:`person_required`) — the tool gate asks a person even when
  the rule is Always, in a YOLO window and under a session allowance.
* :data:`CHECKOUT_WRITABLE` — the **allow-list**: the only places of a Stira checkout's
  source a grant's ``editor`` writes (``docs/proposer-edit-boundary.md`` §4: what is not
  listed is out of bounds). :data:`NEVER_EDITABLE` and :data:`NEVER_EDITABLE_NAMES` are the
  second line inside it and the named record of the approval / containment / egress /
  masking / floor / evaluator / budget / persona-core / axis files and the served
  dashboard (ADR 0029 H3).
* :data:`CHECKOUT_DATA` — the places of a checkout that are not its source (the default
  data directory ``.local/`` and ``workspace/``): a grant whose subtree lies there is an
  ordinary grant.

A Stira checkout is a directory from which ``src/stiracore/__init__.py`` opens (as the
file system resolves the names: any case on a case-insensitive volume). The running
server's own tree is its package directory (``stiracore.__file__``) and every checkout
that holds it. :func:`subtree_verdict` answers how a grant subtree relates to them. Every
function here only reads names (``stat`` / ``scandir``); an error is raised to the caller,
who refuses (fail closed). Reads are not restricted.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from typing import Any

from stiracore.identity_paths import path_key

__all__ = [
    "CHECKOUT",
    "CHECKOUT_DATA",
    "CHECKOUT_WRITABLE",
    "FREE",
    "GRANT_RUN_LATER_PATTERNS",
    "NEVER_EDITABLE",
    "NEVER_EDITABLE_NAMES",
    "PERSON_WRITE_PATTERNS",
    "RUNNING",
    "RUN_LATER",
    "RUN_LATER_SUFFIXES",
    "VERDICTS",
    "WALK_MAX_DIRS",
    "CannotJudge",
    "checkout_roots_above",
    "grant_write_refused",
    "person_required",
    "subtree_protected",
    "subtree_verdict",
]

_MARKER = ("src", "stiracore", "__init__.py")
# The walk below a subtree is bounded; past it the subtree is refused (cannot judge).
WALK_MAX_DIRS = 200_000

# Consecutive path components, anywhere in the path (the entry itself and all below it).
# ``.git`` is the directory (hooks, ``config``, ``info/``) and a worktree's pointer file.
GRANT_RUN_LATER_PATTERNS: tuple[tuple[str, ...], ...] = (
    (".git",),
    (".gitattributes",),
    (".gitmodules",),
    (".github",),
    (".gitlab-ci.yml",),
    (".pre-commit-config.yaml",),
    (".vscode",),
    (".idea",),
    (".devcontainer",),
    (".envrc",),
    (".husky",),
    (".kiro",),
    (".claude",),
    (".cursor",),
)
# A component that ends with one of these (``*.code-workspace``), anywhere in the path.
RUN_LATER_SUFFIXES: tuple[str, ...] = (".code-workspace",)
# What needs a person's card in a chat is everything a grant never writes.
PERSON_WRITE_PATTERNS = GRANT_RUN_LATER_PATTERNS

_PACKAGE = ("src", "stiracore")
# Relative to a checkout root: the places that are not its source. A grant whose subtree
# lies in one is an ordinary grant (only the run-later list applies to its writes).
CHECKOUT_DATA: tuple[tuple[str, ...], ...] = ((".local",), ("workspace",))
# Relative to a checkout root: the only places of its source a grant's editor writes.
CHECKOUT_WRITABLE: tuple[tuple[str, ...], ...] = (("docs",),)
# Relative to a checkout root: the entry itself and everything below it — refused also
# inside :data:`CHECKOUT_WRITABLE`.
NEVER_EDITABLE: tuple[tuple[str, ...], ...] = (
    # import-time code that runs before every layer below (and is the checkout marker)
    (*_PACKAGE, "__init__.py"),
    # approval and containment layers, and what wires them
    (*_PACKAGE, "core", "approval.py"),
    (*_PACKAGE, "core", "approval_store.py"),
    (*_PACKAGE, "core", "engine.py"),
    (*_PACKAGE, "core", "containment.py"),
    (*_PACKAGE, "core", "turn_lock.py"),
    (*_PACKAGE, "core", "checkpoint.py"),
    (*_PACKAGE, "control", "job_approvals.py"),
    (*_PACKAGE, "agent", "checkpoint_hooks.py"),
    (*_PACKAGE, "agent", "nxp_agent.py"),
    (*_PACKAGE, "agent", "grant_child.py"),
    (*_PACKAGE, "tools", "tool_policy.py"),
    (*_PACKAGE, "tools", "tool_consent.py"),
    (*_PACKAGE, "tools", "tool_config.py"),
    (*_PACKAGE, "tools", "tool_registry.py"),
    (*_PACKAGE, "tools", "exec_gate.py"),
    (*_PACKAGE, "yolo"),
    (*_PACKAGE, "grants"),
    (*_PACKAGE, "authority"),
    # egress and masking
    (*_PACKAGE, "labels.py"),
    (*_PACKAGE, "tools", "own_dashboard_gate.py"),
    (*_PACKAGE, "tools", "http_consent.py"),
    (*_PACKAGE, "tools", "web_tools.py"),
    (*_PACKAGE, "integrations"),
    (*_PACKAGE, "providers"),
    (*_PACKAGE, "agent", "model_factory.py"),
    (*_PACKAGE, "observability", "masking.py"),
    (*_PACKAGE, "observability", "security.py"),
    (*_PACKAGE, "middleware", "taint_observer.py"),
    (*_PACKAGE, "middleware", "containment_observer.py"),
    (*_PACKAGE, "tools", "episodic_formatter.py"),
    # floors (this module is one of them)
    (*_PACKAGE, "identity_paths.py"),
    (*_PACKAGE, "self_protect.py"),
    (*_PACKAGE, "middleware", "identity_guard.py"),
    (*_PACKAGE, "tools", "os_protection.py"),
    (*_PACKAGE, "tools", "checked_open.py"),
    (*_PACKAGE, "tools", "file_editor_tool.py"),
    (*_PACKAGE, "tools", "file_tools.py"),
    (*_PACKAGE, "tools", "child_env.py"),
    # persona core
    (*_PACKAGE, "core", "neural_state.py"),
    (*_PACKAGE, "core", "coherence.py"),
    (*_PACKAGE, "core", "amygdala.py"),
    (*_PACKAGE, "agent", "stimulus_classifier.py"),
    (*_PACKAGE, "agent", "ja_hybrid"),
    (*_PACKAGE, "middleware", "sycophancy_monitor.py"),
    # evaluators, the axis check and the boundary document itself
    ("tests",),
    ("ci",),
    ("scripts",),
    ("docs", "eval"),
    ("docs", "adr"),  # decision records are the person's (B6 decision D2, #791)
    ("docs", "proposer-edit-boundary.md"),
    ("conftest.py",),
    ("pyproject.toml",),
    ("release-exclude.txt",),
    (*_PACKAGE, "eval"),
    (*_PACKAGE, "core", "breaker_sim.py"),
    (*_PACKAGE, "tools", "run_checks.py"),
    (*_PACKAGE, "tools", "page_check.py"),
    (*_PACKAGE, "tools", "page_check_guard.py"),
    # audit records
    (*_PACKAGE, "observability", "event_store.py"),
    (*_PACKAGE, "observability", "audit_chain.py"),
    (*_PACKAGE, "observability", "event_chain.py"),
    (*_PACKAGE, "observability", "turn_events.py"),
    # budgets, limits, settings defaults
    (*_PACKAGE, "tools", "delegate_tool.py"),
    (*_PACKAGE, "scheduler"),
    (*_PACKAGE, "diagnostics"),
    (*_PACKAGE, "config"),
    # everything the server serves and its wiring: the pages, scripts and catalogs are the
    # browser half of the approval layer (same origin as the approval cards)
    ("dashboard",),
)
# A component with one of these names, at any depth of a checkout's source: import-time
# code and the files that redirect the evaluators (a ``pytest.ini`` beats pyproject's).
NEVER_EDITABLE_NAMES: tuple[str, ...] = (
    "__init__.py",
    "conftest.py",
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    "setup.py",
    "pyproject.toml",
    "ruff.toml",
    ".ruff.toml",
    "sitecustomize.py",
    "usercustomize.py",
)

# How a grant subtree relates to Stira's own code (:func:`subtree_verdict`).
FREE = "free"  # an ordinary subtree
CHECKOUT = "checkout"  # the protected part of a checkout: only with the widening option
RUNNING = "running_checkout"  # the running server's own tree: never
RUN_LATER = "run_later"  # inside a run-later directory: never
VERDICTS = frozenset({FREE, CHECKOUT, RUNNING, RUN_LATER})

# Tools that write a file where an argument points -> that argument.
_WRITE_PATH_TOOLS = {"file_write": "path", "editor": "path", "speak": "output_path"}
_EDITOR_READS = ("view", "find_line")


class CannotJudge(OSError):
    """The walk passed its bound: the caller refuses."""


def _name(name: str) -> str:
    key = path_key(name)
    return key[-1] if len(key) > 1 else ""


def _parts(path: str) -> tuple[str, ...]:
    return path_key(path)[1:]


def _holds(parts: tuple[str, ...], patterns: tuple[tuple[str, ...], ...]) -> bool:
    for pattern in patterns:
        width = len(pattern)
        for start in range(len(parts) - width + 1):
            if parts[start : start + width] == pattern:
                return True
    return any(part.endswith(RUN_LATER_SUFFIXES) for part in parts)


def _matches(path: str, patterns: tuple[tuple[str, ...], ...]) -> bool:
    """The path as spelled or as resolved names one of ``patterns`` (or a
    :data:`RUN_LATER_SUFFIXES` component)."""
    absolute = os.path.abspath(path)
    if _holds(_parts(absolute), patterns):
        return True
    return _holds(_parts(os.path.realpath(absolute)), patterns)


def _is_checkout_root(directory: str) -> bool:
    """``directory/src/stiracore/__init__.py`` opens — as the file system resolves the
    names (any case on a case-insensitive volume; a link in the way is followed). Only
    "it does not exist" answers ``False``; any other error is raised."""
    try:
        os.stat(os.path.join(directory, *_MARKER))
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _server_package() -> str:
    import stiracore

    return os.path.realpath(os.path.dirname(os.path.abspath(stiracore.__file__)))


def _within(child: tuple[str, ...], parent: tuple[str, ...]) -> bool:
    return child[: len(parent)] == parent


def _in(relative: tuple[str, ...], entries: tuple[tuple[str, ...], ...]) -> bool:
    return any(_within(relative, entry) for entry in entries)


def _write_in_source_refused(relative: tuple[str, ...]) -> bool:
    """A write at ``relative`` (to a checkout root, judged as its source): outside the
    allow-list, or on the second line inside it."""
    if not _in(relative, CHECKOUT_WRITABLE) or _in(relative, NEVER_EDITABLE):
        return True
    return any(part in NEVER_EDITABLE_NAMES for part in relative)


def _subtree_in_source(relative: tuple[str, ...]) -> bool:
    """A subtree at ``relative`` (to a checkout root) overlaps the protected part: it is
    not in a data place, and it is outside the allow-list, inside a never-editable entry
    or holds one."""
    # Every place of a Stira checkout except its data places is Stira's own tree (review
    # #791 N1): a subfolder of ``docs/`` is not an ordinary project folder either. What a
    # self-development grant may write inside it is the allow-list's business (writes only).
    return not _in(relative, CHECKOUT_DATA)


def checkout_roots_above(path: str) -> list[str]:
    """Every Stira checkout root at or above ``path`` (resolved; missing components are
    skipped). Raises ``OSError`` when a directory cannot be listed."""
    node = os.path.realpath(path)
    roots = []
    while True:
        if os.path.isdir(node) and _is_checkout_root(node):
            roots.append(node)
        parent = os.path.dirname(node)
        if parent == node:
            return roots
        node = parent


def subtree_verdict(resolved: str, *, max_dirs: int = WALK_MAX_DIRS) -> str:
    """How the directory ``resolved`` relates to Stira's own code — one of :data:`VERDICTS`:

    * :data:`RUNNING` — it is, is inside or holds the running server's package directory,
      or it overlaps the protected part of a checkout that holds that package;
    * :data:`RUN_LATER` — it lies inside a run-later directory (``.git/`` …);
    * :data:`CHECKOUT` — it overlaps the protected part of another checkout (it is outside
      the checkout's data places and its allow-list, or inside or around a never-editable
      entry), or it holds a checkout;
    * :data:`FREE` — none of these (also a data place or an allow-listed directory of a
      checkout).

    Symlinked directories below it are not entered (a grant never writes through one to
    the outside). Raises ``OSError`` (:class:`CannotJudge` past ``max_dirs`` directories):
    the caller refuses."""
    resolved = os.path.realpath(resolved)
    here = _parts(resolved)
    package_dir = _server_package()
    package = _parts(package_dir)
    if _within(here, package) or _within(package, here):
        return RUNNING
    running = {_parts(root) for root in checkout_roots_above(package_dir)}
    verdict = FREE
    for root in checkout_roots_above(resolved):
        base = _parts(root)
        if _subtree_in_source(here[len(base) :]):
            if base in running:
                return RUNNING
            verdict = CHECKOUT
    if _holds(here, GRANT_RUN_LATER_PATTERNS):
        return RUN_LATER
    if verdict != FREE:
        return verdict
    pending, seen = [resolved], 0
    while pending:
        directory = pending.pop()
        seen += 1
        if seen > max_dirs:
            raise CannotJudge("too many directories below the subtree")
        marker = False
        with os.scandir(directory) as entries:
            for entry in entries:
                if _name(entry.name) == _MARKER[0]:
                    marker = True
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
        if marker and _is_checkout_root(directory):
            return CHECKOUT
    return FREE


def subtree_protected(resolved: str, *, max_dirs: int = WALK_MAX_DIRS) -> bool:
    """Whether :func:`subtree_verdict` is anything but :data:`FREE`."""
    return subtree_verdict(resolved, max_dirs=max_dirs) != FREE


def grant_write_refused(path: str, subtree: str | None = None) -> bool:
    """Whether a grant's ``editor`` must not write the absolute ``path``:

    * a run-later file (:data:`GRANT_RUN_LATER_PATTERNS`, as spelled or as resolved), in
      any workspace;
    * inside a Stira checkout: anything outside :data:`CHECKOUT_WRITABLE`, an entry of
      :data:`NEVER_EDITABLE`, a :data:`NEVER_EDITABLE_NAMES` component, or an existing file
      with a second name (a hard link may be a listed file) — unless the grant's
      ``subtree`` and the path both lie in one of that checkout's data places
      (:data:`CHECKOUT_DATA`; without ``subtree`` nothing is a data place);
    * inside the running server's package directory: always.

    Raises ``OSError``."""
    if _matches(path, GRANT_RUN_LATER_PATTERNS):
        return True
    resolved = os.path.realpath(path)
    here = _parts(resolved)
    home = _parts(os.path.realpath(subtree)) if subtree is not None else None
    relatives = []
    for root in checkout_roots_above(resolved):
        base = _parts(root)
        relative = here[len(base) :]
        lives_in_data = (
            home is not None
            and _within(home, base)
            and _in(home[len(base) :], CHECKOUT_DATA)
            and _in(relative, CHECKOUT_DATA)
        )
        if not lives_in_data:
            relatives.append(relative)
    package = _parts(_server_package())
    if _within(here, package):
        relatives.append((*_PACKAGE, *here[len(package) :]))
    if not relatives:
        return False
    if any(_write_in_source_refused(relative) for relative in relatives):
        return True
    try:
        info = os.lstat(resolved)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink > 1


def person_required(tool_name: Any, tool_input: Any, policy: Any = None) -> bool:
    """Whether this call is a write to a run-later file (:data:`PERSON_WRITE_PATTERNS`) by
    ``editor`` / ``file_write``, or by ``speak`` saving at ``output_path``: only a person's
    card may approve it. ``policy`` (the agent's ``WritePathPolicy``) anchors a relative
    path at the workspace. A path that cannot be judged needs a person; a call without a
    usable path does not (the tool refuses it itself, or writes no file)."""
    if tool_name not in _WRITE_PATH_TOOLS or not isinstance(tool_input, Mapping):
        return False
    if tool_name == "editor" and tool_input.get("command") in _EDITOR_READS:
        return False
    raw = tool_input.get(_WRITE_PATH_TOOLS[tool_name])
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        return False
    try:
        if tool_name == "editor":
            from stiracore.tools.file_editor_tool import from_editor_path

            raw = from_editor_path(raw)
        absolute = policy.absolute(raw) if policy is not None else os.path.expanduser(raw)
        return _matches(os.fspath(absolute), PERSON_WRITE_PATTERNS)
    except Exception:  # noqa: BLE001 - a path that cannot be judged: a person decides
        return True
